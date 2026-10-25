# 可塑性记忆 / plastic-memory

给私人智能体的一套**会遗忘、会做梦、会改主意**的记忆。

不是向量库 + RAG。RAG 的目标是"记得越多越好"，这里的目标是**像人一样记**：只留判断，细节会烂掉，重要的钉住，说错了能撤。

> 实验性。经过 90 天 × 每天 100 条的模拟压测和一套单元测试，但**没有经过长期真实使用**。欢迎试用并提 issue。

## 三种用法，同一套核心

```
plastic_memory/          ← 核心：零依赖，只有标准库
  store.py                SQLite 三区、分类、检索、追溯
  dream.py                梦：合并 / 提升 / 降权 / 淘汰 / 半衰期压缩
  distill.py              蒸馏：规则档 + 模型档
  persona.py              交叉人设：触发词、注意力、记忆偏向、张力
  engine.py               宿主无关的流程：这一轮给什么、记什么、睡不睡
  llm.py                  任何 OpenAI 兼容端点（DeepSeek / OpenAI / Ollama）
  tools.py                9 个工具的实现（schema + handler）
  mcp.py                  MCP server（stdio JSON-RPC，零依赖）
  cli.py                  命令行

adapters/                ← 给老配置留的薄壳，指向上面的实现
__init__.py / hooks.py   ← Hermes 插件入口（只有这三个文件是 Hermes 专用）
```

核心不 import 任何宿主的东西。适配器只负责三件事：**把消息递进来**、**把返回的上下文塞给模型**、**把模型回复递回去**。

`pyproject.toml` 让它可以 `pip install`；`plugin.yaml` 让它同时是一个 Hermes 插件。同一个仓库，两种身份。

## 它和普通记忆有什么不同

| | 普通 RAG 记忆 | 可塑性记忆 |
|---|---|---|
| 存什么 | 原文块 + 向量 | 事件（会烂）+ 印象（判断） |
| 忘不忘 | 不删 | 按类型半衰期：身份 1 年、情绪 7 天、事实 14 天 |
| 什么时候处理 | 写入即索引 | 离线"做梦"，按主题合并成判断 |
| 用户能不能看 | 能 listing | **核心区没有任何导出入口**，只有日记出口 |
| 记错了怎么办 | 再写一条 | 识别"你记错了"，撤下那条 |
| 人会变 | 不知道 | 检测态度翻转（"不打算跳槽" → "想跳槽"） |

## 三层

```
你的话
  ├─ 筛选门 ─ 重复 / 寒暄 ──▶ 不落盘（只留计数）
  │          关键事件 ──────▶ 事件层（按类型衰减 7–365 天；带时间的日程到期前不衰减）
  └─ 梦（离线，攒够才睡）
        ├─ 按主题聚合：同一件事的不同侧面 → 一条判断
        ├─ 单条够重的（身份/约定/边界）单独提升
        ├─ 矛盾降权、衰减淘汰、跨时间联想
        └─▶ 核心印象区（每轮注入 4–8 条）
                     │
                     └─ 太多时 → 半衰期压缩（10 → 6 条更粗的判断）
```

## 安装

三种方式，按你的宿主选一个。

### 1. 当成 Python 包用（推荐先跑这个）

```bash
pip install git+https://github.com/<you>/plastic-memory.git
plastic-memory observe "我下个月要结婚，在老家办"
plastic-memory dream
plastic-memory status
```

装完就有两个命令：`plastic-memory`（命令行）和 `plastic-memory-mcp`（MCP server）。

### 2. 接 MCP 宿主（Claude Code / Codex CLI / Cursor / Windsurf）

装完包之后**不需要填任何路径**：

```json
{
  "mcpServers": {
    "plastic-memory": {
      "command": "plastic-memory-mcp",
      "env": { "PLASTIC_MEMORY_API_KEY": "sk-..." }
    }
  }
}
```

Claude Code 一行搞定：`claude mcp add plastic-memory -- plastic-memory-mcp`

Codex CLI 写进 `~/.codex/config.toml`：

```toml
[mcp_servers.plastic-memory]
command = "plastic-memory-mcp"
env = { PLASTIC_MEMORY_API_KEY = "sk-..." }
```

不想装包也行，指文件即可：`"command": "python", "args": ["-m", "plastic_memory.mcp"]`（需要 `PYTHONPATH` 指到仓库根）。

### 3. Hermes 插件

```bash
cd ~/.hermes/plugins          # Windows: %LOCALAPPDATA%\hermes\plugins
git clone https://github.com/<you>/plastic-memory.git
hermes plugins enable plastic-memory
```

开新会话生效。工具是 `pm_*`，斜杠命令 `/plastic-memory`。

### 先跑个 demo 看看

```bash
python examples/quickstart.py
```

三十天对话、每天晚上做梦、看它留下什么判断、又忘掉了什么。数据写在临时目录，不碰你的库。

## 配置

### 数据在哪

`PLASTIC_MEMORY_HOME` 决定一切：

- Hermes 里跑 → 默认 `<hermes home>/plugin-data/plastic-memory/`
- 独立跑（MCP / CLI）→ 默认 `~/.plastic-memory/`
- 显式指定 → 设环境变量就行

### 梦用什么模型

梦要蒸馏（把几条原话压成一句判断）。给个 OpenAI 兼容端点就能用：

```bash
export PLASTIC_MEMORY_API_KEY=sk-...                      # 也认 DEEPSEEK_API_KEY / OPENAI_API_KEY
export PLASTIC_MEMORY_BASE_URL=https://api.deepseek.com/v1
export PLASTIC_MEMORY_MODEL=deepseek-chat
```

不配也能跑：蒸馏退回规则档，印象会变成"干净的原话"，不会崩。

### 人设

编辑 `plastic_memory/persona.yaml`：

```yaml
facets:
  - id: engineer
    baseline: 0.45          # 平时占多少权重
    assertiveness: 0.45     # 多爱说自己的、多容易岔开话题
    triggers: [代码, 部署, 报错]
    attention:
      sharp: [可验证的数字, 失败路径]    # 会停下来看什么
      blind: [客套, 情绪安慰]            # 会自动略过什么
    memory_bias: {fact: 1.6, emotion: 0.4}   # 哪类事更容易变成印象
    voice: 先给结论和数字，不铺垫。
    tension: 想给精确答案，容易忽略对方此刻的状态。

self_facts:                 # 它自己的生活（改完立刻生效）
  places: [新疆, 喀什]
  stances: ["旅行不是打卡，是走丢。"]
```

## 工具

| 工具 | 作用 |
|---|---|
| `pm_status` | 三层容量、梦的次数、当前人设（只返回数字） |
| `pm_recall` | 按语义标签检索事件层 |
| `pm_observe` | 显式记一条 |
| `pm_dream` | 立刻做一次梦 |
| `pm_persona` | 查看当前人设面与权重 |
| `pm_trace` | 追一条印象是从哪几句原话长出来的 |
| `pm_pin` | 钉住 / 取消钉住 |
| `pm_compact` | 半衰期压缩（需用户同意后 `confirm=true`） |
| `pm_diary` | 把核心区写成日记（唯一的出口） |

## 测试

```bash
python selftest.py        # 30 天模拟，验证三层行为，不依赖宿主
python test_negative.py   # 18 条负面断言 + 边界（"不该出现的东西没出现"）
python fuzz.py 12000      # 随机操作序列，只为找崩溃
python consistency.py     # 数据自洽性：引用 / 时间戳 / owner 归属
python stress90.py        # 90 天 × 每天 100 条（约 1 分钟）
python stress90k.py       # 90 天 × 每天 1000 条，共 9 万轮（约 1 分钟）
```

只测"该有的有"抓不到 bug——前几轮修的毛病（同一件事说两遍、助手回复被当成用户的话、过期条目还在召回）全是"不该出现的东西出现了"。所以另有一组负面断言和一组随机 fuzz。

selftest 的断言包括：杂讯拦截、去重强化、衰减淘汰、梦的提升与合并、注入块去重，以及**核心区没有任何导出工具**。

三个月压测（Windows，90000 轮）：observe **0.28 ms/句**（1000 句共 276ms）、dream 47ms/次、注入块 577 字、常驻 14 条、印象 41 条、db 3.5 MB、半衰期压缩触发 3 次。第 10 天后进入稳态，事件层存活稳定在 190–350 条，不再随天数增长；db 呈锯齿增长（每 30 天物理清理一次死行）。

六个探针（埋进 9 万条噪声里，每 5 天查一次）：

| 探针 | 类型 | 结果 |
|---|---|---|
| 工号 A7391 | identity | 90/90，全程命中 |
| 下周去杭州出差 | 有时效 | 有效期内 8/8；到期后遗忘（第 23 天） |
| 妈住院做手术 | critical | 61/61，自动 pin |
| 猫改名煤球 | life | 46/46 |
| 不吃香菜 | pref | 31/31 |
| 答应周五前交文档 | promise | 16/16 |

有时效的那条不靠 `pin` 活着，靠 `expires_at`：到期前 `retention` 恒为 1.0，到期那天开始正常衰减并从召回里消失。忘了，是因为它该被忘。

假说也不再只生不灭：三个月下来 8 条 confirmed（提出之后，它盯的事件真的进了核心区）、54 条 open（上限 50 条 + 当天新增）、156 条 stale（30 天没印证）。

## 修复记录

**v0.3.7** — 多用户隔离的最后一圈、假说上限

| 位置 | 原来的问题 | 现在 |
|---|---|---|
| `store.observe` 的印象强化 | `SELECT ... FROM impressions WHERE resident=1` 没带 owner，A 提一件事会去强化 B 的同名印象 | 加 owner |
| `store.recent_events` / `unconsolidated_count` | 都没带 owner，B 的事件数会触发 A 的梦 | 加 owner |
| `hypotheses` 表 | **根本没有 owner 列**，两个人的假说混在一张表、去重时互相压制 | 加列 + 迁移，插入/查询/结案全带 owner |
| 假说的 open 上限 | 每梦最多造 6 条，90 天能堆几百条；它们只是"猜测"，却比核心印象还多 | open 超过 50 条把最旧的结案 |
| `engine.build_block` | 空输入也会注入人设块，白占 token | 空文本直接返回 `None` |
| `hooks.py` | 作为顶层模块被测试/工具导入时 `attempted relative import` | 回退绝对导入 |

**v0.3.6** — 多用户隔离、假说判据、两处性能

| 位置 | 原来的问题 | 现在 |
|---|---|---|
| `store.promoted_event_ids` | `provenance` 是 `{"from":"events","ids":[1]}`，代码直接遍历 dict 拿到键名 `"from"`/`"ids"`，`int()` 抛错被吞 → **永远返回空集**，注入时同一件事会说两遍（印象一遍、原话一遍） | 取 `p["ids"]`；顺便加 owner 过滤 |
| `dream._has_impression` / `store.find_impression` | 都没按 owner 过滤：A 的印象会挡住 B 建同样的印象，`pm_pin` 会钉到别人的记忆 | 加 owner |
| `dream._targets_confirmed` | 判据"任意一条 target 进任意一条印象"等于没有——假说盯的本来就是常被提升的事，442 条里 261 条 confirmed | 用假说自己的 `ts` 当下界，**提出之后**才被提升才算印证；实测 11 confirmed / 88 open / 119 stale |
| `store._grams` | dream 里同一段话被反复切 gram，一次梦跑 1.3 万次，占 61% 时间 | `lru_cache(8192)` + `frozenset`：dream **145 → 47 ms**，observe 462 → 276 ms |
| `store.maintain` | 淘汰只是 `alive=0`，行永远留着；9 万轮里 3 万行是死的，db 线性涨 | 每 30 天物理删掉"超过 180 天且没被任何印象引用"的死事件 + `VACUUM`；被引用的不动（追溯链） |

**v0.3.5** — 字段审计 & 性能

| 位置 | 原来的问题 | 现在 |
|---|---|---|
| `store.connect()` | 每轮对话都 commit，默认 rollback journal + FULL 同步在 Windows 上一次 9ms，占 observe 的 90% | WAL + `synchronous=NORMAL`，并把一次写轮的 commit 合成一次：**10.9ms → 0.46ms/句** |
| `engine.observe_turn` 里 `divert_stats` | 写入用无后缀的键，读取带 `:owner` 后缀，多用户模式下永远读到空，"岔开有没有被接受"的自适应从不生效 | 两边统一带 owner |
| `store.trace_impression` | `merged_into` 写了但从不读，追溯时看不出这条原话是"已并入"还是"已淘汰" | trace 取 `alive` + `merged_into`，显示「（已并入 #12）」 |
| `dream` 假说结案 | `hypotheses.status` 只被 SELECT 读、从没被 UPDATE，`hypotheses_open` 单调涨 | 先看 targets 里的事件是否进了核心区（→ `confirmed`），再把 30 天没印证的标 `stale` |
| `dream.compress_core` | `last_compaction` 只写不读，死键 | 删掉 |

**v0.4.1** — 干净环境里的一处致命

| 位置 | 原来的问题 | 现在 |
|---|---|---|
| `plastic_memory/persona.py` `_mini_yaml` | 行尾注释没剥：`assertiveness: 0.45   # 多爱说自己的` 整串被当值，`assertiveness()` 里 `float()` 抛 `ValueError`。没装 pyyaml 的环境（正是这一版主打的"干净环境"）里 `build_block` 每轮都崩——selftest / test_negative / consistency / fuzz / quickstart 五个入口全挂在这一行 | 新增 `_strip_comment()`：引号外、且 `#` 前面是空白才算注释；值里真带 `#` 的不受影响 |

**v0.3.4** — dropped 去重

| 位置 | 原来的问题 | 现在 |
|---|---|---|
| `dropped` 表 | "只留一个哈希计数"没落地：每句寒暄插一行。60 天 2400 条输入 → 841 行，其中 836 行同一个 digest | UPSERT + `count` 列，迁移时合并历史重复行；`junk_blocked` 改 `SUM(count)`，语义从"多少种"变回"多少次" |

**v0.3.3** — 宿主解耦后的第一批修复

| 位置 | 原来的问题 | 现在 |
|---|---|---|
| `plastic_memory/dream.py` 提升 / 合并印象 | 事件的 `expires_at` 被一起抄进印象：「我下个月开始学日语，报了线下班」这种长期事实，在第 31 天连印象一起过期消失 | 提升时 `expires_at=None`，印象的寿命只由半衰期和 `pin` 决定 |
| `plastic_memory/store.py` `find_impression` | 函数末尾两个分支都 `return best`，门槛形同虚设：传一个跟哪条都不相干的词，会静默钉住一条随机印象 | `coverage` 门限 0.6；单字/双字 query 走子串匹配（`_grams` 对单字返回单字集合、对长文本返回双字集合，交集恒空——不修的话「猫」「钱」会一条都找不到） |
| `tools.py` `handle_pm_diary` | 被 MCP server / CLI 当顶层模块导入时抛 `attempted relative import with no known parent package` | 相对导入失败回退绝对导入 |
| `adapters/cli.py` | `--owner` 只塞进一个没人读的 dict，多用户隔离不生效 | 写入 `meta.active_owner` |
| `adapters/mcp_server.py` | `serverInfo.version` 硬编码 | 跟随 `plastic_memory.__version__` |

## 已知限制

- **分类是关键词规则，不是语义模型。** 词表外的说法（"这破班是一天也下不下去了"）会漏。检索用同义簇 + 上位标签做一阶语义，「睡不好」能召回「失眠」，但没有真 embedding。
- **MCP 宿主拿不到自动注入。** 只有 Hermes 有 `pre_llm_call` 这类前置钩子。其他宿主里模型得自己想起来调 `pm_recall`——所以自动注入这一层只有 Hermes 版完整。
- **"不回复"做不到。** 沉默只能降级成"极简回应"，因为插件没法延迟响应。
- **单机边界。** 用户有磁盘权限，能直接读 sqlite。"不可见"是产品边界，不是密码学边界。
- **梦的蒸馏需要模型。** 没模型时质量降一档。

## License

MIT
