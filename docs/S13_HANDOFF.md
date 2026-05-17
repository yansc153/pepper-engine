# S13 HANDOFF — Discord 审批闸门

**Owner**: S13 | 入口: `src/discord/bot.py`

## 3 个公开 API

```python
from src.discord.bot import push_draft_to_discord, poll_reactions, run_poll_once

# 1. 把 candidate 推到 Discord，加 ✅❌🔄 三个 reaction
message_id = await push_draft_to_discord(draft_id)

# 2. cron 每 5 min 扫一次：拉所有 pushed_to_discord 的 draft，看 owner 有没有点
advanced = await poll_reactions()

# 3. cron 同步入口（sync wrapper，等价 asyncio.run(poll_reactions())）
n = run_poll_once()
```

handler 函数（reactions 内部分发，外部一般不直接调）：
`src.discord.publisher_callback.handle_approval`、`rejection_pool.handle_rejection`、`revise_handler.handle_revise`。

## State machine

```
candidate
  └─ push_draft_to_discord →  pushed_to_discord  (discord_message_id 写入)
                               │
                  poll_reactions 每 5min 扫:
                               ├─ ✅ owner → approved → publisher.post_tweet
                               │             ├─ ok  → published (tweet_url)
                               │             └─ 空  → 停在 approved，下轮重试
                               ├─ ❌ owner → rejected + INSERT human_rejection_pool
                               └─ 🔄 owner → candidate (discord_message_id = NULL)
```

非 owner 的 reaction 一律忽略；同一 message 处理过后 `status` 不再是 `pushed_to_discord`，再 poll 自动跳过（幂等）。

## 给 S11 orchestrator 的 cron 调用示例

```cron
# 5 分钟一次扫 Discord 反应（UTC）
*/5 * * * * cd /app && /usr/bin/python3 -m src.discord.bot >> logs/discord-poll.log 2>&1
```

或在 `src/main.py` 里：

```python
import asyncio
from src.discord.bot import poll_reactions, push_draft_to_discord

async def push_all_fresh_candidates() -> None:
    from src.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM drafts WHERE status='candidate' "
            "ORDER BY generated_at LIMIT 5"
        ).fetchall()
    for row in rows:
        await push_draft_to_discord(int(row["id"]))

asyncio.run(push_all_fresh_candidates())
asyncio.run(poll_reactions())
```

## 待补 env（owner 自己加）

`secrets/discord.env` 现只有 `DISCORD_BOT_TOKEN` + `DISCORD_DRAFT_CHANNEL_ID`。
**必须补**：

```
DISCORD_OWNER_USER_ID=<你的 Discord user id>
```

拿法：Discord 客户端 → 设置 → 高级 → 打开开发者模式 → 头像右键 → Copy User ID。
没设这个变量时，`poll_reactions()` 会跳过所有反应（防止任何人点了就发推）。

## 设计要点

- **不长连 WebSocket**：用 `httpx` 直接打 Discord v10 REST，run-to-completion 后退出，吃 cron 调度
- **DRY_RUN=1** 时 `push_draft_to_discord` 不真发，返回 `dryrun-<id>` 占位，方便端到端 smoke
- **Publisher 解耦**：`publisher_callback._post_tweet` lazy import `src.publisher.post_tweet`，S7 还没 land 也能跑测试
- **失败安全**：publisher 抛错 / 返回空 → draft 停在 `approved`，下轮 poll 重试（不重复 ask 用户）
- **依赖**：`httpx`、`python-dotenv`（已在 `requirements.txt`）

## 测试

`pytest tests/unit/discord/ -v` → 20 passed
