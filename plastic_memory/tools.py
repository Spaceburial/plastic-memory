"""tools.py — 给 agent 用的工具（Hermes 工具 schema 层）。

逻辑全在 plastic_memory/ 里；这里只做 schema 包装和参数解析。
刻意没有 pm_core / pm_dump 之类的工具：核心印象区的唯一读取路径是注入，
agent 看不到一份可复述的清单，用户也就没有办法让它念出来。
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import dream as dream_mod
from . import persona as persona_mod
from . import store


def _owner(conn) -> str:
    """当前是谁在说话。pre_llm_call 会把它写进 meta，工具据此只看这个人的记忆。"""
    return str(store.get_meta(conn, "active_owner", "") or "")


def _schema(name: str, description: str, props: dict, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props,
                           "required": required or []}}


PM_STATUS = _schema(
    "pm_status",
    "可塑性记忆的健康度与容量：三层各有多少条、被拦下多少杂讯、做过几次梦。"
    "只返回数字，不返回核心印象的内容。",
    {})

PM_RECALL = _schema(
    "pm_recall",
    "在事件层里按语义检索旧事（用户说过的话、被记下的关键事件）。"
    "用于需要具体细节时；核心印象层不通过这个工具暴露。",
    {"query": {"type": "string", "description": "要找什么，用自然语言"},
     "k": {"type": "integer", "description": "最多返回几条，默认 6"}},
    ["query"])

PM_OBSERVE = _schema(
    "pm_observe",
    "显式记下一件事。日常对话会自动观察，只有当你判断某句话重要到必须落盘时才手动调用。",
    {"text": {"type": "string", "description": "要记的内容，尽量保留原话要点"},
     "kind": {"type": "string",
              "enum": ["identity", "promise", "boundary", "emotion", "pref", "fact", "life", "misc"],
              "description": "可选，不填则自动分类"}},
    ["text"])

PM_DREAM = _schema(
    "pm_dream",
    "立刻做一次梦（离线巩固）：按主题把事件合并成判断、检出矛盾、淘汰衰减到阈值的杂事。"
    "返回本次的操作计数。",
    {})

PM_PERSONA = _schema(
    "pm_persona",
    "查看当前的人设面与权重。可以给一段假设的话，预演它会激活哪几面、是否进入交叉态。",
    {"text": {"type": "string", "description": "可选：用来试算的一句话"}})

PM_COMPACT = _schema(
    "pm_compact",
    "半衰期压缩：核心区太满、快要挤掉重要的东西时，把大部分印象交给梦做二次压缩，"
    "合并成更少的几条判断。必须先跟用户讲清楚（会丢掉一些细节）并得到同意，confirm=true 才执行。"
    "旧条目不删除，只是退出常驻，仍可追溯。",
    {"confirm": {"type": "boolean", "description": "用户是否已经明确同意压缩"}},
    ["confirm"])

PM_TRACE = _schema(
    "pm_trace",
    "追溯一条印象是怎么来的：它由哪几句原话压出来、或者由哪几条旧印象再压出来的。"
    "用于自查『我为什么记得这件事』，也用于确认梦有没有编造。",
    {"text": {"type": "string", "description": "印象的大致内容，会找最接近的一条"},
     "id": {"type": "integer", "description": "也可以直接给印象 id"}})

PM_PIN = _schema(
    "pm_pin",
    "把一条记忆钉住（或取消钉住）。钉住的不受半衰期影响、不参与常驻轮换，永远在注入里。"
    "用于婚丧嫁娶、承诺、健康、钱、法律这类只提一次但后果很大的事。",
    {"text": {"type": "string", "description": "要钉的内容，会找最接近的一条"},
     "id": {"type": "integer", "description": "或者直接给印象 id"},
     "unpin": {"type": "boolean", "description": "true = 取消钉住"}})

PM_DIARY = _schema(
    "pm_diary",
    "把核心印象写成一篇日记交给用户。核心区平时不对用户开放，这是唯一的出口——"
    "只在用户明确要求、或临别/迁移时用。输出是叙事，不是条目清单。",
    {"style": {"type": "string", "enum": ["letter", "log"],
               "description": "letter=一封回顾的信；log=按时间的日记条目"},
     "raw": {"type": "boolean",
             "description": "是否附上结构化原文供备份/迁移（默认否，只给叙事）"}})


# ── handlers ─────────────────────────────────────────────────────────────────

def handle_pm_status(args: dict, **_kw) -> str:
    conn = store.connect()
    now = time.time()
    ow = _owner(conn)
    st = store.stats(conn, now, owner=ow)
    spec = persona_mod.load_spec()
    pst = persona_mod.active(spec, "")
    top = " / ".join(f"{f['name']}({f['weight']:.2f})" for f in pst["facets"]) or "无"
    return json.dumps({
        "层": {"核心印象": f"{st['impressions']} 条（常驻 {st['impressions_resident']}）",
               "事件层": f"存活 {st['events_alive']} / 已遗忘 {st['events_forgotten']}",
               "杂讯已拦": st["junk_blocked"], "待验证假说": st["hypotheses_open"]},
        "梦次数": st["dreams"], "当前人设": top,
        "说明": "核心印象只以注入方式参与判断，不可导出。",
    }, ensure_ascii=False, indent=1)


def handle_pm_recall(args: dict, **_kw) -> str:
    q = str(args.get("query") or "")
    if not q.strip():
        return "需要 query。"
    k = int(args.get("k") or 6)
    conn = store.connect()
    rows = store.recall_events(conn, q, k=k, owner=_owner(conn))
    if not rows:
        return "事件层里没有相关的。"
    out = [{"天前": int((time.time() - float(r["ts"])) / store.DAY), "类型": r["kind"],
            "内容": r["text"], "被想起": r["uses"]} for r in rows]
    return json.dumps(out, ensure_ascii=False, indent=1)


def handle_pm_observe(args: dict, **kw) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        return "需要 text。"
    conn = store.connect()
    res = store.observe(conn, text, role="user",
                        session_id=str(kw.get("session_id") or ""), kind=args.get("kind"),
                        owner=_owner(conn))
    label = {"new": "已记下", "repeat": "这条以前说过，已累加强度",
             "junk": "判为杂讯，未落盘"}.get(res["reason"], res["reason"])
    return f"{label}（{res.get('kind', '')}）"


def handle_pm_dream(args: dict, **_kw) -> str:
    conn = store.connect()
    bias = persona_mod.memory_bias(persona_mod.load_spec(), "")
    rep = dream_mod.dream(conn, bias=bias, owner=_owner(conn))
    return json.dumps({"合并": rep["merged"], "提升为核心印象": rep["promoted"],
                       "矛盾降权": rep["conflicts"], "淘汰": rep["evicted"],
                       "新假说": rep["hypotheses"]}, ensure_ascii=False, indent=1)


def handle_pm_persona(args: dict, **_kw) -> str:
    spec = persona_mod.load_spec()
    text = str(args.get("text") or "")
    st = persona_mod.active(spec, text)
    return json.dumps({
        "触发这句": text or "（空）",
        "当前生效": [f"{f['name']}({f['weight']:.2f})" for f in st["facets"]],
        "交叉态": st["crossed"], "张力": st["tension"] or "无",
        "全部面": [{"面": f["name"], "权重": f["weight"], "命中": f["hits"]} for f in st["all"]],
        "记忆偏向": persona_mod.memory_bias(spec, text),
    }, ensure_ascii=False, indent=1)


def handle_pm_compact(args: dict, **_kw) -> str:
    if not args.get("confirm"):
        return ("没有确认。先跟用户说清楚：核心区太满，要睡一觉把旧的压一压，"
                "会丢掉一些细节但不会编造；等他同意后再调用，并把 confirm 设为 true。")
    conn = store.connect()
    rep = dream_mod.compress_core(conn, owner=_owner(conn))
    if rep.get("skipped"):
        return f"没压：{rep['skipped']}（当前 {rep.get('before')} 条常驻）。"
    return json.dumps({"方式": "模型提炼" if rep["mode"] == "llm" else "规则合并",
                       "压缩前": rep["before"], "交给梦的": rep["compressed_from"],
                       "压成": rep["compressed_to"], "现在常驻": rep["after"]},
                      ensure_ascii=False, indent=1)


def _fmt_trace(node: dict, indent: int = 0) -> list[str]:
    pad = "  " * indent
    head = f"{pad}· [{node.get('slot','')}] {node.get('text','')[:60]}"
    if not node.get("resident", 1):
        head += "（已退出常驻）"
    out = [head]
    for c in node.get("children", []):
        if c["type"] == "event":
            cd = int((time.time() - float(c["ts"])) / 86400)
            gone = ""
            if not c.get("alive", 1):
                gone = f"（已并入 #{c['merged_into']}）" if c.get("merged_into") else "（已淘汰）"
            out.append(f"{pad}  ← 原话（{cd}天前，{c['kind']}）：{c['text'][:70]}{gone}")
        else:
            out += _fmt_trace(c, indent + 1)
    return out


def handle_pm_trace(args: dict, **_kw) -> str:
    conn = store.connect()
    row = store.find_impression(conn, str(args.get("text") or ""), int(args.get("id") or 0), _owner(conn))
    if not row:
        return "核心区还是空的，没有可追溯的东西。"
    node = store.trace_impression(conn, row["id"])
    if not node:
        return "这条印象没有可追溯的来源。"
    lines = _fmt_trace(node)
    if not node.get("children"):
        lines.append("  （没有可追的来源——这条是孤的，可能是手工写入的）")
    return "\n".join(lines)


def handle_pm_pin(args: dict, **_kw) -> str:
    conn = store.connect()
    row = store.find_impression(conn, str(args.get("text") or ""), int(args.get("id") or 0), _owner(conn))
    if not row:
        return "没找到对应的印象。可以先 pm_observe 记下来，下次做梦会提升。"
    val = 0 if args.get("unpin") else 1
    conn.execute("UPDATE impressions SET pinned=?, resident=1 WHERE id=?", (val, row["id"]))
    conn.commit()
    return f"{'已取消钉住' if not val else '已钉住'}：{row['text'][:50]}"


def handle_pm_diary(args: dict, **_kw) -> str:
    from . import distill as distill_mod
    conn = store.connect()
    rows = conn.execute(
        "SELECT text, slot, ts FROM impressions WHERE resident=1 AND owner=? ORDER BY ts ASC",
        (_owner(conn),)).fetchall()
    entries = [{"text": r["text"], "slot": r["slot"], "ts": r["ts"]} for r in rows]
    text = distill_mod.diary(entries, style=str(args.get("style") or "letter"))
    if args.get("raw"):
        text += "\n\n---\n以下是原始条目（供备份/迁移）：\n" + json.dumps(
            entries, ensure_ascii=False, indent=1)
    return text


TOOLS = (
    ("pm_status", PM_STATUS, handle_pm_status, "🧠"),
    ("pm_recall", PM_RECALL, handle_pm_recall, "🔎"),
    ("pm_observe", PM_OBSERVE, handle_pm_observe, "📌"),
    ("pm_dream", PM_DREAM, handle_pm_dream, "🌙"),
    ("pm_persona", PM_PERSONA, handle_pm_persona, "🎭"),
    ("pm_compact", PM_COMPACT, handle_pm_compact, "⏳"),
    ("pm_trace", PM_TRACE, handle_pm_trace, "🧭"),
    ("pm_pin", PM_PIN, handle_pm_pin, "📌"),
    ("pm_diary", PM_DIARY, handle_pm_diary, "📖"),
)
