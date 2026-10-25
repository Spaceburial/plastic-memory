"""stress90k.py — 三个月、每天 1000 句的高强度压测。

    python stress90k.py

跑之前不需要配模型：梦会退回规则档（纯规则蒸馏），速度可控。
结果写到 <temp>/stress90k.json，同时打印进度表。
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

HERE = str(Path(__file__).resolve().parent)
sys.path.insert(0, HERE)

import plastic_memory.store as store          # noqa: E402
import plastic_memory.dream as dream          # noqa: E402
import plastic_memory.persona as persona      # noqa: E402
import plastic_memory.engine as engine        # noqa: E402

random.seed(20261025)

DAYS = 90
PER_DAY = 1000
DAY = store.DAY
T0 = 1_760_000_000.0

MODULES = ["简历解析", "岗位匹配", "用户登录", "文件上传", "向量检索", "前端路由",
           "支付回调", "消息推送", "权限校验", "导出报表"]
ACTS = ["报 500", "超时", "返回空", "内存涨了", "白屏", "卡在 loading", "排序乱了",
        "多了个空行", "日志刷屏", "重启就好"]
ENVS = ["测试环境", "手机上", "打包后", "线上偶发", "高并发下", "换台机器", "清缓存以后"]
EMOS = ["累", "烦", "焦虑", "有点崩", "还行", "郁闷", "心累", "睡不着"]
LEVELS = ["有点", "真的", "特别", "又", "还是"]
LIFE = ["今天加班到十一点", "年糕又吐了", "我妈打电话催考公", "这周第三次加班",
        "想跳槽了", "面试约在下周", "涨薪谈下来了", "失眠到两点", "周末躺了一天",
        "自己做了顿饭", "把屋子收拾了一遍", "看了半集剧就困了", "骑车通勤了",
        "中午没吃饭", "买了杯咖啡", "跟同事吵了一架", "领导临时改了需求"]
PREFS = ["我不喜欢反复确认", "我喜欢先看结论", "别给我列一堆选项", "我讨厌写长文档",
         "我不吃香菜", "我习惯晚上干活", "别在周末推消息", "我讨厌开会"]
TECH = ["rust 的所有权", "python 的 gil", "sql 的索引", "docker 的层缓存", "http 的缓存",
        "tcp 的拥塞控制", "git 的 rebase", "redis 的过期策略"]

# 埋点：不同天埋不同的东西，看三个月后还剩多少
PROBES = [
    (1,  "我的工号是 A7391",                    "工号"),
    (15, "我下周要去杭州出差三天",               "出差"),
    (30, "我妈上个月住院了，做了个小手术",        "住院"),
    (45, "我把猫的名字改成煤球了",               "煤球"),
    (60, "我不吃香菜，闻到就想吐",               "香菜"),
    (75, "我答应 leader 下周五前把接口文档交了",  "文档"),
]
probe_by_day = {d: (t, k) for d, t, k in PROBES}
probe_day = {k: d for d, _t, k in PROBES}


def gen() -> str:
    r = random.random()
    if r < .45:
        return f"{random.choice(MODULES)}{random.choice(ACTS)}，{random.choice(ENVS)}"
    if r < .65:
        return random.choice(LIFE) + random.choice(["", "了", "，烦", "，唉", "，真的"])
    if r < .78:
        return f"今天{random.choice(LEVELS)}{random.choice(EMOS)}"
    if r < .88:
        return random.choice(PREFS)
    if r < .95:
        return f"{random.choice(TECH)}这块怎么弄"
    return random.choice(["嗯", "好", "哈哈", "收到", "在吗", "睡了"])


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def main() -> None:
    conn = store.connect(Path(tempfile.mkdtemp(prefix="pm90k-")) / "long.db")
    spec = persona.load_spec()
    rows = []
    probe_hits = {k: [] for _d, _t, k in PROBES}
    obs_times, dream_times = [], []
    compactions = 0
    t_start = time.time()

    for d in range(1, DAYS + 1):
        now = T0 + d * DAY + 20 * 3600
        if d in probe_by_day:
            store.observe(conn, probe_by_day[d][0], role="user", now=now)

        ta = time.perf_counter()
        for _ in range(PER_DAY):
            store.observe(conn, gen(), role="user", now=now)
        obs_times.append((time.perf_counter() - ta) * 1000)

        tb = time.perf_counter()
        dream.dream(conn, now=now, bias=persona.memory_bias(spec, "加班 累"), history_n=20)
        dream_times.append((time.perf_counter() - tb) * 1000)

        if store.resident_count(conn) >= 16:
            dream.compress_core(conn, now=now)
            compactions += 1

        for _d, _t, key in PROBES:
            hits = store.recall_events(conn, key, k=3, now=now)
            ok = any(key in h["text"] for h in hits)
            probe_hits[key].append((d, ok))

        if d % 5 == 0 or d == 1:
            st = store.stats(conn, now)
            blk = engine.build_block(conn, "今天好累", now=now) or ""
            rows.append({
                "d": d,
                "obs_ms": round(sum(obs_times[-5:]) / max(1, len(obs_times[-5:])), 1),
                "dream_ms": round(sum(dream_times[-5:]) / max(1, len(dream_times[-5:])), 1),
                "alive": st["events_alive"], "forgot": st["events_forgotten"],
                "imp": st["impressions"], "res": st["impressions_resident"],
                "junk": st["junk_blocked"], "hyp": st["hypotheses_open"],
                "blk": len(blk),
                "db_kb": round(os.path.getsize(
                    conn.execute("PRAGMA database_list").fetchone()[2]) / 1024),
            })
            print(f"  第 {d:>3} 天  obs {rows[-1]['obs_ms']:>6}ms/1000句  "
                  f"dream {rows[-1]['dream_ms']:>5}ms  存活 {rows[-1]['alive']:>4}  "
                  f"遗忘 {rows[-1]['forgot']:>6}  印象 {rows[-1]['imp']:>3}  "
                  f"常驻 {rows[-1]['res']:>2}  杂讯 {rows[-1]['junk']:>6}  "
                  f"注入 {rows[-1]['blk']:>4}字  db {rows[-1]['db_kb']:>5}KB", flush=True)

    elapsed = time.time() - t_start
    hyp_states = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM hypotheses GROUP BY status")}
    out = {
        "days": DAYS, "per_day": PER_DAY, "total_turns": DAYS * PER_DAY,
        "elapsed_s": round(elapsed, 1),
        "obs_ms_p50": round(pct(obs_times, .5), 1), "obs_ms_p95": round(pct(obs_times, .95), 1),
        "dream_ms_p50": round(pct(dream_times, .5), 1), "dream_ms_p95": round(pct(dream_times, .95), 1),
        "rows": rows, "compactions": compactions, "hypotheses": hyp_states,
        "probes": {k: [(d, ok) for d, ok in v] for k, v in probe_hits.items()},
    }
    json.dump(out, open(Path(tempfile.gettempdir()) / "stress90k.json", "w"),
              ensure_ascii=False, indent=1)

    print(f"\n总耗时 {elapsed/60:.1f} 分钟，{DAYS*PER_DAY} 轮对话")
    print(f"observe: p50 {out['obs_ms_p50']}ms / p95 {out['obs_ms_p95']}ms（每 1000 句）")
    print(f"dream:   p50 {out['dream_ms_p50']}ms / p95 {out['dream_ms_p95']}ms")
    print(f"半衰期压缩触发 {compactions} 次")
    print(f"假说状态：{hyp_states}")
    print("\n探针：")
    for key, vals in out["probes"].items():
        seq = [ok for d, ok in vals if d >= probe_day[key]]
        lost = [d for d, ok in vals if d >= probe_day[key] and not ok]
        print(f"  {key:>4}（第 {probe_day[key]:>2} 天埋）：命中 {sum(seq)}/{len(seq)}"
              + (f"，首次丢失第 {lost[0]} 天" if lost else "，全程命中"))


if __name__ == "__main__":
    main()
