import sys, tempfile, time, json, random, os
from pathlib import Path
HERE = str(Path(__file__).resolve().parent)
sys.path.insert(0, HERE)
import plastic_memory.store as store, plastic_memory.dream as dream, plastic_memory.persona as persona, plastic_memory.engine as engine

random.seed(11)
mods = ["简历解析", "岗位匹配", "用户登录", "文件上传", "向量检索", "前端路由", "支付回调", "消息推送"]
acts = ["报 500", "超时", "返回空", "内存涨了", "白屏", "卡在 loading", "排序乱了"]
objs = ["测试环境", "手机上", "打包后", "重启就好", "线上偶发", "高并发下"]
life = ["今天加班到十一点", "年糕又吐了", "我妈打电话催考公", "这周第三次加班", "想跳槽了",
        "面试约在下周", "涨薪谈下来了", "失眠到两点", "周末躺了一天", "自己做了顿饭"]
emos = ["累", "烦", "焦虑", "有点崩", "还行", "郁闷", "心累", "睡不着"]
prefs = ["我不喜欢反复确认", "我喜欢先看结论", "别给我列一堆选项", "我讨厌写长文档"]

def gen():
    r = random.random()
    if r < .5:  return f"{random.choice(mods)}{random.choice(acts)}，{random.choice(objs)}"
    if r < .7:  return random.choice(life) + random.choice(["", "了", "，烦", "，唉"])
    if r < .85: return f"今天{random.choice(['有点', '真的', '特别'])}{random.choice(emos)}"
    return random.choice(prefs)

# 埋点：第 1/30/60 天各说一件独特的事，看多久还找得到
PROBES = [
    (1,  "我的工号是 A7391",                 "工号"),
    (30, "我下周要去杭州出差三天",             "出差"),
    (60, "我把猫的名字改成煤球了",             "煤球"),
]
probe_by_day = {d: (text, key) for d, text, key in PROBES}
probe_day = {key: d for d, _text, key in PROBES}

conn = store.connect(Path(tempfile.mkdtemp()) / "long.db")
DAY = store.DAY; t0 = 1_760_000_000.0
spec = persona.load_spec()
rows = []
probe_hits = {k: [] for _, _, k in PROBES}

for d in range(1, 91):
    now = t0 + d * DAY + 20 * 3600
    if d in probe_by_day:
        store.observe(conn, probe_by_day[d][0], role="user", now=now)
    ta = time.perf_counter()
    for _ in range(100):
        store.observe(conn, gen(), role="user", now=now)
    t_obs = time.perf_counter() - ta

    tb = time.perf_counter()
    dream.dream(conn, now=now, bias=persona.memory_bias(spec, "加班 累"), history_n=12)
    t_dream = time.perf_counter() - tb

    if store.resident_count(conn) >= 16:
        dream.compress_core(conn, now=now)

    st = store.stats(conn, now)
    for _, ptext, key in PROBES:
        hits = store.recall_events(conn, key, k=3, now=now)
        ok = any(key in h["text"] for h in hits)
        if ok:
            state = "hit"
        elif d < probe_day[key]:
            state = "before"        # 还没说出口
        else:
            r = conn.execute("SELECT expires_at FROM events WHERE text=? ORDER BY id DESC LIMIT 1",
                             (ptext,)).fetchone()
            state = "expired" if (r and r["expires_at"] and float(r["expires_at"]) <= now) else "lost"
        probe_hits[key].append((d, ok, state))

    if d % 5 == 0 or d == 1:
        blk = engine.build_block(conn, "今天好累", now=now) or ""
        rows.append({"d": d, "obs_ms": round(t_obs * 10, 1), "dream_ms": round(t_dream * 1000),
                     "alive": st["events_alive"], "forgot": st["events_forgotten"],
                     "imp": st["impressions"], "res": store.resident_count(conn),
                     "blk": len(blk), "db_kb": round(os.path.getsize(conn.execute("PRAGMA database_list").fetchone()[2]) / 1024)})

out = {"rows": rows, "probes": {k: [(d, ok, st) for d, ok, st in v] for k, v in probe_hits.items()}}
json.dump(out, open(Path(tempfile.gettempdir()) / "stress90.json", "w"), ensure_ascii=False, indent=1)

print(f"{'天':>3} {'observe':>8} {'dream':>8} {'存活':>5} {'遗忘':>6} {'印象':>4} {'常驻':>4} {'注入':>5} {'db':>8}")
for r in rows:
    print(f"{r['d']:>3} {r['obs_ms']:>7.1f}ms {r['dream_ms']:>7}ms {r['alive']:>5} {r['forgot']:>6} "
          f"{r['imp']:>4} {r['res']:>4} {r['blk']:>4}字 {r['db_kb']:>6}KB")
print("\n探针保持情况（分三种：有效期内命中 / 到期正确遗忘 / 没到期却丢了）：")
for key, vals in out["probes"].items():
    seq = [(d, st) for d, _ok, st in vals if st != "before"]
    if not seq:
        continue
    hit = sum(1 for _, st in seq if st == "hit")
    exp = sum(1 for _, st in seq if st == "expired")
    lost = [d for d, st in seq if st == "lost"]
    span = hit + len(lost)
    line = (f"  {key:>4}（第 {probe_day[key]} 天埋）：有效期内命中 {hit}/{span}"
            + (f"，误丢在第 {lost[0]} 天，共 {len(lost)} 天" if lost else "，一次没丢")
            + (f"；到期后正确遗忘 {exp} 天" if exp else ""))
    print(line)
