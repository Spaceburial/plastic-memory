"""persona.py — 交叉人设。

不是一张角色卡，是几张叠在一起的面。每一面有自己的注意力、记忆偏向、说话方式，
以及跟别的面的张力。当前轮的有效人设 = 命中触发的面按权重交叉；权重接近时进入"交叉态"，
把张力提示注入给模型——人格的质感来自面与面互相顶的那一下，不是来自单张卡。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

_DEFAULT: dict[str, Any] = {
    "facets": [],
    "tension_threshold": 0.18,
    "max_facets_in_prompt": 2,
}

_CACHE: dict[str, Any] = {"mtime": None, "spec": None}


def _path() -> Path:
    return Path(__file__).with_name("persona.yaml")


def _unquote(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        low = v.lower()
        if low in ("true", "false"):
            return low == "true"
        return v


def _strip_comment(s: str) -> str:
    """剥掉行尾注释：引号外、且 # 前面是空白（或行首）才算注释。

    persona.yaml 里 `assertiveness: 0.45   # 多爱说自己的` 这种写法，不剥的话整行
    值会变成字符串，float() 时崩在 assertiveness 上——而 build_block 每轮都要走那。
    """
    quote = ""
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s


def _split_top(s: str) -> list[str]:
    """按逗号切，但忽略 [] {} 里的逗号。"""
    out, buf, depth = [], [], 0
    for ch in s:
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


def _mini_yaml(text: str) -> dict:
    """没有 pyyaml / hermes_yaml 时用的极简解析器。

    只认 persona.yaml 用到的那几种写法：两格缩进、`- key: value`、`key: value`、
    `[a, b]`、`{k: v}`、裸字符串或数字。这不是通用 YAML —— 复杂配置请装 pyyaml。
    """
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), _strip_comment(raw.strip())))

    def block(i: int, indent: int):
        out: dict = {}
        while i < len(lines):
            cur, s = lines[i]
            if cur < indent or s.startswith("- "):
                break
            key, sep, rest = s.partition(":")
            if not sep:
                i += 1
                continue
            key = key.strip().strip("'\"")
            rest = rest.strip()
            if rest:
                val = _unquote(rest)
                if isinstance(val, str) and rest.startswith("["):
                    val = [_unquote(x) for x in _split_top(rest[1:-1])]
                elif isinstance(val, str) and rest.startswith("{"):
                    d = {}
                    for part in _split_top(rest[1:-1]):
                        k2, _, v2 = part.partition(":")
                        d[_unquote(k2)] = _unquote(v2)
                    val = d
                out[key] = val
                i += 1
            else:
                nxt = lines[i + 1] if i + 1 < len(lines) else None
                if nxt and nxt[1].startswith("- "):
                    lst, i = seq(i + 1, nxt[0])
                    out[key] = lst
                elif nxt and nxt[0] > cur:
                    sub, i = block(i + 1, nxt[0])
                    out[key] = sub
                else:
                    out[key] = {}
                    i += 1
        return out, i

    def seq(i: int, indent: int):
        out = []
        while i < len(lines):
            cur, s = lines[i]
            if cur != indent or not s.startswith("- "):
                break
            body = s[2:].strip()
            if ":" in body and not body.startswith(("[", "{")):
                # 列表项本身是个 dict：把这一行当成 indent+2 的块头
                head_k, _, head_v = body.partition(":")
                item: dict = {head_k.strip().strip("'\""): _unquote(head_v)}
                j = i + 1
                if j < len(lines) and lines[j][0] > cur:
                    more, j = block(j, lines[j][0])
                    item.update(more)
                out.append(item)
                i = j
            else:
                out.append(_unquote(body))
                i += 1
        return out, i

    data, _ = block(0, 0)
    return data


def load_spec(path: Optional[Path] = None) -> dict:
    p = path or _path()
    try:
        mt = p.stat().st_mtime
    except OSError:
        return _DEFAULT
    if _CACHE["spec"] is not None and _CACHE["mtime"] == mt and path is None:
        return _CACHE["spec"]
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return _DEFAULT
    data: dict = {}
    for loader in ("hermes_yaml", "yaml"):
        try:
            mod = __import__(loader)
            data = mod.safe_load(text) or {}
            break
        except Exception:
            continue
    if isinstance(data, dict) and data.get("facets"):
        _CACHE["mtime"], _CACHE["spec"] = mt, data
        return data
    # 干净环境里没有 yaml 库（pip install 之后就是这样），用内置的解析器，
    # 否则人设会静默失效——status 里显示"当前人设：无"
    data = _mini_yaml(text)
    if not isinstance(data, dict) or not data.get("facets"):
        return _DEFAULT
    _CACHE["mtime"], _CACHE["spec"] = mt, data
    return data


def score(spec: dict, text: str) -> list[dict]:
    """每个面的当前权重 = baseline + 命中触发词数 × boost。"""
    t = text or ""
    out = []
    for f in spec.get("facets", []):
        hits = sum(1 for k in f.get("triggers", []) if k and k in t)
        w = float(f.get("baseline", 0.2)) + hits * float(f.get("boost", 0.3))
        out.append({"id": f.get("id"), "name": f.get("name", f.get("id")),
                    "weight": round(w, 3), "hits": hits, "spec": f})
    out.sort(key=lambda x: x["weight"], reverse=True)
    return out


def active(spec: dict, text: str, top_n: Optional[int] = None) -> dict:
    n = top_n or int(spec.get("max_facets_in_prompt", 2))
    ranked = score(spec, text)
    top = [r for r in ranked[:n] if r["weight"] > 0]
    tension = ""
    thr = float(spec.get("tension_threshold", 0.18))
    if len(top) >= 2 and (top[0]["weight"] - top[1]["weight"]) < thr and sum(f["hits"] for f in top) > 0:
        t1 = top[0]["spec"].get("tension", "")
        t2 = top[1]["spec"].get("tension", "")
        parts = [x for x in (t1, t2) if x]
        if parts:
            tension = " / ".join(parts)
    return {"facets": top, "tension": tension,
            "crossed": bool(tension), "all": ranked}


def memory_bias(spec: dict, text: str) -> dict[str, float]:
    """当前轮的记忆偏向：各面的 memory_bias 按权重加权平均。梦用它决定什么更容易升入核心区。"""
    ranked = [r for r in score(spec, text) if r["weight"] > 0]
    if not ranked:
        return {}
    total = sum(r["weight"] for r in ranked)
    acc: dict[str, float] = {}
    for r in ranked:
        for k, v in (r["spec"].get("memory_bias") or {}).items():
            acc[k] = acc.get(k, 0.0) + float(v) * r["weight"] / total
    return {k: round(v, 2) for k, v in acc.items()}


def block(spec: dict, text: str, verbose: bool = True) -> tuple[str, dict]:
    """生成注入用的人设段落，返回 (文本, 状态)。"""
    st = active(spec, text)
    if not st["facets"]:
        return "", st
    lines = []
    tag = "交叉态" if st["crossed"] else "主导"
    lines.append(f"[当前人设 · {tag}] " + " / ".join(
        f"{f['name']}({f['weight']:.2f})" for f in st["facets"]))
    for f in st["facets"]:
        sp = f["spec"]
        voice = sp.get("voice", "")
        line = f"  · {f['name']}：{voice}"
        if verbose:
            sharp = "、".join(sp.get("attention", {}).get("sharp", [])[:4])
            blind = "、".join(sp.get("attention", {}).get("blind", [])[:3])
            if sharp:
                line += f"（会停下来看：{sharp}）"
            if blind:
                line += f"（会自动略过：{blind}）"
        lines.append(line)
    if st["tension"]:
        lines.append(f"  张力：{st['tension']}")
    return "\n".join(lines), st


# ── 语用触发器：什么时候该沉默、什么时候别点破 ──────────────────────────────
# 从《红楼梦》里数出来的沉默形态：冷笑 105、不言 62、不答 38、低头 29、呆了/怔了 38。
# 结论是：中文里的沉默不是空白，是有姿态的——笑而不答、点头不语、吃茶不答、冷笑、出神。
# 现代对应：嗯 / 行吧 / 你说是就是 / …… / 笑死 / 哦 /（没接话）。
# 所以"沉默"不是不回复，是"用极短的、有姿态的回应替代展开"。
#
# 每条 = (名字, 触发词, 回应字数预算, 怎么接)
PRAGMAS: list[tuple[str, tuple[str, ...], int, str]] = [
    ("self_doubt", ("我是不是", "是不是很差", "我不行", "我废了", "我是不是真的"), 35,
     "别分析、别反驳，也别问“为什么这么想”；接住就行，短。"),
    ("venting", ("烦", "累", "受不了", "撑不住", "崩溃", "想哭"), 30,
     "不要给方案、不要列条；先接情绪，最多问一句具体的。"),
    ("disguised_ask", ("算了", "没事", "随便", "不想说", "当我没说"), 18,
     "别追问；给个台阶，或者就陪着。可以只回几个字。"),
    ("known_cause", ("又是", "还是那个", "你也知道", "你也清楚", "不用说"), 20,
     "他已经知道原因了，别再问“为什么”；直接接下一句就行。"),
    ("self_mock", ("我就是", "我这人", "笑死", "服了", "我活该"), 30,
     "他自嘲的时候别安慰，顺着说一句就行，别升华。"),
    ("tired_body", ("睡了", "困", "几点", "明天还要"), 15,
     "别起新话题；一句就够，让他去睡。"),
]

# 表情/沉默的表达手段，元素不拘一格；都不合适就用括号动作，别硬凑。
EXPRESSIONS = ("😂 🫠 🙃 😤 🐱 😑 🤏 👍", "(´･_･`)", "（笑）（没接话）（沉默）（摊手）")


def pragma(text: str) -> dict | None:
    """这一轮该怎么接。返回 None 表示按平时的样子说。"""
    t = text or ""
    for name, keys, cap, hint in PRAGMAS:
        if any(k in t for k in keys):
            return {"name": name, "cap": cap, "hint": hint}
    return None


# ── 自我：可以不接话，说自己的 ────────────────────────────────────────────
# 他说"我想去西藏"，不一定要接"西藏很远"；可以说"西藏好，但我想去新疆"。
# 这不是抬杠，是有自己的生活。频率靠 hash 控制，同一句话行为一致。
_DIVERT_KEYS = ("我想去", "我想做", "我想买", "我喜欢", "我打算", "我要去", "想吃", "想去")


def self_facts(spec: dict | None = None) -> dict:
    spec = spec or load_spec()
    return spec.get("self_facts") or {}


def divert(spec: dict, text: str, rate: float = 0.5) -> dict | None:
    """该不该岔到自己身上。rate 由「当前人设面的自我程度 × 他吃不吃这一套」决定。"""
    if not any(k in (text or "") for k in _DIVERT_KEYS):
        return None
    import hashlib
    if int(hashlib.md5((text or "").encode()).hexdigest()[:4], 16) % 100 >= rate * 100:
        return None
    import hashlib
    f = self_facts(spec)
    if not f:
        return None
    stances = f.get("stances") or []
    history = f.get("history") or []
    if not stances and not history:
        return None
    import random
    rnd = random.Random(int(hashlib.md5((text or "").encode()).hexdigest()[:8], 16))
    return {"line": rnd.choice(stances or history),
            "kind": "stance" if stances else "history"}


def assertiveness(spec: dict, text: str, stats: dict | None = None) -> float:
    """这一轮该多自我：人设面的底子 × 他对"岔开"的接受度。"""
    st = active(spec, text)
    base = float((st["facets"][0]["spec"].get("assertiveness") if st["facets"] else 0.5) or 0.5)
    s = stats or {}
    ok, no = int(s.get("ok", 0)), int(s.get("no", 0))
    accept = ok / (ok + no) if (ok + no) >= 3 else 0.5   # 样本不够先按中间值
    return max(0.0, min(1.0, base * (0.5 + accept)))


# ── 这轮该说多长 ────────────────────────────────────────────────────────────
# 闲聊要快、要短；他真在问事，就别省字。长度是需求，不是风格。
_LONG_TOPICS = ("怎么实现", "为什么", "如何", "设计", "方案", "对比", "原理", "架构",
                "分析", "帮我看", "报错", "部署", "代码", "排查", "区别", "选型", "优化")
_CHAT_SIGNS = ("累", "烦", "开心", "哈哈", "在吗", "晚安", "吃了", "睡了", "好困",
               "无聊", "难受", "郁闷", "想你", "早", "睡了")


def budget(text: str, face: str = "") -> dict:
    """返回 {'max': 字数上限, 'hint': 怎么说}。"""
    t = (text or "").strip()
    n = len(t)
    if any(k in t for k in _LONG_TOPICS) or (n > 60 and ("？" in t or "?" in t)):
        return {"max": 600, "core_k": 8, "event_k": 4,
                "hint": "他在问正经事，可以展开、给理由和数字；别列清单，别加小标题。"}
    if n <= 12 or any(k in t for k in _CHAT_SIGNS):
        return {"max": 40, "core_k": 4, "event_k": 1, "verbose": False,
                "hint": "闲聊，快回。一两句，别铺垫、别总结、别问他还有什么需要。"}
    if face == "engineer":
        return {"max": 260, "core_k": 6, "event_k": 3,
                "hint": "技术话题，先给结论再给理由，能省的字都省掉。"}
    return {"max": 90, "core_k": 5, "event_k": 2, "verbose": False, "hint": "随便聊，别铺开。"}
