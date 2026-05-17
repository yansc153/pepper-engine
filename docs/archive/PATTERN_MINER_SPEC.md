# Pattern Miner Spec — Observe · Distill · Weave · Research

> **Status:** Draft v1
> **Drafted:** 2026-05-17
> **Parent doc:** `docs/PRD_v2.md` §4.2
> **Inspiration:** WisMe.ai 的 4 步范式（闭源）+ Cognee（OSS, MIT）+ Graphiti（OSS, Apache-2.0）
> **约束:** 严格遵守 CLAUDE.md — SQLite only，禁 Neo4j / Redis / 云存储，禁 LangChain，本地 claude CLI

---

## 0. 决策快照

| 维度 | 选择 | 理由 |
|---|---|---|
| 架构范式 | **WisMe 的 Observe → Distill → Weave → Research 4 步** | 已被产品验证，自然映射到内容运营 |
| 是否引入 Cognee/Graphiti 作为依赖 | **不直接引入，但抄其 prompt + schema** | Graphiti 强依赖 Neo4j/FalkorDB，违反 CLAUDE.md SQLite 锁定 |
| 图层实现 | **SQLite + nodes 表 + edges 表 + 递归 CTE** | 不引入新基础设施，技法量级（万级）SQLite 完全够 |
| Distill 时机 | **每晚 batch（cron 16:00 UTC，与 review 串行）** | 省 token、聚合后 LLM 判断更稳，对齐 WisMe 的 "every night" |
| Weave 触发 | **Distill 完成后串行 + 每周日全量回扫一次** | 增量 + 周期校准 |
| Research 接口 | **Writer 调用 `miner.retrieve(context)` 同步返回 Top-K** | 写作时 < 200ms，不能等长流程 |
| LLM 调用 | **本地 claude CLI（claude-sonnet-4-6）** | 复用 `src/llm.py`，与全项目一致 |

---

## 1. 四步范式映射

| WisMe 步骤 | 我们的对应 | 实体 | 落地代码 |
|---|---|---|---|
| Observe | 抓 KOL 推文 + 雪球达人 feed，按 viralScore 过滤噪音 | `Observation` | `src/observers/*` + `reaction_observations` 表 |
| Distill | 每晚把当日 viral observations → 结构化 `TechniqueEntry` | `TechniqueEntry` | `src/miner/distiller.py` + `technique_entries` 表 |
| Weave | 新 entry 与已有图谱建立 5 种关联边，找跨日跨域链接 | `TechniqueEdge` | `src/miner/weaver.py` + `technique_edges` 表 |
| Research | 写作时按 `(topic_lane, hour, persona)` 检索 Top-K，注入 prompt | `RetrievalContext` | `src/miner/retriever.py`（被 `writer.py` 调用） |

**关键差异 vs WisMe**：他们的 Observe 信号是"网页 dwell"，我们是"社交互动量"——量化得更硬（reply×27、profile_click×12），过噪更准。

---

## 2. 数据模型（SQLite Schema）

```sql
-- Observe 层（PRD_v2 §5 已声明，本文档补全字段）
CREATE TABLE reaction_observations (
  id INTEGER PRIMARY KEY,
  source TEXT,                -- x_list / xueqiu / news_flash
  author_handle TEXT,
  author_tier INTEGER,        -- 1/2/3
  content TEXT,
  posted_at TIMESTAMP,
  likes INTEGER, retweets INTEGER, replies INTEGER, impressions INTEGER,
  has_image INTEGER,
  raw_url TEXT,
  viral_score REAL,           -- 入库时按 reply×27 + ... 算
  is_viral INTEGER,           -- viral_score > 作者 p80
  observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  distilled_at TIMESTAMP NULL -- NULL = 待蒸馏
);
CREATE INDEX idx_obs_viral ON reaction_observations(is_viral, distilled_at);

-- Distill 层
CREATE TABLE technique_entries (
  id INTEGER PRIMARY KEY,
  observation_id INTEGER REFERENCES reaction_observations(id),
  -- 结构化字段（Distill prompt 输出 schema）
  hook_pattern TEXT,                -- '反共识开场' / '数字暴击' / '反问' / '场景代入' / ...
  hook_example TEXT,                -- 脱敏后的首句样本（去人名/股票代码）
  syntax_signature TEXT,            -- 'short_comma_no_period' / 'long_run_on' / ...
  sentence_len_avg REAL,
  sentence_len_p90 REAL,
  stance_strength INTEGER,          -- 0-5
  emotion_triggers TEXT,            -- JSON array ['FOMO', '嘲讽', '认知优越']
  image_style TEXT,                 -- 'kline_with_doodle' / 'screenshot' / 'meme' / 'chart'
  post_hour_utc INTEGER,
  topic_lane TEXT,                  -- 'pre_market' / 'intraday' / 'general_tech_ai' / ...
  applicable_personas TEXT,         -- JSON ['finance_neutral', 'finance_contrarian']
  -- 元信息
  distilled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  success_score REAL,               -- 继承 observation.viral_score，做后续加权用
  times_retrieved INTEGER DEFAULT 0,
  times_used_in_post INTEGER DEFAULT 0,
  recency_weight REAL DEFAULT 1.0   -- 由 cron 每周衰减
);
CREATE INDEX idx_te_lane_hour ON technique_entries(topic_lane, post_hour_utc);

-- Weave 层（图谱的边，节点直接复用 technique_entries）
CREATE TABLE technique_edges (
  id INTEGER PRIMARY KEY,
  src_entry_id INTEGER REFERENCES technique_entries(id),
  dst_entry_id INTEGER REFERENCES technique_entries(id),
  edge_type TEXT,                   -- 5 种见下文
  weight REAL,                      -- 0-1
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(src_entry_id, dst_entry_id, edge_type)
);
CREATE INDEX idx_edge_src ON technique_edges(src_entry_id, edge_type);

-- Research 层（检索日志，用于回看 Top-K 是否真有用）
CREATE TABLE retrieval_log (
  id INTEGER PRIMARY KEY,
  post_id INTEGER REFERENCES posts(id),
  retrieved_entry_ids TEXT,         -- JSON array
  context_signature TEXT,           -- '{lane:pre_market, hour:23, persona:neutral}'
  retrieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 5 种边类型（来自 Graphiti 的设计，简化）

| edge_type | 含义 | 何时建 |
|---|---|---|
| `same_hook` | 钩子模式相同 | hook_pattern 一致 |
| `same_lane_diff_angle` | 同 topic_lane 但 hook_pattern 不同 | Weave 阶段批量计算 |
| `co_occurring_emotion` | emotion_triggers 集合 IoU > 0.5 | Weave 阶段 |
| `temporal_chain` | 同一作者 48 小时内连续 viral | Weave 阶段，用于学"系列爆款" |
| `cross_domain_bridge` | 金融 entry × 泛流量 entry，syntax_signature 相同 | Weave 阶段，**这是发现"被低估角度"的关键** |

---

## 3. Pipeline DAG（cron + Python，无 Airflow）

```
                       ┌─────────────────────┐
   cron 0 * * * *  ───▶│ run.sh observe      │  每小时
                       │   ▶ adapters.fetch  │
                       │   ▶ viral_score()   │
                       │   ▶ INSERT obs      │
                       └─────────────────────┘
                                  │
                                  │ (async trigger if is_viral)
                                  ▼
                       ┌─────────────────────┐
                       │ light_distill()     │  增量，单条
                       │ 只抽 hook + lane    │  ≤ 30s/条
                       └─────────────────────┘

   cron 0 16 * * *  ──▶┌─────────────────────┐
   (每天 00:00 CST)    │ run.sh mine         │  夜间深加工
                       │ ▶ full_distill()    │  当日全量
                       │ ▶ weaver.weave()    │  建边
                       │ ▶ retriever.warmup()│  缓存 Top-K
                       └─────────────────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │ run.sh review       │  紧接在 mine 后
                       │ ▶ reviewer.update_  │
                       │    weights()        │
                       └─────────────────────┘

   cron 0 4 * * 0    ─▶┌─────────────────────┐
   (每周日 12:00 CST)  │ run.sh remine       │  全量校准
                       │ ▶ re-weave all      │
                       │ ▶ recency_decay()   │
                       │ ▶ prune low score   │
                       └─────────────────────┘

   写作请求（同步） ──▶ retriever.retrieve(ctx) ──▶ Top-K + cached edges
                       ≤ 200ms，纯 SQL 查询
```

### Pipeline 失败处理（参考 ml-pipeline 最佳实践）

- 每个阶段写 `source_health` 表：`(stage, last_success_at, consecutive_failures, last_error)`
- 阶段连续失败 3 次 → osascript 弹本机通知，**不** 自动发推（熔断）
- Distill / Weave 是幂等的（按 `observation_id` UPSERT），失败重跑安全
- Retriever 必须有降级：图谱无数据时回退到 `templates/hooks_finance.md` 静态模板

---

## 4. Distill Prompt（抄 Cognee ECL 思路）

`src/miner/distiller.py` 里组装。**注意：每次只喂 1 条 observation**，不批喂（保证 JSON 输出稳定）。

```
你是一名内容拆解员。任务：把下面这条爆款推文拆成结构化技法 entry。

【原文】
作者: {author_handle} (tier={author_tier})
发布时间: {posted_at} (UTC hour={hour_utc})
互动: 点赞{likes} 转推{retweets} 评论{replies} (viralScore={vs})
正文:
{content}
配图: {has_image_desc}

【输出严格 JSON，不要多余文字】
{
  "hook_pattern": "<从 [反共识开场|数字暴击|场景代入|反问|金句压尾|对比悖论|身份代入] 选一>",
  "hook_example": "<首句脱敏后原句，去掉人名、股票代码、币种名，保留句式>",
  "syntax_signature": "<short_comma_no_period | long_run_on | stacked_short | dialog_style>",
  "sentence_len_avg": <int>,
  "sentence_len_p90": <int>,
  "stance_strength": <0-5，越大越坚决>,
  "emotion_triggers": ["<最多 3 个，从 FOMO|嘲讽|认知优越|焦虑|共情|猎奇|愤怒 中选>"],
  "image_style": "<kline_with_doodle | screenshot | meme | chart | photo | none>",
  "topic_lane": "<pre_market | intraday | post_market | overnight | general_tech_ai | general_meme_career | other>",
  "applicable_personas": ["<finance_neutral | finance_contrarian | finance_macro | general_observer>"]
}

【硬约束】
- hook_example 必须脱敏，否则整条 entry 作废
- emotion_triggers 不能为空数组
- 输出之外不要任何解释
```

### 输出校验

- JSON parse 失败 → 重试 1 次 → 失败丢弃，但 observation 标记 `distilled_at=NOW` 防止反复跑
- `applicable_personas` 不能含我们没定义的 persona key（白名单校验）
- 用 `jsonschema` 库验证字段类型 + 枚举范围

---

## 5. Weave 算法（每晚 + 每周）

```python
def weave_nightly(new_entry_ids: list[int]) -> int:
    """对当日新蒸馏的 entries 建边。返回新增边数。"""
    edges_added = 0
    for entry_id in new_entry_ids:
        entry = load_entry(entry_id)
        # 候选池：最近 30 天的 entries（recency_weight > 0.3）
        candidates = sql_query("""
            SELECT id, hook_pattern, topic_lane, emotion_triggers,
                   syntax_signature, author_handle, posted_at
            FROM technique_entries
            WHERE recency_weight > 0.3 AND id != ?
        """, (entry_id,))
        for cand in candidates:
            for edge_type, weight in compute_edges(entry, cand):
                upsert_edge(entry_id, cand.id, edge_type, weight)
                edges_added += 1
    return edges_added

def compute_edges(a, b) -> list[tuple[str, float]]:
    out = []
    if a.hook_pattern == b.hook_pattern:
        out.append(('same_hook', 1.0))
    if a.topic_lane == b.topic_lane and a.hook_pattern != b.hook_pattern:
        out.append(('same_lane_diff_angle', 0.7))
    emo_iou = iou(a.emotion_triggers, b.emotion_triggers)
    if emo_iou > 0.5:
        out.append(('co_occurring_emotion', emo_iou))
    if a.author_handle == b.author_handle and abs(time_delta(a, b)) < 48 * 3600:
        out.append(('temporal_chain', 0.8))
    if is_cross_domain(a.topic_lane, b.topic_lane) and a.syntax_signature == b.syntax_signature:
        out.append(('cross_domain_bridge', 0.9))
    return out
```

**Weekly remine** 加做两件事：
- `recency_weight *= 0.93`（半衰期约 14 天）
- 删除 `success_score < p20 AND times_used_in_post == 0` 的 entries（永远没用上的低分技法）

---

## 6. Research（Retrieval）—— Writer 同步调用

```python
@dataclass
class RetrievalContext:
    topic_lane: str
    post_hour_utc: int
    persona: str
    fact_spine_keywords: list[str]   # 当前要写的话题词
    avoid_recent_patterns: list[int] # 最近 7 天用过的 entry_id

def retrieve(ctx: RetrievalContext, k: int = 5) -> list[TechniqueEntry]:
    """≤ 200ms，纯 SQL，不调 LLM"""
    sql = """
    WITH lane_hits AS (
      SELECT id, success_score, recency_weight, hook_pattern, hook_example,
             syntax_signature, stance_strength, emotion_triggers
      FROM technique_entries
      WHERE topic_lane = ?
        AND ABS(post_hour_utc - ?) <= 2
        AND ? IN (SELECT value FROM json_each(applicable_personas))
        AND id NOT IN (SELECT value FROM json_each(?))
    ),
    -- 通过 cross_domain_bridge 找泛流量里同句式的（10% 配额）
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
      SELECT * FROM lane_hits ORDER BY success_score * recency_weight DESC LIMIT ?
      UNION ALL
      SELECT * FROM bridge_hits ORDER BY success_score * recency_weight DESC LIMIT ?
    );
    """
    rows = db.execute(sql, (
        ctx.topic_lane, ctx.post_hour_utc, ctx.persona,
        json.dumps(ctx.avoid_recent_patterns),
        int(k * 0.9), max(1, int(k * 0.1))
    ))
    log_retrieval(ctx, [r.id for r in rows])
    increment_times_retrieved(rows)
    return rows
```

### Writer 集成点

`writer.py` 在 `build_angle_card()` 阶段：

```python
techniques = retrieve(ctx, k=5)
angle_card = render_template("angle_card.md", {
    "fact_spine": ...,
    "reaction_pack": ...,
    "retrieved_techniques": [
        {
            "hook_pattern": t.hook_pattern,
            "hook_example": t.hook_example,
            "stance_strength": t.stance_strength,
            "emotion_triggers": t.emotion_triggers,
        } for t in techniques
    ],
    "instruction": "参考以上 5 条爆款的钩子和句式，但不要复制原文。",
})
```

---

## 7. 学习闭环（与 Reviewer 协同）

**Reviewer 每晚做的事**（PRD_v2 §4.5）现在多两条：

1. 读 `retrieval_log` × `post_metrics_timeseries`：
   - 对每条 post，找出当时检索到的 entry_ids
   - 如果 post viralScore 进入当周 top 30%：所有 retrieved entries `times_used_in_post += 1`，`success_score` 按 EMA 上调
   - 如果 post viralScore 进入当周 bottom 30%：所有 retrieved entries `success_score` 按 EMA 下调
   - **这是真实的"用了爆款 → 学了；用了哑炮 → 忘了"反馈**

2. 计算每个 `hook_pattern` 的累积 success_rate：
   - sample_size ≥ 5 才进 `strategy_weights`
   - 累计 success_rate 排名 bottom 20% 且 sample_size ≥ 10 的 hook_pattern → 写进 `voice/slop_words.md` 的"已知哑炮句式"段（双通道沉淀）

---

## 8. 冷启动策略（前 30 天没数据怎么办）

| 阶段 | 数据状态 | Retriever 行为 |
|---|---|---|
| Day 0-3 | entries < 20 | 100% 回退到 `templates/hooks_finance.md` 静态模板 |
| Day 4-14 | 20 ≤ entries < 100 | 50% retrieved + 50% 静态模板 |
| Day 15-30 | entries ≥ 100，但 sample_size 不够 | 100% retrieved，但忽略 success_score 排序，按 recency 排 |
| Day 30+ | 进入正常态 | success_score × recency_weight 排序 |

**Seed Pool**：用户冷启动时手工提供 5-10 条历史爆款（PRD_v2 §13 第 4 项），跑一次 Distill 灌入，加速到 Day 4 状态。

---

## 9. 性能预算

| 操作 | 预算 | 实测预期 |
|---|---|---|
| `observe` 一次（fetch + viral_score + INSERT） | 30s | 取决于网络 |
| `light_distill` 单条（1 次 CLI 调用） | 30s | claude-sonnet-4-6 ~5-15s |
| `full_distill` 夜间（~30 条 viral） | 15min | 串行调 CLI |
| `weave_nightly`（~30 new × ~500 candidates） | 2min | 纯 Python + SQL |
| `retrieve` 写作时 | 200ms | 索引齐全 |
| `weekly_remine` | 30min | 周日跑 |

SQLite 数据增长估算：
- 每日 ~30 viral × 30 天 = 900 entries/月
- edges ≈ entries × 50（平均每个 entry 50 条边）= 45k edges/月
- 年化 ~500k 行，单表 SQLite 无压力（百万级以内）

---

## 10. 与 PRD_v2 的对应关系

| PRD_v2 章节 | 本 spec 章节 | 状态 |
|---|---|---|
| §4.2 Pattern Miner | 全文 | **本文档展开** |
| §5 Schema 增量 | §2 数据模型 | 本文档更详细，replaces PRD §5 中的 technique_library ALTER |
| §10 Phase 3 退出条件 | §8 冷启动策略 | Phase 3 退出 = 进入 Day 30+ 正常态 |
| §11 风险表 | §7 + §9 | 学习跑飞的对策已含 sample_size 门槛 |

> **PRD_v2 §5 的 `technique_library` ALTER TABLE 作废**，改用本 spec §2 的 `technique_entries` + `technique_edges` + `retrieval_log` 三表。

---

## 11. Phase 3 拆解（PRD_v2 里只是说 "2 周"，这里详细化）

| Sub-phase | 工作 | 退出条件 |
|---|---|---|
| 3a (2d) | 建表 + `distiller.py` + 单条蒸馏 | 手工挑 5 条 viral 推文，能输出合法 JSON entry |
| 3b (2d) | `light_distill` 异步触发 + `full_distill` cron | 当晚跑完产出 ≥ 10 entries |
| 3c (3d) | `weaver.py` + 5 种边 | edges 表 ≥ 200 行，每种边类型至少 10 条 |
| 3d (2d) | `retriever.py` + writer 集成 | 写作时 prompt 里能看到注入的 hook_example，QPS 测试 < 200ms |
| 3e (2d) | `retrieval_log` 回看 + reviewer 联动 | 连续 5 条 post 后能看到 times_used_in_post 上涨 |
| 3f (3d) | 冷启动 seed + 静态模板回退 + 监控告警 | 全链路压测：模拟 0/20/100/500 entries 状态都能正常出推文 |

---

## 12. 不做的事（Out of Scope）

- 真正的图数据库（Neo4j / FalkorDB）—— 用 SQLite + 递归 CTE 模拟
- 向量检索（Pinecone / Qdrant / pgvector）—— 我们的检索是结构化字段过滤，不需要语义相似
- DSPy MIPRO 自动优化 prompt —— Phase 4+ 再考虑（先把数据闭环跑起来）
- 跨账号共享 technique_library —— Phase 5+，预留 `account_scope` 字段但暂不实现
- Letta MemFS / Mem0 / Cognee 作为依赖 —— 抄 schema 和 prompt 思路，不引入包

---

## 13. 风险

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| Distill JSON 输出不稳定 | 中 | 高 | 单条蒸馏 + retry 1 次 + 失败丢弃 + 跑 jsonschema 校验 |
| 图谱"爆款回响"：连续抓到几个相似爆款 → entries 同质化 → retrieve 永远返回相同 5 条 | 高 | 中 | retrieve 用 `avoid_recent_patterns` 排除 7 天用过的；weekly remine 检测 hook_pattern 集中度 |
| `cross_domain_bridge` 把金融号写成科技博主 | 中 | 高 | 配额限制 10%；persona 白名单强制；Scorer 5 维度里"合规安全度"兜底 |
| SQLite 单文件随时间膨胀 | 低 | 低 | weekly remine prune；年化 500k 行可控 |
| claude CLI 限流 | 低 | 中 | light_distill 失败排队到 full_distill 一起跑 |
| KOL 抓到广告号/僵尸号污染图谱 | 中 | 高 | observer 入库前过 `config/kol_list_*.md` 白名单 + `author_tier` 字段限制只学 tier 1/2 |

---

## 14. 测试

- **单元**：`compute_edges()` 5 种边的 truth table；`viral_score()` 边界值
- **集成**：用 fixtures 灌入 50 条 mock observations，跑完整 observe → distill → weave → retrieve，断言 Top-K 符合预期
- **回归**：每次 schema 改动跑 `pytest tests/miner/` ，必须包含「冷启动 0 数据」「正常态 500 entries」两个场景
- **人工 sanity**：每周一手工 review 当周 Top-10 success_score entries，确认句式确实好（防止 LLM 抽歪了我们不知道）

---

**End of Pattern Miner Spec v1.**
