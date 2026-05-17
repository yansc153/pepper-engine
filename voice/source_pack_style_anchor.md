# Source Pack + Style Anchor — Finance Main Account

> 用途：给后续 prompt assembly 读取。
> 目标：把金融素材先压成可写的 source pack，再按中文金融号的口气规划角度、写作和评分。
> 边界：只服务 A股 / 港股 / 美股金融账号。不要写 AI 创业、工具测评、OPC、小红书生活经验贴。

---

## 1. Source Pack Fields

每条素材进入写作前，先整理成下面字段。事实来源和风格参考必须分开，不能让 style pack 里的例子变成事实。

| Field | Required | Meaning |
|---|---:|---|
| `source_id` | yes | 稳定 ID，便于去重和回溯 |
| `source_url` | yes | 原始链接 |
| `source_name` | yes | 雪球 / 富途 / 财报 / 交易所 / KOL / 新闻源 |
| `source_type` | yes | `market_move` / `earnings` / `filing` / `macro_data` / `broker_note` / `kol_post` / `rumor` |
| `published_at` | yes | 发布时间或财报日期 |
| `market_scope` | yes | `A股` / `港股` / `美股` / `跨市场` |
| `tickers` | no | 股票、ETF、指数、板块名 |
| `raw_claim` | yes | 原素材最核心的一句话，不改写 |
| `evidence_anchor` | yes | 可截图、可核验的数字 / 日期 / 原话 / 图表 |
| `price_or_flow_signal` | no | 涨跌幅、成交额、资金流、期权、财报指引、库存、运价等 |
| `reader_cost` | yes | 读者如果没看懂会错过什么、踩什么坑、付出什么代价 |
| `consensus_to_push_against` | no | 市场正在偷懒相信的共识 |
| `angle_candidates` | yes | 3-5 个可写角度，先规划再写 |
| `finance_risk` | yes | `low` / `medium` / `high`，说明是否接近荐股、谣言、夸大收益 |
| `image_asset` | no | 原图 URL / 本地路径 / 截图证据。X 原图必须保持原貌，不加画布、裁切、调色、水印 |
| `style_refs` | no | 3-5 条结构参考，只学节奏和结构，不复制事实 |

最低可写标准：

- 有 `raw_claim`
- 有 `evidence_anchor`
- 有 `reader_cost`
- 有至少 3 个 `angle_candidates`
- `finance_risk` 不是无法处理的高风险谣言

缺任何一项，先补 source pack，不要直接写。

---

## 2. Angle Planning Rules

写作前先选角度。不要从“我想怎么写”开始，要从“这条素材让谁账户疼”开始。

### Good Finance Angles

1. **账户体感**
   - 指数很强，但哪些人账户没新高
   - 板块涨了，但谁没吃到

2. **预期差**
   - 市场以为利好，实际只利好某一类资产
   - 数据看着热，真正该盯的是另一个数字

3. **交易代价**
   - 追高成本
   - 踏空成本
   - 拿不住的原因
   - 满仓和看多之间的差别

4. **板块/人群点名**
   - 不写“市场分化”
   - 直接写白酒、新能源、红利、医药、AI 硬件、券商、半导体、航运、银行、高股息、科技成长等

5. **下一步观察位**
   - 成交额、量能、库存、运价、利润率、指引、期权 IV、资金流、汇率、美债收益率

### Angle Selection Order

1. 先找最硬的 `evidence_anchor`
2. 再问这个事实伤到谁、帮到谁、骗到谁
3. 再把角度压成一句判断
4. 最后才决定结构和开头

### Reject These Angles

- 只是复述新闻
- 只是说“值得关注”
- 只是把利好/利空翻译一遍
- 把金融题硬拐成 AI 工具、创业、效率、workflow
- 没有数字、板块、人群、成本、观察位

---

## 3. Human-Like Finance Writing Cues

参考 artinmemes-style 市场写法：热信号进场，快速翻成可交易地图，再补一个人话判断。不是研报，不是教程。

### Positive Cues

- 第一行直接给判断，不铺背景
- 一条内容只打一个确定性判断，不要边讲边打圆场
- 用“市场触发 -> 可交易信息 -> 人话判断 -> 一锤子结尾”
- 有 ticker、板块、日期、成交额、涨跌幅、财报数字或具体观察位
- 可以有交易室口气：吃肉、踏空、上车、赌财报、妖股、香、别上头
- 判断要有体感：我偏多但不拉满、这票不是不能看是不能追、指数新高不等于你赚钱
- 允许不完美，允许短句跳跃，但事实不能错
- 长列表可以用，但必须是有用清单：tickers、日期、财报周、板块排序、观察位
- 结尾要落地：一句判断、一个观察位、或者直接戛然而止

### Sentence Shape

```text
市场触发

最硬的数字 / 原话

这件事真正影响谁

我的判断 / 仓位态度

下一步盯什么
```

每行尽量短。行间空行。不要结构标签。不要行尾句号。
不要在结尾解释“我会怎么做”。不要 CTA。不要“你怎么看”。

### Better Than Generic Judgments

| Generic | Finance-account Rewrite |
|---|---|
| 值得关注 | 我会盯成交额能不能守住 2.5 万亿 |
| 更好的状态是 | 如果看多但怕回撤，仓位就别一次拉满 |
| 听起来舒服吗？当然舒服 | 这句话舒服，但账户不靠舒服赚钱 |
| 市场风险偏好提升 | 全 A 放量了，但白酒和新能源老哥未必笑得出来 |
| 未来空间很大 | 先看下季指引能不能把这个估值接住 |

---

## 4. Negative Cues

命中下面任一类，重写或丢弃。

### Hard No

- 投资建议口吻：建议买入、强烈推荐、稳赚、目标价、跟我
- 无来源定性：业内人士表示、市场普遍认为、数据显示但不写数据
- 研报摘要腔：长期来看、具有重要意义、基本面持续向好、估值修复空间
- AI 腔：赋能、构建、打造、生态、闭环、抓手、范式、底层逻辑
- 平台错位：AI 创业、工具测评、OPC、一人公司、小红书生活经验贴
- 低信息填充：值得关注、更好的状态是、听起来舒服吗当然舒服、你怎么看
- 对称句式过多：不是 X 而是 Y、真正的 X 是 Y、关键不在 A 而在 B

### Finance-Specific Failure

- 没有数字
- 没有板块或人群
- 没有 reader cost
- 只讲观点不讲观察位
- 情绪大于事实
- 把 style_refs 里的例子当事实写进去
- 原图被加边框、裁切、改色、加水印或做成新海报
- 结尾像分析师总结，而不是像人突然停住
- 出现太多“我偏多但”“我不会”“后面先看”这类自我管理句，导致确定性泄劲

---

## 5. 0-100 Style Scoring Anchor

这个分数只评“是否适合本金融账号 + 是否像人话市场写作”，不替代事实审核。

| Dimension | Points | Pass Signal |
|---|---:|---|
| Market trigger | 15 | 第一屏能看出是哪个实时市场信号、财报、KOL 热帖、宏观数据或板块异动 |
| Evidence anchor | 15 | 至少一个数字、日期、原话、图表或可核验 source |
| Tradable map | 15 | 能落到 ticker、板块、ETF、观察位、仓位态度或资金流 |
| Reader cost | 15 | 说清楚踏空、追高、拿不住、误判、仓位失衡的具体代价 |
| Human judgment | 15 | 有明确个人判断，不装中立，不像研报摘要 |
| Compression | 10 | 没有背景课，短句推进，一行一个动作 |
| Finance-account fit | 10 | 只写 A股 / 港股 / 美股，不混 AI 创业/工具叙事 |
| Ending | 5 | 以观察位、钉子句或交易室式短判断收住 |
| Determinism | 5 | 主观判断够硬，不左右摇摆，不用 CTA 收尾 |

### Thresholds

- `90-100`: 可直接进入发布前事实/合规审核
- `80-89`: 可用，需要小修句子或结尾
- `70-79`: 有金融内容，但人味、代价或交易地图不够
- `55-69`: 像普通财经总结，需要重写角度
- `<55`: 丢弃，不进入改写循环

### Score Before Publishing

最低通过线：`80`。

任何事实无法回到 `source_url` / `evidence_anchor`，即使风格分高也不发。
