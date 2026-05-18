"""Unit tests for src.config_loader.

Run from repo root::
    pytest tests/unit/test_config_loader.py -v
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from src.config_loader import (
    AppConfig,
    ConfigError,
    SourcesYaml,
    load_all_configs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo_clone(tmp_path: Path) -> Path:
    """Copy real config/ into a tmp dir so tests can mutate it."""
    dest = tmp_path / "repo"
    (dest / "config").mkdir(parents=True)
    for src in (REPO_ROOT / "config").glob("*.yaml"):
        shutil.copy(src, dest / "config" / src.name)
    return dest


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_load_real_configs_succeeds() -> None:
    """The shipped yaml files all validate."""
    cfg = load_all_configs(REPO_ROOT)
    assert isinstance(cfg, AppConfig)
    # spot check: 6 adapters total (added eastmoney_guba)
    names = {a.name for a in cfg.sources.adapters}
    assert names == {
        "x_list_finance",
        "x_list_general",
        "xueqiu",
        "futu",
        "news_flash",
        "eastmoney_guba",
    }


def test_app_config_is_importable_as_module_object() -> None:
    """Other subagents can import AppConfig and pass it around."""
    cfg = load_all_configs(REPO_ROOT)
    # Reproduce the typical downstream usage: pluck nested fields
    assert cfg.topic_blend.default_daily_quota > 0
    assert cfg.compliance.A_kill
    assert cfg.personas.personas["finance_neutral"].stance_max == 4
    # Round-trip back to dict so downstream JSON serialization works
    assert "sources" in cfg.model_dump()


def test_futu_adapter_has_click_refresh_true() -> None:
    cfg = load_all_configs(REPO_ROOT)
    futu = next(a for a in cfg.sources.adapters if a.name == "futu")
    assert futu.click_refresh is True


# ---------------------------------------------------------------------------
# failure cases
# ---------------------------------------------------------------------------


def test_sources_missing_cookie_env_key_raises(repo_clone: Path) -> None:
    """xueqiu adapter without cookie_env_key must blow up at load time."""
    (repo_clone / "config" / "sources.yaml").write_text(
        """
adapters:
  - name: xueqiu
    enabled: true
    feed_url: "https://xueqiu.com/v4/statuses/topic.json"
    rate_limit_per_hour: 24
    tier_default: 2
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_all_configs(repo_clone)
    assert "cookie_env_key" in str(excinfo.value)


def test_quota_sum_mismatch_warns_but_loads(
    repo_clone: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """quota total != default_daily_quota should log a warning, not raise."""
    (repo_clone / "config" / "topic_blend.yaml").write_text(
        """
default_daily_quota: 999
blend:
  pre_market:          { quota: 2, hours_utc: [22],     persona: finance_neutral }
  intraday:            { quota: 3, hours_utc: [2],      persona: finance_neutral }
  post_market:         { quota: 2, hours_utc: [8],      persona: finance_contrarian }
  overnight:           { quota: 1, hours_utc: [15],     persona: finance_macro }
  general_tech_ai:     { quota: 2, hours_utc: [12],     persona: general_observer }
  general_meme_career: { quota: 2, hours_utc: [13],     persona: general_observer }
fallback_when_dry: general_tech_ai
publish_cap_by_week:
  1: 5
  2: 7
  3: 8
""",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="src.config_loader"):
        cfg = load_all_configs(repo_clone)
    assert cfg.topic_blend.default_daily_quota == 999
    assert any("default_daily_quota" in r.message for r in caplog.records)


def test_persona_stance_max_above_5_raises(repo_clone: Path) -> None:
    (repo_clone / "config" / "personas.yaml").write_text(
        """
personas:
  finance_neutral:    { description: "X", stance_max: 6 }
  finance_contrarian: { description: "X", stance_max: 5 }
  finance_macro:      { description: "X", stance_max: 4 }
  general_observer:   { description: "X", stance_max: 3 }
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_all_configs(repo_clone)
    assert "stance_max" in str(excinfo.value)


def test_empty_a_kill_raises(repo_clone: Path) -> None:
    (repo_clone / "config" / "compliance_lexicon.yaml").write_text(
        """
A_kill: []
B_warn: ["抄底"]
compliance_named_stock_threshold: 3
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_all_configs(repo_clone)
    assert "A_kill" in str(excinfo.value)


def test_publish_cap_missing_week_1_raises(repo_clone: Path) -> None:
    (repo_clone / "config" / "topic_blend.yaml").write_text(
        """
default_daily_quota: 12
blend:
  pre_market:          { quota: 2, hours_utc: [22],     persona: finance_neutral }
  intraday:            { quota: 3, hours_utc: [2],      persona: finance_neutral }
  post_market:         { quota: 2, hours_utc: [8],      persona: finance_contrarian }
  overnight:           { quota: 1, hours_utc: [15],     persona: finance_macro }
  general_tech_ai:     { quota: 2, hours_utc: [12],     persona: general_observer }
  general_meme_career: { quota: 2, hours_utc: [13],     persona: general_observer }
fallback_when_dry: general_tech_ai
publish_cap_by_week:
  2: 7
  3: 8
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_all_configs(repo_clone)
    assert "week" in str(excinfo.value).lower() or "1" in str(excinfo.value)


def test_missing_yaml_raises_file_not_found(repo_clone: Path) -> None:
    (repo_clone / "config" / "sources.yaml").unlink()
    with pytest.raises(FileNotFoundError):
        load_all_configs(repo_clone)


def test_blend_references_unknown_persona_raises(repo_clone: Path) -> None:
    """Cross-yaml check: lane.persona must exist in personas.yaml."""
    (repo_clone / "config" / "topic_blend.yaml").write_text(
        """
default_daily_quota: 12
blend:
  pre_market:          { quota: 2, hours_utc: [22],     persona: nonexistent_persona }
  intraday:            { quota: 3, hours_utc: [2],      persona: finance_neutral }
  post_market:         { quota: 2, hours_utc: [8],      persona: finance_contrarian }
  overnight:           { quota: 1, hours_utc: [15],     persona: finance_macro }
  general_tech_ai:     { quota: 2, hours_utc: [12],     persona: general_observer }
  general_meme_career: { quota: 2, hours_utc: [13],     persona: general_observer }
fallback_when_dry: general_tech_ai
publish_cap_by_week:
  1: 5
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_all_configs(repo_clone)
    assert "nonexistent_persona" in str(excinfo.value)


def test_sources_yaml_is_standalone_validatable() -> None:
    """SourcesYaml model can be used directly without loading the rest."""
    data = {
        "adapters": [
            {
                "name": "news_flash",
                "enabled": True,
                "rate_limit_per_hour": 30,
                "tier_default": 0,
                "sources": ["eastmoney_kuaixun"],
            }
        ]
    }
    cfg = SourcesYaml.model_validate(data)
    assert cfg.adapters[0].name == "news_flash"
