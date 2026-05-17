# S4 Handoff — Configs + Schema Validator

## Files shipped
- `config/sources.yaml` — 5 adapters (x_list_finance, x_list_general (disabled), xueqiu, futu w/ `click_refresh=true`, news_flash)
- `config/topic_blend.yaml` — 6 lanes + `publish_cap_by_week {1:5, 2:7, 3:8}`
- `config/personas.yaml` — finance_{neutral,contrarian,macro} + general_observer
- `config/compliance_lexicon.yaml` — A_kill (15) + B_warn (15) + `compliance_named_stock_threshold: 3`
- `config/political_lexicon.yaml` — 30+ A_kill terms (政治 / 宗教 / 性别 / 黄)
- `config/kol_list_finance.yaml` — real List URL + TODO handles
- `config/kol_list_general.yaml` — all TODO
- `config/persona.md` — 中文金融账号定位（覆写原 AI 创业版）
- `.env.example` — full §5.5 env vars，secret 项标 `# SECRET - do not commit`
- `src/config_loader.py` — pydantic v2 models + `load_all_configs()`
- `tests/unit/test_config_loader.py` — 11 tests，全绿

## Pydantic models (importable from `src.config_loader`)
`AppConfig` · `SourcesYaml` / `SourceConfig` · `TopicBlendConfig` / `LaneConfig` · `PersonasYaml` / `PersonaConfig` · `ComplianceLexicon` · `PoliticalLexicon` · `KolList` / `KolEntry` · `ConfigError`

## 字段速查
- `SourceConfig`: name, enabled, rate_limit_per_hour, tier_default, list_url?, feed_url?, cookie_env_key?, max_posts_per_fetch?, sources?, click_refresh (default False)
- `LaneConfig`: quota>0, hours_utc[0-23], persona (key into personas)
- `TopicBlendConfig`: default_daily_quota, blend (LaneName→LaneConfig), fallback_when_dry, publish_cap_by_week (dict[int,int]; week 1 必填)
- `PersonaConfig`: description, stance_max (1-5)
- `ComplianceLexicon`: A_kill (non-empty), B_warn, compliance_named_stock_threshold (0-5)

## 给其他 subagent 的导入示例
```python
from pathlib import Path
from src.config_loader import load_all_configs, ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
try:
    cfg = load_all_configs(REPO_ROOT)
except ConfigError as exc:
    raise SystemExit(f"config invalid: {exc}")

# S5 Observers
for adapter in cfg.sources.adapters:
    if adapter.enabled:
        spawn_observer(adapter.name, cookie_env=adapter.cookie_env_key)

# S14 Topic Selector
quota = cfg.topic_blend.blend["intraday"].quota
cap = cfg.topic_blend.publish_cap_by_week[current_week]

# S7 Writer / Scorer
stance_max = cfg.personas.personas[lane.persona].stance_max
if any(term in draft for term in cfg.compliance.A_kill):
    reject(draft)
if any(term in draft for term in cfg.political.A_kill):
    reject(draft)
```

## 注意
- 本 module **不读 env vars**。env 处理归别人。
- `kol_list_*.yaml` 里全是 TODO handles，写真实数据前别真发评论。
- `persona.md` 已彻底改成金融方向，确认 voice/templates 不再保留 AI 创业残留是 S8 的事。
