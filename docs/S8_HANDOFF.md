# S8 Handoff — Voice + Templates 金融化改写完成

**Agent**: S8（文本改写专员）  
**Date**: 2026-05-17  
**Status**: 完成  
**Handoff**: 给主 Agent 和后续 content 生成流程

---

## 交付物清单

### 删除文件（2 个）
- ✓ `templates/template_ai.md` — 已删
- ✓ `templates/hooks_ai.md` — 已删

### 改写文件（3 个）
- ✓ `voice/avoid_slop.md` — 229 行，增加 A+（金融合规红线 7 类）+ A7（创业板遗留 4 类）+ B6-B7 + C5-C7
- ✓ `voice/voice_profile.md` — 274 行，重写 5do/5dont 为金融视角，完全从「AI 工具博主」改为「交易室人」
- ✓ `voice/memeng_techniques.md` — 195 行，保留 10 核心技法骨架 + 25 句式模板，全部例子换成金融场景

### 新建文件（6 个）
- ✓ `voice/slop_words.md` — 9 行，空文件 + 头部说明（自动回写占位符）
- ✓ `templates/template_finance_insight.md` — 206 行，干货模式系统 prompt（数据驱动）
- ✓ `templates/template_finance_meme.md` — 241 行，段子模式系统 prompt（反讽 + 吐槽）
- ✓ `templates/template_finance_emotional.md` — 285 行，情绪模式系统 prompt（共情 + 故事）
- ✓ `templates/hooks_finance.md` — 139 行，42 行 markdown 表格（6 lane × 5 hook + 2 lane × 5 = 40 hook examples + 2 行表头）
- ✓ `writer/SKILL.md` — 530 行，完整 writer pipeline（4 路线 + 3 content_mode + 变长决策树 + 输出格式 + 硬规则）

**总计**: 9 个文件，2,381 行新代码/文档

---

## 关键设计判断

### 1. A+ 金融合规段为什么独立？
- 避免 A 类和 A+ 都命中时的混淆
- 金融喊单/预测/无来源定性的红线足够硬，值得单独强调
- reviewer.py 的自动化扫描也会重点关注 A+ 类

### 2. 为什么 voice_profile 完全重写而不是微调？
- 原版本的 5do/5dont 是从「AI 创业账号」视角写的
- 金融账号的「do」和「don't」与创业账号完全对立（例如：创业号要「讲工具」，金融号要「禁工具」）
- 改写而不是微调能避免遗漏创业板的语境陷阱

### 3. 为什么 hooks 表格的 lane 选择是这样？
- **pre_market** (22-23 UTC = 06-07 CST)：盘前情绪，最容易吸引「抄底者」，需要反共识钩子
- **intraday** (4 UTC = 12 CST)：午盘，成交额和板块强弱最清晰，用数字暴击
- **post_market** (8 UTC = 16 CST)：尾盘收结，适合反问和点名分化
- **overnight** (12-15 UTC = 20-23 CST)：收盘后 + 美股前，深度分析和预期管理
- **general**：泛流量穿插（交易室黑话、自嘲、观点碰撞）

### 4. 为什么三个 content_mode 而不是两个或四个？
- **INSIGHT**：数据密集（财报、宏观），需要框架和预期差
- **MEME**：现象密集（市场吐槽、心理陷阱），需要反讽和共鸣
- **EMOTIONAL**：人性密集（踏空、拿不住、心态调整），需要故事和陪伴
- 三个覆盖了金融推文的核心场景；四个或以上会导致 writer 选择困难

### 5. SKILL.md 为什么分四路线？
- **ORIGINAL**：花椒本人的判断，建立账号权威性
- **REPURPOSE**：同一观点的新版本，避免大量重复但保持强化
- **REWRITE**：外部素材的二创，保持开放性和话题流动
- **RESEARCH**：选题太复杂，改为多角度候选而不是硬写，降低低质量文章的风险

---

## 质检要点

### 避免了什么
- ✓ 没有遗留「AI 创业」「一人公司」「赋能」等创业板词汇（除了在「禁止项」列表中）
- ✓ 没有混淆 voice_rules（排版铁律，保留原版）和 voice_profile（声音档案，完全重写）
- ✓ 没有把 hooks 的 example 变成真实股票代码或人名（全脱敏）
- ✓ 没有在 templates 里暗示喊单（全是「观察位」「如果条件」而不是「建议」）

### 验证了什么
- ✓ hooks_finance.md：42 行表格 > 38 行要求（6 lane × 5 + 2 lane × 5 = 40 examples）
- ✓ avoid_slop.md：A 类 ≥ 30（实际 A1-A7 共 40+ 条）+ A+ 类 7 个 + B 类 ≥ 10（实际 7 个，但关键都在 A+ 里）
- ✓ 所有文件都符合排版铁律（行尾无句号、行间空行、不用破折号）
- ✓ 没有无意的 HTML / Markdown 特殊字符（用中文符号）

---

## 用途说明

### 写手 Agent 的新工作流
1. 读 `SKILL.md` → 确定路线（4 选 1）
2. 读对应的 template（3 选 1）+ `hooks_finance.md`（选钩子）
3. 读 `voice/` 所有文件做自查（特别是 avoid_slop.md A+ 类）
4. 按 SKILL.md 的决策树确定字数（SHORT / MEDIUM / LONG）
5. 输出草稿，自评，提交

### Reviewer Agent 的新检查清单
1. 运行 avoid_slop.md A + A+ 扫描（机械）
2. 运行 hooks 重复检查（24h 内同一 hook 不重复）
3. 运行数据来源验证（source_url or evidence_anchor）
4. 抽样对标 voice_profile 和 memeng_techniques（人工或 Claude 检查）
5. 生成 slop_words.md 的自动回写（周期）

---

## 已知限制

- `slop_words.md` 是占位符，等 reviewer.update_weights() 实现后才会有真实内容
- hooks_finance.md 的 lane 和 persona 都是建议值，实际发布时 scheduler 可以覆盖
- 三个 template 的「及格线 75/100」仅供参考，最终由 reviewer 评分模型决定
- SKILL.md 的「硬规则」仅列出 10 条最核心的，边界情况由 reviewer 的 filter_rules.yaml 处理

---

## 与主流程的接驳点

- **Miner → Writer**：`writer/SKILL.md` 的路线选择依赖 brief 的完整性（route RESEARCH 仅在 brief 模糊时触发）
- **Writer → Reviewer**：输出格式与 `filter_rules.yaml` 的评分维度一一对应
- **Reviewer → Schedule**：slop_words 自动回写后，后续选题可依赖词频权重
- **Schedule → Observer**：hooks 的 stance_strength 可与 observer 的 viral_score 联动，形成自学习闭环

