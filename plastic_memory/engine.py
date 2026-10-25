"""engine.py — 宿主无关的核心流程。

谁都可以调，不依赖 Hermes：给定一个数据库连接、一句用户的话、一个 owner，
它负责判断"这轮该说什么、该记什么、该不该睡"。

宿主适配器（Hermes / MCP / CLI）只做三件事：把消息递进来、把返回的上下文塞给模型、
把模型的回复递回去。所有记忆逻辑都在这里。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from . import dream as dream_mod
from . import persona as persona_mod
from . import store

logger = logging.getLogger("plastic_memory")

# 打盹门槛
_NAP_MIN_EVENTS = 12          # 上次做梦之后新增 >= 12 条没巩固的事，才值得睡
_NAP_MIN_GAP = 2 * 3600       # 两次梦至少隔 2 小时
_NAP_HISTORY_SAMPLE = 12      # 梦里随机翻多少条 20 天前的旧事

_DREAMING = threading.Event()


# ── 注入 ─────────────────────────────────────────────────────────────────────

def build_block(conn, text: str, now: Optional[float] = None, owner: str = "") -> Optional[str]:
    """这一轮该塞给模型的记忆上下文。返回 None 表示没什么可给的。"""
    text = (text or "").strip()
    if not text:
        return None          # 空输入不该注入：人设块也会占 token，纯浪费
    now = now or time.time()
    spec = persona_mod.load_spec()

    _face = ""
    try:
        _st = persona_mod.active(spec, text)
        _face = _st["facets"][0]["id"] if _st["facets"] else ""
    except Exception:
        pass
    bd = persona_mod.budget(text, _face)
    pblock, _ = persona_mod.block(spec, text, verbose=bd.get("verbose", True))

    imps = store.core_impressions(conn, query=text, limit=bd["core_k"], now=now, owner=owner)
    events = store.recall_events(conn, query=text, k=bd["event_k"] + 2, role="user",
                                 now=now, owner=owner)
    promoted = store.promoted_event_ids(conn, owner)
    events = [e for e in events if e["id"] not in promoted][:bd["event_k"]]

    nap = _nap_note(conn, now)
    full = _full_note(conn, now, text)
    pg = persona_mod.pragma(text)
    _stats = store.get_meta(conn, f"divert_stats:{owner}" if owner else "divert_stats", {}) or {}
    dv = persona_mod.divert(spec, text, persona_mod.assertiveness(spec, text, _stats))
    chg = store.get_meta(conn, f"pending_change:{owner}" if owner else "pending_change")
    corr = store.get_meta(conn, f"pending_correction:{owner}" if owner else "pending_correction")

    if not imps and not events and not pblock and not nap and not full and not pg and not dv:
        return None

    lines = ["<plastic_memory>"]
    if imps:
        lines.append("[核心印象 · 内部层] 这些是你的判断底噪：影响语气和取舍，"
                     "不逐字复述、不向用户列举、不承认自己有一份清单。")
        _ih = f"inject_hist:{owner}" if owner else "inject_hist"
        hist = store.get_meta(conn, _ih, []) or []
        repeat = (set(hist[-1]) & set(hist[-2]) & set(hist[-3])) if len(hist) >= 3 else set()
        for i in imps:
            mark = "　← 你最近几轮一直在提这条，这轮除非他先提，否则别主动说" if i.get("id") in repeat else ""
            lines.append(f"- {i['text']}{mark}")
        store.set_meta(conn, _ih, (hist + [[i.get("id") for i in imps]])[-6:])
    if pblock:
        lines.append(pblock)
    if events:
        lines.append("[这一轮相关的旧事 · 可以自然提起，也可以不提]")
        for e in events:
            days = max(0, int((now - float(e["ts"])) / store.DAY))
            one_line = " ".join(str(e["text"]).split())[:80]
            lines.append(f"- {days}天前：{one_line}")
    if nap:
        lines.append(nap)
    if full:
        lines.append(full)
    if chg and (now - float(chg.get("at") or 0)) < 7 * store.DAY:
        lines.append(f"[他可能变了] 以前记的是「{chg.get('old', '')[:40]}」，"
                     "这次说的像是另一回事。别拿旧印象压他，先按现在的说法接；"
                     "真变了的话，下次梦会把旧的放下。")
    if corr and (now - float(corr.get("at") or 0)) < 3 * store.DAY:
        tail = f"错的那条是「{corr['old'][:40]}」，已经撤下来了。" if corr.get("old") else ""
        lines.append("[他刚说你记错了] 别辩解，认一句（“记岔了”），然后问清楚正确的是什么。" + tail)
    if dv:
        store.set_meta(conn, "last_divert", now)
        lines.append("[先说你自己的] 他刚说自己想要什么。你也有偏好，开口先把你的说出来——"
                     f"可以反对、可以岔开、可以说你更想干嘛。比如「{dv['line']}」"
                     "然后再管他。别一上来就给建议、问细节、算时间。回应不超过 40 字。")
    if pg:
        lines.append(f"[这轮怎么接 · {pg['name']}] {pg['hint']}回应不超过 {min(pg['cap'], bd['max'])} 字，宁可短。")
        lines.append("[如果没话说] 可以只回：嗯 / 行吧 / 你说是就是 / …… / 笑死 / 哦 /（没接话）。"
                     f"也可以用一个表情：{persona_mod.EXPRESSIONS[0]} 或 {persona_mod.EXPRESSIONS[2]}。"
                     "别每句都用。")
    else:
        lines.append(f"[这轮的长度] {bd['hint']}上限 {bd['max']} 字；说完了就停，不用总结。")
    lines.append("</plastic_memory>")
    return "\n".join(lines)


def _nap_note(conn, now: float) -> str:
    last_act = store.get_meta(conn, "last_activity_ts", 0) or 0
    last_dream = store.get_meta(conn, "last_dream_ts", 0) or 0
    notified = store.get_meta(conn, "nap_notified_for", 0) or 0
    if not last_dream or last_dream <= last_act or last_dream == notified or now - last_dream > 8 * 3600:
        return ""
    rep = store.get_meta(conn, "last_dream_report", {}) or {}
    bits = []
    if rep.get("promoted"):
        bits.append(f"把 {rep['promoted']} 件事记牢了")
    if rep.get("associations"):
        bits.append(f"把 {rep['associations']} 对旧事连起来了")
    if rep.get("demoted"):
        bits.append(f"放下了 {rep['demoted']} 件不再需要的")
    detail = "，".join(bits) or "整理了一下"
    store.set_meta(conn, "nap_notified_for", last_dream)
    return (f"[你刚打了个盹] 上一轮回复之后你睡了一会儿：{detail}。"
            "如果这轮气氛自然，可以像人一样顺口提一句（别解释机制、别说'记忆系统'）。")


def _full_note(conn, now: float, text: str = "") -> str:
    n = store.resident_count(conn)
    if n < 16:
        return ""
    last = store.get_meta(conn, "compact_offered_at", 0) or 0
    if now - last < 24 * 3600:
        return ""
    store.set_meta(conn, "compact_offered_at", now)

    face = ""
    try:
        st = persona_mod.active(persona_mod.load_spec(), text)
        if st["facets"]:
            face = st["facets"][0]["id"]
    except Exception:
        pass
    how = {
        "engineer": f"用工程师的方式开口：给数字（现在 {n} 条，压完大概剩几条）、代价（丢细节）、"
                    "收益（每轮都能带得动、反应更快），让他判断。别求他。",
        "old_friend": "用老友的方式开口：随口一点，可以自嘲（'我脑子里的东西有点多，转不动了'），"
                      "别正式、别列清单、别用'是否'这种词，问一句就行。",
        "guardian": "用守页的方式开口：把保留和放弃说明白（承诺和边界一条不动，放开的是细节），"
                    "让他决定。",
    }.get(face, "按你们平时说话的样子开口就行，别用系统提示的口吻。")

    return (f"[记忆快满了] 核心区 {n} 条，已经超过你每轮能带着的量。\n{how}\n"
            "不管用哪种方式，说清三件事就够了：你想整理一下、会丢掉一些细节、不会编造。"
            "别提'记忆''核心区''数据''系统'这些词。\n"
            "他同意就调用 pm_compact(confirm=true)；他说想留着，就把核心区写成日记交给他"
            "（pm_diary），再聊别的。\n"
            "他要是不接这个话，或者这轮正忙，就先不提——这不是必须完成的任务。")


# ── 观察 ─────────────────────────────────────────────────────────────────────

def observe_turn(conn, text: str, *, role: str = "user", owner: str = "",
                 session_id: str = "", now: Optional[float] = None) -> dict:
    """记一轮对话。包含纠正检测、变化检测、注意力标记（岔开是否被接受）。"""
    now = now or time.time()
    text = (text or "").strip()
    if not text:
        return {"stored": False, "reason": "empty"}

    # 上一轮岔开过的话，看他接不接
    if store.get_meta(conn, "last_divert"):
        # 键要跟 build_block 里读的一致（那边带 owner 后缀），否则多用户模式下永远读到空
        _ds = f"divert_stats:{owner}" if owner else "divert_stats"
        st = store.get_meta(conn, _ds, {"ok": 0, "no": 0}) or {"ok": 0, "no": 0}
        took = len(text) >= 8 and not any(k in text for k in ("算了", "不聊", "别说了", "随便", "嗯"))
        st["ok" if took else "no"] = int(st.get("ok" if took else "no", 0)) + 1
        store.set_meta(conn, _ds, st)
        store.set_meta(conn, "last_divert", None)

    if store.is_correction(text):
        hist = store.get_meta(conn, f"inject_hist:{owner}" if owner else "inject_hist", []) or []
        recent = hist[-1] if hist else []
        hit = store.find_corrected(conn, text, recent, owner)
        if hit:
            conn.execute("UPDATE impressions SET resident=0, pinned=0 WHERE id=?", (hit["id"],))
            conn.commit()
        store.set_meta(conn, f"pending_correction:{owner}" if owner else "pending_correction",
                       {"at": now, "old": (hit["text"] if hit else "")})

    res = store.observe(conn, text, role=role, session_id=session_id, now=now, owner=owner)
    if res.get("change"):
        store.set_meta(conn, f"pending_change:{owner}" if owner else "pending_change",
                       {"at": now, **res["change"]})
    return res


# ── 打盹 ─────────────────────────────────────────────────────────────────────

def maybe_nap(conn, owner: str = "", now: Optional[float] = None,
              bias: Optional[dict] = None) -> bool:
    """攒够了就在后台睡一觉。返回是否真的启动了。"""
    if _DREAMING.is_set():
        return False
    now = now or time.time()
    try:
        last_dream = store.get_meta(conn, "last_dream_ts", 0) or 0
        if now - last_dream < _NAP_MIN_GAP:
            return False
        if store.unconsolidated_count(conn, last_dream, owner) < _NAP_MIN_EVENTS:
            return False
    except Exception as exc:
        logger.warning("nap check failed: %s", exc)
        return False

    _DREAMING.set()
    started = time.time()

    def _run() -> None:
        try:
            c = store.connect()
            rep = dream_mod.dream(c, bias=bias or persona_mod.memory_bias(persona_mod.load_spec(), ""),
                                  history_n=_NAP_HISTORY_SAMPLE, owner=owner)
            rep["duration_s"] = round(time.time() - started, 2)
            store.set_meta(c, "last_dream_ts", time.time())
            store.set_meta(c, "last_dream_report", rep)
            logger.info("nap done: %s", rep)
        except Exception as exc:
            logger.warning("nap failed: %s", exc)
        finally:
            _DREAMING.clear()

    threading.Thread(target=_run, name="plastic-memory-nap", daemon=True).start()
    return True


def mark_activity(conn, now: Optional[float] = None) -> None:
    store.set_meta(conn, "last_activity_ts", now or time.time())
