# Codex Review Checklist — content_2 (Pepperbot)

> 这份 checklist 给 Codex 做独立第二意见 review。Claude 已经完成 5-agent 并行 review + 修复 4 个 CRITICAL。本表列出 Claude **未修复** 的中低优先级项 + Claude 可能漏掉的盲点，让 Codex 独立判断。

## 系统概览（30 秒）

- **目的**：Chinese 金融 Twitter 自动化（@off_tehtarget），KOL 观察 → 蒸馏 viral pattern → 写稿 → Discord 人工审批 → 用户手动发推 → self_monitor 绑回 metrics → reviewer 学习
- **状态机**：`candidate → pushed_to_discord → approved → published → metrics_collected → learned`
- **手动模式**（默认）：`DISCORD_APPROVAL_MODE=manual`，Discord ✅ 仅标记 `approved`，从不调 publisher；user 手动在 X 客户端发推，self_monitor 6 小时 cron 通过 content match 绑 `tweet_url`
- **部署**：VPS Docker（Playwright headless + Moonshot API + SQLite + cron）
- **测试**：330 tests passing
- **CLAUDE.md**：`/Users/oxjames/Downloads/CC_testing/花椒的content_2/CLAUDE.md` 含所有项目约束
- **UNIFIED_SPEC**：`docs/UNIFIED_SPEC.md` 是完整规范

## 入口文件清单

```
src/main.py                          # 8 commands: observe/post/mine/review/remine/discord_poll/self_monitor/test
src/database.py                      # SQLite, get_conn(), with_retry(), migrations runner
src/migrations/                      # 5 SQL files, 18 tables
src/observers/runner.py              # asyncio.gather across all adapters
src/observers/xueqiu_adapter.py      # cookie scrape
src/observers/futu_adapter.py        # Playwright click 推荐 then scrape
src/observers/news_flash_adapter.py  # tier=0 fact spine only
src/observers/x_list_finance_adapter.py
src/observers/self_monitor_adapter.py # 6h cron, binds @off_tehtarget tweets
src/miner/distiller.py               # LLM per-observation, jsonschema validation
src/miner/weaver.py                  # nightly graph build, 5 edge types
src/miner/retriever.py               # cold-start tiered, <200ms SQL
src/miner/feedback.py                # EMA + pattern_cooling
src/miner/viral_scorer.py            # reply×27 / profile×12 / negfb×-74
src/miner/static_fallback.py         # parses templates/hooks_finance.md
src/writer.py                        # fact spine → angle card → draft → score
src/scorer.py                        # 5-dim scoring
src/guardrails.py                    # A_kill / B_warn, MAX_REWRITE_ATTEMPTS=3
src/selector/topic_scorer.py         # cluster + LLM virality predict
src/selector/virality_predictor.py
src/selector/db.py
src/discord/bot.py                   # REST mode (httpx)
src/discord/publisher_callback.py    # manual-mode default
src/discord/revise_handler.py
src/discord/rejection_pool.py
src/reviewer.py                      # 4-feedback state machine + dual-channel learning
src/twitter_bot.py                   # Playwright headless, cookie injection
src/publisher.py                     # post_tweet (DRY_RUN=1 never posts)
src/llm.py                           # claude_cli vs moonshot dual backend
scripts/entrypoint.sh                # Docker entrypoint
crontab.txt                          # 6 cron rows
Dockerfile / docker-compose.yml
```

## P0 — Claude 已修复，请验证

请确认这 5 个修复确实 work，没有引入新 bug：

1. **State machine 推进** (`src/reviewer.py`): 新加的 `_advance_draft_status(draft_ids, "published", "metrics_collected")` 和 `(..., "metrics_collected", "learned")` 是否正确处理了 happy path？如果 `_write_metrics_timeseries` 中途失败，状态会卡在哪里？是否能 retry？

2. **strategy_weights 消费** (`src/selector/topic_scorer.py:pick_top_topic`): 现在 `pick_top_topic` 用 `virality_score * weight` 选最佳，但 `weight` 默认 `max(0.1, w)`。空 `strategy_weights` 表时所有 lane 用 1.0（max default），首日冷启动是否正确？

3. **self_monitor binding** (`src/observers/self_monitor_adapter.py:_bind_draft`): 加了 `AND status IN ('pushed_to_discord', 'approved')`，但如果 reviewer 已经把 status 从 `published` 改成 `metrics_collected`，再来一次 self_monitor 还需要绑吗？（应该不需要，因为 tweet_url 已绑）

4. **/etc/environment chmod** (`scripts/entrypoint.sh`): `chmod 600` 在 cron 仍以 root 跑的前提下 OK。但如果将来 cron 以非 root 跑（best practice），cron 就读不到 env 了——是否应该加注释提醒？

5. **image_path 校验** (`src/twitter_bot.py:_validate_image_path`): 现在白名单是 `/app/tmp_images` 和 `<project>/tmp_images`。dev 机的 `/tmp/...` 临时图（如测试或本地调试）会被拒——是否需要 env 可配？

## P1 — 性能（未修复，请独立判断是否上线前必修）

来自 performance agent 的 HIGH findings：

| File | 问题 | Claude 判断 | Codex 你怎么看？ |
|------|------|------------|----------------|
| `src/reviewer.py:122-129` | `_collect_metrics` 串行 `await metrics_fetcher` 30 个 draft | 上线后再优化（cron 每天 1 次，30×几秒 = 几分钟可接受） | ? |
| `src/miner/distiller.py:255-258` | `full_distill` 串行 LLM call | 同上，nightly cron 可接受 | ? |
| `src/miner/weaver.py:60-70` | N×M `compute_edges` + per-pair `upsert_edge` | 语料小（<500 entries）暂时不优化 | ? |
| `src/reviewer.py:188-213` | N+1 query (per-draft JOIN posts) | 30 个 draft 够小 | ? |
| `src/observers/runner.py:259-273` | `INSERT OR IGNORE` in Python for-loop | 单次几十行可接受 | ? |

**重点请 Codex 判断**：哪些 HIGH 在 90 天 10k follower 目标下会真的变成瓶颈？

## P1 — 测试覆盖（未补，请判断哪些必须先补）

| Module | Gap | Claude 判断 |
|--------|-----|------------|
| `src/miner/distiller.py` | jsonschema 验证只测了 stance 越界，没测 missing required / extra properties | 可补 |
| `src/miner/weaver.py` | symmetric-edge 约束只在 DB 层测，weaver 层无测试 | 可补 |
| `src/miner/feedback.py` | EMA α 系数无数值 pin-test，pattern_cooling 7 天边界未测 | 可补 |
| `src/discord/revise_handler.py` | 只测 happy path，bot.poll 调度未测 | **建议补** |
| `src/reviewer.py` | stale-draft >7d 边界无 pin-test (6d59m vs 7d01m) | 可补 |
| `src/selector/virality_predictor.py` | jsonschema 深度验证缺失 | **建议补** |
| `src/guardrails.py` | "attempt 3 通过" off-by-one 无测试 | **建议补** |
| `src/main.py` | dry-run env 是否真传到 publisher，端到端未测 | 可补 |

**Codex 请判断**：哪些是 silent-failure 风险（生产会 break 但测试看不到）？

## P1 — 可观测性（部分未修，请判断风险）

| File | 问题 | Claude 判断 |
|------|------|------------|
| `crontab.txt` | log 写到 `/app/logs/observe.log`（非 CLAUDE.md 要求的 `pepperbot-<slot>-YYYY-MM-DD.log`）；无 rotation，会无限增长 | **必修** |
| `crontab.txt:7` | `flock -n` 静默退出，contention 无告警 | 可加 trap |
| `src/main.py:290` | 顶层 catch 只 `LOGGER.exception`，无 webhook 告警 | **建议补** |
| `src/observers/runner.py:207` | `_fetch_with_health` 用 `logger.warning` 不带 traceback | 改 `logger.exception` |
| `src/main.py:91-93` | `_cmd_post` 吃掉 writer 异常返回 success=true | **必修**（chronic failure 看不到） |
| `src/reviewer.py:75` | `_alert` 是 stub（`src.alerting` 模块不存在） | 上线前接 Webhook |

## P2 — 架构盲点（请 Codex 独立判断）

1. **`selector` vs `src.selector` import 不一致**：当前 `sys.path` 包含 `src/`，所以 `from selector import db` 和 `from src.selector import db` 都能 import 但是 **不同 module 实例**，会导致同样的类在两个 namespace 中不互相 `isinstance`。Codex 你看一下 `src/main.py` / `src/selector/__init__.py` / `src/selector/topic_scorer.py` 的 import，是否真的有 dual-instance 风险？还是 sys.path 实际只解析到一个？

2. **Self-monitor 状态机跳跃**：在 manual 模式下，`_bind_draft` 直接 `pushed_to_discord → published`，跳过 `approved`。这意味着用户在 X 手动发推但**没点 ✅** 也会被绑成 published。这是 feature 还是 bug？

3. **Self-monitor content 精确匹配**：现在用 `WHERE content = ?`，假如用户在 X 客户端微调一个字（删个标点）就匹配不上。spec §16.13 要求 `content_hash`，但 `drafts` 表无该列。Codex 你建议：
   - (a) 加 migration 给 drafts 加 `content_hash` + 模糊匹配？
   - (b) 加 `LIKE '%...%'` 第一句兜底？
   - (c) 当前精确匹配可接受？

4. **冷启动 corpus-size vs day-based**：retriever 用 `_corpus_size()` 阈值而不是日历日，spec docstring 写的是 "Day 0-3 / 4-14 / 15+"。Codex 判断：这是 spec 错还是 code 错？

5. **Reviewer 不区分 author**：published drafts 都按 `viral_score` 排队加权，但小号大号混淆否？（manual 模式下应该都是大号 @off_tehtarget，但代码没限制）

## Codex 输出格式要求

请按以下格式回我：

```
## P0 验证
- 修复 #1: [PASS / FAIL / CONCERN] + 原因
- 修复 #2: ...
- ...

## P1 必修判断
- 性能 HIGH: 哪些上线前必修，哪些可延后
- 测试覆盖: 哪些是 silent-failure 风险
- 可观测性: 哪些必修

## P2 架构盲点
- 1. selector import: ...
- 2. self_monitor 跳过 approved: ...
- 3. content 匹配: 选 (a)/(b)/(c) + 原因
- 4. 冷启动 spec vs code: ...
- 5. author 混淆: ...

## Codex 额外发现
- (任何 Claude 5-agent review 漏掉的问题)

## 总结
- 上线 GO / NO-GO + 一句话理由
```
