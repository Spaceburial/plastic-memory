"""selftest.py — 不依赖 Hermes runtime，直接验证三层记忆的行为。

跑法：python selftest.py
用一个临时 db 模拟 30 天对话，检查：杂讯拦截 / 去重强化 / 衰减淘汰 / 梦的提升与合并 /
注入块的样子 / 核心区没有导出入口。
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

from . import store      # noqa: E402
from . import dream      # noqa: E402
from . import persona    # noqa: E402

DAY = store.DAY
T0 = 1_770_000_000.0

tmp = Path(tempfile.mkdtemp(prefix="pmselftest-"))
conn = store.connect(tmp / "test.db")

feed: list[tuple[float, str, str]] = [
    (0.0, "我叫李工，我在郑州大学做 B2 精准就业项目", "user"),
    (0.1, "不要写太长的报告，我看不完", "user"),
    (0.2, "好的", "user"),
    (1.0, "最近很累，连着加班到两点", "user"),
    (2.0, "不要写太长的报告，我看不完", "user"),
    (2.1, "哈哈", "user"),
    (3.0, "部署用 uv，记得这台机器没有 pip", "user"),
    (3.5, "在吗", "user"),
    (5.0, "我不喜欢反复确认，直接做就行", "user"),
    (7.0, "C 盘只剩 5GB，大文件放 D 盘", "user"),
    (9.0, "不要写太长的报告，我看不完", "user"),
    (12.0, "晚安", "user"),
    (15.0, "上次说的 PPT 配色不用了", "user"),
    (18.0, "有点焦虑，比赛快到了", "user"),
    (21.0, "下周一把演示版本给我", "user"),
    (24.0, "算了", "user"),
    (27.0, "演示版本要能在手机上打开", "user"),
]

results = {"new": 0, "repeat": 0, "junk": 0}
for day, text, role in feed:
    r = store.observe(conn, text, role=role, session_id="s1", now=T0 + day * DAY)
    results[r["reason"]] = results.get(r["reason"], 0) + 1

print("=" * 66)
print("写入 30 天事件后：", results)
st = store.stats(conn, T0 + 30 * DAY)
print("分层统计：", json.dumps(st, ensure_ascii=False))

# 30 天后做一次梦
spec = persona.load_spec()
bias = persona.memory_bias(spec, "最近很累，项目要部署")
rep = dream.dream(conn, now=T0 + 30 * DAY, bias=bias)
print("\n梦的报告：", json.dumps(rep, ensure_ascii=False))
post = store.stats(conn, T0 + 30 * DAY)
print("做梦后统计：", json.dumps(post, ensure_ascii=False))

imps = store.core_impressions(conn, query="报告 太累 项目", now=T0 + 30 * DAY, limit=10)
print(f"\n核心印象（内部层，共 {len(imps)} 条可注入）：")
for i in imps:
    print(f"  · {i['text']}  [ret={i['retention']}]")

print("\n事件层残留：")
for e in store.recent_events(conn, limit=8):
    print(f"  · {e['kind']:8s} {e['text'][:46]}  uses={e['uses']}")

print("\n召回测试（query='部署'）：")
for e in store.recall_events(conn, "部署", k=3, now=T0 + 30 * DAY):
    print(f"  · {e['text'][:50]}  score={e['score']}")

# 注入块——走真实的 build_block，不是自测里另写一份
from . import engine  # noqa: E402
text_q = "这个功能怎么部署，顺便我最近真的很累"
block = engine.build_block(conn, text_q, now=T0 + 30 * DAY) or ""
print("\n" + "=" * 66)
print("注入块（本应发给模型的那段，约 %d 字）：" % len(block))
print(block)

# 断言
assert results["junk"] >= 4, "短寒暄应该被杂讯门拦下"
assert results["repeat"] >= 2, "重复的话应该只累加强度、不新建"
assert post["impressions"] >= 3, "关键事件应该升入核心区"
assert len(imps) >= 3
assert len(block) < 1200, "注入块不能膨胀"

from . import tools as pm_tools  # noqa: E402
names = [t[0] for t in pm_tools.TOOLS]
for forbidden in ("pm_core", "pm_dump", "pm_export", "pm_impressions"):
    assert forbidden not in names, f"核心区不允许有导出工具：{forbidden}"
assert not any("导出" in str(t[1]) for t in pm_tools.TOOLS), "工具描述里不该出现导出核心区的入口"
print("\n断言全部通过。核心区工具入口：", names)
print("db 位置：", tmp / "test.db")
