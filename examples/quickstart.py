#!/usr/bin/env python3
"""五分钟看懂它到底在干什么。

    python examples/quickstart.py

不要求配模型：没有 API key 时梦会退回规则档，照样能跑完。
数据写在临时目录，不碰你自己的库。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 必须在 import 之前指定数据目录，否则会写到你自己的库里
os.environ["PLASTIC_MEMORY_HOME"] = tempfile.mkdtemp(prefix="pm-quickstart-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plastic_memory import dream, engine, store          # noqa: E402
from plastic_memory.llm import from_env                 # noqa: E402

DAY = store.DAY
T0 = 1_760_000_000.0

# 一段普通的对话：抱怨、交代、随口一提、重要的事
DIALOG = [
    (0, "今天加班到十一点，累死了"),
    (0, "leader 又画饼，说下季度涨薪"),
    (1, "我想跳槽了，简历好久没改"),
    (1, "年糕今天吐了，带它去医院花了八百"),
    (3, "面试约在下周三，有点慌"),
    (3, "面的不好，问我分布式事务"),
    (5, "我妈又催我回家考公，烦"),
    (5, "涨薪谈下来了，但是活更多"),
    (8, "我不喜欢反复确认，你直接做就行"),
    (10, "明天下午三点前要把演示版本交上去"),
    (12, "这周又是天天加班"),
    (15, "我妈说给我介绍对象"),
    (20, "年糕好了，又开始拆家"),
    (25, "失眠到两点，脑子停不下来"),
    (30, "这工作真的没什么意思"),
]

def main() -> None:
    conn = store.connect()
    llm = from_env()
    print(f"数据目录：{store.data_dir()}")
    print(f"蒸馏方式：{'模型' if llm else '规则（没配 API key）'}\n")

    if llm:
        from plastic_memory import distill
        distill.set_llm(llm)
    else:
        print("  没有 API key 时是规则档：印象会退化成「清理过的原话」，不是判断。")
        print("  配好 PLASTIC_MEMORY_API_KEY 再跑，看到的才是抽象后的判断。\n")

    # 1) 一天天聊，每天结束做一次梦
    for day, text in DIALOG:
        store.observe(conn, text, role="user", now=T0 + day * DAY)
    for d in sorted({d for d, _ in DIALOG} | {30}):
        dream.dream(conn, now=T0 + d * DAY + 3600, history_n=8)

    # 2) 看看核心区里留下了什么判断
    told = store.get_meta(conn, "active_owner", "")
    rows = conn.execute(
        "SELECT slot, text FROM impressions WHERE resident=1 AND owner=? ORDER BY id",
        (told,)).fetchall()
    print(f"三十天聊下来，核心区留下 {len(rows)} 条判断：")
    for r in rows:
        print(f"  [{r['slot']:8s}] {r['text']}")

    # 3) 每一轮到底注入什么
    blk = engine.build_block(conn, "最近好累，工作也没什么意思", now=T0 + 31 * DAY) or ""
    print(f"\n这一轮会塞给模型的上下文（{len(blk)} 字）：")
    print(blk)

    # 4) 三件不同的事，看它记得住什么、忘得掉什么
    print("\n三个月后问起：")
    for q, expect in [("面试", "该记得"), ("年糕", "该记得"), ("午饭吃了什么", "该说不知道")]:
        hits = store.recall_events(conn, q, k=2, now=T0 + 90 * DAY)
        print(f"  「{q}」→ {'记得：' + hits[0]['text'][:28] if hits else '想不起来了'}"
              f"   （{expect}）")

    # 5) 追溯：一条判断是从哪句话长出来的
    first = conn.execute(
        "SELECT id, text FROM impressions WHERE resident=1 ORDER BY id LIMIT 1").fetchone()
    if first:
        print(f"\n「{first['text']}」是从这些话里长出来的：")
        node = store.trace_impression(conn, first["id"]) or {}
        for c in node.get("children", []):
            if c["type"] == "event":
                print(f"  ← {c['text']}")

    print("\n试着改一行：往 DIALOG 里加你自己的一句话，重跑一次。")


if __name__ == "__main__":
    main()
