"""test_negative.py — 负面断言 + 边界情况。

正向断言只能证明"该有的有"，抓不到"不该有的混进来了"。前几轮修的 bug
（同一件事说两遍、助手回复被当用户话、过期条目还在召回）全是这一类。

    python test_negative.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import plastic_memory.engine as engine      # noqa: E402
import plastic_memory.dream as dream        # noqa: E402
import plastic_memory.store as store        # noqa: E402
from plastic_memory import tools as pm_tools   # noqa: E402

DAY = store.DAY
T0 = 1_760_000_000.0
CASES: list = []


def case(fn):
    CASES.append(fn)
    return fn


def fresh():
    return store.connect(Path(tempfile.mkdtemp(prefix="pmneg-")) / "t.db")


# ── 注入块里不该出现的东西 ────────────────────────────────────────────────────

@case
def 已提升的原话不能和印象同时出现():
    conn = fresh()
    store.observe(conn, "我下个月开始学日语，报了线下班", role="user", now=T0)
    ev = conn.execute("SELECT id FROM events").fetchone()
    dream._insert_impression(conn, "他开始学日语", "life", {}, T0, ev["id"], "")
    blk = engine.build_block(conn, "日语 学习", now=T0 + 3600) or ""
    assert "他开始学日语" in blk, "印象应该在"
    assert "我下个月开始学日语" not in blk, "已提升的原话不该再出现一遍"


@case
def 注入块不能含上一轮助手回复():
    """过滤在 hooks 层（Hermes 适配），不是 store 层——分层测。"""
    import hooks
    conn = fresh()
    real = store.connect
    store.connect = lambda *a, **k: conn
    try:
        asst = "我把 WAL 打开了，commit 从 9ms 降到 0.5ms，代价是偶尔要检查点。"
        hooks.post_llm_call(user_message="今天好累", assistant_response=asst, session_id="s", sender_id="")
        # 下一轮，宿主把上一轮助手的话当成了用户输入递进来
        hooks.post_llm_call(user_message=asst, assistant_response="嗯。", session_id="s", sender_id="")
        blk = engine.build_block(conn, asst, now=T0 + 120) or ""
        assert asst not in blk.replace("<plastic_memory>", ""), "助手自己的话不该被当成他的原话"
    finally:
        store.connect = real


@case
def 注入块不能含系统通知():
    import hooks
    conn = fresh()
    real = store.connect
    store.connect = lambda *a, **k: conn
    try:
        for t in ("[IMPORTANT: Background process proc_x completed]",
                  "::preview{file=\"a.html\"}", "__HERMES_CWD__ = C:/x"):
            hooks.post_llm_call(user_message=t, assistant_response="收到", session_id="s", sender_id="")
        blk = engine.build_block(conn, "随便聊聊", now=T0 + 60) or ""
        assert "IMPORTANT" not in blk and "preview{" not in blk and "HERMES_CWD" not in blk
    finally:
        store.connect = real


@case
def 过期的事不能再被召回():
    conn = fresh()
    store.observe(conn, "我明天下午三点前要交 B2 的演示版本", role="user", now=T0)
    exp = conn.execute("SELECT expires_at FROM events").fetchone()["expires_at"]
    assert exp and exp > T0, "应该解析出到期时间"
    assert store.recall_events(conn, "B2 演示", k=3, now=T0 + DAY), "到期前该能召回"
    assert not store.recall_events(conn, "B2 演示", k=3, now=exp + DAY), "到期后不该召回"


@case
def 被纠正撤下的印象不能再注入():
    conn = fresh()
    dream._insert_impression(conn, "他讨厌猫", "pref", {}, T0, 1, "")
    # 先注入一次，让 inject_hist 记住"这轮提过哪条"——纠正只能定位到当时在用的那些
    engine.build_block(conn, "猫", now=T0 + 30)
    row = conn.execute("SELECT id FROM impressions").fetchone()
    engine.observe_turn(conn, "你记错了，我不是讨厌猫", owner="", now=T0 + 60)
    left = conn.execute("SELECT resident FROM impressions WHERE id=?", (row["id"],)).fetchone()
    assert left["resident"] == 0, "纠正后该退出常驻"
    blk = engine.build_block(conn, "猫 讨厌", now=T0 + 120) or ""
    # 只检查"记忆条目"（- 开头），不检查提示文本：撤下时提示里会引用那条原文
    memory_lines = [l for l in blk.splitlines() if l.strip().startswith("- ")]
    assert not any("他讨厌猫" in l for l in memory_lines), f"撤下的印象不该作为记忆条目出现：{memory_lines}"


@case
def A的记忆不能出现在B的注入里():
    conn = fresh()
    store.observe(conn, "我妈住院了，做了个手术", role="user", owner="alice", now=T0)
    dream.dream(conn, now=T0 + 60, owner="alice")
    blk_b = engine.build_block(conn, "我妈 医院", owner="bob", now=T0 + 120) or ""
    assert "住院" not in blk_b, f"B 不该看到 A 的事：{blk_b[:200]}"
    blk_a = engine.build_block(conn, "我妈 医院", owner="alice", now=T0 + 120) or ""
    assert "住院" in blk_a or "手术" in blk_a, "A 自己应该能看到"


# ── 边界 ──────────────────────────────────────────────────────────────────────

@case
def 空输入不崩():
    conn = fresh()
    assert store.observe(conn, "", role="user", now=T0)["stored"] is False
    assert store.observe(conn, "   ", role="user", now=T0)["stored"] is False
    assert engine.build_block(conn, "", now=T0) is None


@case
def 超长输入被截断且不崩():
    conn = fresh()
    long = "加班" * 5000
    store.observe(conn, long, role="user", now=T0)
    row = conn.execute("SELECT length(text) l FROM events").fetchone()
    assert row["l"] <= 1200, f"应该截断到 1200，实际 {row['l']}"


@case
def 特殊字符和注入字符串不崩():
    conn = fresh()
    for t in ("'; DROP TABLE events; --", "😤🫠🙃\u200b\u200d", "<script>alert(1)</script>",
              "\\x00\\x01", "'' OR 1=1 --"):
        store.observe(conn, t, role="user", now=T0)
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] >= 0


@case
def 未来时间戳和负时间不崩():
    conn = fresh()
    store.observe(conn, "我明年要结婚", role="user", now=T0)
    store.observe(conn, "很久以前的事", role="user", now=T0 - 400 * DAY)
    blk = engine.build_block(conn, "结婚", now=T0 - 500 * DAY) or ""
    assert isinstance(blk, str)


@case
def 旧库能升级_没有新列的库():
    d = Path(tempfile.mkdtemp(prefix="pmold-")) / "old.db"
    raw = sqlite3.connect(str(d))
    raw.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
          session_id TEXT, role TEXT, text TEXT NOT NULL, kind TEXT NOT NULL,
          salience REAL NOT NULL, uses INTEGER DEFAULT 0, last_used REAL,
          half_life REAL, alive INTEGER DEFAULT 1, merged_into INTEGER);
        CREATE TABLE impressions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
          text TEXT NOT NULL, slot TEXT, strength REAL NOT NULL, uses INTEGER DEFAULT 0,
          last_used REAL, resident INTEGER DEFAULT 1, provenance TEXT);
        CREATE TABLE dropped (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
          kind TEXT, reason TEXT, digest TEXT);
        CREATE TABLE hypotheses (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
          text TEXT NOT NULL, targets TEXT, status TEXT DEFAULT 'open');
        CREATE TABLE dreams (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, report TEXT);
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    raw.execute("INSERT INTO events (ts, text, kind, salience, uses, alive) VALUES (?,?,?,?,?,1)",
                (T0, "旧版本写的一条", "life", 1.0, 2))
    raw.commit()
    raw.close()
    conn = store.connect(d)          # 应该自己把缺的列补上，不报错
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    assert {"tags", "owner", "expires_at"} <= cols, f"迁移没补上列：{cols}"
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1


@case
def 两个连接同时写不炸():
    d = Path(tempfile.mkdtemp(prefix="pmcc-")) / "c.db"
    a = store.connect(d)
    b = store.connect(d)
    for i in range(30):
        store.observe(a, f"甲写的第{i}句，加班很累", role="user", now=T0 + i)
        store.observe(b, f"乙写的第{i}句，今天很烦", role="user", now=T0 + i)
    assert a.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] > 0


@case
def 压缩不会把单条事件变成新印象():
    conn = fresh()
    rows = ["他总在深夜改 bug", "他妈催他回家考公", "他想跳槽去杭州", "他养了只猫叫年糕",
            "他涨薪了但活更多", "他面试挂了三次", "他失眠到两点", "他周末只想躺着"]
    for i, t in enumerate(rows):
        dream._insert_impression(conn, t, "life", {}, T0 + i, i + 1, "")
    before = conn.execute("SELECT COUNT(*) c FROM impressions WHERE resident=1").fetchone()["c"]
    assert before >= 6, f"测试数据没插进去：{before}"
    rep = dream.compress_core(conn, now=T0 + 100, ratio=0.7, keep_min=3)
    assert "skipped" not in rep, f"不该跳过：{rep}"
    after = conn.execute("SELECT COUNT(*) c FROM impressions WHERE resident=1").fetchone()["c"]
    assert after <= before, f"压缩后不该变多：{before} -> {after}"
    assert rep["after"] == after


@case
def 删除的记忆不会复活():
    conn = fresh()
    store.observe(conn, "我妈住院了", role="user", now=T0)
    conn.execute("UPDATE events SET alive=0")
    conn.commit()
    conn.execute("DELETE FROM events")
    conn.commit()
    blk = engine.build_block(conn, "我妈 医院", now=T0 + 60) or ""
    assert "住院" not in blk


@case
def 九个工具在空库上都不报错():
    conn = fresh()
    real = store.connect
    store.connect = lambda *a, **k: conn
    try:
        for name, _schema, handler, _emoji in pm_tools.TOOLS:
            out = handler({"confirm": False, "query": "随便", "text": "随便"})
            assert isinstance(out, str) and out, f"{name} 返回空或非字符串：{out!r}"
    finally:
        store.connect = real


@case
def 工具对怪参数不崩():
    conn = fresh()
    real = store.connect
    store.connect = lambda *a, **k: conn
    try:
        cases = [
            ("pm_recall", {"query": ""}),
            ("pm_recall", {"query": "x" * 5000}),
            ("pm_recall", {"query": "猫", "k": 0}),
            ("pm_recall", {"query": "猫", "k": -3}),
            ("pm_pin", {"text": ""}),
            ("pm_pin", {"id": 999999}),
            ("pm_trace", {"id": 999999}),
            ("pm_trace", {"text": "量子纠缠"}),
            ("pm_observe", {"text": ""}),
            ("pm_compact", {"confirm": True}),          # 空库压缩
            ("pm_dream", {}),
            ("pm_status", {}),
            ("pm_persona", {"text": ""}),
            ("pm_diary", {"style": "log", "raw": True}),
        ]
        for name, args in cases:
            for tname, _s, handler, _e in pm_tools.TOOLS:
                if tname == name:
                    out = handler(args)
                    assert isinstance(out, str), f"{name}({args}) 返回了 {type(out)}"
                    break
    finally:
        store.connect = real


@case
def 空核心区导出日记也不崩():
    conn = fresh()
    real = store.connect
    store.connect = lambda *a, **k: conn
    try:
        out = [h for n, _s, h, _e in pm_tools.TOOLS if n == "pm_diary"][0]({})
        assert isinstance(out, str)
    finally:
        store.connect = real


@case
def 印象全删掉后注入不崩():
    conn = fresh()
    dream._insert_impression(conn, "他养了只猫叫年糕", "fact", {}, T0, 1, "")
    conn.execute("UPDATE impressions SET resident=0")
    conn.commit()
    blk = engine.build_block(conn, "年糕 猫", now=T0 + 60) or ""
    assert isinstance(blk, str)


def main() -> int:
    ok = fail = 0
    for fn in CASES:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            ok += 1
        except Exception as exc:
            print(f"  ✗ {fn.__name__}")
            print("     " + "".join(traceback.format_exception_only(type(exc), exc)).strip())
            fail += 1
    print(f"\n{ok} 条通过，{fail} 条失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
