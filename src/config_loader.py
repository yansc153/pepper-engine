"""Config loader and pydantic v2 schemas for PepperBot.

Reads all yaml files under ``config/`` and returns a validated ``AppConfig``.
This module only validates YAML structure. Environment variable handling
lives elsewhere (e.g. ``src/runtime/env.py``).

Usage::

    from pathlib import Path
    from src.config_loader import load_all_configs, ConfigError

    try:
        cfg = load_all_configs(Path("/path/to/repo_root"))
    except ConfigError as exc:
        ...
    cfg.sources.adapters[0].name  # "x_list_finance"

Failure modes:
- yaml file missing               -> FileNotFoundError
- yaml structure invalid          -> ConfigError (includes file + field)
- semantic violation (e.g. empty
  A_kill, missing week 1 cap)     -> ConfigError
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when a config file fails schema or semantic validation."""


# ---------------------------------------------------------------------------
# sources.yaml
# ---------------------------------------------------------------------------


AdapterName = Literal[
    "x_list_finance",
    "x_list_general",
    "xueqiu",
    "futu",
    "news_flash",
]


class SourceConfig(BaseModel):
    """A single observer adapter entry from ``config/sources.yaml``."""

    model_config = ConfigDict(extra="forbid")

    name: AdapterName = Field(..., description="Adapter logical name")
    enabled: bool = Field(..., description="Whether this adapter runs in cron")
    rate_limit_per_hour: int = Field(..., gt=0, description="Hard cap on fetches/hour")
    tier_default: int = Field(..., ge=0, le=3, description="Default tier (0=highest)")

    # optional adapter-specific fields
    list_url: str | None = Field(default=None, description="X List URL")
    feed_url: str | None = Field(default=None, description="HTTP feed URL")
    cookie_env_key: str | None = Field(
        default=None, description="Env var name pointing at cookie JSON path"
    )
    max_posts_per_fetch: int | None = Field(default=None, gt=0)
    sources: list[str] | None = Field(default=None, description="Sub-source ids")
    click_refresh: bool = Field(
        default=False, description="Futu: click '推荐' tab before scrape"
    )

    @model_validator(mode="after")
    def _require_cookie_for_authed_sources(self) -> "SourceConfig":
        # Adapters that need a session cookie MUST declare cookie_env_key.
        needs_cookie = {"x_list_finance", "x_list_general", "xueqiu", "futu"}
        if self.name in needs_cookie and not self.cookie_env_key:
            raise ValueError(
                f"adapter '{self.name}' requires cookie_env_key"
            )
        return self


class SourcesYaml(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapters: list[SourceConfig] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# topic_blend.yaml
# ---------------------------------------------------------------------------


LaneName = Literal[
    "pre_market",
    "intraday",
    "post_market",
    "overnight",
    "general_tech_ai",
    "general_meme_career",
]


class LaneConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quota: int = Field(..., gt=0, description="Daily candidate quota for this lane")
    hours_utc: list[int] = Field(..., min_length=1)
    persona: str = Field(..., description="Persona key referenced from personas.yaml")

    @field_validator("hours_utc")
    @classmethod
    def _hours_valid(cls, v: list[int]) -> list[int]:
        for h in v:
            if not 0 <= h <= 23:
                raise ValueError(f"hours_utc entry {h} not in [0, 23]")
        return v


class TopicBlendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_daily_quota: int = Field(..., gt=0)
    blend: dict[LaneName, LaneConfig]
    fallback_when_dry: LaneName
    publish_cap_by_week: dict[int, int] = Field(
        ..., description="Week index (1-based) -> max published posts/day"
    )

    @model_validator(mode="after")
    def _validate_blend_and_caps(self) -> "TopicBlendConfig":
        # Cap must include at least week 1 (spec §16.8)
        if 1 not in self.publish_cap_by_week:
            raise ValueError("publish_cap_by_week must include key 1 (first week)")
        # All caps must be positive ints
        for week, cap in self.publish_cap_by_week.items():
            if cap <= 0:
                raise ValueError(f"publish_cap_by_week[{week}] must be > 0")

        # Soft check: quota sum vs default_daily_quota -> warn only
        total = sum(lane.quota for lane in self.blend.values())
        if total != self.default_daily_quota:
            logger.warning(
                "topic_blend.yaml: sum(blend.quota)=%d != default_daily_quota=%d "
                "(this is allowed but unusual)",
                total,
                self.default_daily_quota,
            )
        # fallback_when_dry must reference an existing lane
        if self.fallback_when_dry not in self.blend:
            raise ValueError(
                f"fallback_when_dry '{self.fallback_when_dry}' not in blend keys"
            )
        return self


# ---------------------------------------------------------------------------
# personas.yaml
# ---------------------------------------------------------------------------


class PersonaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(..., min_length=1)
    stance_max: Annotated[int, Field(ge=1, le=5)] = Field(
        ..., description="Maximum stance strength (1-5)"
    )


class PersonasYaml(BaseModel):
    model_config = ConfigDict(extra="forbid")
    personas: dict[str, PersonaConfig] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# compliance_lexicon.yaml + political_lexicon.yaml
# ---------------------------------------------------------------------------


class ComplianceLexicon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    A_kill: list[str] = Field(..., description="Reject-on-match phrases")
    B_warn: list[str] = Field(default_factory=list)
    compliance_named_stock_threshold: int = Field(..., ge=0, le=5)

    @field_validator("A_kill")
    @classmethod
    def _a_kill_non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("A_kill must contain at least one phrase")
        return v


class PoliticalLexicon(BaseModel):
    model_config = ConfigDict(extra="forbid")
    A_kill: list[str] = Field(...)

    @field_validator("A_kill")
    @classmethod
    def _a_kill_non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("political_lexicon A_kill must contain at least one phrase")
        return v


# ---------------------------------------------------------------------------
# kol_list_*.yaml
# ---------------------------------------------------------------------------


class KolEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str
    tier: int = Field(..., ge=1, le=3)
    note: str = ""


class KolList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    list_url: str = ""
    handles: list[KolEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# AppConfig — combined
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """Top-level validated config aggregate."""

    model_config = ConfigDict(extra="forbid")

    sources: SourcesYaml
    topic_blend: TopicBlendConfig
    personas: PersonasYaml
    compliance: ComplianceLexicon
    political: PoliticalLexicon
    kol_finance: KolList
    kol_general: KolList


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


_FILES = {
    "sources": "config/sources.yaml",
    "topic_blend": "config/topic_blend.yaml",
    "personas": "config/personas.yaml",
    "compliance": "config/compliance_lexicon.yaml",
    "political": "config/political_lexicon.yaml",
    "kol_finance": "config/kol_list_finance.yaml",
    "kol_general": "config/kol_list_general.yaml",
}

_MODELS: dict[str, type[BaseModel]] = {
    "sources": SourcesYaml,
    "topic_blend": TopicBlendConfig,
    "personas": PersonasYaml,
    "compliance": ComplianceLexicon,
    "political": PoliticalLexicon,
    "kol_finance": KolList,
    "kol_general": KolList,
}


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        # Re-raise as plain FileNotFoundError so callers can distinguish
        # "config missing" from "config invalid".
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level must be a mapping, got {type(data).__name__}")
    return data


def _validate(model: type[BaseModel], data: dict, label: str) -> BaseModel:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        # Re-package with file label so the user knows which yaml broke.
        errors = "; ".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"{label}: {errors}") from exc


def load_all_configs(root: Path) -> AppConfig:
    """Load and validate every yaml in ``config/`` under ``root``.

    Args:
        root: Repo root (the directory that contains ``config/``).

    Returns:
        Fully validated ``AppConfig`` ready to import into other modules.

    Raises:
        FileNotFoundError: if any expected yaml file is missing.
        ConfigError: on schema or semantic validation failure.
    """
    loaded: dict[str, BaseModel] = {}
    for key, rel in _FILES.items():
        path = root / rel
        raw = _load_yaml(path)
        loaded[key] = _validate(_MODELS[key], raw, str(path))

    # Cross-yaml checks: every blend.persona must exist in personas.yaml
    topic_blend: TopicBlendConfig = loaded["topic_blend"]  # type: ignore[assignment]
    personas: PersonasYaml = loaded["personas"]  # type: ignore[assignment]
    for lane_name, lane in topic_blend.blend.items():
        if lane.persona not in personas.personas:
            raise ConfigError(
                f"topic_blend.yaml: lane '{lane_name}' references unknown "
                f"persona '{lane.persona}' (not in personas.yaml)"
            )

    return AppConfig(
        sources=loaded["sources"],  # type: ignore[arg-type]
        topic_blend=loaded["topic_blend"],  # type: ignore[arg-type]
        personas=loaded["personas"],  # type: ignore[arg-type]
        compliance=loaded["compliance"],  # type: ignore[arg-type]
        political=loaded["political"],  # type: ignore[arg-type]
        kol_finance=loaded["kol_finance"],  # type: ignore[arg-type]
        kol_general=loaded["kol_general"],  # type: ignore[arg-type]
    )


__all__ = [
    "AppConfig",
    "ComplianceLexicon",
    "ConfigError",
    "KolEntry",
    "KolList",
    "LaneConfig",
    "PersonaConfig",
    "PersonasYaml",
    "PoliticalLexicon",
    "SourceConfig",
    "SourcesYaml",
    "TopicBlendConfig",
    "load_all_configs",
]
