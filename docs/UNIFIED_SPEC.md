# UNIFIED SPEC — 花椒中文金融 X 账号自学习运营系统

> **Status:** Source of truth (supersedes `PRD_v2.md` + `PATTERN_MINER_SPEC.md`, which are archived under `docs/archive/`).
> **Drafted:** 2026-05-17
> **Owner:** huajiao
> **Repo root:** `/Users/oxjames/Downloads/CC_testing/花椒的content_2/`
> **Reference codebase to fork:** `/Users/oxjames/Downloads/CC_testing/花椒创业板/`
> **Hard constraint:** 严格遵守本文档 §11 的双模 backend，不引入 LangChain/向量库/云存储。

---

## 0. Decisions Resolved（all blocking conflicts closed）

| 决策 | 选择 | 备注 |
|---|---|---|
| 目标仓库 | content_2 | 在原地重构 |
| 参考蓝本 | 花椒创业板 fork-and-modify | 80% 模块直接搬，20% 新写（Pattern Miner） |
| LLM 后端 | dev=local claude CLI；prod=Moonshot kimi API | 走 `LLM_BACKEND` env 切换 |
| 浏览器后端 | Playwright headless（dev + prod 都用） | VPS Docker 跑 headless Chromium，dev 本机也走 headless 保持一致 |
| OpenCLI | 不引入生产依赖 | 仅做调研参考；其 Twitter GraphQL 逆向可阅读 |
| 图存储 | SQLite + 自建 edges 表 + 递归 CTE | 不引 Neo4j/FalkorDB |
| 调度 | 容器内 cron | 复用创业板的 entrypoint env 注入方案 |
| 告警 | Telegram/Discord webhook | 替换 PRD 中所有 `osascript` 引用 |
| 部署 | VPS Docker 独立运行 | Mac 仅作开发机，cookie scp 一次性注入 VPS |

---

## 1. Goals & Non-Goals

### 1.1 Goals
- 把 content_2 从「雪球→Discord 单管道」重构为「KOL 观察→爆款蒸馏→图谱编织→写作纠偏→真实回测」的自学习闭环
- 中文金融为主（70%）+ 泛流量穿插（30%，非政治非黄）
- 完全可在 VPS Docker 内独立运行；Mac 仅做开发与 cookie 初始化
- 90 天达 10k 真粉 + 1M 月曝光

### 1.2 Non-Goals
- Discord / XHS / 微博等其他平台发布
- 视觉素材生成（仍走 og:image 提取或 KOL 帖原图）
- 多账号管理（架构预留 `account_scope` 字段但不实现）
- 真正的图数据库（Neo4j/FalkorDB）
- 向量检索（Pinecone/Qdrant/pgvector）
- DSPy 自动优化 prompt（Phase 5+ 才考虑）
- 任何 GUI / Web UI 控制台

---

## 2. North Star Metrics

| 层级 | 指标 | 90 天目标 |
|---|---|---|
| 业务 | 真粉数（去僵尸） | 10,000 |
| 业务 | 月曝光 | 1,000,000 |
| 算法 | 平均 reply / 推文 | ≥ 5 |
| 算法 | 平均 profile click（估算） / 推文 | ≥ 20 |
| 算法 | 负反馈率（mute+block+report / 曝光） | ≤ 0.5‰ |
| 写作 | 反 AI 腔扫描首稿通过率 | ≥ 80% |
| 写作 | 爆款命中率（≥ 200 likes 占比） | ≥ 10% |
| 学习 | technique_entries 周新增 | ≥ 20 |
| 学习 | strategy_weights 周调整幅度 std | 第 4 周起 < 0.05 |

算法层指标全部对齐 X 开源算法权重（reply 27x / profile_click 12x / negfb -74x），不以裸阅读量为目标。

---

## 3. System Architecture

### 3.1 学习范式：Observe → Distill → Weave → Research（借鉴 WisMe.ai）

| 步骤 | 对应组件 | 数据 |
|---|---|---|
| Observe | `src/observers/` | KOL 推文/雪球达人/快讯 → `reaction_observations` |
| Distill | `src/miner/distiller.py` | viral observation → 结构化 `technique_entries` |
| Weave | `src/miner/weaver.py` | entries → 5 种边 → `technique_edges` |
| Research | `src/miner/retriever.py` | writer 写作时同步检索 Top-K |

### 3.2 组件拓扑

```
                       ┌─────────────────────┐
   cron 0 * * * *  ───▶│ run.sh observe      │  每小时抓信号
                       └──────┬──────────────┘
                              │
                              ▼ (末尾同步增量 distill)
   cron 0 16 * * *  ──▶┌─────────────────────┐
   (00:00 CST)         │ run.sh mine         │  夜间深加工
                       │ ▶ full_distill      │  当日 viral 全量
                       │ ▶ weaver.weave      │  建边
                       └──────┬──────────────┘
                              │
                              ▼
                       ┌─────────────────────┐
                       │ run.sh review       │  紧接 mine
                       │ ▶ metrics 抓取      │
                       │ ▶ viralScore 计算   │
                       │ ▶ strategy_weights  │
                       │ ▶ slop_words 回写   │
                       └─────────────────────┘

   cron 0 22-23,0-15/2 * * * ─▶┌──────────────────────┐
   (每 2h 发推 12 条/天)       │ run.sh post          │
                               │ ▶ select topic_lane  │
                               │ ▶ build fact_spine   │
                               │ ▶ retrieve Top-K     │
                               │ ▶ writer (CLI/API)   │
                               │ ▶ scorer (5 维度)    │
                               │ ▶ guardrails         │
                               │ ▶ publisher          │
                               └──────────────────────┘

   cron 0 4 * * 0    ──▶┌─────────────────────┐
   (周日 12:00 CST)     │ run.sh remine       │  全量校准
                        │ ▶ re-weave          │
                        │ ▶ recency_decay     │
                        │ ▶ prune low_score   │
                        └─────────────────────┘

   全部组件 SQLite (data/pepperbot.db) 串联：
   posts │ reaction_observations │ technique_entries │ technique_edges │
   retrieval_log │ post_metrics_timeseries │ strategy_weights │
   learning_log │ daily_stats │ circuit_breaker │ slop_words │ source_health
```

### 3.3 LLM + Browser 后端抽象

LLM 后端通过 `src/llm.py` 的 `call_llm(prompt, backend=env)` 函数统一封装，dev 走 claude_cli，prod 走 moonshot。Browser 后端只有一种：Playwright headless + cookie file 注入，dev/prod 一致。

---

## 4. Module Decomposition（12 Subagents）

### 4.1 File ownership

| # | Subagent | 文件 | 行数估 | Fork/新写 | 依赖 | Stop condition |
|---|---|---|---|---|---|---|
| S1 | DB & Migrations | `src/database.py`、`src/migrations/001_init.sql`、`src/migrations/002_pattern_miner.sql`、`src/migrations/runner.py` | 600+300+150+80 | fork+新写 | — | `init_db()` 建 12 张表无错；migrations 幂等 |
| S2 | Observer Protocol | `src/observers/base.py` | 80 | 新写 | — | `mypy --strict` 过；Observation round-trip JSON |
| S3 | LLM Adapter | `src/llm.py` | 280 | fork+改 | — | `call_llm("ping")` 在 claude_cli 与 moonshot 两个 backend 都返回非空 |
| S4 | Config & Persona | `config/sources.yaml`、`config/topic_blend.yaml`、`config/personas.yaml`、`config/compliance_lexicon.yaml`、`config/persona.md`、`config/kol_list_finance.yaml`、`config/kol_list_general.yaml` | 60+40+30+60+150+80+50 | 新写 | — | 全部 yaml 过 pydantic schema 校验 |
| S5 | Observers | `src/observers/x_list_adapter.py`、`xueqiu_adapter.py`、`news_flash_adapter.py`、`runner.py` | 220+280+180+120 | 新写+fork（xueqiu 沿用现有 `run.py` 抓取部分） | S2, S7 | 3 个 adapter 各 `fetch_latest()` 至少返回 1 条；写入 DB |
| S6 | Pattern Miner | `src/miner/__init__.py`、`viral_scorer.py`、`distiller.py`、`weaver.py`、`retriever.py`、`feedback.py` | 30+120+250+200+180+150 | 新写 | S1, S3 | 灌 5 条 mock viral → 5 条 entry → 3 条边 → retrieve k=5 < 200ms |
| S7 | Publisher & Browser | `src/twitter_bot.py`、`src/publisher.py`、`src/extract_ogimage.py` | 908+150+98 | fork 创业板 + 适配 headless | — | DRY_RUN 模式发 1 条带图测试推 + DB 有记录 |
| S8 | Voice / Templates | `voice/*.md`（含 slop_words.md reviewer 回写目标）、`templates/template_finance.md`、`hooks_finance.md`、`template_general.md`、`writer/SKILL.md` | — | 改写 | — | A 类 slop ≥ 30 项；金融合规段齐 |
| S9 | Writer + Scorer + Guardrails | `src/writer.py`、`scorer.py`、`guardrails.py` | 700+180+380 | fork 创业板 + 加 retrieve_techniques | S3, S6.retriever, S8 | fixture 输入产 ≤280 字推文；scorer 5 维度都 0-10；guardrails catch 1 条违例 |
| S10 | Reviewer | `src/reviewer.py` | 350 | 新写 | S1, S6, S7 | 灌 10 条 post + mock metrics → strategy_weights 表 ≥ 1 行更新；步长 ≤ 0.05 |
| S11 | Orchestrator | `src/main.py`、`src/run.sh` | 400+30 | 新写 | 全部 | `run.sh observe/post/mine/review/remine` 5 命令 exit 0 |
| S12 | Deployment | `Dockerfile`、`docker-compose.yml`、`scripts/entrypoint.sh`、`scripts/vps_setup.sh`、`scripts/cookie_sync.sh`、`.env.example`、`crontab.txt` | — | fork 创业板 + 改 | S11 | `docker compose up -d` 成功；cron + 第一次 observe 跑通 |

S2/S3/S4/S8 是叶子，可 day-0 并行启动。S1 是唯一真正的串行瓶颈。

### 4.2 集成点（Phase A 完成后所有人只读不改的 5 个契约文件）

1. `src/observers/base.py` — `SourceAdapter` Protocol + `Observation` dataclass
2. `src/database.py` 的 schema 部分 + `migrations/*.sql`
3. `src/miner/__init__.py` 暴露的 `retrieve()` 签名 + `RetrievalContext` dataclass
4. `src/llm.py` 的 `call_llm()` 签名
5. `config/sources.yaml`、`topic_blend.yaml`、`personas.yaml` 的 YAML schema

任何契约变更必须发"契约变更通知"，重新分发到所有下游 subagent。

### 4.3 并行死锁规避

| Pair | 处置 |
|---|---|
| S1 ↔ S6 | S6 不许改 .sql；schema 需求向 S1 提 issue |
| S5 ↔ S7 | 锁定 `twitter_bot.scrape_list_by_url(url, limit) -> list[dict]` 签名 |
| S9 ↔ S6 | S6 先 ship 返回 mock 的 retriever stub，S9 用 stub 开发 |
| S9 ↔ S8 | S8 先冻结文件名清单 |
| S10 ↔ S7 | S7 留 `get_post_metrics(post_id)` placeholder；S10 mock 开发 |
| S11 ↔ all | S11 最后做；所有模块 expose `run() -> int` 退出码统一 |

---

## 5. Interface Contracts（FROZEN AFTER PHASE A）

### 5.1 Data Classes

```python
# src/observers/base.py
from typing import Protocol, Literal
from dataclasses import dataclass
from datetime import datetime

AuthorTier = Literal[1, 2, 3]
SourceName = Literal["x_list_finance", "x_list_general", "xueqiu", "news_flash"]
TopicLane = Literal[
    "pre_market", "intraday", "post_market", "overnight",
    "general_tech_ai", "general_meme_career", "other",
]

@dataclass(frozen=True)
class Observation:
    source: SourceName
    author_handle: str            # 不带 @
    author_tier: AuthorTier
    content: str
    posted_at: datetime           # UTC
    likes: int
    retweets: int
    replies: int
    impressions: int | None       # X List 抓不到时为 None
    has_image: bool
    raw_url: str
    topic_hint: TopicLane | None

class SourceAdapter(Protocol):
    name: SourceName
    cookie_env_key: str
    rate_limit_per_hour: int

    async def fetch_latest(self, since: datetime) -> list[Observation]: ...
    async def health_check(self) -> bool: ...

# src/miner/__init__.py
@dataclass(frozen=True)
class TechniqueEntry:
    id: int
    observation_id: int
    hook_pattern: str
    hook_example: str
    syntax_signature: str
    sentence_len_avg: float
    sentence_len_p90: float
    stance_strength: int          # 0-5
    emotion_triggers: list[str]
    image_style: str
    post_hour_utc: int
    topic_lane: TopicLane
    applicable_personas: list[str]
    distilled_at: datetime
    success_score: float
    times_retrieved: int
    times_used_in_post: int
    recency_weight: float

@dataclass(frozen=True)
class RetrievalContext:
    topic_lane: TopicLane
    post_hour_utc: int
    persona: str
    fact_spine_keywords: list[str]
    avoid_recent_pattern_ids: list[int]
```

### 5.2 Database Schema（single source of truth）

完整 schema 见 `src/migrations/001_init.sql`（建表）和 `src/migrations/002_pattern_miner.sql`（Pattern Miner 增量）。本节列关键表。

```sql
-- posts（5 维 scorer + content_hash 去重）
CREATE TABLE posts (
  id INTEGER PRIMARY KEY,
  content TEXT NOT NULL,
  content_hash TEXT UNIQUE NOT NULL,
  topic_lane TEXT NOT NULL,
  persona TEXT NOT NULL,
  scheduled_for TIMESTAMP,
  posted_at TIMESTAMP,
  tweet_url TEXT,
  image_path TEXT,
  is_dry_run INTEGER DEFAULT 0,
  status TEXT DEFAULT 'pending',
  score_information INTEGER,
  score_stance INTEGER,
  score_counter INTEGER,
  score_hook INTEGER,
  score_compliance INTEGER,
  score_total INTEGER
);

CREATE TABLE reaction_observations (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  author_handle TEXT NOT NULL,
  author_tier INTEGER NOT NULL,
  content TEXT NOT NULL,
  posted_at TIMESTAMP NOT NULL,
  likes INTEGER NOT NULL,
  retweets INTEGER NOT NULL,
  replies INTEGER NOT NULL,
  impressions INTEGER,
  has_image INTEGER NOT NULL,
  raw_url TEXT NOT NULL UNIQUE,
  topic_hint TEXT,
  viral_score REAL NOT NULL,
  is_viral INTEGER NOT NULL,
  observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  distilled_at TIMESTAMP
);
CREATE INDEX idx_obs_viral ON reaction_observations(is_viral, distilled_at);

CREATE TABLE strategy_weights (
  topic_lane TEXT PRIMARY KEY,
  weight REAL NOT NULL,
  reason TEXT,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE learning_log (
  id INTEGER PRIMARY KEY,
  ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  window_days INTEGER,
  winning_patterns TEXT,
  losing_patterns TEXT,
  weights_before TEXT,
  weights_after TEXT,
  sample_size INTEGER
);

CREATE TABLE source_health (
  adapter_name TEXT PRIMARY KEY,
  last_success_at TIMESTAMP,
  consecutive_failures INTEGER DEFAULT 0,
  last_error TEXT,
  rate_limit_hit_at TIMESTAMP
);

CREATE TABLE circuit_breaker (
  scope TEXT PRIMARY KEY,
  tripped_at TIMESTAMP,
  reason TEXT,
  reset_after TIMESTAMP
);

CREATE TABLE slop_words (
  word TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  source TEXT
);

CREATE TABLE daily_stats (
  date TEXT PRIMARY KEY,
  posts_published INTEGER DEFAULT 0,
  observations_collected INTEGER DEFAULT 0,
  entries_distilled INTEGER DEFAULT 0,
  edges_woven INTEGER DEFAULT 0
);

-- 002_pattern_miner.sql
CREATE TABLE technique_entries (
  id INTEGER PRIMARY KEY,
  observation_id INTEGER NOT NULL REFERENCES reaction_observations(id),
  hook_pattern TEXT NOT NULL,
  hook_example TEXT NOT NULL,
  syntax_signature TEXT NOT NULL,
  sentence_len_avg REAL NOT NULL,
  sentence_len_p90 REAL NOT NULL,
  stance_strength INTEGER NOT NULL,
  emotion_triggers TEXT NOT NULL,
  image_style TEXT NOT NULL,
  post_hour_utc INTEGER NOT NULL,
  topic_lane TEXT NOT NULL,
  applicable_personas TEXT NOT NULL,
  distilled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  success_score REAL NOT NULL,
  times_retrieved INTEGER DEFAULT 0,
  times_used_in_post INTEGER DEFAULT 0,
  recency_weight REAL DEFAULT 1.0
);
CREATE UNIQUE INDEX idx_te_obs ON technique_entries(observation_id);
CREATE INDEX idx_te_lane_hour ON technique_entries(topic_lane, post_hour_utc);

CREATE TABLE technique_edges (
  id INTEGER PRIMARY KEY,
  src_entry_id INTEGER NOT NULL REFERENCES technique_entries(id),
  dst_entry_id INTEGER NOT NULL REFERENCES technique_entries(id),
  edge_type TEXT NOT NULL,
  weight REAL NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CHECK (src_entry_id < dst_entry_id),
  UNIQUE(src_entry_id, dst_entry_id, edge_type)
);
CREATE INDEX idx_edge_src ON technique_edges(src_entry_id, edge_type);

CREATE TABLE retrieval_log (
  id INTEGER PRIMARY KEY,
  post_id INTEGER REFERENCES posts(id),
  retrieved_entry_ids TEXT NOT NULL,
  context_signature TEXT NOT NULL,
  retrieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE post_metrics_timeseries (
  post_id INTEGER NOT NULL REFERENCES posts(id),
  collected_at TIMESTAMP NOT NULL,
  likes INTEGER NOT NULL,
  retweets INTEGER NOT NULL,
  replies INTEGER NOT NULL,
  impressions INTEGER,
  viral_score REAL NOT NULL,
  PRIMARY KEY (post_id, collected_at)
);
```

**写锁串行化**：所有 cron 入口在 Python 层包 `with FileLock("/tmp/pepperbot.lock")`；DB 层 helper `db.with_retry(fn, retries=3, backoff=0.2)` 处理 SQLITE_BUSY。

### 5.3 Module APIs

```python
# src/miner/viral_scorer.py（single source of truth for viralScore）
WEIGHT_LIKE = 0.5
WEIGHT_RETWEET = 1.0
WEIGHT_REPLY = 27.0
WEIGHT_PROFILE_CLICK = 12.0
WEIGHT_NEGATIVE_FEEDBACK = -74.0
PROFILE_CLICK_RATE_ESTIMATE = 0.02
NEGATIVE_FEEDBACK_RATE_ESTIMATE = 0.001

def viral_score(likes: int, retweets: int, replies: int,
                impressions: int | None) -> float: ...

def is_viral(score: float, author_p80: float) -> bool:
    """严格大于 p80 才算 viral；恰好相等返回 False"""

def author_p80(author_handle: str, window_days: int = 30) -> float:
    """历史不足 5 条时返回全局默认 50.0"""

# src/miner/__init__.py
def retrieve(ctx: RetrievalContext, k: int = 5) -> list[TechniqueEntry]: ...
def light_distill(observation_id: int) -> int | None: ...
def full_distill(since: datetime) -> list[int]: ...
def weave_nightly(new_entry_ids: list[int]) -> int: ...
def weave_full() -> tuple[int, int]: ...

# src/miner/db.py
def load_entry(entry_id: int) -> TechniqueEntry | None: ...
def upsert_entry(observation_id: int, fields: dict) -> int: ...
def upsert_edge(src: int, dst: int, edge_type: str, weight: float) -> bool: ...
def log_retrieval(ctx: RetrievalContext, ids: list[int]) -> None: ...
def increment_times_retrieved(ids: list[int]) -> None: ...

# src/miner/feedback.py
def apply_post_outcome(post_id: int,
                       outcome: Literal["top", "mid", "bottom"],
                       ema_alpha: float = 0.2) -> None: ...

# src/miner/weave_rules.py
EdgeType = Literal[
    "same_hook", "same_lane_diff_angle", "co_occurring_emotion",
    "temporal_chain", "cross_domain_bridge",
]
FINANCE_LANES = {"pre_market", "intraday", "post_market", "overnight"}
GENERAL_LANES = {"general_tech_ai", "general_meme_career"}

def compute_edges(a: TechniqueEntry, b: TechniqueEntry) -> list[tuple[EdgeType, float]]: ...
def iou(a: list[str] | None, b: list[str] | None) -> float:
    """两边均空返回 0.0；单边空返回 0.0"""
def is_cross_domain(lane_a: TopicLane, lane_b: TopicLane) -> bool: ...

# src/llm.py
def call_llm(prompt: str,
             *,
             model: str | None = None,
             backend: str | None = None,
             response_format: Literal["text", "json"] = "text",
             timeout: int = 90,
             max_retries: int = 1) -> str:
    """失败抛 LLMError；不做静默 fallback"""

# src/publisher.py
@dataclass
class PostResult:
    success: bool
    tweet_url: str | None
    error: str | None

async def post_tweet(text: str, image_path: str | None, dry_run: bool) -> PostResult: ...
async def get_post_metrics(tweet_url: str) -> dict: ...

# src/twitter_bot.py（fork 创业板）
async def scrape_list_by_url(self, list_url: str, max_posts: int = 30) -> list[dict]: ...
async def get_post_metrics(self, tweet_url: str) -> dict: ...
async def ensure_connected(self) -> None: ...
```

### 5.4 Config YAML Schemas

```yaml
# config/sources.yaml
adapters:
  - name: x_list_finance
    enabled: true
    list_url: "https://x.com/i/lists/{ID}"
    cookie_env_key: TWITTER_COOKIE_FILE
    rate_limit_per_hour: 12
    tier_default: 1
    max_posts_per_fetch: 30
  - name: x_list_general
    enabled: false                       # 待用户提供
    list_url: ""
    cookie_env_key: TWITTER_COOKIE_FILE
    rate_limit_per_hour: 12
    tier_default: 3
    max_posts_per_fetch: 30
  - name: xueqiu
    enabled: true
    feed_url: "https://xueqiu.com/v4/statuses/topic.json"
    cookie_env_key: XUEQIU_COOKIE_FILE
    rate_limit_per_hour: 24
    tier_default: 2
  - name: news_flash
    enabled: true
    sources: ["eastmoney_kuaixun"]
    rate_limit_per_hour: 30
    tier_default: 0

# config/topic_blend.yaml
default_daily_quota: 12
blend:
  pre_market:          { quota: 2, hours_utc: [22, 23, 0],  persona: finance_neutral }
  intraday:            { quota: 3, hours_utc: [2, 4, 6],    persona: finance_neutral }
  post_market:         { quota: 2, hours_utc: [8, 10],      persona: finance_contrarian }
  overnight:           { quota: 1, hours_utc: [15],         persona: finance_macro }
  general_tech_ai:     { quota: 2, hours_utc: [12, 14],     persona: general_observer }
  general_meme_career: { quota: 2, hours_utc: [13, 16],     persona: general_observer }
fallback_when_dry: general_tech_ai

# config/personas.yaml
personas:
  finance_neutral:    { description: "中性观察者",  stance_max: 4 }
  finance_contrarian: { description: "反共识",      stance_max: 5 }
  finance_macro:      { description: "宏观视角",    stance_max: 4 }
  general_observer:   { description: "金融人看世界", stance_max: 3 }

# config/compliance_lexicon.yaml
A_kill:    # 命中即 reject，不重写
  - 稳赚不赔
  - 必涨
  - 满仓干
  - 一定要买
B_warn:    # 命中加 -2 分；重复命中 reject
  - 抄底
  - 暴跌
  - 抛售
compliance_named_stock_threshold: 3   # 立场强度 > 3 且带具体股票代码 → reject
```

### 5.5 Environment Variables（Full List）

| Var | dev | prod | secret | 用途 |
|---|---|---|---|---|
| `LLM_BACKEND` | `claude_cli` | `moonshot` | no | LLM 后端切换 |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | — | no | claude CLI 模型 |
| `MOONSHOT_MODEL` | — | `kimi-k2-0905-preview` | no | moonshot 模型 |
| `MOONSHOT_API_KEY` | — | required | **yes** | moonshot key |
| `MOONSHOT_BASE_URL` | — | `https://api.moonshot.cn/v1` | no | |
| `CLAUDE_CLI_PATH` | `claude` | — | no | CLI 二进制 |
| `BROWSER_BACKEND` | `playwright_headless` | `playwright_headless` | no | 锁定 |
| `TWITTER_HANDLE` | required | required | no | 自己账号 |
| `TWITTER_COOKIE_FILE` | path | `/app/secrets/twitter_cookies.json` | **yes** | |
| `XUEQIU_COOKIE_FILE` | path | `/app/secrets/xueqiu_cookies.json` | **yes** | |
| `PEPPERBOT_ROOT` | repo path | `/app` | no | 根目录 |
| `DB_PATH` | `data/pepperbot.db` | `/app/data/pepperbot.db` | no | SQLite 路径 |
| `LOG_DIR` | `logs/` | `/app/logs/` | no | 日志目录 |
| `ALERT_WEBHOOK_URL` | optional | required | **yes** | Telegram/Discord webhook |
| `ALERT_CHANNEL` | `telegram` / `discord` | same | no | |
| `DRY_RUN` | `1` 或 unset | `0` | no | 不真发推 |
| `TZ` | host | `Asia/Shanghai` | no | 时区（cron 走 UTC） |
| `PLAYWRIGHT_BROWSERS_PATH` | — | `/ms-playwright` | no | image 内固定 |
| `PYTHONUNBUFFERED` | `1` | `1` | no | 日志立刻刷出 |

`.gitignore` 必须含：`secrets/`、`data/*.db`、`data/browser_session/`、`logs/`、`.env.local`、`tmp_*`。

---

## 6. Pattern Miner（详细）

### 6.1 Distill Prompt

每次喂 1 条 viral observation，严格 JSON 输出。完整 prompt 在 `src/miner/prompts/distill.txt`，关键字段：

```
hook_pattern: 反共识开场 | 数字暴击 | 场景代入 | 反问 | 金句压尾 | 对比悖论 | 身份代入
hook_example: 首句脱敏后原句（去人名/股票代码/币种名）
syntax_signature: short_comma_no_period | long_run_on | stacked_short | dialog_style
sentence_len_avg: int
sentence_len_p90: int
stance_strength: 0-5
emotion_triggers: ["FOMO" | "嘲讽" | "认知优越" | "焦虑" | "共情" | "猎奇" | "愤怒"] (最多 3)
image_style: kline_with_doodle | screenshot | meme | chart | photo | none
topic_lane: pre_market | intraday | post_market | overnight | general_tech_ai | general_meme_career | other
applicable_personas: [finance_neutral | finance_contrarian | finance_macro | general_observer]
```

校验：
- `jsonschema` 严格校验；不在白名单的枚举值 → entry 作废
- `hook_example` 含 `@` 或股票代码正则（`60\d{4}|00\d{4}|30\d{4}|68\d{4}|HK\d{4}|[A-Z]{1,5}`） → entry 作废
- JSON parse 失败 → retry 1 次 → 失败仍写 `distilled_at=NOW` 防雪崩

### 6.2 Weave 算法 & 5 种边

5 种边类型 truth table（在 `src/miner/weave_rules.py` 中实现为纯函数）：

| edge_type | 触发条件 | weight |
|---|---|---|
| `same_hook` | a.hook_pattern == b.hook_pattern | 1.0 |
| `same_lane_diff_angle` | a.topic_lane == b.topic_lane AND a.hook_pattern != b.hook_pattern | 0.7 |
| `co_occurring_emotion` | iou(a.emotion_triggers, b.emotion_triggers) > 0.5 | = iou 值 |
| `temporal_chain` | a.author == b.author AND \|a.posted_at - b.posted_at\| < 48h | 0.8 |
| `cross_domain_bridge` | is_cross_domain(a.lane, b.lane) AND a.syntax_signature == b.syntax_signature | 0.9 |

`weave_nightly()` 候选池：`recency_weight > 0.3`（约 1 月内）。对称边强制 `src_id < dst_id`（schema CHECK 约束），UPSERT 保证不增重。

`weave_full()`（周日）：
- `recency_weight *= 0.93`（约 14 天半衰期）
- 删除 `success_score < p20 AND times_used_in_post == 0` 的 entries 及其边

### 6.3 Retriever SQL（同步 ≤ 200ms）

```sql
WITH lane_hits AS (
  SELECT id, success_score, recency_weight, hook_pattern, hook_example,
         syntax_signature, stance_strength, emotion_triggers
  FROM technique_entries
  WHERE topic_lane = :lane
    AND ABS(post_hour_utc - :hour) <= 2
    AND :persona IN (SELECT value FROM json_each(applicable_personas))
    AND id NOT IN (SELECT value FROM json_each(:avoid_json))
),
bridge_hits AS (
  SELECT te.id, te.success_score * 0.7 AS success_score, te.recency_weight,
         te.hook_pattern, te.hook_example, te.syntax_signature,
         te.stance_strength, te.emotion_triggers
  FROM technique_edges ed
  JOIN technique_entries te ON te.id = ed.dst_entry_id
  WHERE ed.edge_type = 'cross_domain_bridge'
    AND ed.src_entry_id IN (SELECT id FROM lane_hits)
)
SELECT * FROM (
  SELECT * FROM lane_hits
    ORDER BY success_score * recency_weight DESC LIMIT :k_main
  UNION ALL
  SELECT * FROM bridge_hits
    ORDER BY success_score * recency_weight DESC LIMIT :k_bridge
);
```

`k_main = int(k * 0.9)`、`k_bridge = max(1, int(k * 0.1))`（k=5 时分别 4 和 1）。
执行后写 `retrieval_log` + `increment_times_retrieved`（best-effort）。

### 6.4 Cold Start

| Day | Entries 量 | Retriever 行为 |
|---|---|---|
| 0-3 | < 50 | 100% 回退到 `templates/hooks_finance.md` 静态模板（内存 dict filter） |
| 4-14 | 50-100 | 50% retrieved + 50% 静态模板 |
| 15-30 | > 100 但 sample_size 不足 | 100% retrieved，按 recency 排序 |
| 30+ | 正常态 | success_score × recency_weight 排序 |

**Seed Pool**：用户提供 5-10 条历史爆款 → `scripts/seed_techniques.py` 跑 distiller 灌入 → 加速到 Day 4 状态。
**静态模板的 entry_id**：负数空间（-1, -2…）持久化到 technique_entries。

### 6.5 与 Reviewer 协同（学习闭环）

`reviewer.update_weights()` 每晚做：

1. 读 `retrieval_log` × `post_metrics_timeseries`：
   - post viralScore ∈ 当周 top 30% → retrieved entries `apply_post_outcome(id, "top")`，EMA 上调 success_score
   - post viralScore ∈ bottom 30% → `apply_post_outcome(id, "bottom")`，EMA 下调
2. 计算每个 hook_pattern 累积 success_rate：
   - `sample_size ≥ 5` 才进 strategy_weights
   - 累计 success_rate bottom 20% 且 `sample_size ≥ 10` 的 hook_pattern → 写进 slop_words 表 + 渲染回 `voice/slop_words.md`

权重约束：
- `MIN_SAMPLE_FOR_WEIGHT_UPDATE = 5`
- `MAX_WEIGHT_STEP_PER_REVIEW = 0.05`
- 更新后所有 lane 权重归一化为 1.0
- 更新前后 weights snapshot 写 learning_log

---

## 7. Writer Pipeline

### 7.1 三段式（fork 创业板）

```
build_fact_spine(news, observations)
  → build_angle_card(fact_spine, reaction_pack, retrieved_techniques)
    → draft(angle_card, persona)                    # call_llm
      → audit_for_template(draft)                   # A 类 slop 扫描
        → guardrails.check(draft, persona)          # 重写循环最多 3 次
          → scorer.score(draft)                     # 5 维度
            → 通过则进 publisher
```

新增：`writer.retrieve_techniques(ctx)` 调用 `miner.retrieve()`，结果注入 angle_card。
删除：`learner.get_learned_techniques()` legacy shim。

### 7.2 Scorer 5 维度

| 维度 | 0-10 | 评分依据 |
|---|---|---|
| 信息密度 | 0=空话 / 10=每句有事实 | LLM 评分 |
| 立场强度 | 0=骑墙 / 10=直接结论 | LLM 评分 |
| 反共识度 | 0=人云亦云 / 10=有独到角度 | LLM 评分 |
| 钩子强度 | 0=开头平淡 / 10=首句抓人 | LLM 评分 |
| 合规安全度 | 0=触红线 / 10=完全安全 | guardrails 已 reject → 0；否则 LLM 评分 |

阈值 `pass = score_total ≥ 60`（满分 100），阈值在 config，不硬编码。

### 7.3 Guardrails

- A 类（直接 reject）：`compliance_lexicon.yaml#A_kill` + `voice/slop_words.md`
- B 类（警告+重写）：`compliance_lexicon.yaml#B_warn`
- 立场强度 > 3 且带具体股票代码 → reject
- 政治 / 黄 / 宗教关键词 → reject（独立子库 `config/political_lexicon.yaml`）
- 最多重写 3 次，超限抛 `GuardrailsExhausted`，写 circuit_breaker
- 词库文件缺失 → 抛 FileNotFoundError，**不静默放行**

---

## 8. Twitter Algorithm Alignment

| 算法事实（X 开源 + punk2898） | 系统层落地 |
|---|---|
| Reply 27x，Like 0.5x | scorer 钩子强度权重最高；writer prompt 强制结尾抛二选一 / 反共识问 |
| Profile click 12x | 配图必发；首句留悬念 |
| Negative feedback -74x | guardrails 拦截标题党 / 情绪宣泄 / 人身攻击 |
| 30 分钟早期互动窗口 | 发帖时间贴目标受众上线前 5-10 分钟 |
| In-network RealGraph | KOL 评论 loop 每 slot 跑（Phase 4+） |
| 主推带链接降权 | publisher 检测正文含 http → 自动改成「正文 + 首条 reply 放链接」 |
| 长推阅读时长权重高 | 每日 quota 含 1 条长推（>500 字），走 X Article 接口 |

---

## 9. Content Strategy

70% 金融 / 30% 泛流量。泛流量必须**从金融人视角切入**（"作为做 A 股的看 AI"）。

不碰：政治、宗教、性别对立、黄、宏观政策直接评价。
可碰：AI 工具、码农段子、职场观察、书评、历史、城市生活。

quota 配置见 `config/topic_blend.yaml`（§5.4）。

---

## 10. Cron Schedule & Orchestration

```cron
# 容器内 /etc/cron.d/pepperbot (UTC)
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
0 *  * * *           root flock -n /tmp/observe.lock /app/src/run.sh observe >> /app/logs/observe.log 2>&1
0 22-23,0-15/2 * * * root flock -n /tmp/post.lock    /app/src/run.sh post    >> /app/logs/post.log    2>&1
0 16 * * *           root flock -n /tmp/mine.lock    /app/src/run.sh mine    >> /app/logs/mine.log    2>&1 && \
                          flock -n /tmp/mine.lock    /app/src/run.sh review  >> /app/logs/review.log  2>&1
0 4  * * 0           root flock -n /tmp/mine.lock    /app/src/run.sh remine  >> /app/logs/remine.log  2>&1
```

`run.sh <command>` 通过 `python -m src.main <command>` 分发。每个命令独立解耦。

---

## 11. Deployment

### 11.1 Dev vs Prod 对照

| 维度 | Dev (macOS) | Prod (VPS Docker) |
|---|---|---|
| LLM 后端 | `claude_cli` | `moonshot` |
| 浏览器 | Playwright headless 本机 Chromium | Playwright headless 容器内 Chromium |
| Cookie | mac 本地 json 文件 | scp 到 VPS → `secrets/*.json` 挂只读卷 |
| Cron | 手动跑 `python -m src.main <cmd>` 验证 | 容器内 cron 自动跑 |
| 数据库 | `./data/pepperbot.db` | `./data:/app/data` 卷挂载 |
| 告警 | `print()` 到 stdout | Telegram/Discord webhook |
| DRY_RUN | `1`（默认不真发） | `0` |

### 11.2 Dockerfile（fork 创业板）

基底 `mcr.microsoft.com/playwright:v1.49-noble`（自带 Chromium + CJK 字体 + 所有依赖）。

### 11.3 docker-compose.yml

```yaml
services:
  pepperbot:
    build: .
    container_name: pepperbot
    restart: unless-stopped
    shm_size: '512m'
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./tmp_images:/app/tmp_images
      - ./secrets:/app/secrets:ro
    env_file:
      - .env
    environment:
      - TZ=Asia/Shanghai
      - PYTHONUNBUFFERED=1
      - LLM_BACKEND=moonshot
      - BROWSER_BACKEND=playwright_headless
      - DRY_RUN=0
    mem_limit: 2g
```

### 11.4 Cookie Lifecycle

```
1. dev: 用户 Mac 上手动登录 Twitter + 雪球
2. 导出 cookie:
   - Twitter: 装 EditThisCookie 扩展 → 导出 JSON
   - 雪球: 同上
3. 放 ./secrets/twitter_cookies.json + ./secrets/xueqiu_cookies.json (不入仓)
4. scripts/cookie_sync.sh: rsync 到 VPS:/path/to/repo/secrets/
5. docker compose restart pepperbot  → Playwright context.add_cookies()
6. publisher 检测登录态失败 → consecutive_failures += 1 → 触发告警
   ALERT: "Twitter cookie 失效，请重新导出注入" → webhook
7. 每 30 天用户手动跑 cookie_sync.sh
```

### 11.5 Alerting

`src/alerting.py` 单一函数：

```python
def alert(level: str, message: str, context: dict | None = None) -> None: ...
# level: info / warn / error / critical
# critical 触发: source_health.consecutive_failures >= 3, cookie expired,
#                guardrails exhausted, publisher 连续失败 3 次, circuit_breaker tripped
# 渠道: Telegram (ALERT_CHANNEL=telegram) 或 Discord
```

完全替换 spec 之前所有 `osascript` 引用。

### 11.6 DB Migration Policy

- `src/migrations/` 目录，文件名 `NNN_description.sql`，按字典序执行
- `src/migrations/runner.py` 在 `init_db()` 时跑，记录到 `schema_migrations` 表
- 每次 schema 变更 → 新建 `NNN+1_xxx.sql`，**不改老文件**
- 容器启动 entrypoint 自动跑 runner
- 重大变更（DROP TABLE / 改主键）先在 dev 手动验证 + 备份 `data/pepperbot.db.bak.YYYYMMDD`

### 11.7 创业板 Dockerfile 改动点

| 创业板原版 | 本项目 |
|---|---|
| cron 跑 `periodic2h` + `review` | 改为 `observe / post / mine / review / remine` 5 条 |
| `run_slot_vps.sh slotN` | 改为 `run.sh <command>` |
| `LLM_BACKEND=moonshot` 默认 | 保持 |
| 单个 Chromium profile | 保持 |
| 无 flock | 加 cron 每行 `flock -n /tmp/<cmd>.lock` |
| osascript 告警 | webhook |

---

## 12. Testing Strategy

### 12.1 金字塔

| 层 | 定义 | 数量目标 | 触发 |
|---|---|---|---|
| Unit | 纯函数 + 算法 + DB 层，全部外部依赖 mock | 50-80 | pre-commit hook（≤30s） |
| Integration | 多模块协作 + 真实 SQLite（tempfile），不碰 Chrome/CLI/网络 | 15-25 | 手动 `make test`（≤90s） |
| Smoke | 完整 happy path，mock LLM + mock browser，真 SQLite + 真 guardrails 词库 | 1-3 | Phase 升级前手动 |
| E2E | 真 Chrome + 真 LLM + 真网络；`DRY_RUN=1` 拦截发送 | 0 | 仅手动，绝不 CI |

### 12.2 Unit Test 重点 case

- **observers**：cookie 过期 / rate limit / 白名单过滤 / `impressions=None` 合法
- **viral_scorer**：标准算法值；p80 边界（恰好相等返 False）；负反馈不致 NaN
- **distiller**：合法/非法 JSON；triple-backtick 包裹；未知 persona；未脱敏 hook_example；幂等
- **weave (compute_edges)**：5 种边 truth table；IoU 边界 (0.5 vs 0.49)；时间差 (47h vs 49h)
- **retriever**：空 DB；avoid_recent；hour 容差；bridge 配额；200ms 性能（500 entries fixture）
- **writer**：empty retrieved；>280 字；无图；A 类 slop；guardrails 3 次重写超限
- **scorer**：维度 clamp；权重归一化；纯函数 deterministic
- **guardrails**：A/B 类优先级；立场强度+股票代码；词库缺失抛异常（不静默放行）
- **publisher**：DRY_RUN；content_hash 24h 去重；URL 自动放 reply
- **reviewer**：sample 不足不更新；步长 ≤ 0.05；归一化；EMA 上下调

### 12.3 Integration 场景

1. 冷启动 0 entries → retrieve 返回空 → writer fallback 静态模板
2. 正常态 500 entries → Top-K 排序正确 → bridge ≤ 10%
3. 连续失败 3 次熔断 → source_health 写入 → observer 停发推
4. 双通道学习：bottom 20% hook → slop_words.md 被修改
5. DB migration 幂等：跑两次 `init_db()` 不报错

### 12.4 Smoke

```
fixture xueqiu JSON → observer.ingest → DB
  → mock light_distill (返回固定 entry JSON)
  → writer (mock LLM 返回固定推文)
  → scorer (真) → guardrails (真词库)
  → publisher (mock post_tweet)
  → 断言: posts 表 1 条 / topic_lane 非空 / retrieval_log 1 条 /
         source_health.consecutive_failures 未增 / 字数 ≤ 280 / 无 A 类 slop
```

Mock：claude CLI subprocess、Playwright、雪球/财联社 HTTP。
不 Mock：SQLite（tempfile）、scorer、guardrails 词库加载、viral_score。

### 12.5 Fixtures

```
tests/fixtures/
├── observations/
│   ├── xueqiu_20260101.json
│   ├── x_list_finance_20260101.json
│   └── bulk_500.json
├── llm/
│   ├── distill_valid.json
│   ├── distill_invalid_json.txt
│   ├── distill_bad_persona.json
│   └── draft_finance.txt
└── db/
    └── seed_50_entries.sql
```

确定性保障：手工写定 + `posted_at` 固定时间戳 + LLM mock patch 函数层（不依赖网络录像）。
引入库：`pytest-asyncio`、`pytest-timeout`、`jsonschema`、`responses`（HTTP mock）。

### 12.6 CI / 触发规则

```
# .git/hooks/pre-commit
pytest tests/unit/ -x -q --timeout=10            # 不允许 --no-verify

# 手动推送前
pytest tests/unit/ tests/integration/ -q --timeout=30

# Phase 升级前
pytest tests/smoke/ -v --timeout=60

# VPS 部署后
docker compose exec pepperbot python -m src.main test
```

### 12.7 人工 Sanity（每周一）

- Top-10 success_score entries 句式是否真的好
- learning_log 权重调整方向与真实互动正相关
- slop_words 回写内容合理（防止误伤好句式）

---

## 13. Multi-Agent Review（Phase E）

5 个并行 review agent，独立 dimension，输出 `findings.md`。

### 13.1 Security checklist
1. guardrails 覆盖所有 A 类 slop
2. compliance lexicon 含"建议买入"/"稳赚"/"必涨"
3. publisher content_hash 24h 去重
4. SQLite 写入用事务 + 备份
5. source_health 熔断逻辑
6. technique_entries 爆款原句脱敏
7. observer KOL 白名单
8. cron `set -e`

### 13.2 Performance checklist
1. retriever 索引齐全，500 entries < 200ms
2. weave 候选池裁剪 (recency > 0.3)
3. full_distill 单条 CLI > 60s 自动 kill
4. DB 连接 with/try-finally 关闭
5. observer adapter 并发执行（asyncio.gather）
6. post_metrics_timeseries 月增量 + VACUUM 计划

### 13.3 Architecture checklist
1. SourceAdapter Protocol 全实现（mypy --strict）
2. run.sh 5 命令真解耦
3. publisher 是薄封装
4. 冷启动降级用策略模式
5. strategy_weights 更新前备份
6. Distill/Weave 幂等被真实测试覆盖

### 13.4 Test Coverage checklist
1. compute_edges 5 边 truth table 全覆盖（含边界）
2. 熔断单测
3. reviewer sample 不足 test
4. guardrails 3 次重写超限 test
5. smoke 真实 DB 写入断言（不只是"没抛异常"）
6. slop_words 双通道回写 integration

### 13.5 Observability checklist
1. source_health 每个失败路径都写
2. 日志结构化（slot_name / observation_count）
3. retrieval_log 信息足以重现 writer 看到的 Top-K
4. learning_log 记录权重前后 snapshot
5. cron 执行时间记录（start/end/duration）
6. circuit_breaker 触发原因字符串化

---

## 14. Risks & Mitigations

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| Twitter 反爬升级 | 中 | 高 | adapter 抽象层；source_health 监控；保留 nitter 备选 |
| 雪球 cookie 过期 | 高 | 中 | 检测连续失败 → webhook 告警 |
| Moonshot 限流 / 价格波动 | 中 | 中 | distill 排队到 nightly batch；writer fallback 静态模板 |
| Chromium OOM | 中 | 中 | shm_size 512m + mem_limit 2g + restart unless-stopped |
| Distill JSON 不稳定 | 中 | 高 | jsonschema 校验 + retry 1 次 + 失败丢弃 |
| 图谱"爆款回响"同质化 | 高 | 中 | avoid_recent_patterns 排除 7 天用过；weekly remine 检测集中度 |
| cross_domain_bridge 写飞人设 | 中 | 高 | 10% 配额硬限 + persona 白名单 + scorer 合规维度兜底 |
| KOL 抓到广告号 | 中 | 高 | kol_list_*.yaml 白名单 + author_tier 限只学 tier 1/2 |
| 学习闭环自举放大 slop | 中 | 高 | 权重步长 ≤ 0.05；每周人工 sanity；sample_size 门槛 |
| VPS 单点故障 | 低 | 中 | 数据 rsync 回 Mac 每日 backup |

---

## 15. Phase Rollout

| Phase | 工作 | 时长 | 退出条件 |
|---|---|---|---|
| **A. 冻结契约** | 5 个契约文件落地：observers/base.py、database.py schema、miner/__init__.py、llm.py、config/*.yaml | 2 天 | mypy --strict 过；example yaml 通过 pydantic 校验 |
| **B. 清理 + Fork** | 删 src/run.py / discord_poster.py / broken tests；从创业板 fork S7 (twitter_bot/publisher) + S9 (writer/scorer/guardrails) 骨架 | 1 天 | 仓库结构对齐 §4 目录；首次 docker compose build 成功 |
| **C. 12 subagent 并行实现** | S1-S12 按 §4.1 表并行；每模块自带 unit test | 7-10 天 | 各 stop condition 全通过；unit + integration suite 通过 |
| **D. Smoke + 多 agent review** | 跑 1-3 个 smoke；5 个 review agent 并行扫 §13 | 2 天 | findings 全 close 或显式 wontfix |
| **E. VPS 上线** | scp cookies → docker compose up → cron 跑 24h → review logs | 2 天 | observe ≥ 24 次写 reaction_observations；post ≥ 12 次（dry_run=1） |
| **F. 真发推 + 学习闭环** | 切 DRY_RUN=0，连续 7 天监控；每周一人工 sanity | ongoing | 第 4 周 strategy_weights std < 0.05 |

总工期估算：14-17 天到 Phase E 完成。

---

## Appendix A — Glossary

- **Observation**：一条 KOL 推文或雪球达人帖的结构化记录
- **TechniqueEntry**：从 viral observation 蒸馏出的"为什么爆"的结构化技法
- **TechniqueEdge**：两个 entry 之间的关联（5 种类型）
- **RetrievalContext**：writer 写作时的检索请求，含 lane/hour/persona/avoid
- **viralScore**：reply×27 + retweet×1 + like×0.5 + profile_click_est×12 + negfb_est×(-74)
- **topic_lane**：内容赛道（pre_market / intraday / general_tech_ai 等）
- **persona**：人设变体（finance_neutral / finance_contrarian / finance_macro / general_observer）
- **fact spine**：写作前的事实骨架（来自 news_flash + reaction_pack）
- **angle card**：fact spine + reaction pack + retrieved techniques 合成的角度卡，喂给 draft
- **reaction pack**：最近 8h KOL 反应的 LLM 蒸馏结果（实时性）
- **technique library**：长期沉淀的爆款规律（持久性，本文档核心）

## Appendix B — Algorithm Constants（v1）

```
# src/miner/viral_scorer.py
WEIGHT_LIKE = 0.5
WEIGHT_RETWEET = 1.0
WEIGHT_REPLY = 27.0
WEIGHT_PROFILE_CLICK = 12.0
WEIGHT_NEGATIVE_FEEDBACK = -74.0
PROFILE_CLICK_RATE_ESTIMATE = 0.02
NEGATIVE_FEEDBACK_RATE_ESTIMATE = 0.001

# src/miner/retriever.py
TOP_K_TECHNIQUES = 5
BRIDGE_QUOTA_RATIO = 0.1
HOUR_WINDOW = 2

# src/miner/weaver.py
RECENCY_HALFLIFE_DAYS = 14
RECENCY_DECAY_FACTOR = 0.93        # 周衰减
PRUNE_THRESHOLD_PERCENTILE = 20

# src/reviewer.py
MIN_SAMPLE_FOR_WEIGHT_UPDATE = 5
MAX_WEIGHT_STEP_PER_REVIEW = 0.05
EMA_ALPHA = 0.2

# src/writer.py
MAX_TWEET_LENGTH = 280
MAX_REWRITE_ATTEMPTS = 3
SCORE_PASS_THRESHOLD = 60
```

## Appendix C — 环境变量 dev/prod 完整清单

见 §5.5。

## Appendix D — 静态 fallback 模板 schema

`templates/hooks_finance.md` 用 markdown 表格，retriever 启动加载到内存：

```
| hook_pattern | hook_example | topic_lane | post_hour_utc | persona | stance_strength |
|---|---|---|---|---|---|
| 反共识开场 | 今天所有人都看好 X 但有个细节没人提 | pre_market | 22 | finance_neutral | 4 |
| 数字暴击 | 三个数字告诉你 Y 的真相 | intraday | 4 | finance_macro | 3 |
```

种子条目至少 20 条/lane，覆盖全部 4 个金融 lane + 2 个泛流量 lane。

## Appendix E — 相对 PRD_v2 + PATTERN_MINER_SPEC 的 diff

| 项 | 旧 | 新 |
|---|---|---|
| 文档数 | 2 份 | 1 份（旧文件归档） |
| Subagent 拆分 | 5-6 模糊 | 12 个明确（S1-S12，文件归属表） |
| LLM 后端 | 仅 claude CLI | dev=claude CLI / prod=Moonshot 双模 |
| 浏览器后端 | Chrome MCP + window.name | Playwright headless 单模 |
| Docker / VPS | 仅提及 | 完整 §11 |
| 接口契约 | 散落 | §5 统一 + Phase A 冻结清单 |
| 函数签名 | 大量缺失 | §5.3 全部明确 |
| YAML schema | 仅模糊提及 | §5.4 完整 |
| 环境变量 | 局部 | §5.5 + Appendix C 完整 |
| 测试 | 4 行 | §12 完整金字塔 + fixture 规格 |
| 多 agent review | 未定义 | §13 5 dimension 各 8-10 checklist |
| 算法常量 | 散落 | Appendix B 统一 |
| 静态 fallback schema | 未定义 | Appendix D |
| osascript 告警 | 全文 | 全部替换为 webhook |
| light_distill 进程模型 | 不明 | 删除概念，统一为 observer 末尾同步增量 distill |
| 对称边去重 | 未定义 | CHECK 约束 src_id < dst_id |
| SQLite 写锁 | 未定义 | cron 每行 flock + db.with_retry |

---

**End of UNIFIED SPEC v1.** 等用户 review 拍板后进入 Phase B（清理 + fork）。

---

# §16. AMENDMENTS v1.1 (2026-05-17 第二轮 review)

本节列出对 §1-§15 的所有 amend。如有冲突，**本节为准**。

## 16.1 新增子系统 S13: Discord 审批闸门

**Why**: 不能让 AI 直接发推，要人工筛选保账号安全 + 品控。Discord 是 UI，DB 是真相源。

**S13 文件归属**:
- `src/discord/bot.py` — Discord 反应轮询器（每 5min cron）
- `src/discord/publisher_callback.py` — 收到 ✅ 后调 publisher
- `src/discord/rejection_pool.py` — ❌ 候选进"人工拒绝池"

**State machine（覆盖 §4.1 隐含的简单发推流程）**:
```
candidate
  → pushed_to_discord (记 discord_message_id)
    → reviewed
       ├─ ✅ approved → published (记 tweet_url)
       │             → metrics_collected (24/48/72h)
       │             → learned (回调 miner.feedback)
       ├─ ❌ rejected → in_rejection_pool (留作分析)
       └─ 🔄 revise → 重新生成 → 新 candidate
```

**新增表**:
```sql
CREATE TABLE drafts (
  id INTEGER PRIMARY KEY,                          -- draft_id
  content TEXT NOT NULL,
  content_length INTEGER NOT NULL,
  content_mode TEXT NOT NULL,                      -- insight | meme | emotional
  optimal_length TEXT NOT NULL,                    -- short | medium | long
  topic_lane TEXT NOT NULL,
  persona TEXT NOT NULL,
  pattern_ids TEXT NOT NULL,                       -- JSON: 用到的 technique_entry ids
  source_observation_ids TEXT NOT NULL,            -- JSON: 触发的 obs ids
  image_path TEXT,
  generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  status TEXT DEFAULT 'candidate',                 -- candidate | pushed_to_discord | approved | rejected | published | metrics_collected | learned
  discord_message_id TEXT,
  discord_reaction TEXT,                           -- ✅ ❌ 🔄
  discord_reacted_at TIMESTAMP,
  tweet_url TEXT,
  posted_at TIMESTAMP,
  cross_referenced INTEGER DEFAULT 0               -- 1 = 6h 小号扫到补绑
);
CREATE INDEX idx_drafts_status ON drafts(status);
CREATE INDEX idx_drafts_discord_msg ON drafts(discord_message_id);
```

`posts` 表（§5.2）现在改为 `drafts` 表的**视图**：`SELECT * FROM drafts WHERE status IN ('published', 'metrics_collected', 'learned')`。或保留 `posts` 表作为 published 后的快照，由 publisher 在切 `published` 状态时同步插入。**选后者**（独立表更容易回滚 + 简化 reviewer 查询）。

## 16.2 新增子系统 S14: 选题引擎

**Why**: 之前只学"怎么写才爆"，没学"写什么才爆"。Topic-level virality predictor 补这个空缺。

**S14 文件归属**:
- `src/selector/__init__.py`
- `src/selector/topic_scorer.py` — 给候选选题打分
- `src/selector/virality_predictor.py` — LLM 判断"这个选题有没有评论区讨论潜力"
- `src/selector/db.py`

**调度**: 不独立 cron。**observer 每小时跑完后顺手跑**（数据池就是 observer 刚抓的，紧耦合不浪费）。

**新增表**:
```sql
CREATE TABLE topic_candidates (
  id INTEGER PRIMARY KEY,
  generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  source_observations TEXT NOT NULL,               -- JSON: 触发的 obs ids
  topic_summary TEXT NOT NULL,                     -- LLM 一句话提炼这个 topic 是什么
  virality_score REAL NOT NULL,                    -- 0-100
  predicted_content_mode TEXT,                     -- insight | meme | emotional
  predicted_length TEXT,                           -- short | medium | long
  predicted_topic_lane TEXT,
  kol_reaction_count INTEGER,                      -- 几个 tier-1 KOL 在反应
  emotional_intensity REAL,                        -- 0-1
  debate_potential REAL,                           -- 0-1
  status TEXT DEFAULT 'fresh',                     -- fresh | consumed | expired
  consumed_at TIMESTAMP,
  consumed_by_draft_id INTEGER REFERENCES drafts(id)
);
CREATE INDEX idx_topic_fresh ON topic_candidates(status, virality_score DESC);
```

**virality_score 评分维度** (LLM prompt 在 `src/selector/prompts/score.txt`):
- 多少 tier-1 KOL 在反应（engagement gradient）
- 是否匹配历史 top success_score 的 hook_pattern
- 情绪触发强度
- 评论区分歧潜力（争议越大讨论越多）
- 是否有干货/搞笑/情绪三种角度可切

**Writer 改动**: §7.1 流程改为：
```
1. selector.pick_top_topic() → 拿 1 条 fresh 且 virality_score 最高的 topic_candidate
2. miner.retrieve(ctx) → 按 topic_candidate.predicted_* 字段构造 ctx
3. build_fact_spine + angle_card + draft (原流程)
4. mark topic_candidate.status='consumed' + 记 draft_id
```

## 16.3 6 小时 cross-reference monitor (新增到 S5 Observers)

**Why**: emoji 自动发布可能失败 / 用户在 X 客户端手动发了 / 系统外野生推文。需要兜底把 tweet_url 绑回 draft_id。

**实现**: `src/observers/self_monitor_adapter.py`（用小号 cookie）
- 每 6h 跑（独立 cron）
- 抓自己大号 timeline 最近 48h 推文（每条得到 tweet_url + text + posted_at）
- 对每条 tweet 计算 content_hash
- 在 drafts 表查相同 content_hash 但 tweet_url 为空的行 → 补 tweet_url + 设 `cross_referenced=1`
- 找不到匹配 → 写 `wild_posts` 表（你手动发的非系统内容，不进学习库）

**新增表**:
```sql
CREATE TABLE wild_posts (
  tweet_url TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  posted_at TIMESTAMP NOT NULL,
  discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Cron** (§10 追加):
```
0 */6 * * *  root flock -n /tmp/selfmon.lock /app/src/run.sh self_monitor >> /app/logs/selfmon.log 2>&1
```

## 16.4 4 类反馈状态机 (覆盖 §6.5)

`reviewer.update_weights()` 现在处理 4 种 case：

| Case | trigger | 处理 |
|---|---|---|
| ✅ 选了 + 爆了 | status=learned AND viral_score > weekly_p70 | pattern + topic + time + author **全部加权** (EMA α=0.3) |
| ✅ 选了 + 哑炮 | status=learned AND viral_score < weekly_p30 | **小幅降权** (EMA α=0.1, 力度比加权小) |
| ❌ 没选 + 系统打分高 | status=rejected AND scorer.score_total > 70 | 写 `human_rejection_pool` 表，**留作分析不直接惩罚** |
| 🔄 连续哑炮 | 同一 pattern 连续 3 次 viral_score < p30 | 进 **cooling list**（7 天不被 retriever 选中），不永久封杀 |

**新增表**:
```sql
CREATE TABLE human_rejection_pool (
  id INTEGER PRIMARY KEY,
  draft_id INTEGER REFERENCES drafts(id),
  scorer_score INTEGER NOT NULL,
  pattern_ids TEXT NOT NULL,
  rejected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  reason TEXT                                       -- 用户可选填，默认 NULL
);

CREATE TABLE pattern_cooling (
  pattern_id INTEGER PRIMARY KEY REFERENCES technique_entries(id),
  cooled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  reset_after TIMESTAMP,                            -- 7 天后自动解封
  consecutive_misses INTEGER NOT NULL
);
```

`retriever.retrieve()` SQL (§6.3) 加 WHERE 条件: `AND id NOT IN (SELECT pattern_id FROM pattern_cooling WHERE reset_after > CURRENT_TIMESTAMP)`。

## 16.5 变长内容 (覆盖 §7.1, Appendix B)

- 删除 `MAX_TWEET_LENGTH = 280` 常量
- `technique_entries` 加 `optimal_length TEXT` 字段，distiller 输出时判定 short/medium/long
- writer 不再硬截断，按 `topic_candidate.predicted_length` 走
- `optimal_length` 枚举: `short ≤280` / `medium 281-1000` / `long 1001-2500` / `article >2500`（走 X Article 接口）
- publisher 检测长度自动路由：≤ 280 走普通发推；>280 走 X 长推；> 2500 走 X Article 编辑器

**Cost 监控** (新增): `daily_stats` 表加 `tokens_spent INTEGER DEFAULT 0` 字段；reviewer 每周复盘把"长度 × 爆款率"做相关分析，**不卡死**纯观察。

## 16.6 三种 content_mode (新增 distiller 字段)

- `insight` — 干货 / 深度分析 / 数据
- `meme` — 段子 / 反讽 / 二创
- `emotional` — 情绪宣泄 / 共情 / 故事

`technique_entries.content_mode TEXT` 字段（distiller 必须输出）。
`topic_candidates.predicted_content_mode` 字段。
writer 按 mode 选择不同 system prompt 段落（`templates/template_finance_insight.md` / `_meme.md` / `_emotional.md`）。

## 16.7 富途 adapter (新增到 S5)

`src/observers/futu_adapter.py`:
- 用 `secrets/futu_cookies.json`
- 走 `https://q.futunn.com/nnq/recommend`（牛牛圈推荐）
- **关键**: 抓之前必须点一次"推荐" tab 才会刷新 → Playwright `await page.click('text=推荐'); await page.wait_for_load_state('networkidle')`
- 筛选条件: `has_image=true AND (likes+comments) > author_p80`
- 输出标准 `Observation` 对象

`config/sources.yaml` 加：
```yaml
  - name: futu
    enabled: true
    feed_url: "https://q.futunn.com/nnq/recommend"
    cookie_env_key: FUTU_COOKIE_FILE
    rate_limit_per_hour: 12
    tier_default: 2
    click_refresh: true                            # adapter 标志位
```

`.env` 加: `FUTU_COOKIE_FILE=/app/secrets/futu_cookies.json`

## 16.8 风控梯度 (覆盖 §15 Phase F)

| 周次 | 每日发布上限 | 备注 |
|---|---|---|
| 第 1 周 | 3-5 条 | 探账号风控边界 |
| 第 2 周 | 5-7 条 | 没问题就抬 |
| 第 3 周+ | 7-8 条 | 稳态 |

候选仍每天 12 条；超出上限的进**备选池**，下轮如某 lane 无料可复用。

`config/topic_blend.yaml` 加：
```yaml
publish_cap_by_week:
  1: 5
  2: 7
  3: 8
```

`src/main.py` 在 post 命令前检查当日已发数 vs `publish_cap_by_week[current_week]`。

## 16.9 大号 cookie 待补

当前 secrets/ 里只有：
- ✅ `x_xiaohao_cookies.json` — 小号（cross-reference 用）
- ✅ `futu_cookies.json` — 富途
- ✅ `xueqiu_cookies.json` — 雪球
- ❌ `x_dahao_cookies.json` — **缺**，发推用。Phase E VPS 部署前必须补上。

提醒：spec §11.4 cookie lifecycle 适用同样安全规则。

## 16.10 清理顺序 (覆盖 §15 Phase B)

**不再一刀切删 tests/**。新顺序：

1. **先建状态机** (Phase B.1): 建 §16.1 + §16.2 + §16.3 + §16.4 的 5 张新表 + 最小测试保护 `draft → published → learned` 链路
2. **再删旧入口** (Phase B.2): `src/run.py`（雪球→Discord）+ `src/discord_poster.py`
3. **清临时目录** (Phase B.3): `tmp_images/` + `tmp_screenshots/`
4. **审计旧测试** (Phase B.4): 逐个看 `tests/*.py`，旧世界测试（涉及已删模块的）才删；能保护新闭环的留下或改写

## 16.11 更新后的 Subagent 拆分

§4.1 表追加 2 行：

| # | Subagent | 文件 | 行数估 | 依赖 |
|---|---|---|---|---|
| S13 | Discord Gateway | `src/discord/bot.py`、`publisher_callback.py`、`rejection_pool.py` | 250+150+80 | S1 (drafts schema), S7 (publisher) |
| S14 | Topic Selector | `src/selector/topic_scorer.py`、`virality_predictor.py`、`db.py`、`__init__.py` | 180+200+100+30 | S1 (topic_candidates schema), S3 (LLM), S6.retriever |

S5 Observers 追加 1 文件：`src/observers/futu_adapter.py` + `self_monitor_adapter.py`（用小号 cookie 监控大号）。

## 16.12 更新后的 Pipeline 拓扑（覆盖 §3.2）

```
   每小时 ─▶ observer (4 sources: xueqiu, futu, x_list_finance, news_flash)
              ↓
            selector (打分 topic_candidates)
              ↓
            light_distill (新 viral obs)

   每 2h  ─▶ post:
              ↓
            selector.pick_top_topic
              ↓
            miner.retrieve (按 topic ctx)
              ↓
            writer (变长 + 3 mode)
              ↓
            scorer + guardrails
              ↓
            push to Discord (status=pushed_to_discord)
              ↓
            [等用户 emoji]

   每 5min ▶ discord.poll_reactions
              ├─ ✅ → publisher (Playwright headless 大号 cookie)
              │       → status=published, tweet_url 记入
              ├─ ❌ → status=rejected, 进 human_rejection_pool
              └─ 🔄 → 重新生成

   每 6h  ─▶ self_monitor (用小号 cookie 扫大号 timeline)
              → cross-reference: 补绑 tweet_url 或写 wild_posts

   每天   ─▶ 24/48/72h metrics 抓取
              ↓ ↓ ↓
   每晚 0 ─▶ mine + review (4 类反馈状态机更新权重)
   每周日 ─▶ remine
```

---

**End of Amendments v1.1.** 上面所有变化都已落入 spec；后续 Phase B/C/D 按 amended 版执行。

## 16.13 No-Auto-Publish 默认（覆盖 §16.1 ✅ 反应）

**Why**: 用户明确「不建议直接大号发」。账号安全 + 品控由人工兜底。

**New default**: `DISCORD_APPROVAL_MODE=manual`（在 .env 配置）。

| 反应 | 行为 (manual 模式) |
|---|---|
| ✅ | `drafts.status='approved'`，**bot 不发推**。用户手动在 X 客户端 copy-paste 发。|
| ❌ | 同原设计：进 human_rejection_pool |
| 🔄 | 同原设计：回 candidate 让 writer 重生 |

**关闭闭环**: self_monitor cron（每 6h，§16.3）扫大号 timeline → 按 content 匹配 status='approved' AND tweet_url IS NULL 的 drafts → 补 tweet_url + status='published' + posted_at。

**大号 cookie 不再需要**: `secrets/x_dahao_cookies.json` 已删除（manual 模式 publisher 不调用）。
**新增 env**: `TWITTER_HANDLE=off_tehtarget`（self_monitor 扫这个 handle 的 timeline）。
**legacy 路径保留**: `DISCORD_APPROVAL_MODE=auto` 仍可用，但需要重新提供大号 cookie。

**风险变更**: cross-reference 失败（self_monitor 抓不到 / content 改了 / 用户没发）→ draft 卡在 status='approved' 永远不进 metrics 学习。对策：reviewer 每周日检查 status='approved' 且 > 7 天的 drafts → 移到 stale 状态 + 告警 webhook。

