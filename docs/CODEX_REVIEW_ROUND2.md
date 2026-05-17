# Codex Review Round 2 — content_2 (Pepperbot)

> 上一轮（`docs/CODEX_REVIEW_CHECKLIST.md`）你给出 **NO-GO**，blocker 是 silent failure / self_monitor 绑定 / 学习状态机假推进。Claude 按你的优先级 (`silent failure > self_monitor > 测试 > 性能`) 做了一轮修复。这是 second-opinion。

项目路径：`/Users/oxjames/Downloads/CC_testing/花椒的content_2/`
测试套件：**355 passed, 0 failed**（比 round 1 多 25 个新测试）

---

## Round 1 → Round 2 改了什么

### T1 — Silent failure & observability（你上轮列为 blocker）

| 你上轮的指责 | Claude 这轮的修复 | 文件 |
|------|------|------|
| `_cmd_post` 吃掉 writer 异常返回 success=true | catch 块改成 `return (1, ...)` + 调 `alert()` | `src/main.py:90-100` |
| cron 日志不符约定 + 无 rotation | 新 `scripts/cron_wrap.sh` 统一处理：`pepperbot-<slot>-YYYY-MM-DD.log`、14 天 retention、每行过 wrap | `scripts/cron_wrap.sh`, `crontab.txt` |
| flock `-n` 静默退出 | wrap.sh 检测到 lock 持有时 log + alert + exit 0（非 failure 但可见） | `scripts/cron_wrap.sh:33-42` |
| `_alert` 是 stub | 实现 `src/alerting.py`，通过 **Discord bot REST**（不再要 webhook），never raises | `src/alerting.py`, `src/reviewer.py:75` |
| main.py 顶层异常无告警 | 加 `except Exception` 调 alerting | `src/main.py:296-304` |

### T2 — self_monitor 业务正确性（你上轮列为最大风险）

| 你上轮的指责 | Claude 这轮的修复 | 文件 |
|------|------|------|
| `WHERE content = ?` 精确匹配，X 上改一个字就绑不上 | 三阶段匹配：hash → normalized hash → difflib similarity ≥ 0.85 | `src/observers/self_monitor_adapter.py:200-270` |
| `drafts` 无 `content_hash` 列 | migration 006 加列 + index | `src/migrations/006_drafts_content_hash.sql` |
| 没有 normalize 工具 | 新 `src/content_match.py`：NFKC fold + 去 ASCII/中文标点 + 去 zero-width 空格 | `src/content_match.py` |
| writer 不写 hash | `_persist_draft` 计算 normalized hash 写入 | `src/writer.py:308-340` |
| **决策保留**：manual 模式下用户没点 ✅ 也绑回 published（你说有风险但用户拍板保留） | 但加了 `AND status IN ('pushed_to_discord', 'approved')` 防止误绑 candidate/rejected | `src/observers/self_monitor_adapter.py:226-228` |

### T3 — 关键 silent-failure 测试

新增测试文件（25 个新 test）：
- `tests/unit/test_alerting.py` — 8 tests，覆盖 bot REST + fallback channel + thread + 错误吞掉
- `tests/unit/test_content_match.py` — 10 tests，hash 幂等、标点编辑不破坏、similarity 阈值
- `tests/unit/test_main.py::test_post_returns_exit_1_when_writer_raises` — 你 P1 列的 silent-failure 修复
- `tests/unit/observers/test_self_monitor_adapter.py` × 4 — 三阶段匹配 + candidate 跳过
- `tests/unit/test_writer.py::test_write_draft_third_attempt_can_succeed` — 你 P1 列的 attempt-3 off-by-one
- `tests/unit/selector/test_topic_scorer.py::test_pick_top_topic_honors_strategy_weights` — 验证 reviewer→selector 闭环

### Bonus（你上轮 P2 提的 bug，smoke 阶段抓到了真实例）

| Round 1 P2 提到 | Round 2 实证 + 修复 |
|------|------|
| `selector` vs `src.selector` import 不一致 | smoke `python -m src.main post` 真的 ModuleNotFoundError，已在 `src/main.py:32-37` 加 `sys.path.insert(src)` |

### 用户不再需要做（基于你上轮 P1 提议）

- ~~建 Discord webhook~~ → 复用现有 bot token + draft channel id；可选 ALERT_THREAD_ID 隔离

---

## Round 2 验证请求

### A. 上一轮 NO-GO blocker 是否都解除？

请检查每一项是 PASS / FAIL / CONCERN：

1. **`_cmd_post` silent failure** —
   验证：`src/main.py:89-100`、`tests/unit/test_main.py::test_post_returns_exit_1_when_writer_raises`
   命令：`python3 -m pytest tests/unit/test_main.py::test_post_returns_exit_1_when_writer_raises -v`

2. **cron 日志命名 + rotation + flock 可见** —
   验证：`scripts/cron_wrap.sh`（shell syntax + 4 个责任：rotate / lock / run / alert）+ `crontab.txt` 6 行都过 wrap

3. **`_alert` 真实接 Discord** —
   验证：`src/alerting.py`、8 个 alerting 测试。
   特别看：fallback 链 `ALERT_CHANNEL_ID → DISCORD_DRAFT_CHANNEL_ID`，thread_id 拼接。

4. **self_monitor 三阶段 + status 过滤** —
   验证：`src/observers/self_monitor_adapter.py:_bind_draft`、4 个新 stage test
   特别看：normalize 是否漏了某种 punctuation；fuzzy 阈值 0.85 是否合理。

5. **strategy_weights 反馈闭环** —
   验证：`src/selector/topic_scorer.py:pick_top_topic`、`test_pick_top_topic_honors_strategy_weights`

6. **state machine 推进** —
   验证：`src/reviewer.py:_advance_draft_status`、流程 `published → metrics_collected → learned`

### B. 你上一轮没专门点名但可能有问题的点

请你重点扫一下：

1. **`content_match.normalize_text`**: 我们用 `unicodedata.normalize("NFKC")` + 一个 punct regex。
   - 这个 regex 是否能正确处理中英混排（如 `「A股」` 里的全角引号）？
   - `similarity()` 用 `difflib.SequenceMatcher` 对长中文 tweet 性能如何？

2. **`cron_wrap.sh` 的 alert payload**: 我们把日志 tail 用 `sed 's/"/\\"/g'` 转义，但只转义双引号——遇到 backslash、单引号、控制字符可能炸 JSON。
   - 风险等级？

3. **`alerting.py` 在异步上下文**: `httpx.Client` 是 sync。reviewer / main.py 都从异步 caller 调它，会不会阻塞 event loop？我用 5s timeout，但仍然 sync。
   - 该改 async 吗？

4. **`src/main.py` 顶层 sys.path 注入**: 这会污染 `python -m src.main` 之外的调用吗？比如 `import src.main` from another script。

5. **Migration 006 是 ALTER TABLE ADD COLUMN**: 老数据库的 `drafts.content_hash` 默认 NULL。self_monitor 第一阶段 hash 查询会跳过这些老 draft，依赖 fuzzy fallback。
   - 是否需要 backfill 脚本？

### C. 端到端 smoke 我跑了什么

- `python3 -m src.main test`：7/7 modules ok, db_ok
- `python3 -m src.main mine / remine / review`：exit 0，空库友好兜底
- `python3 -m src.main post`：exit 0, skipped=no_fresh_topic
- `python3 -m src.main discord_poll`：exit 0
- `python3 -m src.main self_monitor`：exit 0, skipped=twitter_handle_unset

**未跑**（需要真实 cookie + 用户参与）：observe 真抓取、Discord 真推送、X 真发推、self_monitor 真绑定、review 真学习。
完整序列在 `docs/SMOKE_CHECKLIST.md`。

---

## Codex Round 2 输出格式

```
## A. NO-GO blocker 解除验证
- 1. _cmd_post silent failure: PASS / FAIL / CONCERN + 一句话
- 2. cron 日志: ...
- 3. _alert: ...
- 4. self_monitor: ...
- 5. strategy_weights: ...
- 6. state machine: ...

## B. 新增疑问点判断
- 1. normalize regex: ...
- 2. cron_wrap.sh JSON 转义: ...
- 3. alerting sync httpx: ...
- 4. sys.path 注入污染: ...
- 5. 老 draft backfill: ...

## C. Round 2 新发现
- (任何 Claude 漏掉的)

## 总结
- 上线 GO / GO-with-risk / NO-GO + 一句话
- 如果不是 GO：剩下的最关键 1-2 项是什么
```
