# S14 Topic Selector — HANDOFF

选题引擎：把 observer 刚抓到的 KOL 反应聚类、打分、落库到
`topic_candidates`；writer 写之前先 pick 一条。LLM 只用本地
`src.llm.call_llm`（claude-sonnet-4-6），miner 是可选 cold-start 兼容。

## 三个 API（`from selector import ...`）

| 函数 | 签名 | 行为 |
|---|---|---|
| `score_topics(conn, *, lookback_hours=1, now=None, miner_retrieve=None, llm_caller=None)` | → `ScoreResult(created, top_score)` | 从 `reaction_observations` 取 lookback 内 tier>0 的行，按 handle + Jaccard token overlap (≥0.35) 聚类，逐 cluster 调 `virality_predictor.predict_virality`，INSERT 到 `topic_candidates` (status='fresh')。失败的 cluster 直接 skip，不抛。 |
| `pick_top_topic(conn, *, draft_id=None, topic_lane=None)` | → `dict \| None` | 单事务 claim：取 fresh + virality_score 最高（可按 lane 过滤）的候选，原子翻转为 consumed 并写 `consumed_by_draft_id`。 |
| `expire_old_candidates(conn, *, older_than_hours=6, now=None)` | → `int` | 把 fresh 但超过 N 小时未消费的标记为 expired。返回受影响行数。 |

## S5 observer runner 调用示例（post-observe hook）

```python
from selector import score_topics, expire_old_candidates

def post_observe(conn):
    expire_old_candidates(conn, older_than_hours=6)
    result = score_topics(conn)  # 自动用 src.llm.call_llm
    log.info("selector: created=%d top=%.1f", result.created, result.top_score)
```

## S9 writer 调用示例

```python
from selector import pick_top_topic

def pick_next_topic(conn, lane=None):
    topic = pick_top_topic(conn, topic_lane=lane)
    if topic is None:
        return None
    # topic dict 携带：topic_summary / predicted_content_mode / predicted_length /
    # predicted_topic_lane / source_observations(list[int]) / virality_score
    # writer 用这些字段决定 hook 模板 + 长度 + persona
    # 写完 draft 后回填 consumed_by_draft_id（通过事务包裹两步即可）
    return topic
```

写 draft 失败时无回滚 API：候选已 consumed。可选补救是把 `consumed_by_draft_id`
置 NULL 并把 status 改回 fresh，但目前 writer 链路简单，丢一条即可，下一轮
observer 会重生候选。

## 实现要点

- Clustering 故意便宜：先按 author_handle 分桶，再 Jaccard 合并相同话题的不同 KOL；
  没有 TF-IDF / embedding 依赖。LLM 每个 cluster 调一次，不是每条 obs。
- `kol_reaction_count` 在后端用 `author_tier<=1` 统计，不信 LLM 数字，避免 hallucination 抬高分。
- Numeric 字段全部 clamp（score 0-100、emotional/debate 0-1）。
- miner 可选：`miner_retrieve(topic_lane=...)` 抛异常或返回空都安全（cold start）。
- 所有写操作在 `with conn:` 事务里。

## 文件清单

- `src/selector/{__init__,db,topic_scorer,virality_predictor}.py`
- `src/selector/prompts/score_topic.txt`
- `tests/unit/selector/test_{db,topic_scorer,virality_predictor}.py`（29 tests，全绿）

## 已知 follow-ups

- writer 集成后，应把 `pick_top_topic` 调用包在和 draft INSERT 同一事务里，避免
  claim 后写失败留下孤儿 consumed 行。
- miner.retrieve 接口稳定后，runner 应注入它而不是默认 None。
