# PRD v2 — 花椒中文金融账号 · 学习驱动的内容运营系统

> **Owner:** huajiao
> **Drafted:** 2026-05-17
> **Status:** Draft for review（未实施）
> **Repo:** `/Users/oxjames/Downloads/CC_testing/花椒的content_2/`
> **参考蓝本:** `/Users/oxjames/Downloads/CC_testing/花椒创业板/`（同栈、可跑的发推 + 学习闭环，但学习链条只闭合 2/3）

---

## 0. TL;DR

把当前已经退化为「雪球 → Discord 单管道」的 content_2 重构成一个**自学习的中文金融 Twitter 运营系统**，核心是一个**可被 writer 长期检索的爆款模式记忆**（不是每次临时蒸馏完就丢）。架构沿用创业板已验证的 Playwright + Chrome CDP + SQLite + 本地 Claude CLI 栈，但要补三件创业板没做的事：

1. **真正启用 `technique_library`**：KOL 爆款句式 / 钩子 / 节奏被结构化抽取后**持久化**，写作时按场景检索 Top-N 注入 prompt，而不是只靠 8h reaction pack。
2. **权重学习用真实互动而不是 LLM 自评分**：viralScore = f(reply×27 + profile_click×12 + like×0.5 - negfb×74) 直接驱动 strategy_weights。
3. **金融 + 泛流量双轨混发**：用 `topic_blend.md` 配置当日内容配比（默认 70% 金融 / 30% 泛流量），避免单一垂类被推流降权。

不引入任何新框架（LangChain / API SDK / 云存储均禁止），完全在现有技术栈内做完。

---

## 1. 为什么要重构（现状诊断）

调研子 agent 的事实结论：

| 维度 | 创业板（参考） | content_2（现状） |
|---|---|---|
| 入口可跑性 | `src/slot_runner.py` + cron 真实生产 | 只有 `src/run.py`（雪球→Discord），crontab 实际指向另一个仓库 |
| Playwright 发推 | ✅ `twitter_bot.py` 908 行 | ❌ 不存在 |
| KOL 抓取 | ✅ X List timeline | ❌ 只抓雪球 |
| Reaction Pack | ✅ 8h 蒸馏注入 writer | ❌ 不存在 |
| Review 回测 | ✅ 但用 LLM 自评分调权 | ❌ 不存在 |
| 爆款句式长期记忆 | ❌ `technique_library` 表 0 行，`get_learned_techniques()` 是 legacy shim 直接返回 `[]` | ❌ 同样空 |
| DB schema | ✅ 11 张表都在 | ✅ schema 还在但没人写 |
| voice/templates | 全是 AI 创业向 | 文件名带 `_ai` 但内容是金融 persona，错配 |

**结论**：不要在创业板上"原地升级"，而是把它的**能跑模块 fork 进 content_2**，同时把它**没做完的第三条链路（KOL 爆款 → 持久记忆 → writer）补上**。content_2 干净的空架子是个优势。

---

## 2. 北极星指标

| 层级 | 指标 | 90 天目标 |
|---|---|---|
| 业务北极星 | 真粉数（去僵尸） | 10,000 |
| 业务次级 | 月曝光 | 1,000,000 |
| 算法层 | 平均 reply / 推文 | ≥ 5 |
| 算法层 | 平均 profile click / 推文 | ≥ 20 |
| 算法层 | 负反馈率（mute+block+report / 曝光） | ≤ 0.5‰ |
| 写作层 | 反 AI 腔扫描通过率（首稿） | ≥ 80% |
| 写作层 | 爆款命中率（>=200 likes） | ≥ 10% |
| 学习层 | technique_library 周新增条目 | ≥ 20 |
| 学习层 | strategy_weights 周调整幅度收敛 | 第 4 周起 std < 0.05 |

> 算法层指标全部对齐 X 开源算法的权重系数（reply 27x / profile click 12x / negfb -74x），不再追求"裸阅读量"。

---

## 3. 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                    Cron (本地 macOS, UTC)                        │
│  observe 0 */1 * * *   periodic2h 0 */2 * * *   review 0 16 * * *│
└────────────────┬────────────────┬─────────────────┬─────────────┘
                 │                │                 │
    ┌────────────▼──────┐ ┌──────▼─────────┐ ┌─────▼──────────┐
    │  Observer Loop    │ │  Posting Loop  │ │  Review Loop   │
    │  (signal source)  │ │  (publish)     │ │  (backtest)    │
    └────────┬──────────┘ └────────┬───────┘ └────────┬───────┘
             │                     │                  │
   ┌─────────▼───────────┐   ┌─────▼─────────┐   ┌────▼──────────┐
   │ Source Adapters     │   │ FactSpine     │   │ Metrics       │
   │ - x_list_adapter    │   │ ├ ReactionPack│   │ Collector     │
   │ - xueqiu_adapter    │   │ ├ Retrieval   │   │ (Chrome MCP)  │
   │ - news_flash_adapter│   │ │ (techniques)│   └────┬──────────┘
   │ - viral_kol_adapter │   │ └ TopicBlend  │        │
   │   (泛流量)          │   │     ↓         │        ▼
   └─────────┬───────────┘   │ Writer (CLI)  │   ┌──────────────┐
             │               │     ↓         │   │ Pattern Miner│
             ▼               │ Scorer        │   │ (CLI)        │
   ┌──────────────────────┐  │     ↓         │   │ ├ viralScore │
   │ raw_observations DB  │  │ Guardrails    │   │ ├ technique  │
   │ + technique_extractor│──┤     ↓         │   │ │ extractor  │
   │   (异步后台)         │  │ Publisher     │   │ └ weight     │
   └──────┬───────────────┘  │ (Chrome MCP)  │   │   updater    │
          │                  └─────┬─────────┘   └────┬─────────┘
          ▼                        │                  │
   ┌──────────────────────────────────────────────────▼──────────┐
   │  SQLite (data/pepperbot.db)                                  │
   │  posts │ reaction_observations │ technique_library (启用) │  │
   │  learning_log │ strategy_weights │ post_metrics_timeseries │ │
   │  daily_stats │ circuit_breaker │ slop_words                 │ │
   └──────────────────────────────────────────────────────────────┘
                                  ↑
                                  └── 长期记忆，跨 session 复用
```

---

## 4. 子系统规格

### 4.1 Observer Loop — 信号源采集

**职责**：每小时跑一次，独立于发帖 loop。把"看的"和"写的"解耦，避免抓慢拖发帖。

**Source Adapter 协议**（`src/observers/base.py`）：

```python
class SourceAdapter(Protocol):
    name: str                                    # x_list / xueqiu / news_flash / viral_kol
    cookie_key: str                              # env 里的 cookie 变量名
    rate_limit_per_hour: int

    async def fetch_latest(self, since: datetime) -> list[Observation]: ...

@dataclass
class Observation:
    source: str
    author_handle: str
    author_tier: int                # 1=核心 KOL, 2=次级, 3=泛流量
    content: str
    posted_at: datetime
    likes: int
    retweets: int
    replies: int
    impressions: int | None         # X 给, 雪球没有
    has_image: bool
    raw_url: str
    topic_hint: str | None          # 'pre_market' / 'tech' / 'meme' / ...
```

**已确定的 adapter（首发 4 个）**：
1. `x_list_adapter` — 金融 X List timeline（同创业板，复用 `twitter_bot.scrape_list_by_url`）
2. `x_list_adapter` (instance 2) — **泛流量** X List（用户后续提供）
3. `xueqiu_adapter` — 雪球达人 feed（content_2 现有 `scraper.py` 已验证，重命名搬迁）
4. `news_flash_adapter` — 财联社 / Wallstreetcn 7×24 快讯（仅作为 fact spine 输入，不进 learning loop）

> KOL list 配置走 `config/sources.yaml`，不写死在代码里。

**输出**：`reaction_observations` 表（原始流，永久保留）。

### 4.2 Pattern Miner — 学习引擎核心（**创业板没有的部分**）

**职责**：把 raw observations 转化为**结构化的、可检索的、长期沉淀的**技法。**不是每次蒸馏完就丢**。

**触发**：observer 写入后异步触发 + 每 6 小时全量回扫一次。

**算法**：

```
For each new observation:
  1. 计算 viralScore = likes*0.5 + retweets*1.0 + replies*27.0 + (impressions ? profile_click_est*12 : 0)
                       - negfb_est * 74
     (impressions 没有时用作者 baseline 估计)
  2. 标记 is_viral = viralScore > (作者过去 30 天 p80)
  3. If is_viral:
     CLI prompt → 抽取 technique JSON:
        {
          "hook_pattern": "首句以反共识开场",
          "hook_example": "今天所有人都在说要抄底，但是...",
          "syntax_signature": "短句+逗号+空格无句号",
          "stance_strength": 4,  # 0-5
          "emotion_trigger": ["FOMO", "嘲讽", "认知优越"],
          "sentence_len_avg": 18,
          "image_style": "K线截图+涂鸦标注",
          "post_hour_utc": 4,
          "topic_lane": "pre_market" | "general_tech" | ...,
          "applicable_persona": ["finance_neutral", "finance_contrarian"]
        }
  4. UPSERT 到 technique_library，更新 times_observed, success_rate (按 viralScore 聚合)
  5. 触发 weight_updater: 对应 topic_lane 的 strategy_weights 微调
```

**Retrieval（写作时用）**：
- writer 拿到 fact_spine 后，按 `(topic_lane, hour_of_day, persona_compatible)` 查 technique_library Top-K（K=5，按 success_rate × recency_decay 排序），把 `hook_pattern + hook_example + syntax_signature` 注入 prompt 的 angle_card 阶段。
- **example bank**：高分原句（脱去人名、币种、股票代号后）存 `technique_library.hook_example`，DSPy-style few-shot 注入。

### 4.3 Writer + Scorer + Guardrails — 复用创业板三段式

| 阶段 | 函数 | 改造点 |
|---|---|---|
| FactSpine | `writer.build_fact_spine(news, observations)` | 加入 news_flash_adapter 输出 |
| Retrieval | **新增** `writer.retrieve_techniques(topic_lane)` | 查 technique_library Top-K |
| AngleCard | `writer.build_angle_card()` | prompt 加 retrieved techniques 段 |
| 起稿 | `writer.draft()` | claude-sonnet-4-6 |
| AntiTemplate Audit | `writer.audit_for_template()` | A 类 slop 扫描 |
| Guardrails 循环 | `guardrails.check()` 最多 3 次重写 | 加金融合规词库（不喊单 / 不预测点位） |
| Scorer | `scorer.score(post)` | 4 维度→**改成 5 维度**：信息密度 / 立场强度 / 反共识度 / 钩子强度 / **合规安全度** |

### 4.4 Publisher — Chrome MCP 发推

完全复用创业板 `twitter_bot.py` + CLAUDE.md 已记录的 window.name 跨域桥 + DOM 原型注入。**不动**。

### 4.5 Reviewer — 真实数据回测 + 权重更新

**关键差异 vs 创业板**：
- 不再用 `scorer.score_total`（LLM 主观）驱动权重，**改用真实 viralScore**。
- 引入显著性检查：某 `topic_lane` 至少有 5 条数据才允许调权，避免单条爆款误导。
- 学习结果**双通道落地**：
  - 通道 A：写 `strategy_weights`（影响下一轮 topic_lane 抽样概率）
  - 通道 B：**回写 `voice/slop_words` 和 `voice/avoid_slop.md`**（创业板的缺口：losing_patterns 只活在 prompt 里，没沉淀）

**Cron**：每天 16:00 UTC（00:00 CST）跑一次，回扫近 30 条 posts 的 24h + 72h 互动数。

---

## 5. 数据层 Schema 增量

复用创业板 11 张表，**新增 / 启用** 3 项：

```sql
-- 新增：每条帖子的时序互动（不是只存最新）
CREATE TABLE post_metrics_timeseries (
  post_id INTEGER REFERENCES posts(id),
  collected_at TIMESTAMP,
  likes INTEGER, retweets INTEGER, replies INTEGER, impressions INTEGER,
  viral_score REAL,
  PRIMARY KEY (post_id, collected_at)
);

-- 启用（创业板有表但 0 行）：扩展字段
ALTER TABLE technique_library ADD COLUMN hook_example TEXT;
ALTER TABLE technique_library ADD COLUMN syntax_signature TEXT;
ALTER TABLE technique_library ADD COLUMN topic_lane TEXT;
ALTER TABLE technique_library ADD COLUMN applicable_persona TEXT;  -- JSON
ALTER TABLE technique_library ADD COLUMN recency_decay REAL DEFAULT 1.0;

-- 新增：source adapter 健康
CREATE TABLE source_health (
  adapter_name TEXT PRIMARY KEY,
  last_success_at TIMESTAMP,
  consecutive_failures INTEGER,
  rate_limit_hit_at TIMESTAMP
);
```

---

## 6. 写作策略：金融 + 泛流量混发

**问题**：纯金融日发 12 条易被算法判垂类疲劳，且非交易时段曝光低。

**配比规则**（`config/topic_blend.yaml`）：

```yaml
default_daily_quota: 12
blend:
  finance_pre_market:   { quota: 2, hours_utc: [22, 23, 0] }   # CST 06-08
  finance_intraday:     { quota: 3, hours_utc: [2, 4, 6] }     # CST 10-14
  finance_post_market:  { quota: 2, hours_utc: [8, 10] }       # CST 16-18
  finance_overnight:    { quota: 1, hours_utc: [15] }          # CST 23
  general_tech_ai:      { quota: 2, hours_utc: [12, 14] }      # CST 20-22 泛流量黄金时段
  general_meme_career:  { quota: 2, hours_utc: [13, 16] }      # CST 21, 00

fallback_when_dry: general_tech_ai   # 该时段无 fact spine 时退到这条
```

**人设约束**（`config/persona.md` 补充）：
- 泛流量推文必须**从金融人视角切入**（"作为做 A 股的看 AI"），不假装是技术博主。
- 不碰：政治、宗教、性别对立、黄、宏观政策直接评价。
- 可碰：AI 工具、码农段子、职场观察、书评、历史、城市生活。

---

## 7. Twitter 算法对齐（基于 punk2898 + X 开源算法）

| 算法事实 | 系统层落地 |
|---|---|
| Reply 27x，Like 0.5x | scorer 5 维度里"钩子强度"权重最高；writer prompt 强制结尾抛二选一/反共识问题 |
| Profile click 12x | 配图必发；首句留悬念；bio 持续优化（不在本 PRD 范围） |
| Negative feedback -74x | guardrails 加"避雷词库"：标题党、情绪宣泄、人身攻击 → reject |
| 30 分钟早期互动窗口 | 发帖时间贴在目标受众上线前 5-10 分钟（quota.hours_utc 已对齐） |
| In-network RealGraph | KOL 评论 loop 每 slot 跑（复用创业板） |
| 主推文带链接降权 | publisher 检测正文含 http → 自动改成发"正文" + 首条 reply 放链接 |
| 长推文 / Article 阅读时长权重高 | quota 里每日至少 1 条长推（>500 字），走 X Article 接口 |

---

## 8. 调度（替换 CLAUDE.md 现有 5 slot）

```cron
# Observer：每小时抓信号，不发帖
0 * * * *      /Users/oxjames/Downloads/CC_testing/花椒的content_2/src/run.sh observe

# Posting：每 2 小时发 1 条（CST 06-24 共 12 条）
0 22-23,0-15/2 * * * /Users/oxjames/Downloads/CC_testing/花椒的content_2/src/run.sh post

# Reviewer：每天 00:00 CST 复盘 + 权重更新
0 16 * * *     /Users/oxjames/Downloads/CC_testing/花椒的content_2/src/run.sh review

# Pattern Miner：每 6 小时全量回扫一次（增量挖掘已经在 observer 异步触发）
0 */6 * * *    /Users/oxjames/Downloads/CC_testing/花椒的content_2/src/run.sh mine
```

`run.sh <command>` 统一入口，参数化分发。**不再保留** slot1-5 这种时间魔法数命名。

---

## 9. 目录结构（重构后的最终态）

```
花椒的content_2/
├── CLAUDE.md
├── MEMORY.md
├── docs/
│   ├── PRD_v2.md                 # 你正在读的文件
│   └── migration_log.md          # phase 推进记录
├── config/
│   ├── persona.md
│   ├── sources.yaml              # 新增：KOL list、cookie key、rate limit
│   ├── topic_blend.yaml          # 新增：金融/泛流量配比
│   ├── kol_list_finance.md       # 金融 KOL（带 tier）
│   ├── kol_list_general.md       # 泛流量 KOL（你后续给）
│   └── filter_rules.md
├── voice/
│   ├── voice_rules.md
│   ├── voice_profile.md
│   ├── avoid_slop.md             # 加金融合规避雷段
│   ├── memeng_techniques.md
│   └── source_pack_style_anchor.md
├── templates/
│   ├── template_finance.md       # 重写：金融向（旧 template_ai.md 删除）
│   └── hooks_finance.md          # 重写
├── writer/
│   └── SKILL.md                  # 加 retrieval 阶段
├── ops/
│   ├── daily_routine.md
│   └── playwright_rules.md       # 复用现有
├── src/
│   ├── run.sh                    # 统一 cron 入口
│   ├── main.py                   # 编排器，对应 observe/post/review/mine
│   ├── database.py               # 从创业板搬，加 timeseries 表
│   ├── llm.py                    # 已有，复用
│   ├── twitter_bot.py            # 从创业板搬
│   ├── writer.py                 # 从创业板搬 + 加 retrieve_techniques
│   ├── scorer.py                 # 从创业板搬 + 改 5 维度
│   ├── guardrails.py             # 从创业板搬 + 加金融合规
│   ├── reviewer.py               # 重写：真实 viralScore 驱动
│   ├── observers/
│   │   ├── base.py               # SourceAdapter Protocol
│   │   ├── x_list_adapter.py
│   │   ├── xueqiu_adapter.py     # 从现有 scraper.py 抽取
│   │   └── news_flash_adapter.py
│   ├── miner/
│   │   ├── viral_scorer.py       # 算法层 viralScore 计算
│   │   ├── technique_extractor.py# CLI 抽取技法
│   │   └── weight_updater.py
│   └── publisher.py              # Chrome MCP 发推（薄封装 twitter_bot）
├── data/
│   └── pepperbot.db              # 复用，加 ALTER TABLE
├── logs/
└── tests/                        # 旧的全删，按 phase 重建
```

> 现有 `src/run.py`（雪球→Discord）拆成：抓取逻辑→`observers/xueqiu_adapter.py`，Discord 推送**删除**（不在本 PRD 范围内）。
> `src/discord_poster.py`、`src/config.py` 大部分内容删除，配置改走 yaml。

---

## 10. 迁移路径（4 个 Phase，每个 phase 可独立验证）

### Phase 1 — Lift & Shift（1 周）
- 从创业板 fork：`twitter_bot.py / database.py / writer.py / scorer.py / guardrails.py / main.py`
- 重命名 voice/templates 内容（AI 创业→金融）
- 跑通：手动 `python -m src.main post` 发 1 条测试推（dry-run 模式先）
- **退出条件**：能发出 1 条配图金融推文，DB 有记录。

### Phase 2 — Observer + Source Adapter（1 周）
- 实现 `SourceAdapter` Protocol + 4 个 adapter
- Observer cron 上线
- **退出条件**：`reaction_observations` 表 24h 内 ≥ 200 条新增。

### Phase 3 — Pattern Miner 启用 technique_library（**最关键，2 周**）
- 实现 `viral_scorer.py` + `technique_extractor.py` + `weight_updater.py`
- writer 加 retrieval 阶段
- **退出条件**：technique_library ≥ 50 条，writer prompt 里能看到注入的 hook_example，连续 10 条推文里至少 6 条命中检索到的技法。

### Phase 4 — Reviewer 真实数据驱动 + 双通道学习（1 周）
- 实现 `reviewer.py` 用 viralScore 调权
- 加回写 `voice/avoid_slop.md` 的通道（learning_log 里识别出"反复失败的句式" → 加到 slop_words 表 → 渲染回 .md）
- **退出条件**：连续 2 周复盘，strategy_weights 调整方向跟真实互动数据正相关。

---

## 11. 风险与对策

| 风险 | 对策 |
|---|---|
| Twitter 反爬升级，X List 抓不到 | adapter 抽象层隔离，可换 nitter / 第三方镜像。`source_health` 表监控 |
| 雪球 cookie 过期 | source_health 检测连续失败 → 发本地通知（Mac osascript），不发 IM |
| technique_library 被 LLM 抽出垃圾技法（标题党） | guardrails 在 retrieve 阶段二次过滤，applicable_persona 兜底；review 时检测某技法 sample size ≥ 5 才允许进入 hot rotation |
| 金融合规事故（被认为荐股） | guardrails 关键词 reject + 立场强度 ≤ 3（5 分制）才允许带具体标的 |
| Chrome MCP 抢占（同一 tab 跑发帖+抓取冲突） | observer 用独立 Chrome profile（`--user-data-dir` 区分），不复用发帖 profile |
| 学习闭环跑飞（自举式 slop 放大） | strategy_weights 调整步长 ≤ 0.05，每周人工 review learning_log 一次（写进 ops/weekly_routine.md） |
| 泛流量 KOL list 未提供时无法启动 | Phase 2 可先只跑金融 adapter，泛流量 adapter 留 stub，配置开关 |

---

## 12. Out of Scope（本 PRD 不做）

- Discord / XHS / 微博等其他平台发布
- 视觉素材生成（仍走 og:image 提取）
- 多账号管理（架构预留，但本期只服务 1 个金融账号）
- 付费 X Premium API
- 任何云端推理（Moonshot / OpenAI / Anthropic API）

---

## 13. 待用户后续提供

1. **金融 X List URL** 和 **泛流量 X List URL**（写进 `config/sources.yaml`）
2. **雪球 cookie**（写进 `.env`，key: `XUEQIU_COOKIE`）
3. **Twitter cookie**（已通过 Chrome CDP 解决，无需单独提供）
4. **泛流量 KOL 名单 + 你认可的 5 条历史爆款**（后者用作 technique_library 冷启动 seed）
5. **金融合规红线词清单**（你的下游受众容忍度，比如能不能说"机构"、能不能点名上市公司）

---

## Appendix A — 与创业板差异速查

| 项 | 创业板 | 本系统 |
|---|---|---|
| 账号方向 | AI / OPC 创业（英文+中文） | 中文金融 |
| 主信号源 | AIHOT API + GitHub Trending | 雪球 + 金融 X List + 财联社快讯 |
| 学习闭环 | 2/3 通（缺 technique_library） | **3/3 通** |
| 权重驱动 | LLM 自评分 | **真实 viralScore** |
| 调度 | 5 slot + periodic2h 并存 | **统一 periodic2h + observe 解耦** |
| 写作中文/英文 | 中英混合 | 全中文 |
| 长记忆通道 | 仅 prompt 注入 | **prompt + 回写 voice/.md 双通道** |

## Appendix B — 关键算法常量（v1，可在 Phase 4 后通过实验调整）

```python
# src/miner/viral_scorer.py
WEIGHT_LIKE = 0.5
WEIGHT_RETWEET = 1.0
WEIGHT_REPLY = 27.0
WEIGHT_PROFILE_CLICK = 12.0
WEIGHT_NEGATIVE_FEEDBACK = -74.0

# 估算项（impressions 不可得时）
PROFILE_CLICK_RATE_ESTIMATE = 0.02   # 经验值，先用 X 算法 paper 的均值
NEGATIVE_FEEDBACK_RATE_ESTIMATE = 0.001

# Retrieval
TOP_K_TECHNIQUES = 5
RECENCY_DECAY_HALFLIFE_DAYS = 14

# Weight update
MIN_SAMPLE_FOR_WEIGHT_UPDATE = 5
MAX_WEIGHT_STEP_PER_REVIEW = 0.05
```

---

**End of PRD v2.** Review 后由你决定进入 Phase 1 实施还是先调整本 spec。
