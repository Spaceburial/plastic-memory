"""store.py — 三层记忆的物理层。

核心印象表（impressions）刻意不提供任何 dump/list/export 接口：只有注入函数能读它。
这不是密码学边界（用户对自己的磁盘当然有权限），是产品边界——没有入口，就不会被翻出来看。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

DAY = 86400.0

# ── 路径 ─────────────────────────────────────────────────────────────────────

def hermes_home() -> Path:
    """Hermes 的 home；不在 Hermes 里运行时返回通用默认目录。"""
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    la = os.environ.get("LOCALAPPDATA")
    if la and (Path(la) / "hermes").exists():
        return Path(la) / "hermes"
    return Path.home() / ".hermes"


def data_dir() -> Path:
    """数据目录，按优先级：
    1. $PLASTIC_MEMORY_HOME（任何宿主都可以指定）
    2. Hermes 的 plugin-data（在 Hermes 里跑时）
    3. ~/.plastic-memory（独立运行 / MCP / CLI）
    """
    env = os.environ.get("PLASTIC_MEMORY_HOME")
    if env:
        d = Path(env)
        d.mkdir(parents=True, exist_ok=True)
        return d
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        d = Path(get_hermes_home()) / "plugin-data" / "plastic-memory"
    except Exception:
        d = Path.home() / ".plastic-memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 分类 ─────────────────────────────────────────────────────────────────────

# 顺序即优先级：身份 > 承诺 > 边界 > 情绪 > 偏好 > 事实 > 杂项
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("promise",  ("答应", "承诺", "说好", "约好", "下次", "以后", "回头", "欠你")),
    ("boundary", ("不要", "别", "不准", "禁止", "不能", "必须", "一定要", "只能", "别再")),
    ("emotion",  ("累", "烦", "难受", "压力", "焦虑", "崩溃", "睡不着", "开心", "郁闷", "慌")),
    ("pref",     ("喜欢", "讨厌", "受不了", "习惯", "偏爱", "更喜欢", "不想", "看不惯")),
    ("fact",     ("版本", "安装", "路径", "端口", "服务器", "模型", "部署", "磁盘", "项目", "接口")),
    # 生活流：私人助手每天听到的其实大多是这一类，不能全归到 misc 里等它过期
    ("life",     ("跳槽", "简历", "面试", "工资", "涨薪", "老板", "领导", "同事", "加班", "下班",
                  "我妈", "我爸", "家里", "房租", "搬家", "生病", "医院", "猫", "狗", "年糕",
                  "失眠", "睡不着", "请假", "裁员", "离职", "入职")),
]

# "我叫" 后面必须跟一个像名字的词——否则「我叫丫头去取斗篷」也会被当成身份。
_FAKE_NAMES = ("丫头", "人", "他", "你", "她", "车", "马", "嬷嬷", "小厮", "婆子",
               "媳妇", "他们", "你们", "她们", "人去", "人来")
_NAME_RE = re.compile(r"(?:我叫|叫我|我的名字叫?)([\u4e00-\u9fff]{2,3})")

_SALIENCE = {"identity": 1.9, "promise": 1.8, "boundary": 1.7, "pref": 1.6,
             "emotion": 1.2, "life": 1.05, "fact": 0.9, "misc": 0.45}

_HALF_LIFE = {"identity": 120.0, "promise": 365.0, "boundary": 365.0,
              "pref": 30.0, "emotion": 7.0, "life": 30.0, "fact": 14.0, "misc": 5.0}

_LATIN_OR_DIGIT = re.compile(r"[A-Za-z0-9]")

# 一辈子可能只说一次、说错了代价很大的事——这类不该被半衰期洗掉
CRITICAL_WORDS = ("结婚", "离婚", "怀孕", "生了", "去世", "走了", "葬礼", "手术", "住院",
                  "癌症", "抑郁", "确诊", "过敏", "辞职", "被裁", "买房", "贷款", "欠",
                  "借", "还钱", "签合同", "违约", "官司", "报警", "报案", "搬走", "分手")


def is_critical(text: str) -> bool:
    return any(k in (text or "") for k in CRITICAL_WORDS)



# ── 语义标签 ────────────────────────────────────────────────────────────────
# bigram 只能匹配字面，"睡不好"和"失眠"永远碰不上。这里用一组手工维护的
# 同义/近义簇做一阶语义：命中簇里任意一个词，就打上整个簇的标签。
# 检索时标签交集直接加分，比字符相似度可靠，而且可解释、零成本。
TAG_GROUPS: dict[str, tuple[str, ...]] = {
    "job":       ("上班", "下班", "加班", "工作", "公司", "单位", "职场", "打工", "老板", "领导", "同事"),
    "quit":      ("跳槽", "换工作", "离职", "裸辞", "辞职", "找下家", "跑路"),
    "interview": ("面试", "笔试", "offer", "终面", "hr", "简历"),
    "money":     ("工资", "涨薪", "薪水", "收入", "待遇", "年终奖", "裁员", "赔偿", "穷"),
    "tired":     ("累", "烦", "难受", "郁闷", "emo", "低落", "不开心", "心累", "撑不住", "没劲"),
    "anxious":   ("焦虑", "慌", "紧张", "压力", "担心", "怕", "内耗", "崩溃"),
    "sleep":     ("睡", "失眠", "熬夜", "困", "早睡", "几点", "醒"),
    "family":    ("妈", "爸", "家里", "父母", "老家", "催婚", "考公", "亲戚"),
    "pet":       ("猫", "狗", "年糕", "主子", "宠物"),
    "health":    ("生病", "医院", "感冒", "发烧", "体检", "身体", "吃药"),
    "travel":    ("旅行", "旅游", "西藏", "新疆", "出去玩", "假期", "机票", "出发"),
    "food":      ("吃", "饭", "火锅", "面", "外卖", "饿", "做饭", "夜宵"),
    "tech":      ("接口", "部署", "报错", "代码", "数据库", "服务器", "模型", "索引", "性能", "bug", "测试"),
    "self":      ("我是不是", "我不行", "我废", "我没用", "我活该"),
    "pref":      ("喜欢", "讨厌", "受不了", "习惯", "偏爱", "不想", "看不惯"),
}


# 上位标签：让"跳槽"也能被"工作"这个更大的域召回
TAG_PARENTS: dict[str, tuple[str, ...]] = {
    "quit": ("job",), "interview": ("job", "money"), "money": ("job",),
    "sleep": ("health",), "pet": ("health",), "anxious": ("tired",),
}


def tags_of(text: str) -> list[str]:
    t = text or ""
    hit = [g for g, words in TAG_GROUPS.items() if any(w in t for w in words)]
    out = list(hit)
    for g in hit:
        for parent in TAG_PARENTS.get(g, ()):
            if parent not in out:
                out.append(parent)
    return out


def _tags_json(text: str) -> str:
    import json as _j
    return _j.dumps(tags_of(text), ensure_ascii=False)

def classify(text: str) -> str:
    m = _NAME_RE.search(text)
    if m and "不叫我" not in text and not any(m.group(1).startswith(f) for f in _FAKE_NAMES):
        return "identity"
    if any(k in text for k in ("我是做", "我住", "我老家", "我今年", "我在郑州")):
        return "identity"
    for kind, keys in _RULES:
        if any(k in text for k in keys):
            return kind
    return "misc"


def is_junk(text: str, kind: str) -> bool:
    """杂乱信息区：短寒暄、无实体、无信息量。判据尽量窄，宁可多留一条也别误杀。"""
    if kind != "misc":
        return False
    t = text.strip()
    if len(t) > 10:
        return False
    if _LATIN_OR_DIGIT.search(t):
        return False
    if any(k in t for k in ("？", "什么", "怎么", "为什么")):
        return False
    return True


@lru_cache(maxsize=8192)
def _grams(s: str) -> frozenset[str]:
    """把字符串切成 gram 集合。缓存是必要的：dream 里同一段话会被反复拿去比，
    一次梦里 _grams 要跑一万多次，缓存能把这些重复切分全免掉。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s.lower())
    if len(s) < 2:
        return frozenset((s,)) if s else frozenset()
    return frozenset({s[i:i + 2] for i in range(len(s) - 1)})


def similarity(a: str, b: str) -> float:
    A, B = _grams(a), _grams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def coverage(query: str, text: str) -> float:
    """query 的 gram 有多少落进了 text。用于召回打分——短 query 对长文本用 Jaccard 会被稀释成 0。"""
    q, t = _grams(query), _grams(text)
    if not q or not t:
        return 0.0
    return len(q & t) / len(q)


def _negated(text: str) -> bool:
    return any(k in text for k in ("不", "别", "没", "取消", "算了", "放弃"))


# ── 连接 ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, session_id TEXT, role TEXT,
    text TEXT NOT NULL, kind TEXT NOT NULL,
    salience REAL NOT NULL, uses INTEGER DEFAULT 0,
    last_used REAL, half_life REAL, alive INTEGER DEFAULT 1,
    merged_into INTEGER
);
CREATE INDEX IF NOT EXISTS ix_events_live ON events(alive, kind);

CREATE TABLE IF NOT EXISTS impressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, text TEXT NOT NULL, slot TEXT,
    strength REAL NOT NULL, uses INTEGER DEFAULT 0,
    last_used REAL, resident INTEGER DEFAULT 1,
    provenance TEXT
);
CREATE INDEX IF NOT EXISTS ix_imp_resident ON impressions(resident);

CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, text TEXT NOT NULL, targets TEXT,
    status TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS dropped (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, kind TEXT, reason TEXT, digest TEXT
);

CREATE TABLE IF NOT EXISTS dreams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, report TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY, v TEXT
);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = path or (data_dir() / "plastic.db")
    conn = sqlite3.connect(str(p), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    # 每轮对话都要写一次：默认的 rollback journal + FULL 同步在 Windows 上
    # 一次 commit 要 9ms，占掉 observe 的 90%。WAL + NORMAL 把 fsync 摊到检查点。
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    conn.executescript(_SCHEMA)
    for table in ("events", "impressions"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN tags TEXT")
        except Exception:
            pass
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN last_shown REAL")
        except Exception:
            pass
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN pinned INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN owner TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN expires_at REAL")
        except Exception:
            pass  # 列已存在
    # dropped 去重计数：原来每句寒暄插一行，60 天 2400 条输入里 dropped 涨到 841 行，
    # 其中 836 行是同一个 digest。"只留一个哈希计数"的设计没落地。
    try:
        conn.execute("ALTER TABLE dropped ADD COLUMN count INTEGER DEFAULT 1")
    except Exception:
        pass
    # 假说原来没有 owner：两个人的假说混在一张表里，去重还会互相压制
    try:
        conn.execute("ALTER TABLE hypotheses ADD COLUMN owner TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        dups = conn.execute(
            "SELECT digest, COUNT(*) c, MIN(id) keep FROM dropped"
            " WHERE digest IS NOT NULL GROUP BY digest HAVING c > 1").fetchall()
        for d in dups:
            conn.execute("UPDATE dropped SET count=? WHERE id=?", (int(d["c"]), int(d["keep"])))
            conn.execute("DELETE FROM dropped WHERE digest=? AND id<>?", (d["digest"], int(d["keep"])))
        if dups:
            conn.commit()
    except Exception:
        pass
    conn.commit()
    return conn


# ── 衰减 ─────────────────────────────────────────────────────────────────────

def strength_of(row: Any) -> float:
    """稳定性（天）：用得越多越抗遗忘。"""
    return float(row["half_life"] or 10.0) * (1.0 + 0.40 * int(row["uses"] or 0)) * float(row["salience"] or 1.0)


def retention(row: Any, now: float) -> float:
    """还要留多久（1.0 = 新鲜，0 = 该扔了）。

    带 expires_at 且还没到期的，不参与衰减——"下周去杭州出差三天"这种
    本来就是有生命周期的事，它不需要靠 pin 才活得过五天。
    """
    exp = None
    try:
        exp = row["expires_at"]
    except (IndexError, KeyError):
        pass
    if exp and float(exp) > now:
        return 1.0
    last = float(row["last_used"] or row["ts"] or 0.0)
    s = strength_of(row) * DAY
    if s <= 0:
        return 0.0
    return math.exp(-max(0.0, now - last) / s)


def imp_strength(row: Any) -> float:
    return float(row["strength"] or 20.0) * (1.0 + 0.35 * int(row["uses"] or 0))


def imp_retention(row: Any, now: float) -> float:
    last = float(row["last_used"] or row["ts"] or 0.0)
    s = imp_strength(row) * DAY
    return math.exp(-max(0.0, now - last) / s) if s > 0 else 0.0




# ── 时间解析 ────────────────────────────────────────────────────────────────
# "我下周要去杭州" 这句话在下周之后就该失效，但半衰期只管"多久淡出"，不管"什么时候已经过期"。
# 这里做一次轻量解析：只认中文里最常见的相对时间，认不出来就不设过期。
import datetime as _dt

_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def parse_expiry(text: str, now: float) -> float | None:
    """从一句话里猜出"这件事什么时候就过去了"。猜不出返回 None。"""
    t = text or ""
    d = _dt.datetime.fromtimestamp(now)
    for kw, days in (("大后天", 4), ("后天", 3), ("明天", 2), ("今天", 1)):
        if kw in t:
            return now + days * DAY
    m = re.search(r"(\d+)\s*天后", t)
    if m:
        return now + (int(m.group(1)) + 1) * DAY
    m = re.search(r"(下周|下星期|这周|本周|周|星期)([一二三四五六日天])", t)
    if m:
        target = _WEEKDAY[m.group(2)]
        if m.group(1) in ("下周", "下星期"):
            # 下周 = 下一个自然周，从下周一起算
            to_monday = (7 - d.weekday()) % 7 or 7
            delta = to_monday + target
        else:
            delta = (target - d.weekday()) % 7 or 7
        return now + (delta + 1) * DAY
    if "下周" in t or "下星期" in t:
        return now + 8 * DAY
    if "下个月" in t or "下月" in t:
        return now + 31 * DAY
    if "月底" in t:
        last = (d.replace(day=28) + _dt.timedelta(days=4)).replace(day=1) - _dt.timedelta(days=1)
        return last.timestamp() + DAY
    if "年底" in t:
        return _dt.datetime(d.year, 12, 31).timestamp() + DAY
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", t)
    if m:
        y = d.year
        try:
            target = _dt.datetime(y, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        if target.timestamp() < now:
            target = target.replace(year=y + 1)
        return target.timestamp() + DAY
    return None


# ── 写入 ─────────────────────────────────────────────────────────────────────

def observe(conn: sqlite3.Connection, text: str, *, role: str = "user",
            session_id: str = "", kind: Optional[str] = None,
            salience: Optional[float] = None, now: Optional[float] = None,
            owner: str = "") -> dict:
    """写入一个事件。返回 {stored, reason, kind, id}。

    - 重复（同 kind、高相似）：不新建，只把命中次数与时间推给旧条目 —— 重复是强度信号。
    - 杂乱信息：不落盘，只在 dropped 表留一个哈希计数。
    """
    now = now or time.time()
    text = (text or "").strip()
    if not text:
        return {"stored": False, "reason": "empty"}
    if len(text) > 1200:
        text = text[:1200]

    kind = kind or classify(text)
    sal = float(salience if salience is not None else _SALIENCE.get(kind, 0.45))
    if is_critical(text):
        sal = max(sal, 2.4)
        if kind == "misc":
            kind = "life"   # 婚丧嫁娶这类不一定命中关键词，不能让它们沉在 misc 里过期

    if is_junk(text, kind):
        dg = hashlib.sha1(text.encode()).hexdigest()[:12]
        row = conn.execute("SELECT id, count FROM dropped WHERE digest=?", (dg,)).fetchone()
        if row:
            # 同一句杂讯只留一行，重复次数记在 count 里 —— 不落盘，但要知道它来过几次
            conn.execute("UPDATE dropped SET count=?, ts=? WHERE id=?",
                         (int(row["count"] or 1) + 1, now, row["id"]))
        else:
            conn.execute("INSERT INTO dropped (ts, kind, reason, digest, count)"
                         " VALUES (?,?,?,?,1)", (now, kind, "junk", dg))
        conn.commit()
        return {"stored": False, "reason": "junk", "kind": kind}

    # 去重：只看近 30 天同 kind 的存活条目
    rows = conn.execute(
        "SELECT * FROM events WHERE alive=1 AND kind=? AND ts > ? AND owner=?"
        " ORDER BY ts DESC LIMIT 60",
        (kind, now - 30 * DAY, owner or "")).fetchall()
    for r in rows:
        if similarity(text, r["text"]) >= 0.52:
            conn.execute("UPDATE events SET uses=uses+1, last_used=?, salience=MIN(2.5, salience+0.06) WHERE id=?",
                         (now, r["id"]))
            conn.commit()
            return {"stored": False, "reason": "repeat", "kind": kind, "id": r["id"]}

    cur = conn.execute(
        "INSERT INTO events (ts, session_id, role, text, kind, salience, uses, last_used,"
        " half_life, tags, owner, expires_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, session_id, role, text, kind, sal, 0, now, _HALF_LIFE.get(kind, 2.0),
         _tags_json(text), owner or "", parse_expiry(text, now)))
    # 用户又提了一次 = 真正的强化。注入不算，否则常驻会被最早的几条永久占住。
    for imp in conn.execute("SELECT id, text FROM impressions WHERE resident=1 AND owner=?",
                            (owner or "",)).fetchall():
        if similarity(text, imp["text"]) >= 0.5:
            conn.execute("UPDATE impressions SET uses=uses+1, last_used=? WHERE id=?", (now, imp["id"]))
            break
    conn.commit()   # 一次写轮只提交一次

    changed = detect_change(conn, text, now)
    return {"stored": True, "reason": "new", "kind": kind, "id": cur.lastrowid, "change": changed}



# 变化检测用的极性词：中文里"改主意"通常表现为 想/不打算 的翻转
_WANT = ("想", "打算", "准备", "计划", "要")
_UNWANT = ("不想", "不打算", "不准备", "没想过", "不会", "不跳", "不换")
_STOP_GRAMS = {"不是", "就是", "现在", "这个", "那个", "我的", "你的", "我们", "他们",
               "一个", "什么", "怎么", "因为", "所以", "但是", "可以", "没有", "已经",
               "还是", "如果", "然后", "自己", "时候", "真的", "有点", "觉得"}


def _polarity(t: str) -> int:
    if any(k in t for k in _UNWANT):
        return -1
    if any(k in t for k in _WANT):
        return 1
    return 0

def detect_change(conn: sqlite3.Connection, text: str, now: Optional[float] = None,
                  owner: str = "") -> dict | None:
    """他这次说的，跟以前记住的某条是不是拧着的。

    半衰期里人会变：三个月前说"我不跳槽"，今天说"我想走了"——这不是记错，
    是他变了。检测到了不是马上改，先标记，等下一次梦优先处理，并且提醒模型
    "按他现在的说法，别拿旧印象压他"。
    先比核心印象；不够就比近 90 天的事件——态度类的话常常还没升格。
    """
    now = now or time.time()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, text, slot, uses FROM impressions WHERE resident=1 AND owner=?",
        (owner or "",)).fetchall()]
    src = "impression"
    if not rows:
        rows = [dict(r) | {"slot": r["kind"]} for r in conn.execute(
            "SELECT id, text, kind, uses FROM events WHERE alive=1 AND ts > ? AND owner=?",
            (now - 90 * DAY, owner or "")).fetchall()]
        src = "event"
    pa = _polarity(text)
    for r in rows:
        sim = similarity(text, r["text"])
        if 0.28 <= sim <= 0.78 and _negated(text) != _negated(r["text"]):
            return {"old_id": r["id"], "old": r["text"], "slot": r["slot"],
                    "sim": round(sim, 2), "from": src}
        # 字面差得远但态度反了：共享实词 + 想/不想 翻转 —— 这才是"他改主意了"
        if pa:
            pb = _polarity(r["text"])
            common = (_grams(text) & _grams(r["text"])) - _STOP_GRAMS
            if pb and pb != pa and common:
                return {"old_id": r["id"], "old": r["text"], "slot": r["slot"],
                        "sim": round(sim, 2), "from": src, "flip": sorted(common)[:3]}
    return None


# ── 检索 ─────────────────────────────────────────────────────────────────────

def recall_events(conn: sqlite3.Connection, query: str, k: int = 6,
                  now: Optional[float] = None, min_ret: float = 0.02,
                  role: Optional[str] = None, owner: str = "") -> list[dict]:
    now = now or time.time()
    rows = conn.execute(
        "SELECT * FROM events WHERE alive=1 AND owner=? "
        "AND (expires_at IS NULL OR expires_at > ?)",
        (owner or "", now)).fetchall()
    scored = []
    for r in rows:
        if role and r["role"] != role:
            continue
        ret = retention(r, now)
        if ret < min_ret:
            continue
        sim = coverage(query, r["text"]) if query else 0.0
        q_tags = set(tags_of(query)) if query else set()
        e_tags = set(json.loads(r["tags"] or "[]")) if r["tags"] else set()
        tag_hit = len(q_tags & e_tags)
        score = (sim * 3.0 + tag_hit * 1.1 + ret * 1.4
                 + float(r["salience"]) * 0.35 + min(int(r["uses"]), 5) * 0.15)
        if query and sim == 0.0:
            score *= 0.45  # 跟这轮无关的旧事别霸榜
        if q_tags and not tag_hit and sim < 0.12:
            continue  # 查询有明确语义域，这条一个字都没沾上——宁可不给，也别硬凑
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:k]:
        out.append({"id": r["id"], "text": r["text"], "kind": r["kind"],
                    "ts": r["ts"], "uses": r["uses"], "score": round(score, 3)})
        conn.execute("UPDATE events SET last_used=?, uses=uses+1 WHERE id=?", (now, r["id"]))
    conn.commit()
    return out


def maintain(conn: sqlite3.Connection, now: Optional[float] = None,
             keep_days: int = 180) -> dict:
    """物理清理。

    事件被淘汰只是 alive=0，行还留着（追溯要用）。但三个月下来死行能占九成：
    9 万轮对话里 3 万行是死的。这里删掉**超过 keep_days 且没有任何印象引用**的，
    再 VACUUM 把文件收紧。还在被引用的死事件一律不动——那是追溯链的一部分。
    """
    now = now or time.time()
    cut = now - keep_days * DAY
    referenced: set[int] = set()
    for r in conn.execute("SELECT provenance FROM impressions WHERE provenance IS NOT NULL"):
        try:
            p = json.loads(r["provenance"] or "{}")
            referenced.update(int(x) for x in (p.get("ids") if isinstance(p, dict) else p) or [])
        except Exception:
            continue
    dead = conn.execute("SELECT id FROM events WHERE alive=0 AND ts < ?", (cut,)).fetchall()
    victims = [int(r["id"]) for r in dead if int(r["id"]) not in referenced]
    for i in victims:
        conn.execute("DELETE FROM events WHERE id=?", (i,))
    # 假说只留最近 500 条（confirmed/stale 都是档案，不参与判断）
    conn.execute("DELETE FROM hypotheses WHERE status<>'open' AND id NOT IN "
                 "(SELECT id FROM hypotheses WHERE status<>'open' ORDER BY id DESC LIMIT 500)")
    conn.commit()
    p = conn.execute("PRAGMA database_list").fetchone()[2]
    before = os.path.getsize(p) if p and os.path.exists(p) else 0
    try:
        conn.execute("VACUUM")
    except Exception:
        pass
    after = os.path.getsize(p) if p and os.path.exists(p) else 0
    return {"deleted_events": len(victims), "kept_referenced": len(dead) - len(victims),
            "kb_before": round(before / 1024), "kb_after": round(after / 1024)}


def promoted_event_ids(conn: sqlite3.Connection, owner: str = "") -> set[int]:
    """已经被提升成印象的事件 id。注入时据此跳过原话——蒸馏后的印象跟原话字面差异大，
    相似度去重不可靠，provenance 才是精确的。"""
    out: set[int] = set()
    for r in conn.execute("SELECT provenance FROM impressions WHERE provenance IS NOT NULL AND owner=?",
                          (owner or "",)):
        try:
            p = json.loads(r["provenance"] or "{}")
            # provenance 是 {"from": "events", "ids": [...]}，直接遍历 dict 会拿到键名
            ids = p.get("ids") if isinstance(p, dict) else p
            for i in ids or []:
                out.add(int(i))
        except Exception:
            continue
    return out


PIN_SLOTS = ("identity", "promise", "boundary")


def core_impressions(conn: sqlite3.Connection, query: str = "", limit: int = 12,
                     now: Optional[float] = None, pin_budget: int = 3,
                     owner: str = "") -> list[dict]:
    """核心区的唯一读取入口。只返回给注入函数，不注册成工具。

    分层取，不按时间衰减排序：
      1. 常驻（身份/约定/边界）占前 pin_budget 个槽位 —— 这些是"这个人是谁、什么不能碰"，
         不该跟新印象抢位置。
      2. 剩下的槽位按"跟这句话的相关度 + 被用过几次"排，给新印象留门。
    注入即强化（uses+1），所以常驻会越来越稳；错误的常驻只能由梦里的矛盾检测降权。
    """
    now = now or time.time()
    rows = conn.execute(
        "SELECT * FROM impressions WHERE resident=1 AND owner=? "
        "AND (expires_at IS NULL OR expires_at > ? OR pinned=1)",
        (owner or "", now)).fetchall()
    if not rows:
        return []

    pins, others = [], []
    for r in rows:
        sim = coverage(query, r["text"]) if query else 0.0
        age = now - float(r["last_used"] or r["ts"] or now)
        recency = max(0.0, 1.0 - age / (60 * DAY))
        if r["slot"] in PIN_SLOTS or int(r["pinned"] or 0):
            # 身份最稳；边界/约定随"最近有没有被重提"轮换，不让最早那几条永久霸占
            bonus = 1.6 if r["slot"] == "identity" else 0.0
            # 新面孔加成：刚进核心区 7 天内、还没被重提过的，先让它露一次脸
            fresh = 0.9 if (now - float(r["ts"] or now)) < 7 * DAY and int(r["uses"] or 0) <= 1 else 0.0
            pin_bonus = 10.0 if int(r["pinned"] or 0) else 0.0     # 钉住的不参与轮换
            pins.append((pin_bonus + bonus + fresh + sim * 1.8 + recency * 1.6 + int(r["uses"] or 0) * 0.06, sim, r))
        else:
            others.append((sim * 2.5 + min(int(r["uses"] or 0), 8) * 0.35 + recency * 0.3, sim, r))

    pins.sort(key=lambda x: x[0], reverse=True)
    others.sort(key=lambda x: x[0], reverse=True)

    picked = [p[2] for p in pins[:pin_budget]]
    picked += [o[2] for o in others[: max(0, limit - len(picked))]]
    if len(picked) < limit:                      # 常驻不够，用剩下的 pin 补满
        picked += [p[2] for p in pins[pin_budget:pin_budget + (limit - len(picked))]]

    out = []
    for r in picked[:limit]:
        # 只记"什么时候被展示过"，不动 uses——注入不是强化
        conn.execute("UPDATE impressions SET last_shown=? WHERE id=?", (now, r["id"]))
        out.append({"id": r["id"], "text": r["text"], "slot": r["slot"],
                    "retention": round(imp_retention(r, now), 2), "uses": int(r["uses"] or 0)})
    conn.commit()
    return out




# ── 追溯 ────────────────────────────────────────────────────────────────────
def _prov(row) -> dict:
    raw = row["provenance"] if "provenance" in row.keys() else None
    try:
        d = json.loads(raw or "[]")
    except Exception:
        return {"from": "events", "ids": []}
    if isinstance(d, list):          # 老格式：直接是事件 id 数组
        return {"from": "events", "ids": [int(x) for x in d if str(x).isdigit()]}
    return {"from": d.get("from", "events"), "ids": [int(x) for x in d.get("ids", [])]}


def trace_impression(conn: sqlite3.Connection, imp_id: int, depth: int = 0) -> dict | None:
    """一条印象是从哪儿长出来的。压缩过的能一路追到原话。"""
    row = conn.execute("SELECT * FROM impressions WHERE id=?", (imp_id,)).fetchone()
    if not row:
        return None
    prov = _prov(row)
    node = {"id": row["id"], "text": row["text"], "slot": row["slot"],
            "ts": row["ts"], "resident": row["resident"], "from": prov["from"], "children": []}
    if prov["from"] == "events":
        for eid in prov["ids"]:
            e = conn.execute("SELECT id, ts, text, kind, alive, merged_into FROM events WHERE id=?",
                             (eid,)).fetchone()
            if e:
                node["children"].append({"type": "event", "id": e["id"], "ts": e["ts"],
                                         "kind": e["kind"], "text": e["text"],
                                         "alive": e["alive"], "merged_into": e["merged_into"]})
    elif depth < 2:
        for iid in prov["ids"]:
            child = trace_impression(conn, iid, depth + 1)
            if child:
                node["children"].append({"type": "impression", **child})
    return node


def find_impression(conn: sqlite3.Connection, text: str = "", imp_id: int = 0,
                    owner: str = "") -> dict | None:
    if imp_id:
        return conn.execute("SELECT * FROM impressions WHERE id=?", (imp_id,)).fetchone()
    rows = conn.execute("SELECT * FROM impressions WHERE owner=?", (owner or "",)).fetchall()
    if not rows:
        return None
    if not text:
        return max(rows, key=lambda r: similarity("", r["text"]))
    # 单字/双字 query 走子串匹配。_grams 对单字返回的是单字集合，而长文本的 gram
    # 是双字，两边交集永远是空 —— 直接用 coverage 的话，"猫""钱""病"这类查询
    # 会一条都找不到，pm_pin / pm_trace 等于对短词失效。
    q = re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower())
    if len(q) <= 2:
        for r in rows:
            if q and q in re.sub(r"[^\w\u4e00-\u9fff]+", "", str(r["text"]).lower()):
                return r
        return None
    best = max(rows, key=lambda r: coverage(text, r["text"]))
    return best if coverage(text, best["text"]) >= 0.6 else None




# ── 纠正 ────────────────────────────────────────────────────────────────────
# "你记错了" 不是一条新记忆，是对旧记忆的否决。以前它会被当普通事件存下来，
# 而错的那条还留在核心区里，下次继续说错。
CORRECT_WORDS = ("你记错", "记错了", "记反了", "不是这样", "我没说过", "我什么时候说过",
                 "搞错", "记的不对", "记错了", "谁跟你说", "我没这么讲", "别瞎编", "不是这个意思")


def is_correction(text: str) -> bool:
    return any(k in (text or "") for k in CORRECT_WORDS)


def find_corrected(conn: sqlite3.Connection, text: str, recent_ids: list[int],
                   owner: str = "") -> dict | None:
    """在他刚才听到的那几条里，找最可能被纠正的那条。找不到就返回 None（不要乱删）。"""
    if not recent_ids:
        return None
    best, best_score = None, 0.0
    q_tags = set(tags_of(text))
    for iid in recent_ids:
        row = conn.execute("SELECT * FROM impressions WHERE id=?", (iid,)).fetchone()
        if not row:
            continue
        # 代词换掉再比：他说"我不是做后端的"，记的是"他做后端开发"，字面只差代词
        a = re.sub(r"[他我你]", "", text)
        b = re.sub(r"[他我你]", "", row["text"])
        common = (_grams(a) & _grams(b)) - _STOP_GRAMS
        sim = similarity(a, b)
        try:
            e_tags = set(json.loads(row["tags"] or "[]"))
        except Exception:
            e_tags = set()
        score = sim + len(common) * 0.06 + len(q_tags & e_tags) * 0.45
        if score > best_score:
            best, best_score = row, score
    return best if best_score >= 0.12 else None


# ── 统计 ─────────────────────────────────────────────────────────────────────

def resident_count(conn: sqlite3.Connection, owner: str = "") -> int:
    return conn.execute("SELECT COUNT(*) c FROM impressions WHERE resident=1 AND owner=?",
                        (owner or "",)).fetchone()["c"]


def stats(conn: sqlite3.Connection, now: Optional[float] = None, owner: str = "") -> dict:
    now = now or time.time()
    ev = conn.execute("SELECT COUNT(*) c FROM events WHERE alive=1 AND owner=?",
                      (owner or "",)).fetchone()["c"]
    dead = conn.execute("SELECT COUNT(*) c FROM events WHERE alive=0").fetchone()["c"]
    imp = conn.execute("SELECT COUNT(*) c FROM impressions WHERE owner=?",
                       (owner or "",)).fetchone()["c"]
    res = conn.execute("SELECT COUNT(*) c FROM impressions WHERE resident=1 AND owner=?",
                       (owner or "",)).fetchone()["c"]
    drop = conn.execute("SELECT COALESCE(SUM(count), 0) c FROM dropped").fetchone()["c"]
    hyp = conn.execute("SELECT COUNT(*) c FROM hypotheses WHERE status='open' AND owner=?",
                       (owner or "",)).fetchone()["c"]
    dreams = conn.execute("SELECT COUNT(*) c FROM dreams").fetchone()["c"]
    return {"events_alive": ev, "events_forgotten": dead, "impressions": imp,
            "impressions_resident": res, "junk_blocked": drop,
            "hypotheses_open": hyp, "dreams": dreams}


def recent_events(conn: sqlite3.Connection, limit: int = 20, owner: str = "") -> list[dict]:
    rows = conn.execute("SELECT id, ts, text, kind, uses FROM events WHERE alive=1 AND owner=?"
                        " ORDER BY ts DESC LIMIT ?", (owner or "", limit)).fetchall()
    return [dict(r) for r in rows]


# ── 元数据 ───────────────────────────────────────────────────────────────────

def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except Exception:
        return row["v"]


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO meta (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


def unconsolidated_count(conn: sqlite3.Connection, since: float, owner: str = "") -> int:
    """上次做梦之后新加进来的、还没被巩固的事件数。"""
    return conn.execute("SELECT COUNT(*) c FROM events WHERE alive=1 AND ts > ? AND owner=?",
                        (since, owner or "")).fetchone()["c"]
