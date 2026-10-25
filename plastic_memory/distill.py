"""distill.py — 把事件压成"印象"。

两档：
  有 ctx.llm  批量让模型提炼（一次调用处理全部候选，不复述原话）。
  没有        规则蒸馏：去掉"记住/我说"这类框架词、只取第一句、截断。

规则档永远可用，LLM 档只是把质量抬高一截——所以梦不会因为模型不可用而罢工。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_LLM = None


def set_llm(llm) -> None:
    global _LLM
    _LLM = llm


_PREFIX = re.compile(r"^\s*(记住|记得|帮我记[一下]*|记一下|别忘了|提醒我|我跟你说)[：:，,\s]*")
_SPLIT = re.compile(r"[。！？；;\n]|另外|顺便|还有，")


def rules(text: str) -> str:
    t = _PREFIX.sub("", str(text or "").strip())
    parts = [p.strip() for p in _SPLIT.split(t) if p and p.strip()]
    t = parts[0] if parts else t
    t = " ".join(t.split())
    return t if len(t) <= 60 else t[:59] + "…"


_PROMPT = """你在为一个长期助手做记忆巩固（相当于人的"梦"）。下面这些是它白天记下的事。
把每一条压成一句"印象"：
1. 不要复述原话，去掉"记住""我说""记一下"这类框架词；
2. 只保留对以后相处有影响的部分：这个人的偏好、边界、身份、承诺、反复出现的情绪；
3. 每条不超过 30 个汉字，写成判断句或名词短语（例如"不喜欢长报告，要结论先行"）；
4. 如果某条其实不值得长期记住，impression 给空字符串。

只输出 JSON：{"items":[{"i":0,"impression":"..."}]}"""


def llm(items: list[tuple[int, str]]) -> dict[int, str]:
    """items: [(event_id, text)] -> {event_id: impression}。失败返回空字典。"""
    if _LLM is None or not items:
        return {}
    body = "\n".join(f"{i}. {t}" for i, (_, t) in enumerate(items))
    try:
        res = _LLM.complete(
            [{"role": "user", "content": _PROMPT + "\n\n" + body}],
            temperature=0.2, max_tokens=700, timeout=25,
            purpose="plastic-memory dream")
        raw = getattr(res, "text", "") or ""
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
        out: dict[int, str] = {}
        for row in data.get("items", []):
            try:
                idx = int(row.get("i"))
                imp = " ".join(str(row.get("impression") or "").split())
            except Exception:
                continue
            if 0 <= idx < len(items) and imp:
                out[items[idx][0]] = imp[:60]
        return out
    except Exception as exc:
        logger.warning("plastic-memory distill llm failed: %s", exc)
        return {}


_COMPRESS_PROMPT = """你在做一个长期助手的"半衰期压缩"：核心印象太多，装不进每天的上下文，
需要把粒度变粗一档。下面这些印象**最多合并成 {target} 条**。规则：
1. **不要跨类别硬凑**：工作的事和工作的事合并，宠物的和宠物合并，钱和钱合并；
   不同类别的东西不要塞进同一句。
2. 同类里可以合并成 1 条；不同类各出各的。
3. 每条不超过 30 个汉字，写成判断句，不复述原话。
4. 身份、承诺、边界优先保留；绝不编造原文没有的内容。
5. 每条必须注明它是从哪几条压出来的（用输入里的序号）。

只输出 JSON：{{"items":[{{"impression":"...","kind":"emotion","from":[0,2,3]}}]}}"""


def compress(texts: list[str], kinds: list[str], target: int) -> list[tuple[str, str]]:
    """把一批印象压成 <= target 条。返回 [(text, kind)]；失败返回空列表（调用方回退到规则档）。"""
    if _LLM is None or not texts or target < 1:
        return []
    body = "\n".join(f"{i}. [{k}] {t}" for i, (t, k) in enumerate(zip(texts, kinds)))
    try:
        res = _LLM.complete(
            [{"role": "user", "content": _COMPRESS_PROMPT.format(target=target) + "\n\n" + body}],
            temperature=0.15, max_tokens=900, timeout=45,
            purpose="plastic-memory half-life compaction")
        raw = getattr(res, "text", "") or ""
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
        out = []
        for row in data.get("items", []):
            t = " ".join(str(row.get("impression") or "").split())
            k = str(row.get("kind") or "pref")
            frm = [int(x) for x in (row.get("from") or []) if str(x).isdigit()]
            if t and len(t) <= 60:
                out.append((t, k, frm))
        return out
    except Exception as exc:
        logger.warning("plastic-memory compress failed: %s", exc)
        return []


_DIARY_LETTER = """你在替一个长期助手写一篇"临别日记"。下面这些是它对一个人的核心印象——
平时它不把这层给别人看，这是第一次也是最后一次摊开。
把它写成一段连贯的第一人称回忆：
1. 像写给对方看的话，不是清单，不要逐条罗列；
2. 保留具体细节（他做过什么、在意什么），但绝不编造原文没有的事；
3. 不提"记忆""数据""印象层""系统"这类词；
4. 300-500 字，语气克制，不煽情。
直接输出日记正文，不要标题、不要解释。"""

_DIARY_LOG = """你在替一个长期助手写日记。下面这些是它对一个人的核心印象。
写成 5-9 条按时间排列的短日记，每条 20-40 字，第一人称，句尾可以留日期占位（如"第 3 天"）。
要求：只写原文里有的事，不编造；不出现"记忆""数据""系统"这类词。
直接输出日记正文。"""


def diary(entries: list[dict], style: str = "letter") -> str:
    """把核心印象渲染成叙事。有模型就用模型，没有就用规则串句——永远给得出东西。"""
    if not entries:
        return "（还没有值得写下来的事。）"
    if _LLM is not None:
        try:
            body = "\n".join(f"- [{e.get('slot','')}] {e['text']}" for e in entries)
            prompt = _DIARY_LETTER if style != "log" else _DIARY_LOG
            res = _LLM.complete([{"role": "user", "content": prompt + "\n\n" + body}],
                                temperature=0.7, max_tokens=900, timeout=40,
                                purpose="plastic-memory diary")
            text = (getattr(res, "text", "") or "").strip()
            if len(text) >= 80:
                return text
        except Exception as exc:
            logger.warning("plastic-memory diary llm failed: %s", exc)

    # 规则档：不假装有文采，只把条目串成人话
    if style == "log":
        out = []
        for i, e in enumerate(entries[:9], 1):
            out.append(f"第 {i} 天：{e['text']}")
        return "\n".join(out)
    head = "我记得的，大概是这些。\n\n"
    lines = [f"· {e['text']}" for e in entries[:12]]
    return head + "\n".join(lines) + "\n\n这些是回忆，不是记录——可能有偏差。"


_MERGE_PROMPT = """这些是同一个人在一段时间里说的几句话，是同一件事的不同侧面。
把它们合成**一条**印象：
1. 不复述原话，不按条目罗列；
2. 保留能说明"他现在处在什么状态"的部分，丢掉具体细节；
3. 不超过 30 个汉字，写成一句判断；
4. 绝对不要添加原文里没有的信息。

只输出这一句话，不要解释、不要引号。"""


def merge(texts: list[str]) -> str:
    """把一组事件合成一条判断。失败时退回最长那句。"""
    if not texts:
        return ""
    if _LLM is None:
        return rules(max(texts, key=len))
    try:
        body = "\n".join(f"- {t}" for t in texts[:6])
        res = _LLM.complete([{"role": "user", "content": _MERGE_PROMPT + "\n\n" + body}],
                            temperature=0.2, max_tokens=200, timeout=40,
                            purpose="plastic-memory dream merge")
        out = " ".join((getattr(res, "text", "") or "").strip().split())
        out = out.strip('"“”「」')
        return out[:60] if out else rules(max(texts, key=len))
    except Exception as exc:
        logger.warning("plastic-memory merge failed: %s", exc)
        return rules(max(texts, key=len))
