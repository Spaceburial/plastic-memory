"""plastic-memory — 会遗忘、会做梦、会改主意的记忆。

核心层零依赖（只用标准库），不绑任何宿主：
    from plastic_memory import store, dream, persona, engine, distill

宿主适配器在 adapters/ 里：
    Hermes 插件、MCP server（Codex / Claude Code / Cursor …）、命令行。
"""

from . import store, dream, persona, distill, engine  # noqa: F401

__version__ = "0.4.1"
__all__ = ["store", "dream", "persona", "distill", "engine"]
