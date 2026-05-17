# CLAUDE.md — 金融账号自动化运营系统

## Project Overview
Twitter 自动化运营系统，当前账号是中文金融方向，不是 AI / OPC 创业方向。
目标：10,000 粉丝 + 1M 月曝光。
技术栈：Python + Playwright + SQLite + 本地 Claude CLI + Cron。

## 核心指令
- 回复用中文，代码注释用英文
- 先给方案，不要直接写代码
- 不确定时列出选项，不要猜测
- 不要用「Great question!」「I'd be happy to help!」等废话
- 文件路径用绝对路径

## Working Mode
- 默认由主 agent 做协调者，不做所有脏活；能并行的研究、审计、执行任务优先交给子 agent
- 每个任务先列 checklist / task list，再开始动手
- 所有子 agent 必须带着明确任务、明确交付物、明确 stop condition 工作
- 始终保持代码库干净：不留临时文件、不留死代码、不留死文件、不额外发明无意义目录层级
- 改动优先沿用现有文件结构，不为一次性操作引入新文件夹或新运行路径

## Tech Stack
- Python 3.11+
- Chrome CDP + Playwright — 连接已登录的 Chrome 浏览器（不独立启动）
- SQLite — 本地数据存储
- 本地 Claude CLI（claude-sonnet-4-6）— 写作、评分、学习默认都走这条
- Cron / schedule — 定时任务
- Obsidian — 日志和策略文档

Do NOT introduce:
- Twitter API（用 Chrome CDP 浏览器控制，不走 API）
- Anthropic Python SDK / API Key
- Google Sheets / Firebase / 任何云存储
- Selenium / Puppeteer（用 Playwright 连接 Chrome CDP）
- Redis / MongoDB（数据层锁定 SQLite）
- LangChain / LangGraph（直接调本地 Claude CLI，不加框架）

## 目录结构
```
花椒的content_2/
├── CLAUDE.md              # 你正在读的文件
├── MEMORY.md              # 跨会话记忆（每次任务结束更新）
├── config/                # 人设、过滤规则、KOL 列表
├── voice/                 # 声音规则、反 AI 腔、爆款技法
├── templates/             # 内容模板和钩子库
├── writer/                # 写手技能定义
├── ops/                   # 运营手册、Playwright UI 规则
├── data/                  # SQLite 数据库
├── logs/                  # Obsidian 兼容日志
└── src/                   # Python 自动化代码
```

## Context Tiers
Tier 1（每次加载）：本文件 — 项目是什么 + 怎么工作
Tier 2（写内容时加载）：`voice/` + `templates/` + `config/persona.md`
Tier 3（按需加载）：`ops/` + `writer/SKILL.md`
Tier 4（忽略）：`data/` + `logs/` — 除非明确要求

## Coding Rules
- 使用 type hints，不用 Any
- async/await 替代同步阻塞
- 单个函数不超过 50 行
- 变量名全拼不缩写（除 db/url/api）
- 只在意图不明显时写注释
- 不留 console.log / print debug
- Playwright 截图用完立刻删除，不留缓存
- 已知 UI 元素位置写入 `ops/playwright_rules.md`，不重复截图

## Chrome CDP 规则
- 启动 Chrome 时加参数: `open -n -a "Google Chrome" --args --user-data-dir=/Users/oxjames/.config/pepperbot/x-main-profile --remote-debugging-port=9224`
- Playwright 通过 `connect_over_cdp` 连接已登录的 Chrome，不独立启动浏览器
- 每次截图后用 `os.remove()` 删除临时文件
- 走过一次的页面路径，把选择器写入 `ops/playwright_rules.md`
- 后续操作优先用已知选择器，不再截图定位
- 截图只在首次探索或页面改版时使用
- 所有等待用 `page.wait_for_selector()` 不用 `time.sleep()`

## 内容生成规则
- 每条推文 ≤ 280 字
- 每条必须配图
- 不用 Twitter API，全部通过 Playwright 操作页面
- 内容必须过 `voice/avoid_slop.md` 检查
- 立场必须坚定，不用「可能」「也许」「或许」
- 写作、评分、学习默认只用本地 Claude CLI
- 默认模型固定为 `claude-sonnet-4-6`
- 不要把写作回退到 Moonshot / Codex / Claude cloud API

## Memory
`MEMORY.md` 记录跨会话的关键发现、最佳实践和已知坑。
每次新任务开始前，先读 MEMORY.md。
每次任务结束后，有新发现就更新进去。

## Cowork 模式发帖流程（零 Computer Use）

> 2026-05-13 验证通过。以下是纯 Chrome MCP 发帖的完整流程，不依赖 Playwright/CDP/Computer Use。

### 核心技术点

1. **window.name 跨域数据桥**
   - `window.name` 在同一个 tab 内跨域导航后仍然保留
   - 用法：先导航到图片 URL → canvas 读取图片 → base64 存入 `window.name` → 导航到 x.com → 从 `window.name` 读取 base64
   - 绕过了 CORS 限制和 Chrome 扩展的安全过滤（base64 不能通过 javascript_tool 返回值传递）

2. **DOM 原型注入绕过 Twitter 文件保护**
   - Twitter 屏蔽了 CDP 的 `DOM.setFileInputFiles`，Chrome MCP 的 `file_upload` 工具无法用
   - 解法：用 `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'files').set.call(fileInput, dt.files)` 直接设置 files 属性
   - 配合 `new DataTransfer()` + `new File([blob], 'image.jpg', {type: 'image/jpeg'})` 构造文件对象
   - 最后 dispatch `change` 事件触发 Twitter 的上传逻辑

3. **og:image 提取**
   - 在文章页用 `document.querySelector('meta[property="og:image"]')?.content` 获取封面图 URL

### 发帖完整步骤

```
Step 1: 获取素材
  - WebSearch 搜索 AI 新闻
  - 找到文章 URL

Step 2: 提取 og:image
  - Chrome MCP navigate 到文章页
  - javascript_tool 提取 og:image URL

Step 3: 图片加载到 window.name
  - Chrome MCP navigate 到图片 URL（同一个 tab）
  - javascript_tool 执行：
    const img = document.querySelector('img') || document.createElement('img');
    // 如果页面直接是图片，img 已经在 DOM 里
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.getContext('2d').drawImage(img, 0, 0);
    window.name = canvas.toDataURL('image/jpeg', 0.85);
    'OK: ' + window.name.length + ' chars'

Step 4: 导航到 Twitter 发帖页
  - Chrome MCP navigate 到 x.com/compose/post（同一个 tab）
  - window.name 数据自动保留

Step 5: 输入文字
  - Chrome MCP find 找到推文输入框
  - Chrome MCP left_click 点击输入框
  - Chrome MCP type 输入推文文字

Step 6: 注入图片
  - javascript_tool 执行：
    const base64 = window.name;
    const byteString = atob(base64.split(',')[1]);
    const mimeString = base64.split(',')[0].split(':')[1].split(';')[0];
    const ab = new ArrayBuffer(byteString.length);
    const ia = new Uint8Array(ab);
    for (let i = 0; i < byteString.length; i++) ia[i] = byteString.charCodeAt(i);
    const blob = new Blob([ab], {type: mimeString});
    const file = new File([blob], 'image.jpg', {type: 'image/jpeg'});
    const dt = new DataTransfer();
    dt.items.add(file);
    const fileInput = document.querySelector('input[type="file"][accept*="image"]');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'files').set.call(fileInput, dt.files);
    fileInput.dispatchEvent(new Event('change', {bubbles: true}));
    window.name = '';  // 清理
    'Image injected'

Step 7: 等待图片预览 + 点发送
  - wait 2-3 秒等图片上传
  - Chrome MCP find 找到 Post 按钮
  - Chrome MCP left_click 点击发送
```

### 已知限制
- `window.name` 只在同一个 tab 内有效，切 tab 会丢数据
- 图片必须能被浏览器直接渲染（不能是需要登录才能看的图）
- base64 数据量大时（>5MB）可能有性能问题，用 JPEG 0.85 质量压缩
- Twitter 的 `input[type="file"]` 选择器可能随改版变化

---

## 运营规则

- 发帖不需要人工确认，直接发布
- 每条推文必须配图，无图不发
- 内容生成后过 avoid_slop.md A 类扫描，命中任何一条删稿重写
- 推文字数 ≤ 280 字
- 声音规则严格遵守 voice_rules.md（行尾无句号、逗号→空格、行间空行、禁结构标签）

---

## 定时任务配置

> 执行方式：本地 macOS crontab → `src/run_slot.sh <slot_name>` → 本地 claude CLI
> Cron 时间为 **UTC**（系统时区 Asia/Shanghai UTC+8）
> SKILL.md 路径：`/Users/oxjames/Documents/Claude/Scheduled/<slot_name>/SKILL.md`

| 任务名 | Cron (UTC) | CST 时间 | 内容 |
|---|---|---|---|
| pepperbot-slot1 | `0 23 * * *` | 07:00 | 2条推文（盘前快评+隔夜美股/中概映射）+ 3条KOL评论 + 5点赞 + 1-2关注 |
| pepperbot-slot2 | `0 4 * * *` | 12:00 | 2条推文（午盘分歧+板块轮动）+ 3条KOL评论(tier2) + 3点赞 + 1关注 |
| pepperbot-slot3 | `0 8 * * *` | 16:00 | 2条推文（尾盘观察+情绪变化）+ 2条KOL评论 + 3点赞 + 1关注 |
| pepperbot-slot4 | `0 12 * * *` | 20:00 | 2条推文（争议观点+财报反应）+ 2条KOL评论 + 3点赞 + 1关注 |
| pepperbot-slot5 | `0 15 * * *` | 23:00 | 2条深度推文（趋势判断+仓位纪律）+ 2条KOL评论 + 2点赞 |
| pepperbot-review | `0 16 * * *` | 00:00 | 数据抓取 + 24h/72h 回测 + 各slot效果对比 + 权重调整 |

**前提条件：** Chrome 必须开着，Claude Code Chrome 扩展必须激活连接。
**日志：** `logs/pepperbot-<slot>-YYYY-MM-DD.log`

---

## 敏感模块红线
- `data/`：SQLite 操作必须用事务，写入前备份
- `config/kol_list.md`：KOL 列表不对外暴露，不写入日志
