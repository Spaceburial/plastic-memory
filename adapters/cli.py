#!/usr/bin/env python3
"""向后兼容的薄壳：老配置里写的是 adapters/cli.py，指过来就行。
新用法（装包之后）：plastic-memory observe "..."
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plastic_memory.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
