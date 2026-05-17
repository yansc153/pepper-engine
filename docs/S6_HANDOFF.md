# S6 — Pattern Miner HANDOFF

**Owner**: S6 | entry: `src/miner/__init__.py`

## Public API (FROZEN)

```python
from src.miner import (
    retrieve, light_distill, full_distill,
    weave_nightly, weave_full,
    TechniqueEntry, RetrievalContext,
)
from src.miner.feedback import apply_post_outcome   # for S10 reviewer

retrieve(ctx: RetrievalContext, k: int = 5) -> list[TechniqueEntry]
light_distill(observation_id: int) -> int | None
full_distill(since: datetime) -> list[int]
weave_nightly(new_entry_ids: list[int]) -> int
weave_full() -> tuple[int, int]                      # (decayed, pruned)
apply_post_outcome(post_id, outcome: "top"|"mid"|"bottom") -> None
```

`RetrievalContext(topic_lane, post_hour_utc, persona, fact_spine_keywords, avoid_recent_pattern_ids, content_mode=None)` — `content_mode` optional, reserved for spec section 16.6 routing.

## Cold-start decision tree (inside retriever)

```
corpus_size = SELECT COUNT(*) FROM technique_entries
if corpus_size < 50:                    # Day 0-3
    return static_fallback(ctx, k)       # 100% from templates/hooks_finance.md, negative ids
elif corpus_size < 100:                 # Day 4-14
    return merge(sql(ctx, k//2), static(ctx, remainder))   # 50/50
else:                                   # Day 15+
    out = sql(ctx, k)                   # success x recency, lane + bridge
    if not out: out = static_fallback(ctx, k)
log_retrieval(ctx, ids); increment_times_retrieved(ids)
```

SQL automatically filters: `pattern_cooling.reset_after > NOW`, `avoid_recent_pattern_ids`, persona not in `applicable_personas`, |hour - post_hour_utc| > 2.
Bridge quota = `ceil(k * 0.1)`, so k=5 yields 1 cross_domain_bridge slot.

## Usage from S9 (writer)

```python
ctx = RetrievalContext(
    topic_lane=topic.predicted_topic_lane,
    post_hour_utc=datetime.now(UTC).hour,
    persona=topic.persona,
    fact_spine_keywords=[...],
    avoid_recent_pattern_ids=db.recent_pattern_ids(days=7),
    content_mode=topic.predicted_content_mode,
)
techniques = retrieve(ctx, k=5)
# Inject into angle_card; techniques[i].hook_example is already de-identified.
# After publisher commits, writer should backfill post_id on the retrieval_log row.
```

## Usage from S10 (reviewer)

```python
# nightly review: one call per post
for post_id, viral in posts_last_24h:
    if viral > p70: apply_post_outcome(post_id, "top")
    elif viral < p30: apply_post_outcome(post_id, "bottom")
    else: apply_post_outcome(post_id, "mid")
# State machine: top clears cooling; bottom enters 7-day cooling on 3rd miss; mid only bumps usage.
```

## Usage from S14 (selector)

Selector does NOT call retrieve. It only reads `technique_entries.hook_pattern` to score "does this topic match a historically high success_score hook". Recommended SQL:

```sql
SELECT hook_pattern, AVG(success_score) AS s
FROM technique_entries
WHERE topic_lane = :lane
GROUP BY hook_pattern ORDER BY s DESC LIMIT 3;
```

## Key files

- `src/miner/__init__.py` — public API surface
- `src/miner/viral_scorer.py` — `viral_score / is_viral / author_p80`
- `src/miner/distiller.py` — `light_distill / full_distill / validate_distillation / DistillError`
- `src/miner/weaver.py` — `weave_nightly / weave_full`
- `src/miner/weave_rules.py` — `compute_edges / iou / is_cross_domain`
- `src/miner/retriever.py` — `retrieve` + cold-start tiering
- `src/miner/feedback.py` — `apply_post_outcome` + cooling state machine
- `src/miner/db.py` — SQL helpers (upsert, log)
- `src/miner/static_fallback.py` — markdown table parser
- `src/miner/prompts/distill.txt` — Distill prompt template

## Tests and performance

`pytest tests/unit/miner/` -> **63 passed** (distiller 12, feedback 6, retriever 9, static_fallback 4, viral_scorer 6, weave_rules 18, weaver 8)

500-entry SQL retrieve performance (M-series macOS, Python 3.14, SQLite WAL):
- median **1.24 ms**, p95 1.52 ms, max 1.67 ms (well under 200 ms budget)

## Cross-module fix

`src/miner/distiller._render_prompt` switched from `str.format` to `str.replace` because the JSON braces in the prompt template collide with format placeholders. No other shared files were touched.

## Known caveats

- Static fallback entries have negative ids. `log_retrieval` records them; `increment_times_retrieved` skips them so counters stay clean.
- `apply_post_outcome` silently skips negative entry ids (static templates have no learned success_score).
- retriever auto-calls `log_retrieval` with `post_id=NULL`. Writer must backfill the post_id after publisher commits (suggested helper: `db.attach_post_id_to_recent_retrieval`, next iteration).
- `tests/unit/test_writer.py::test_retrieve_techniques_returns_entries` now fails when run alongside miner tests. Root cause: writer.py does `from src import miner` which resolves to the package attribute on `src`, not the `sys.modules["src.miner"]` patch the test relies on. Before S6, `src.miner` was not importable so the patch accidentally worked. Fix belongs in writer.py (use `importlib.import_module("src.miner")` after the sys.modules patch) or the test (also `monkeypatch.setattr(src, "miner", fake)`). Not changed here per task scope.
