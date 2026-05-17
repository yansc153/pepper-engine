# 金融钩子库 / Hooks Finance

> 这些是方向示例，不是逐字模板。
> 同一批输出里不能重复使用同一个首句。
> 每个 hook 脱敏，不含人名、股票代码。

---

## Schema

| hook_pattern | hook_example | topic_lane | post_hour_utc | persona | stance_strength |
|---|---|---|---|---|---|
| 反共识开场 | 今天所有人都看好 X 但有个细节没人提 | pre_market | 22 | finance_neutral | 4 |
| 数字暴击 | 这个数字掉了 50%，市场没反应 | intraday | 4 | finance_analytical | 5 |
| 场景代入 | 如果你是拿了半年的白酒老哥，现在感受如何 | post_market | 8 | finance_empathetic | 3 |
| 反问 | 指数新高最容易误伤谁 | pre_market | 23 | finance_skeptical | 4 |
| 金句压尾 | 最贵的不是买错，而是拿不住 | overnight | 12 | finance_reflective | 2 |

---

## 预发市（UTC 22:00 = 次日 CST 06:00 盘前）

| hook_pattern | hook_example | topic_lane | post_hour_utc | persona | stance_strength |
|---|---|---|---|---|---|
| 反共识开场 | 今天所有人都看好 X 但有个细节没人提 | pre_market | 22 | finance_neutral | 4 |
| 反问 | 指数新高最容易误伤谁 | pre_market | 23 | finance_skeptical | 4 |
| 数字暴击 | 美股昨夜跌了 3%，A 股开盘会怎样 | pre_market | 22 | finance_analytical | 5 |
| 反讽 | 外资持续净买入 但港股还在地板 | pre_market | 23 | finance_contrarian | 5 |
| 身份代入 | 做空手的投资者，现在最怕什么 | pre_market | 22 | finance_empathetic | 3 |

## 上午盘中（UTC 04:00 = CST 12:00 午盘）

| hook_pattern | hook_example | topic_lane | post_hour_utc | persona | stance_strength |
|---|---|---|---|---|---|
| 板块分化点名 | 白酒在跌 新能源也在跌 但这个票在涨 | intraday | 4 | finance_analytical | 4 |
| 数字暴击 | 成交额掉到 2 万亿，持仓者的账户在讲故事 | intraday | 4 | finance_skeptical | 5 |
| 预期差 | 市场以为今天会低开 结果跳高 | intraday | 4 | finance_neutral | 3 |
| 心理陷阱 | 这种涨法最容易吸引追高的人 | intraday | 4 | finance_contrarian | 4 |
| 观察位点名 | 接下来看收盘能不能守住这个点位 | intraday | 4 | finance_analytical | 3 |

## 下午盘中（UTC 08:00 = CST 16:00 尾盘）

| hook_pattern | hook_example | topic_lane | post_hour_utc | persona | stance_strength |
|---|---|---|---|---|---|
| 反共识开场 | 指数新高了 但大多数人账户没新高 | post_market | 8 | finance_skeptical | 5 |
| 场景代入 | 如果你是在 3 块买进的 现在什么感受 | post_market | 8 | finance_empathetic | 3 |
| 分化点名 | 创业板掉队了 航运还在撑 医药也没跟上 | post_market | 8 | finance_analytical | 4 |
| 金句压尾 | 今天的涨跌都是明天的伏笔 | post_market | 8 | finance_reflective | 2 |
| 反讽吐槽 | 成交额破纪录 投资者的账户打破纪录的亏 | post_market | 8 | finance_contrarian | 4 |

## 夜间（UTC 12:00-15:00 = CST 20:00-23:00 收盘后 + 美股前）

| hook_pattern | hook_example | topic_lane | post_hour_utc | persona | stance_strength |
|---|---|---|---|---|---|
| 财报解读 | 这份财报的 headline 是增长 但隐藏的坏消息是 | overnight | 12 | finance_analytical | 5 |
| 金句压尾 | 最贵的不是买错 而是拿不住 | overnight | 12 | finance_reflective | 2 |
| 预期管理 | 如果明天低开 先别急着抄底 | overnight | 13 | finance_skeptical | 4 |
| 心理反思 | 为什么今天涨的时候你没追 反而跌的时候手痒 | overnight | 13 | finance_empathetic | 3 |
| 数据对比 | 去年这个时候成交额 3.5 万亿 现在 2 万亿 说明什么 | overnight | 12 | finance_analytical | 4 |

---

## 泛流量钩子（穿插，非特定时段）

| hook_pattern | hook_example | topic_lane | post_hour_utc | persona | stance_strength |
|---|---|---|---|---|---|
| 交易室黑话 | 持仓群里又开始喊「这波走不完」 | general | * | finance_contrarian | 4 |
| 自嘲建信 | 我 2024 年初清仓踏空了那一波 | general | * | finance_reflective | 2 |
| 观点碰撞 | 有人说 AI 硬件是下一波 有人说早凉了 | general | * | finance_neutral | 3 |
| 心理揭示 | 为什么看对的人反而赚的最少 | general | * | finance_empathetic | 3 |
| 反问激发 | 你的仓位管理和你的收益成正比吗 | general | * | finance_skeptical | 3 |

---

## 说明

### lane 定义

- **pre_market**：盘前信号，次日开盘前发布
- **intraday**：盘中快评，当日 9:30-14:30 间发布
- **post_market**：盘后分析，当日收盘后发布
- **overnight**：隔夜观察，当日收盘后发布，可涉及美股 / 港股隔夜走势
- **general**：泛流量，无特定时段，可穿插任何时刻

### persona 定义

- **finance_neutral**：中立观察者，呈现多元观点
- **finance_skeptical**：质疑者，对市场共识提出异议
- **finance_analytical**：分析师，数据和框架驱动
- **finance_empathetic**：共情者，讲故事和心理
- **finance_reflective**：思考者，给出深层启发
- **finance_contrarian**：逆向者，踩踏市场主流，但不是为了赚眼球

### stance_strength 定义

1-5 分，表示观点的确定性强度：
- **1-2**：观察 / 疑问 / 反思，不包含确定性判断
- **3**：中立观点，既有利多也有利空
- **4**：明确判断，偏多或偏空，但允许转圜
- **5**：硬判断，非常确定，很少改口

---

## 使用规则

1. **每条推文选一个 hook**（不混合）
2. **24h 内同一个 hook 不重复**（如果需要类似的 pattern，换个 example）
3. **同一人物/板块 24h 内点名不超过 2 次**（避免重复轰炸）
4. **hook 的 stance_strength 要和当时市场节奏匹配**
   - 大涨：多用 skeptical / contrarian（反共识）
   - 大跌：多用 analytical / empathetic（理性 / 共情）
   - 震荡：多用 neutral / reflective（观察 / 反思）

5. **每周的 5 个 lane 均衡发布**
   - 不能一直只发 pre_market
   - 不能一直只发 intraday
   - 泛流量可随时插播

---

## 反面教材（什么不算好的 hook）

**不好**：「今天市场怎么样」（太宽泛，没有观点）
**好**：「指数新高但白酒掉队」（具体、有分化）

**不好**：「值得关注」（空话，无观点）
**好**：「我会盯下周的库存数据」（有明确观察位）

**不好**：「建议大家小心」（教学口气）
**好**：「我现在偏空，仓位缩到 30%」（个人判断）

**不好**：「这波一定涨」（喊单）
**好**：「如果这几个数据都满足，就有涨的可能」（条件化表述）

---

## 学习记录

- [2026-05-17] v1.0：5 个时段 lane × 5 个 hook_pattern + 2 个泛流量 lane × 5 hook = 35 行 + 表头 = 38 行表格；脱敏处理，避免股票代码和人名
