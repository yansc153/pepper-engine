# 端到端 Smoke Checklist

最后一次自动化 smoke 跑通时间：2026-05-18 03:07 UTC
测试套件状态：**352 passed, 0 failed**

## ✅ Auto smoke (已通过)

| 命令 | 结果 | 用途 |
|------|------|------|
| `python3 -m src.main test --dry-run` | exit 0, 7/7 modules ok, db_ok | 模块 import + DB ping |
| `python3 -m src.main mine` | exit 0, entries_distilled=0 (空库) | distill+weave 链路通 |
| `python3 -m src.main remine` | exit 0 | full re-weave 通 |
| `python3 -m src.main review` | exit 0, ReviewReport(posts_reviewed=0) | reviewer 闭环通 |
| `python3 -m src.main post` | exit 0, skipped=no_fresh_topic | 选题→写→Discord 通到入口 |
| `python3 -m src.main discord_poll` | exit 0, drafts_advanced=0 | Discord REST 调用通 |
| `python3 -m src.main self_monitor` | exit 0, skipped=twitter_handle_unset | self_monitor 守卫起作用 |
| Migration 006 | applied | drafts.content_hash 列就位 |

**Bug 修了一个**：`from selector import ...` 在 `python -m src.main` 下找不到模块（conftest.py 的 sys.path 注入只对测试有效），已在 `src/main.py` 头部加同样的 sys.path 注入。

## ⏳ 上线前必做（人工操作）

### 1. 轮转所有泄露的凭证
泄露过的（聊天里贴过明文）：
- [ ] Discord bot token (`secrets/discord.env:DISCORD_BOT_TOKEN`)
- [ ] X 小号 cookie (`secrets/x_xiaohao_cookies.json`)
- [ ] Futu cookie (`secrets/futu_cookies.json`)
- [ ] Xueqiu cookie (`secrets/xueqiu_cookies.json`)

### 2. （可选）独立 Discord alert thread
告警**默认推到 `DISCORD_DRAFT_CHANNEL_ID`**（复用 bot，无需额外 webhook）。如果想隔离：

1. 在 draft 频道里建一个 thread `pepperbot-alerts`
2. 复制 thread id（URL `/channels/<channel>/<thread_id>`）
3. 填到 `secrets/discord.env:ALERT_THREAD_ID=<thread_id>`
4. 验证：
   ```bash
   set -a; source secrets/discord.env; set +a
   python3 -c "from src.alerting import alert; print(alert('smoke test', context={'env':'local'}))"
   ```
   应在 Discord 看到 `🚨 pepperbot alert — smoke test`

### 3. 验证 cookies（脚本只读，不外泄）
```bash
python3 scripts/verify_cookies.py
```
3 个 cookie 都应该 ✅，并显示剩余天数。

## 🔄 生产烟测（手动跑一遍完整链路）

按顺序，每步**人工确认**结果再走下一步。

### Step 1: observe（真抓数据）
```bash
DB_PATH=data/pepperbot.db python3 -m src.main observe
```
预期：JSON summary 含 `observations_inserted > 0`，DB 里 `reaction_observations` 增加。
```bash
sqlite3 data/pepperbot.db "SELECT source, COUNT(*) FROM reaction_observations GROUP BY source"
```

### Step 2: post（生成草稿 + 推 Discord）
```bash
DB_PATH=data/pepperbot.db python3 -m src.main post
```
预期：JSON summary 含 `draft_id` 和 `discord_message_id`。Discord 频道里应该收到一条带 ✅/🔄/❌ 反应的草稿消息。

### Step 3: 人工审批
在 Discord 上对草稿点 ✅。然后跑：
```bash
DB_PATH=data/pepperbot.db python3 -m src.main discord_poll
```
预期：`drafts_advanced=1`，DB 里该 draft 的 `status` 变成 `approved`。

### Step 4: 人工在 X 客户端发推
打开 X，复制 draft.content 发布。

### Step 5: self_monitor 绑回（≤6 小时内或手动触发）
```bash
TWITTER_HANDLE=off_tehtarget DB_PATH=data/pepperbot.db python3 -m src.main self_monitor
```
预期：JSON summary 含 `bound >= 1`。DB 验证：
```bash
sqlite3 data/pepperbot.db \
  "SELECT id, status, tweet_url FROM drafts WHERE id=<draft_id>"
```
`status` 应为 `published`，`tweet_url` 非空。

### Step 6: 等几小时让推文积累互动数据（或手动加 fake metrics）

### Step 7: review（学习）
```bash
DB_PATH=data/pepperbot.db python3 -m src.main review
```
预期：JSON summary `posts_reviewed >= 1`，`metrics_collected >= 1`。DB 验证：
```bash
sqlite3 data/pepperbot.db \
  "SELECT id, status FROM drafts WHERE id=<draft_id>"
```
`status` 应推进到 `learned`（这次新加的状态机修复就是确保这个）。

### Step 8: 验证 strategy_weights 被更新
```bash
sqlite3 data/pepperbot.db "SELECT * FROM strategy_weights"
```
应有该 lane 的 weight 被 reviewer 调整。

## ⏸️ 已知 Codex 标 "可延后" 的项

性能优化（cron 每天 1-2 次，当前规模不会触底）：
- `reviewer._collect_metrics` 串行 30 个 `await metrics_fetcher` → 应改 `asyncio.gather`
- `full_distill` 串行 LLM call → 应批量
- `weave_nightly` N×M `compute_edges` → 应单 transaction 批量

观测优化：
- `source_health.consecutive_failures` 无 threshold 消费者（值会涨但无告警）— 上线后跑几天看实际数据再加阈值

## 总结

- 测试套件 352/352 ✅
- 全部 7 个 cron 命令在 dry-run 下退出 0 ✅
- 4 个 Codex CRITICAL 已修 ✅
- 1 个 import 路径 bug 在 smoke 阶段抓到并修了 ✅

**剩下需要的是**：用户去轮转 cookie + 建 webhook + 跑生产烟测 Step 1-8。
