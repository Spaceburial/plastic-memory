#!/usr/bin/env python3
"""命令行入口。装完包之后直接 `plastic-memory status` 就能用。

    plastic-memory observe "我下个月要结婚，在老家办"
    plastic-memory recall  "跳槽"
    plastic-memory dream
    plastic-memory status
    plastic-memory context "今天好累"      # 打印这一轮会注入的上下文
    plastic-memory diary
    plastic-memory pin "结婚"
    plastic-memory trace "结婚"
    plastic-memory compact

所有命令都支持 --owner 区分不同的人（默认 default）。
"""

from __future__ import annotations

import argparse
import sys

from . import store
from . import tools as pm_tools
from .llm import from_env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="plastic-memory", description="会遗忘、会做梦、会改主意的记忆")
    ap.add_argument("--owner", default="default", help="给谁记（多用户隔离用）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("observe", help="记一条")
    p.add_argument("text")
    p.add_argument("--kind", default=None)

    p = sub.add_parser("recall", help="检索事件层")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=6)

    sub.add_parser("dream", help="做一次梦")
    sub.add_parser("status", help="容量与状态")
    sub.add_parser("diary", help="把核心区写成日记")
    sub.add_parser("compact", help="半衰期压缩")

    p = sub.add_parser("pin", help="钉住一条记忆")
    p.add_argument("text")
    p.add_argument("--unpin", action="store_true")

    p = sub.add_parser("trace", help="追溯一条印象的来源")
    p.add_argument("text")

    p = sub.add_parser("context", help="打印这一轮会注入的上下文")
    p.add_argument("text")

    p = sub.add_parser("persona", help="当前人设与权重")
    p.add_argument("text", nargs="?", default="")

    p = sub.add_parser("selftest", help="跑内置自测")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        import runpy
        runpy.run_module("plastic_memory.selftest", run_name="__main__")
        return 0

    # tools 层的 handler 从 meta 的 active_owner 取 owner，CLI 必须先把这个值落盘
    if args.cmd != "context":
        store.set_meta(store.connect(), "active_owner", args.owner)

    if from_env():
        from . import distill
        distill.set_llm(from_env())

    handlers = {
        "status": lambda: pm_tools.handle_pm_status({}),
        "recall": lambda: pm_tools.handle_pm_recall({"query": args.query, "k": args.k}),
        "dream": lambda: pm_tools.handle_pm_dream({}),
        "compact": lambda: pm_tools.handle_pm_compact({"confirm": True}),
        "pin": lambda: pm_tools.handle_pm_pin({"text": args.text, "unpin": args.unpin}),
        "trace": lambda: pm_tools.handle_pm_trace({"text": args.text}),
        "diary": lambda: pm_tools.handle_pm_diary({}),
        "persona": lambda: pm_tools.handle_pm_persona({"text": args.text}),
        "observe": lambda: pm_tools.handle_pm_observe({"text": args.text, "kind": args.kind}),
    }
    if args.cmd == "context":
        from . import engine
        print(engine.build_block(store.connect(), args.text, owner=args.owner)
              or "（这轮没有可注入的记忆）")
        return 0
    print(handlers[args.cmd]())
    return 0


if __name__ == "__main__":
    sys.exit(main())
