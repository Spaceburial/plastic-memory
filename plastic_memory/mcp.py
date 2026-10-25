#!/usr/bin/env python3
"""MCP server —— 让任何支持 MCP 的宿主（Claude Code / Codex CLI / Cursor / Windsurf）
都能用这套记忆。零依赖，JSON-RPC 2.0 over stdio。

装完包之后，宿主配置里这样写就行（不需要绝对路径）：

    {
      "mcpServers": {
        "plastic-memory": {
          "command": "plastic-memory-mcp",
          "env": { "PLASTIC_MEMORY_API_KEY": "sk-..." }
        }
      }
    }

或者不装包、直接指文件：
    "command": "python", "args": ["-m", "plastic_memory.mcp"]
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__, distill, tools as pm_tools
from .llm import from_env

PROTOCOL = "2024-11-05"
SERVER = {"name": "plastic-memory", "version": __version__}


def _tool_specs() -> list[dict]:
    return [{"name": name, "description": schema["description"],
             "inputSchema": schema["parameters"]}
            for name, schema, _handler, _emoji in pm_tools.TOOLS]


def _dispatch(name: str, args: dict) -> str:
    for tname, _schema, handler, _emoji in pm_tools.TOOLS:
        if tname == name:
            return handler(args or {})
    return f"未知工具：{name}"


def _reply(rid: Any, result: Any = None, error: Any = None) -> None:
    msg: dict = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    llm = from_env()
    if llm:
        distill.set_llm(llm)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method, rid = req.get("method"), req.get("id")

        if method == "initialize":
            _reply(rid, {"protocolVersion": PROTOCOL,
                         "capabilities": {"tools": {}},
                         "serverInfo": SERVER})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _reply(rid, {"tools": _tool_specs()})
        elif method == "tools/call":
            params = req.get("params") or {}
            try:
                text = _dispatch(params.get("name", ""), params.get("arguments") or {})
                _reply(rid, {"content": [{"type": "text", "text": text}]})
            except Exception as exc:
                _reply(rid, {"content": [{"type": "text", "text": f"出错：{exc}"}],
                             "isError": True})
        elif method == "ping":
            _reply(rid, {})
        else:
            _reply(rid, error={"code": -32601, "message": f"不支持的方法：{method}"})


if __name__ == "__main__":
    main()
