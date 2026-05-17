# S9 HANDOFF — Writer + Scorer + Guardrails

**Owner**: S9 | 入口: `src/writer.py` + `src/scorer.py` + `src/guardrails.py`

## DraftResult 字段

```python
@dataclass
class DraftResult:
    success: bool                  # 入库且分数过线
    draft_id: int | None           # drafts.id (status='candidate')
    content: str | None            # 最终正文（可能 None：LLMError）
    content_length: int            # len(content) 或 0
    content_mode: str              # insight | meme | emotional
    optimal_length: str            # short | medium | long | article
    topic_lane: str
    persona: str                   # 最终生效 persona（override > topic）
    pattern_ids: list[int]         # miner.retrieve 命中的 entry ids
    image_path: str | None         # 当前固定 None（image agent 在 S10）
    score_total: int | None        # 0-100；未跑分则 None
    error: str | None              # 失败原因；success=True 时为 None
    score_breakdown: dict[str, Any] # 5 维度明细
```

## 完整 pipeline 流程

1. `_normalize_topic` 填默认值 + persona override
2. `load_lexicons()` 加载 compliance + political + slop（缺失 → FileNotFoundError）
3. `build_fact_spine(topic)` 拆 topic_summary 成事实骨架（无 LLM）
4. `retrieve_techniques(ctx, k=5)` 调 `src.miner.retrieve`，模块/函数缺失或异常 → []
5. `build_angle_card(spine, techniques)` 注入 hooks/examples + 收集 pattern_ids
6. `_build_writer_prompt` 按 content_mode 加载 `templates/template_finance_{mode}.md` + voice + 长度 hint
7. `for attempt in range(3)`:
   - `_draft_once(prompt)` LLM JSON → {content, image_prompt, stance_strength}
   - 空 content → 提示重写
   - `audit_for_template(content)` LLM 判模板腔；needs_rewrite 且未到末轮 → 重写
   - `guardrails.check(content, persona, stance, lexicons)` → GuardrailReport
   - passed → `_finalize`；否则未到末轮提示 reasons + matched 后重写
8. 末轮仍未过 → `trip_circuit_breaker("writer.guardrails", ...)` + raise `GuardrailsExhausted`
9. `_finalize`: `scorer.score(content, guardrail_report=report)` → 5 维度 ×2 = 0-100；passed=total≥threshold (default 60 from `config/topic_blend.yaml#score_pass_threshold`)
10. 通过 → `_persist_draft` INSERT drafts (status='candidate')；不过 → DraftResult(success=False, error="score N below threshold")

## Guardrails 决策表

| 条件 | 结果 |
|---|---|
| political_lexicon 命中 | A_KILL reject |
| compliance A_kill 命中 | A_KILL reject |
| voice/slop_words 命中 | A_KILL reject |
| stance_strength > stock_threshold(=3) AND 文中含股票代码 | A_KILL reject |
| B_warn distinct ≥2 或同词 ≥2 次 | A_KILL reject |
| 单 B_warn 命中 | passed=True severity=B_WARN penalty=2 |
| 全部清白 | passed=True severity=None |

股票代码识别 regex 覆盖：`sh600519` / `600519.SH` / `00700.HK` / `$AAPL` / `NASDAQ:TSLA`。

## Scorer 5 维度

`info_density` / `stance` / `counter` / `hook`（LLM 0-10）+ `compliance`（确定性 0-10：clean=10, B_WARN=8, A_KILL=0）。
total = sum × 2 = 0-100。LLM 失败时 4 维度均回退 0，compliance 仍按 guardrail 走。

## S11 orchestrator 调用示例

```python
from src.writer import write_draft, DraftResult
from src.guardrails import GuardrailsExhausted

topic = conn.execute(
    "SELECT * FROM topic_candidates WHERE status='fresh' "
    "ORDER BY virality_score DESC LIMIT 1"
).fetchone()

try:
    result: DraftResult = await write_draft(dict(topic))
except GuardrailsExhausted as exc:
    # circuit_breaker 已自动写入；本轮放弃，切下一 topic
    logger.warning("writer guardrails exhausted: %s", exc)
    continue

if not result.success:
    logger.info("draft rejected: %s (score=%s)", result.error, result.score_total)
    continue

# result.draft_id 已 INSERT 进 drafts (status='candidate')；交给 S13 push 到 Discord
discord_bot.queue(result.draft_id, result.content)
```

## 测试

`pytest tests/unit/test_writer.py tests/unit/test_scorer.py tests/unit/test_guardrails.py -v` → **44 passed**
（writer 15, scorer 11, guardrails 18）

## 已知限制

- `image_path` 当前固定 None；S10 image agent land 后由 orchestrator 在 `INSERT` 前/`_persist_draft` 调用前注入
- `src.miner.retrieve` 未实现时静态模板兜底（pattern_ids=[]），不影响写作
- LLM JSON 解析失败 → LLMError → DraftResult(success=False) 而非崩溃
