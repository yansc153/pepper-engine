# Phase B 清理提案（只列单，不删文件）

> 依据：`docs/UNIFIED_SPEC.md` §16.10 清理顺序、§4.1 / §16.11 subagent 归属。
> 调查范围：`src/`, `tests/`, `tmp_images/`, `tmp_screenshots/`, `data/`, 顶层未提交改动。
> **本提案不执行任何删除/重命名/git 操作**，全部待用户拍板。

---

## A. src/ 文件分类

| 路径 | 分类 | 理由 | 数据风险 | 归属 |
|---|---|---|---|---|
| `src/__init__.py` | KEEP | 包标记，1 行注释 | 无 | — |
| `src/run.py` | DELETE | 旧闭环入口（雪球达人 feed → Discord 转发）。新世界由 `src/main.py` (S11) 接管，且抓取逻辑已外迁。 | 无（脚本） | S11 取代 |
| `src/discord_poster.py` | DELETE | 旧 Discord HTTP 转发器（直接 POST 到 channel）。S13 用 `discord.py` bot 做 emoji 闸门，不复用此模块。 | 无 | S13 重写 |
| `src/scraper.py` | REWRITE | 雪球 Playwright 抓取逻辑要保。新世界拆成 `src/observers/xueqiu_adapter.py` + 通用 image 缓存。`ScrapedItem` dataclass 会被 `Observation` (base.py) 取代。 | 无（运行时）；`tmp_images/` 写入由本文件触发 | S5 |
| `src/config.py` | REWRITE | 当前是 env + 路径常量混合体。S4 拆成 `config/personas.yaml` + `config/sources.yaml` + `pydantic config_loader.py`。env 加载部分可挪到 `src/env.py` 或继续 `.env` 读法。 | 无 | S4 |
| `src/llm.py` | KEEP | S3 直接 fork+微调；已暴露 `call_claude` / `call_claude_json` / `call_claude_with_file`，符合 §4.1 S3 stop condition。微调点：加 `backend` 切换（claude_cli / moonshot）。 | 无 | S3 微调 |
| `src/__init__.py` (observers) | KEEP | 空包标记 | 无 | S2 |
| `src/observers/base.py` | KEEP | S2 已完成的 Observer Protocol + Observation dataclass。新闭环骨干。 | 无 | S2 |
| `src/migrations/__init__.py` | KEEP | 空包标记 | 无 | S1 |
| `src/migrations/runner.py` | KEEP | S1 已实现，幂等。 | 写 `data/pepperbot.db` 前已 mkdir，无破坏 | S1 |
| `src/migrations/001_init.sql` … `005_human_feedback.sql` | KEEP | §16.10 Phase B.1 要求的 5 张新表（drafts state-machine、topic_candidates、human_rejection_pool 等）已经在 003/004/005 里，**Phase B.1 已部分完成**。 | 触发 ALTER；S1 自己确认幂等 | S1 |

---

## B. tests/ 文件分类

当前 `src/` 已经不存在 `eval_pipeline.py`, `opencli_sources.py`, `opencli_publish.py`, `content_planner.py`, `main.py`, `database.py`。所有依赖这些模块的旧测试 import 即失败。

| 路径 | 分类 | 理由 | 谁负责重写 |
|---|---|---|---|
| `tests/test_eval_pipeline.py` | DELETE | import 已删除的 `eval_pipeline`；旧"market_hot_take / earnings_reaction"分类不再是 spec 一部分。 | — |
| `tests/test_opencli_sources.py` | DELETE | 依赖 OpenCLI 抓取桥（spec §4.1 改走 Playwright + 自写 adapter），整个模块不再存在。 | — |
| `tests/test_opencli_publish.py` | DELETE | 同上，发推走 S7 Playwright，非 opencli。 | — |
| `tests/test_content_planner.py` | DELETE | 旧 ScrapedItem + content_planner 概念被 Observation + S14 selector 取代。 | S14（新写选题测试） |
| `tests/test_main_integration.py` | DELETE | 旧 `main._ingest_finance_candidates` 流程不存在；新 main 由 S11 写。 | S11（新写 e2e） |
| `tests/test_database_weights.py` | REWRITE | 概念正确（strategy_weights round-trip），但 `database.py` 已无；改成测 `src/database.py` (S1) 新 schema，可作为 S1 stop-condition 测试种子。 | S1 |
| `tests/test_llm_json.py` | KEEP | 测试目标 `src/llm.py` 仍存在且 `call_claude_json` 仍是公开 API。 | S3 |
| `tests/__pycache__/*` | DELETE | 编译产物，`.gitignore` 应覆盖。 | — |

**新闭环保护测试缺口（Phase B.1 要求）**：需要新增最小测试链路 `draft → pushed_to_discord → published → metrics_collected → learned`。建议作为 S1 stop condition 的一部分：`tests/test_drafts_state_machine.py`（新文件，S1 负责）。

---

## C. 临时目录 / 数据 / 顶层

| 路径 | 分类 | 理由 | 数据风险 |
|---|---|---|---|
| `tmp_images/` (8 文件) | DELETE | 旧 discord 转发缓存图，无元数据指向，规则要求"截图用完立刻删"。整目录可删；新 image 流程统一走 `images/` 或内存。 | 无；非 source-of-truth |
| `tmp_screenshots/` (空) | DELETE | 空目录，且 `.gitignore` 已覆盖类似路径。 | 无 |
| `data/pepperbot.db` + `-shm` / `-wal` | KEEP | 含历史 strategy_weights / cookies 业务数据。S1 migrations 是 ALTER + ADD，**不 DROP**。 | **删除即损失历史权重** |
| `data/browser_session/*cookies.json` | AUDIT | 是大号/小号 cookie 快照。文件名提示已被 `secrets/` 同名替代，但需用户确认 `data/browser_session/` 是否还有 Playwright runtime 在写入。 | 高（账号 cookie） |
| `secrets/*cookies.json` | KEEP | 当前 cookie 主存放点（spec §11.4）；`scripts/verify_cookies.py` 读这里。 | 高，勿动 |
| `pepperbot.log`（项目根） | AUDIT | 根目录 log 不符合 `logs/` 约定；可移到 `logs/` 或删。 | 低 |
| `logs/pepperbot-slot1-2026-05-13.log` | KEEP | Obsidian 兼容日志，spec §运营保留。 | 无 |
| `logs/discord_post.log` | DELETE | 旧 discord_poster 产出的日志；与 `src/discord_poster.py` 同时退役。 | 无 |
| `requirements.txt` (M) | AUDIT | 用户已改动；需对照新 spec 依赖（discord.py, pydantic, playwright, anthropic-cli wrapper 已无）。 | 无 |
| `.env.example` (M) | AUDIT | 同上，需对照新 env 变量（DISCORD_BOT_TOKEN, CLAUDE_CLI_PATH, MOONSHOT_API_KEY?）。 | 无 |
| `.gitignore` (M) | KEEP | 确保 `tmp_*`, `data/*.db*`, `secrets/`, `__pycache__/` 全覆盖；审计一遍。 | 无 |
| `config/sources.yaml` (M) | KEEP | S4 配置基线。 | 无 |
| `voice/memeng_techniques.md` (M) | KEEP | S8 输入语料。 | 无 |
| `scripts/verify_cookies.py` | KEEP | 已完成的 cookie 校验脚本（task #12）。 | 无 |

---

## D. AUDIT 项（专列，待用户确认）

1. **`data/browser_session/` 与 `secrets/` 的 cookie 重复**：哪个才是当前 Playwright 实际读取路径？另一份是否删？（建议保留 `secrets/`，因为 `verify_cookies.py` 已锁定该路径。）
2. **`data/pepperbot.db` 是否要在 Phase B 前做完整 dump 备份**？S1 migrations 虽然增量，但 003/004/005 一旦写错很难回滚。建议 `cp pepperbot.db pepperbot.db.bak_phaseB` 作为防御。
3. **`pepperbot.log`（根目录）**：删 / 归档到 `logs/`？
4. **`requirements.txt` 改动**：用户改动是否已经反映 spec 新依赖（discord.py、pydantic、aiosqlite 等）？需要 diff 审一遍。
5. **`.env.example` 改动**：是否含新增 `DISCORD_BOT_TOKEN` / `DISCORD_CHANNEL_ID` / `MOONSHOT_API_KEY`？
6. **顶层未提交的大量 D（删除）记录**：`lib/`, `steps/`, `human-text-prior/`, `skills/`, 旧 `voice/voice_samples/`, `README.md`, `LICENSE` 等都已是工作树删除但未 commit。建议本次 Phase B 一并 stage + commit，统一一次 "Phase B cleanup" 提交，否则 git 树长期混乱。**这部分不在本次实际删除范围（文件已不在磁盘）**，但需要用户决定何时 `git add -A` 收口。

---

## E. 建议执行顺序（4 步）

> 每步独立 commit；任何一步异常立即停下。

1. **Phase B.1 — 状态机底座（已部分完成）**
   - 验证 S1 migrations 001-005 在干净 DB 上幂等运行
   - 新增 `tests/test_drafts_state_machine.py`（draft → published → learned 链路最小保护）
   - **不动任何旧文件**

2. **Phase B.2 — 删旧入口**
   - `git rm src/run.py src/discord_poster.py logs/discord_post.log`
   - 同步删 `config.py` 中只服务于旧 run.py 的常量（或留到 S4 REWRITE）
   - commit "Phase B.2: drop legacy xueqiu→discord pipeline"

3. **Phase B.3 — 清临时**
   - `rm -rf tmp_images/ tmp_screenshots/`
   - 确认 `.gitignore` 含 `tmp_*/`
   - commit "Phase B.3: purge temp dirs"

4. **Phase B.4 — 审计旧测试**
   - DELETE: `tests/test_eval_pipeline.py`, `test_opencli_sources.py`, `test_opencli_publish.py`, `test_content_planner.py`, `test_main_integration.py`, `tests/__pycache__/`
   - REWRITE: `tests/test_database_weights.py` 交 S1（针对新 schema）
   - KEEP: `tests/test_llm_json.py`
   - 跑 `pytest -x` 确认绿
   - commit "Phase B.4: prune dead tests, keep llm + rewrite db test stub"

5. （前置可选） **Phase B.0 — 一次性收口 git 工作树**：把仓库根目录所有已删 (D) 文件 `git add -A && git commit -m "Phase B.0: stage previously deleted files"`，让后续 4 步在干净基线上运行。

---

## F. 统计

- **总文件审计数**: 30
  - `src/` 10 个 Python + 6 个 migrations SQL = 16
  - `tests/` 7 个 .py（+ pycache 1 组）= 7
  - 临时/数据/顶层 = 12
- **分类汇总**:
  - **KEEP**: 13 (`src/__init__.py`, `src/llm.py`, `src/observers/base.py` + `__init__.py`, `src/migrations/runner.py` + `__init__.py` + 5 SQL, `tests/test_llm_json.py`, `data/pepperbot.db`, `secrets/*`, `scripts/verify_cookies.py`, `logs/pepperbot-slot1-*.log`, `.gitignore`, `config/sources.yaml`, `voice/memeng_techniques.md`)
  - **DELETE**: 10 (`src/run.py`, `src/discord_poster.py`, `tmp_images/*`, `tmp_screenshots/`, `logs/discord_post.log`, `tests/test_eval_pipeline.py`, `test_opencli_sources.py`, `test_opencli_publish.py`, `test_content_planner.py`, `test_main_integration.py`, `tests/__pycache__/`)
  - **REWRITE**: 3 (`src/scraper.py` → S5, `src/config.py` → S4, `tests/test_database_weights.py` → S1)
  - **AUDIT**: 6 (`data/browser_session/`, DB 备份决定, 根 `pepperbot.log`, `requirements.txt`, `.env.example`, 顶层未提交 D 收口)
