"""dream.py — 梦：离线巩固。输出的是对已有条目的"操作"，不是新的自然语言摘要。

为什么不让梦自由写总结：只要它生成文本，就一定会补全、会美化、会把模糊的细节编圆，
梦就成了虚构事实的流水线。这里梦只做四件事——合并、提升、降权、淘汰——外加产出
"假说"（在核心区之外，等现实印证）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Optional

from . import distill, store

logger = logging.getLogger("plastic_memory.dream")

# 印象句的模板：规则版只能做到"换一种说法"，不做真正的抽象。
# 真正的抽象（"这个人不喜欢被安排"）需要 LLM，见 dream_llm 扩展点。
_TEMPLATE = {
    "identity": "身份：{t}",
    "promise":  "约定：{t}",
    "boundary": "边界：{t}",
    "pref":     "偏好：{t}",
    "emotion":  "近期情绪：{t}",
    "fact":     "事实：{t}",
}

# 常驻池上限：注入只有 6 个槽位，常驻类无限膨胀等于没有常驻。
_MAX_RESIDENT_PINS = 12


def _has_impression(conn: sqlite3.Connection, text: str, resident_only: bool = False,
                    owner: str = "") -> bool:
    q = "SELECT text FROM impressions WHERE owner=?" + (" AND resident=1" if resident_only else "")
    rows = conn.execute(q, (owner or "",)).fetchall()
    return any(store.similarity(text, r["text"]) > 0.62 for r in rows)


def _eligible(ev, bias: dict, now: float) -> bool:
    """能不能升进核心区：本身够重（身份/约定/边界），或者被反复提起。"""
    kind = ev["kind"]
    if kind == "misc":
        return False
    mult = float(bias.get(kind, 1.0))
    sal = float(ev["salience"])
    if sal < 1.6 and (int(ev["uses"]) * 1.0 + sal) * mult < 3.0:
        return False
    if now - float(ev["ts"]) < 0.5 * store.DAY and int(ev["uses"]) < 1:
        return False  # 刚说的话先别急着升格
    return True


def _insert_impression(conn: sqlite3.Connection, text: str, kind: str,
                       bias: dict, now: float, ev_id, owner: str = "") -> bool:
    ids = [int(x) for x in (ev_id if isinstance(ev_id, (list, tuple, set)) else [ev_id])]
    text = (text or "").strip()
    if not text or _has_impression(conn, text, owner=owner):
        return False
    mult = float(bias.get(kind, 1.0))
    # 印象不继承事件的 expires_at：那个时间是"这件事什么时候发生"，不是"这条记忆
    # 什么时候作废"。否则"下个月开始学日语"这类长期事实会在到期日被整条滤掉。
    conn.execute(
        "INSERT INTO impressions (ts, text, slot, strength, uses, last_used, resident,"
        " provenance, owner, expires_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now, text, kind, 20.0 * max(0.6, mult), 0, now, 1,
         json.dumps({"from": "events", "ids": ids}, ensure_ascii=False), owner or "",
         None))
    if store.is_critical(text):
        conn.execute("UPDATE impressions SET pinned=1, strength=60.0 WHERE id=last_insert_rowid()")
    return True


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _targets_confirmed(conn: sqlite3.Connection, targets: list[int], owner: str = "",
                       since: float = 0.0) -> bool:
    """假说成立 = 它盯的事件在**假说提出之后**真的进了核心区。

    两个都不行：
      · "任意一条进任意一条印象"（原来）：假说盯的本来就是反复出现的事，创建时就已提升，
        三个月下来 442 条假说里 261 条 confirmed，等于没判据；
      · "全部落在同一条印象里"：太严，实测 0 条，假说永远只能过期。
    正确口径是时间：用假说自己的 ts 当下界，之后才发生的提升才算现实给了回应。
    """
    want = {int(t) for t in targets}
    if not want:
        return False
    for r in conn.execute("SELECT provenance FROM impressions WHERE owner=? AND ts>?",
                          (owner or "", float(since))):
        try:
            ids = {int(x) for x in (json.loads(r["provenance"] or "{}") or {}).get("ids") or []}
        except Exception:
            continue
        if want & ids:
            return True
    return False


def dream(conn: sqlite3.Connection, *, now: Optional[float] = None,
          bias: Optional[dict] = None, evict_threshold: float = 0.12,
          history_n: int = 12, owner: str = "") -> dict:
    now = now or time.time()
    bias = bias or {}
    report = {"at": now, "merged": 0, "promoted": 0, "conflicts": 0,
              "evicted": 0, "hypotheses": 0, "junk_seen": 0, "demoted": 0,
              "history_sampled": 0, "associations": 0}

    # 合并只看近 45 天：三个月前的事不该再跟今天的话搅在一起
    rows = conn.execute("SELECT * FROM events WHERE alive=1 AND ts > ? AND owner=? ORDER BY ts ASC",
                        (now - 45 * store.DAY, owner or "")).fetchall()

    # 1) 合并：同 kind、够像的两条，合成一条（保留信息量大的那条）
    merged_away: set[int] = set()
    for i, a in enumerate(rows):
        if a["id"] in merged_away:
            continue
        for b in rows[i + 1:]:
            if b["id"] in merged_away or b["kind"] != a["kind"]:
                continue
            if store.similarity(a["text"], b["text"]) >= 0.45:
                keep, drop = (a, b) if len(a["text"]) >= len(b["text"]) else (b, a)
                conn.execute(
                    "UPDATE events SET uses=uses+?, last_used=?, merged_into=?, alive=0 WHERE id=?",
                    (int(drop["uses"]) + 1, now, keep["id"], drop["id"]))
                merged_away.add(drop["id"])
                report["merged"] += 1

    # 2) 提升：按主题聚合。同一件事的不同侧面合成一条判断；
    #    单条但本身够重（身份/约定/边界）的，单独提升。
    rows = conn.execute("SELECT * FROM events WHERE alive=1 AND owner=?", (owner or "",)).fetchall()
    by_topic: dict[str, list] = {}
    singles: list = []
    for r in rows:
        tags = store.tags_of(r["text"])
        if tags:
            by_topic.setdefault(tags[0], []).append(r)
        elif float(r["salience"]) >= 1.6:
            singles.append(r)

    for topic, evs in by_topic.items():
        if len(evs) < 2 and not any(float(e["salience"]) >= 1.6 for e in evs):
            continue                      # 单个不显著的旧事，继续留在事件层
        merged = distill.merge([e["text"] for e in evs])
        if merged and _insert_impression(conn, merged, evs[0]["kind"], bias, now,
                                         [e["id"] for e in evs], owner):
            report["promoted"] += 1
            report.setdefault("topics", []).append(f"{topic}:{len(evs)}条")

    if singles:
        _one = distill.llm([(r["id"], r["text"]) for r in singles])
    else:
        _one = {}
    for r in singles:
        text = _one.get(r["id"]) or distill.rules(r["text"])
        if _insert_impression(conn, text, r["kind"], bias, now, r["id"], owner):
            report["promoted"] += 1

    # 3) 冲突：同 kind、像但一个人说"要"另一个说"不"——用户改主意了，旧的退出常驻
    imps = conn.execute("SELECT * FROM impressions WHERE resident=1 AND owner=? ORDER BY ts ASC",
                        (owner or "",)).fetchall()
    for i, a in enumerate(imps):
        for b in imps[i + 1:]:
            if a["slot"] != b["slot"]:
                continue
            sim = store.similarity(a["text"], b["text"])
            if 0.25 <= sim <= 0.72 and store._negated(a["text"]) != store._negated(b["text"]):
                conn.execute("UPDATE impressions SET resident=0 WHERE id=?", (a["id"],))
                report["conflicts"] += 1
                break

    # 4) 淘汰：衰减到阈值以下、从没被用过的
    for r in conn.execute("SELECT * FROM events WHERE alive=1 AND owner=?", (owner or "",)).fetchall():
        if int(r["uses"]) == 0 and store.retention(r, now) < evict_threshold:
            conn.execute("UPDATE events SET alive=0 WHERE id=?", (r["id"],))
            report["evicted"] += 1

    # 5) 假说：反复出现的主题，先放在核心区之外，等现实印证
    #    先看有没有被印证（targets 里的事件已经进了核心区），再把过期的关掉。
    #    原来 status 只被 SELECT 读、从没被 UPDATE，hypotheses_open 会单调涨。
    for h in conn.execute("SELECT id, targets, ts FROM hypotheses WHERE status='open' AND owner=?",
                          (owner or "",)).fetchall():
        try:
            tgt = [int(x) for x in json.loads(h["targets"] or "[]")]
        except Exception:
            continue
        if _targets_confirmed(conn, tgt, owner, float(h["ts"] or 0)):
            conn.execute("UPDATE hypotheses SET status='confirmed' WHERE id=?", (h["id"],))
    conn.execute("UPDATE hypotheses SET status='stale' WHERE status='open' AND ts < ? AND owner=?",
                 (now - 30 * store.DAY, owner or ""))
    # open 假说也要有上限：每梦最多造 6 条，90 天下来能堆几百条，
    # 而它们只是"待验证的猜测"，不该比核心印象还多。超过 50 条就把最旧的结案。
    conn.execute(
        "UPDATE hypotheses SET status='stale' WHERE status='open' AND owner=? AND id NOT IN"
        " (SELECT id FROM hypotheses WHERE status='open' AND owner=? ORDER BY id DESC LIMIT 50)",
        (owner or "", owner or ""))
    hot = conn.execute(
        "SELECT * FROM events WHERE alive=1 AND uses>=2 AND owner=? ORDER BY uses DESC LIMIT 3",
        (owner or "",)).fetchall()
    for r in hot:
        text = f"反复出现：{_clip(r['text'], 60)}"
        exists = conn.execute("SELECT 1 FROM hypotheses WHERE text=? AND status='open' AND owner=?",
                              (text, owner or "")).fetchone()
        if not exists:
            conn.execute("INSERT INTO hypotheses (ts, text, targets, owner) VALUES (?,?,?,?)",
                         (now, text, json.dumps([r["id"]]), owner or ""))
            report["hypotheses"] += 1

    report["junk_seen"] = conn.execute("SELECT COALESCE(SUM(count), 0) c FROM dropped").fetchone()["c"]

    # 6) 联想：梦里不该只翻今天的事。随机抽一批 20 天以前的旧事（含已退出注入的），
    #    跟近期的话比"像但不同"的对——这才是梦真正擅长的：发现跨时间的同一件事。
    hist = conn.execute(
        "SELECT * FROM events WHERE ts < ? AND owner=? ORDER BY RANDOM() LIMIT ?",
        (now - 20 * store.DAY, owner or "", max(0, int(history_n)))).fetchall()
    report["history_sampled"] = len(hist)
    recent = [r for r in conn.execute(
        "SELECT * FROM events WHERE alive=1 AND ts > ? AND owner=?",
        (now - 20 * store.DAY, owner or "")).fetchall()]
    made = 0
    for r in recent:
        if made >= 3:
            break
        for h in hist:
            if h["id"] == r["id"] or h["kind"] != r["kind"]:
                continue
            sim = store.similarity(r["text"], h["text"])
            if 0.28 <= sim <= 0.58:
                text = (f"可能是同一件事：{_clip(h['text'], 28)} ／ {_clip(r['text'], 28)}")
                exists = conn.execute(
                    "SELECT 1 FROM hypotheses WHERE text=? AND status='open' AND owner=?",
                    (text, owner or "")).fetchone()
                if not exists:
                    conn.execute("INSERT INTO hypotheses (ts, text, targets, owner) VALUES (?,?,?,?)",
                                 (now, text, json.dumps([h["id"], r["id"]]), owner or ""))
                    made += 1
                    report["associations"] += 1
                break

    # 7) 常驻池上限：核心区的注入槽位只有 6 个，常驻类无限膨胀等于没有常驻。
    #    超限时把最久没被注入的退出常驻（不删，仍可检索），让位置留给还在被用到的事。
    resident_pins = conn.execute(
        "SELECT * FROM impressions WHERE resident=1 AND pinned=0 AND owner=? "
        "AND slot IN ('identity','promise','boundary') ORDER BY last_used ASC",
        (owner or "",)).fetchall()
    if len(resident_pins) > _MAX_RESIDENT_PINS:
        for r in resident_pins[: len(resident_pins) - _MAX_RESIDENT_PINS]:
            conn.execute("UPDATE impressions SET resident=0 WHERE id=?", (r["id"],))
            report["demoted"] += 1
    conn.execute("INSERT INTO dreams (ts, report) VALUES (?,?)", (now, json.dumps(report, ensure_ascii=False)))
    # 7) 维护：每 30 天物理清一次死事件。淘汰只是 alive=0，行一直在；
    #    9 万轮下来死行占九成，不清的话 db 会一直长。
    last_m = store.get_meta(conn, "last_maintain", 0) or 0
    n_hyp = conn.execute("SELECT COUNT(*) c FROM hypotheses").fetchone()["c"]
    # 时间到了就清；假说表涨过 2000 条也强制清（时间戳可能被写坏/来自未来）
    if now - float(last_m) > 30 * store.DAY or n_hyp > 2000:
        try:
            report["maintained"] = store.maintain(conn, now)
            store.set_meta(conn, "last_maintain", now)
        except Exception as exc:
            logger.warning("maintain failed: %s", exc)
    conn.commit()
    return report


# ── 半衰期压缩 ───────────────────────────────────────────────────────────────

def compress_core(conn, *, now: Optional[float] = None, ratio: float = 0.7,
                  keep_min: int = 4, owner: str = "") -> dict:
    """核心区爆满时的"半衰期"：把大部分印象交给梦做二次压缩，粒度变粗一档。

    跟普通淘汰的区别：
      · 由用户点头才发生（pm_compact 的 confirm），不是后台静默做；
      · 压缩是"合并成更少的几条判断"，不是"丢掉最弱的几条"；
      · 旧的退出常驻但不删，provenance 记着它们浓缩成了哪几条，需要时还能追回去。
    """
    now = now or time.time()
    rows = conn.execute("SELECT * FROM impressions WHERE resident=1 AND owner=?",
                        (owner or "",)).fetchall()
    n = len(rows)
    if n <= keep_min + 2:
        return {"skipped": "还不到需要压的时候", "before": n}

    rows.sort(key=lambda r: (int(r["uses"] or 0), float(r["last_used"] or r["ts"] or 0)))
    cut = max(keep_min, int(n * (1 - float(ratio))))     # 最近的这几条原样留着
    victims = rows[: n - cut]
    keep = rows[n - cut:]
    target = max(2, len(victims) // 3)

    raw = distill.compress([v["text"] for v in victims], [v["slot"] for v in victims], target)
    pairs = []          # [(text, kind, [impression_id, ...])]
    mode = "llm"
    if raw:
        for text, kind, frm in raw:
            ids = [victims[i]["id"] for i in frm if 0 <= i < len(victims)] or [v["id"] for v in victims]
            pairs.append((text, kind, ids))
    else:               # 没模型就退到规则档：每个 slot 留一条
        mode = "rules"
        seen: set[str] = set()
        for v in sorted(victims, key=lambda r: int(r["uses"] or 0), reverse=True):
            if v["slot"] in seen:
                continue
            seen.add(v["slot"])
            pairs.append((v["text"], v["slot"], [v["id"]]))
            if len(pairs) >= target:
                break

    before = n
    ids = [v["id"] for v in victims]
    for v in victims:
        conn.execute("UPDATE impressions SET resident=0 WHERE id=?", (v["id"],))
    added = 0
    for text, kind, src_ids in pairs:
        if len(src_ids) == 1:
            # 只从一条来 = 没合并，原样留着，别造一条新的
            conn.execute("UPDATE impressions SET resident=1 WHERE id=?", (src_ids[0],))
            continue
        if _has_impression(conn, text, resident_only=True, owner=owner):
            continue
        conn.execute(
            "INSERT INTO impressions (ts, text, slot, strength, uses, last_used, resident, provenance, owner)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (now, text, kind, 30.0, 0, now, 1,
             json.dumps({"from": "impressions", "ids": src_ids}, ensure_ascii=False), owner or ""))
        added += 1
    conn.commit()
    rep = {"mode": mode, "before": before, "compressed_from": len(victims),
           "compressed_to": added, "after": store.resident_count(conn, owner), "kept": len(keep)}
    conn.commit()
    return rep
