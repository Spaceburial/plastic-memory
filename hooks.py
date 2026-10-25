"""hooks.py — Hermes 适配层。

只做三件事：从 Hermes 的 hook 参数里取出"这一轮谁说了什么"、过滤掉不可靠的消息、
把结果转给 engine。所有记忆逻辑都在 plastic_memory/engine.py 里，别的宿主可以直接用它。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

try:  # 作为插件包的一部分
    from .plastic_memory import engine, store
except ImportError:  # 被测试或 adapters/ 当顶层模块导入时
    from plastic_memory import engine, store

logger = logging.getLogger(__name__)


def _text_of(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        parts = []
        for p in msg:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return " ".join(parts)
    return "" if msg is None else str(msg)


def _debug() -> bool:
    import os
    return os.environ.get("PLASTIC_MEMORY_DEBUG", "").strip() not in ("", "0", "false")


def _is_system_note(text: str) -> bool:
    s = (text or "").strip()
    return (s.startswith("[IMPORTANT:") or s.startswith("[System")
            or "__HERMES_CWD_" in s or s.startswith("<plastic_memory>"))


def _looks_like_user_speech(text: str) -> bool:
    s = (text or "").strip()
    if not s or len(s) > 400:
        return False
    if "```" in s or s.startswith(("MEDIA:", "::")) or "\n## " in s or "\n**" in s:
        return False
    return True


def _looks_assistantish(text: str) -> bool:
    """冷启动兜底：还没有历史回复可对照时，靠形状判断这句像不像我自己说的。"""
    s = (text or "").strip()
    if len(s) > 120:
        return True
    if s.startswith(("我找到", "我修", "加好", "完成", "先给", "结论", "第 ", "顺手", "顺便说")):
        return True
    if "**" in s or s.count("\n") >= 3:
        return True
    return False


# ── hooks ────────────────────────────────────────────────────────────────────

def pre_llm_call(**kw) -> Optional[dict]:
    try:
        text = _text_of(kw.get("user_message"))
        if not text.strip():
            return None
        owner = str(kw.get("sender_id") or "")
        conn = store.connect()
        if owner:
            store.set_meta(conn, "active_owner", owner)
        block = engine.build_block(conn, text, owner=owner)
        if not block:
            return None
        if _debug():
            try:
                with open(store.data_dir() / "inject.log", "a", encoding="utf-8") as f:
                    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{block}\n")
            except Exception:
                pass
        return {"context": block}
    except Exception as exc:
        logger.warning("plastic-memory pre_llm_call failed: %s", exc)
        return None


def post_llm_call(**kw) -> None:
    try:
        user_text = _text_of(kw.get("user_message"))
        asst = _text_of(kw.get("assistant_response"))
        sid = str(kw.get("session_id") or "")
        owner = str(kw.get("sender_id") or "")
        conn = store.connect()
        now = time.time()

        # 自检：有些路径会把"我上一轮说的话"当成这一轮的用户输入递进来
        _ra_key = f"recent_asst:{owner}" if owner else "recent_asst"
        _recent_asst = store.get_meta(conn, _ra_key, []) or []
        _echo = any(store.similarity(user_text, a) > 0.55 for a in _recent_asst if a)
        if asst.strip():
            store.set_meta(conn, _ra_key, ([asst[:400]] + _recent_asst)[:3])
        if _echo and user_text.strip():
            logger.info("plastic-memory: 跳过一条自我回显（%s…）", user_text[:24])
            return
        if not _recent_asst and _looks_assistantish(user_text):
            logger.info("plastic-memory: 冷启动跳过一条不像用户说的话（%s…）", user_text[:24])
            return

        if asst.strip() and store.classify(asst) == "promise":
            store.observe(conn, asst, role="assistant", session_id=sid, kind="promise",
                          now=now, owner=owner)
        engine.mark_activity(conn, now)

        if user_text.strip() and not _is_system_note(user_text) and _looks_like_user_speech(user_text):
            engine.observe_turn(conn, user_text, role="user", owner=owner,
                                session_id=sid, now=now)
        engine.maybe_nap(conn, owner=owner)
    except Exception as exc:
        logger.warning("plastic-memory post_llm_call failed: %s", exc)


def on_session_end(**kw) -> None:
    try:
        engine.mark_activity(store.connect())
    except Exception as exc:
        logger.debug("plastic-memory on_session_end failed: %s", exc)
