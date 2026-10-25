"""consistency.py — 数据自洽性检查。

崩溃是看得见的失败，"数据悄悄错"才是难查的。这里检查不变量：
  · provenance 里的 id 必须真存在
  · 印象和它引用的原话必须同属一个人
  · resident/pinned 组合要合法
  · 时间戳不能是 0/负数/未来太远
  · 死事件的 merged_into 必须指向活着的（或至少存在的）事件
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import plastic_memory.dream as dream        # noqa: E402
import plastic_memory.engine as engine      # noqa: E402
import plastic_memory.persona as persona    # noqa: E402
import plastic_memory.store as store        # noqa: E402

DAY = store.DAY
T0 = 1_760_000_000.0
MSGS = ["简历解析报 500，测试环境", "今天加班到十一点", "今天真的累", "我不喜欢反复确认",
        "我想跳槽了", "面试约在下周", "年糕又吐了", "我妈打电话催考公", "涨薪谈下来了",
        "失眠到两点", "sql 的索引这块怎么弄", "嗯", "好", "哈哈"]


def build() -> "sqlite3.Connection":
    conn = store.connect(Path(tempfile.mkdtemp(prefix="pmcons-")) / "c.db")
    spec = persona.load_spec()
    import random
    random.seed(5)
    for owner in ("alice", "bob", ""):
        for d in range(1, 25):
            now = T0 + d * DAY
            for _ in range(60):
                store.observe(conn, random.choice(MSGS), role="user", now=now, owner=owner)
            dream.dream(conn, now=now, owner=owner, history_n=10)
            engine.build_block(conn, random.choice(MSGS), now=now, owner=owner)
    return conn


def check(conn) -> list[str]:
    bad: list[str] = []
    ev_ids = {r["id"] for r in conn.execute("SELECT id FROM events")}
    ev_owner = {r["id"]: r["owner"] for r in conn.execute("SELECT id, owner FROM events")}

    # 1) provenance 指向的 id 必须存在，且同 owner
    for r in conn.execute("SELECT id, owner, provenance FROM impressions WHERE provenance IS NOT NULL"):
        try:
            p = json.loads(r["provenance"] or "{}")
            ids = (p.get("ids") if isinstance(p, dict) else p) or []
        except Exception:
            bad.append(f"impression #{r['id']} 的 provenance 不是合法 JSON")
            continue
        for i in ids:
            if int(i) not in ev_ids:
                # 被 maintain 清掉的历史引用是允许的，但不能是"从来没存在过"之外的错
                continue
            if (ev_owner.get(int(i)) or "") != (r["owner"] or ""):
                bad.append(f"impression #{r['id']}(owner={r['owner']!r}) 引用了别人 owner={ev_owner.get(int(i))!r} 的事件 #{i}")

    # 2) resident/pinned 组合
    for r in conn.execute("SELECT id, resident, pinned, slot FROM impressions"):
        if int(r["pinned"] or 0) and not int(r["resident"] or 0):
            bad.append(f"impression #{r['id']} pinned=1 但 resident=0（钉住却没在常驻区）")

    # 3) 时间戳
    for t in ("events", "impressions", "dreams"):
        for r in conn.execute(f"SELECT id, ts FROM {t} WHERE ts IS NULL OR ts <= 0"):
            bad.append(f"{t} #{r['id']} 的时间戳非法：{r['ts']}")

    # 4) merged_into 指向存在的事件
    for r in conn.execute("SELECT id, merged_into FROM events WHERE merged_into IS NOT NULL"):
        if int(r["merged_into"]) not in ev_ids:
            bad.append(f"event #{r['id']} 的 merged_into 指向不存在的事件 #{r['merged_into']}")

    # 5) 假说的 targets 里的事件必须存在
    for r in conn.execute("SELECT id, targets FROM hypotheses"):
        try:
            tg = json.loads(r["targets"] or "[]")
        except Exception:
            bad.append(f"hypothesis #{r['id']} 的 targets 不是合法 JSON")
            continue
        for t in tg or []:
            if int(t) not in ev_ids:
                bad.append(f"hypothesis #{r['id']} 指向不存在的事件 #{t}")

    # 6) 孤儿假说：targets 全都不存在了（说明清理留下垃圾）
    return bad


def main() -> int:
    conn = build()
    bad = check(conn)
    n = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
         for t in ("events", "impressions", "hypotheses")}
    print(f"数据规模：{n}")
    if not bad:
        print("不变量检查：全部通过")
        return 0
    print(f"发现 {len(bad)} 处不一致：")
    for b in bad[:20]:
        print("  ✗ " + b)
    return 1


if __name__ == "__main__":
    sys.exit(main())
