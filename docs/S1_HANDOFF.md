# S1 HANDOFF — DB Schema & Migrations

**Owner**: S1 (contracts层) | 入口: `src/database.py` + `src/migrations/`

## 表清单 (18 张)

**§5.2 主表 (001_init.sql)**: `posts`, `reaction_observations`, `strategy_weights`,
`learning_log`, `source_health`, `circuit_breaker`, `slop_words`, `daily_stats`

**§5.2 Pattern Miner (002)**: `technique_entries`, `technique_edges`,
`retrieval_log`, `post_metrics_timeseries`

**§16.1 (003)**: `drafts` — state machine 主轴

**§16.2/16.3 (004)**: `topic_candidates`, `wild_posts`

**§16.4/16.5 (005)**: `human_rejection_pool`, `pattern_cooling`；
`daily_stats.tokens_spent` 字段补齐

内部表: `schema_migrations(filename PK, applied_at)` — runner 自管理。

## 关键约束

- `posts.content_hash` UNIQUE — 去重锚点
- `reaction_observations.raw_url` UNIQUE — 源去重
- `technique_entries(observation_id)` UNIQUE — 一观察对一 entry
- `technique_edges`: CHECK `src_entry_id < dst_entry_id` + UNIQUE(src,dst,edge_type) — 无向图规范化
- `drafts.status` CHECK 枚举 7 种: candidate/pushed_to_discord/approved/rejected/published/metrics_collected/learned
- `topic_candidates.status` CHECK: fresh/consumed/expired
- FK 全启用 (`PRAGMA foreign_keys=ON`)
- WAL mode + synchronous=NORMAL

## 入口 API (从 `src.database` import)

```python
from src.database import get_conn, init_db, with_retry, load_schema_migrations

init_db()                          # 幂等，启动时调一次
conn = get_conn()                  # Row factory, FK on, 10s timeout
with conn:                         # 事务块
    conn.execute("INSERT ...")
with_retry(lambda: do_write(), retries=3, backoff=0.2)   # SQLITE_BUSY 兜底
```

## 给其他 subagent 的速查

**S2 Observer — 写 reaction_observations**:
```python
with conn:
    conn.execute(
      "INSERT OR IGNORE INTO reaction_observations (source, author_handle, "
      "author_tier, content, posted_at, likes, retweets, replies, has_image, "
      "raw_url, viral_score, is_viral) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
      (...))
```

**S6 Miner — 读 viral obs 写 entry**:
```python
rows = conn.execute(
  "SELECT * FROM reaction_observations WHERE is_viral=1 AND distilled_at IS NULL"
).fetchall()
# distill 后:
conn.execute("INSERT INTO technique_entries (...) VALUES (...)")
conn.execute("UPDATE reaction_observations SET distilled_at=? WHERE id=?", ...)
```

**S6 Retriever — 排除 cooling**:
```sql
SELECT id, hook_pattern, success_score FROM technique_entries
WHERE topic_lane = ? AND content_mode = ?
  AND id NOT IN (SELECT pattern_id FROM pattern_cooling
                 WHERE reset_after > CURRENT_TIMESTAMP)
ORDER BY success_score * recency_weight DESC LIMIT 5
```

**S14 Selector — 写 topic_candidates**:
```python
conn.execute(
  "INSERT INTO topic_candidates (source_observations, topic_summary, "
  "virality_score, predicted_content_mode, predicted_length, "
  "predicted_topic_lane) VALUES (?,?,?,?,?,?)",
  (json.dumps(ids), summary, score, mode, length, lane))
```

**S13 Discord — drafts 状态推进**:
```python
conn.execute("UPDATE drafts SET status='pushed_to_discord', "
             "discord_message_id=? WHERE id=?", (msg_id, draft_id))
```

**S7 Publisher — 切到 published**:
```python
with conn:
    conn.execute("UPDATE drafts SET status='published', tweet_url=?, "
                 "posted_at=CURRENT_TIMESTAMP WHERE id=?", (url, did))
    conn.execute("INSERT INTO posts (content, content_hash, topic_lane, "
                 "persona, posted_at, tweet_url, status) "
                 "VALUES (?,?,?,?,CURRENT_TIMESTAMP,?, 'published')", (...))
```

## 跑 migrations

```bash
python3 src/migrations/runner.py              # 默认 data/pepperbot.db
python3 src/migrations/runner.py /tmp/x.db    # 自定义路径
```

幂等：第二次只输出 `[skip] ... already applied`。

## 测试

`pytest tests/unit/test_database.py -v` → 11 passed
