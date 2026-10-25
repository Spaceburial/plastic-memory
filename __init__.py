"""plastic-memory — 可塑性记忆。

三层：
  核心印象区  永久、本地、只以注入方式参与判断，没有任何导出入口。
  事件层      只收关键事件，按强度衰减，过期退出注入但保留可检索。
  杂讯        重复的、无信息量的，写入前就拦掉，不落盘（只留一个哈希计数）。

两个过程：
  筛选门      在线，写之前判"偏离"，不判"价值"。
  梦          离线，只做合并/提升/降权/淘汰，产出操作与假说，不生产新事实。

人设：不是单张角色卡，是几张会互相顶的"面"；权重接近时进入交叉态，把张力注入给模型。
"""

from __future__ import annotations

import logging

from . import hooks
from .plastic_memory import distill
from .plastic_memory.tools import TOOLS

logger = logging.getLogger(__name__)


def _slash(args: str = "", **_kw) -> str:
    from .plastic_memory.tools import handle_pm_status
    return handle_pm_status({})


def register(ctx) -> None:
    try:
        distill.set_llm(ctx.llm)  # 梦的蒸馏优先用宿主模型；拿不到就退回规则档
    except Exception as exc:
        logger.debug("plastic-memory: ctx.llm unavailable: %s", exc)
    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(name=name, toolset="plastic_memory", schema=schema,
                          handler=handler, emoji=emoji)
    ctx.register_hook("pre_llm_call", hooks.pre_llm_call)
    ctx.register_hook("post_llm_call", hooks.post_llm_call)
    ctx.register_hook("on_session_end", hooks.on_session_end)
    try:
        ctx.register_command("plastic-memory", handler=_slash,
                             description="可塑性记忆：三层容量、梦的次数、当前人设")
    except Exception as exc:  # 老版本没有 register_command 也不该让插件挂掉
        logger.debug("plastic-memory: register_command unavailable: %s", exc)
    logger.info("plastic-memory registered: %d tools, 3 hooks", len(TOOLS))
