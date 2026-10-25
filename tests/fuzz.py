"""fuzz.py — 随机操作序列，只为一件事：找崩溃。

    python fuzz.py [轮数]

随机文本 / 随机时间 / 随机 owner / 随机操作，任何异常都算失败。
"""

from __future__ import annotations

import random
import string
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import plastic_memory.dream as dream        # noqa: E402
import plastic_memory.engine as engine      # noqa: E402
import plastic_memory.store as store        # noqa: E402

DAY = store.DAY
T0 = 1_760_000_000.0

WORDS = ["加班", "跳槽", "猫", "我妈", "面试", "涨薪", "失眠", "leader", "房租", "结婚",
         "住院", "考公", "年糕", "rust", "文档", "周报", "出差", "咖啡", "下雨", "周末"]
PUNCT = ["", "。", "，", "？", "！", "…", " ", "\n", "\t"]
WEIRD = ["😤", "🫠", "\u200b", "\u200d", "\x00", "'", '"', "\\", "%s", "{0}", "—" * 20,
         "a" * 300, "汉" * 900]


def rand_text() -> str:
    r = random.random()
    if r < .15:
        return random.choice(WEIRD)
    if r < .25:
        return "".join(random.choice(string.printable) for _ in range(random.randint(0, 80)))
    n = random.randint(1, 8)
    s = "".join(random.choice(WORDS) + random.choice(PUNCT) for _ in range(n))
    return s.strip(" \n\t")


def rand_time() -> float:
    r = random.random()
    if r < .05:
        return 0.0
    if r < .10:
        return T0 - random.randint(1, 3000) * DAY      # 很久以前
    if r < .15:
        return T0 + random.randint(1, 3000) * DAY      # 未来
    return T0 + random.randint(0, 120) * DAY


def rand_owner() -> str:
    return random.choice(["", "", "", "alice", "bob", "张三"])


def main(rounds: int = 3000) -> int:
    random.seed(20261025)
    conn = store.connect(Path(tempfile.mkdtemp(prefix="pmfuzz-")) / "f.db")
    fails = 0
    for i in range(rounds):
        op = random.choice(["observe"] * 6 + ["dream", "recall", "correct", "pin", "block", "nap"])
        text, now, owner = rand_text(), rand_time(), rand_owner()
        try:
            if op == "observe":
                store.observe(conn, text, role="user", now=now, owner=owner)
            elif op == "dream":
                dream.dream(conn, now=now, owner=owner, history_n=random.randint(0, 5))
            elif op == "recall":
                store.recall_events(conn, text, k=random.randint(1, 8), now=now, owner=owner)
            elif op == "correct":
                engine.observe_turn(conn, text, owner=owner, now=now)
            elif op == "pin":
                row = store.find_impression(conn, text, 0, owner)
                if row:
                    conn.execute("UPDATE impressions SET pinned=1 WHERE id=?", (row["id"],))
            elif op == "block":
                engine.build_block(conn, text, now=now, owner=owner)
            elif op == "nap":
                store.stats(conn, now, owner=owner)
                store.resident_count(conn, owner)
        except Exception:
            fails += 1
            print(f"\n✗ 第 {i} 轮崩了：op={op} owner={owner!r} now={now}")
            print(f"  text={text[:120]!r}")
            print(traceback.format_exc()[-900:])
            if fails >= 5:
                print("\n失败太多，停止。")
                return 1

    n_ev = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    n_imp = conn.execute("SELECT COUNT(*) c FROM impressions").fetchone()["c"]
    n_hyp = conn.execute("SELECT COUNT(*) c FROM hypotheses").fetchone()["c"]
    print(f"\n{rounds} 轮随机操作，{fails} 次异常")
    print(f"  events {n_ev} / impressions {n_imp} / hypotheses {n_hyp}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3000))
