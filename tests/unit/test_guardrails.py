"""Unit tests for src/guardrails.py — lexicon-driven deterministic checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from src import guardrails
from src.guardrails import (
    GuardrailReport,
    GuardrailsExhausted,
    MAX_REWRITE_ATTEMPTS,
    Severity,
    check,
    load_lexicons,
)


@pytest.fixture()
def lex() -> dict[str, object]:
    """Real lexicons from repo config/ + voice/."""
    return load_lexicons()


# ---------- Load / config integrity ----------

def test_max_rewrite_attempts_is_three() -> None:
    assert MAX_REWRITE_ATTEMPTS == 3


def test_load_lexicons_returns_all_keys(lex: dict[str, object]) -> None:
    assert set(lex.keys()) == {"a_kill", "b_warn", "political", "slop", "stock_threshold"}
    assert isinstance(lex["a_kill"], list) and lex["a_kill"]
    assert isinstance(lex["b_warn"], list) and lex["b_warn"]
    assert isinstance(lex["political"], list) and lex["political"]
    assert isinstance(lex["stock_threshold"], int)


def test_missing_lexicon_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(guardrails, "_COMPLIANCE_PATH", tmp_path / "no.yaml")
    with pytest.raises(FileNotFoundError):
        load_lexicons()


def test_missing_slop_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(guardrails, "_SLOP_PATH", tmp_path / "missing.md")
    with pytest.raises(FileNotFoundError):
        load_lexicons()


# ---------- Clean pass ----------

def test_clean_draft_passes(lex: dict[str, object]) -> None:
    draft = "今日大盘震荡 资金面温和 关注成交量变化的节奏"
    report = check(draft, persona="finance_neutral", stance_strength=2, lexicons=lex)
    assert report.passed is True
    assert report.severity is None
    assert report.matched == []


# ---------- A-class kills ----------

def test_a_kill_compliance_rejects(lex: dict[str, object]) -> None:
    report = check("这只票稳赚不赔 跟我买", persona="finance_neutral",
                   stance_strength=1, lexicons=lex)
    assert report.passed is False
    assert report.severity is Severity.A_KILL
    assert any("稳赚不赔" in m or "跟我买" in m for m in report.matched)


def test_political_lexicon_rejects(lex: dict[str, object]) -> None:
    report = check("讨论党中央政策走向", persona="finance_neutral",
                   stance_strength=1, lexicons=lex)
    assert report.passed is False
    assert report.severity is Severity.A_KILL
    assert any("党中央" in m for m in report.matched)


def test_political_checked_before_a_kill() -> None:
    # political_hits should win even if A_kill also matches
    lex = {
        "a_kill": ["稳赚不赔"],
        "b_warn": [],
        "political": ["党中央"],
        "slop": [],
        "stock_threshold": 3,
    }
    report = check("党中央 稳赚不赔", persona="x", stance_strength=1, lexicons=lex)
    assert report.passed is False
    assert "political lexicon" in report.reasons[0]


def test_slop_words_reject() -> None:
    lex = {
        "a_kill": [],
        "b_warn": [],
        "political": [],
        "slop": ["赋能"],
        "stock_threshold": 3,
    }
    report = check("AI 给传统行业赋能", persona="x", stance_strength=1, lexicons=lex)
    assert report.passed is False
    assert report.severity is Severity.A_KILL


# ---------- Stance + stock code ----------

def test_high_stance_with_stock_code_rejects(lex: dict[str, object]) -> None:
    # threshold is 3 in repo config; stance=4 + ticker → reject
    report = check("$AAPL 这季要起飞 我重仓做多",
                   persona="finance_contrarian", stance_strength=4, lexicons=lex)
    # may also catch B_warn "重仓" — but we're proving the stock+stance rule fires
    assert report.passed is False
    assert report.severity is Severity.A_KILL


def test_low_stance_with_stock_code_passes() -> None:
    lex = {
        "a_kill": [], "b_warn": [], "political": [],
        "slop": [], "stock_threshold": 3,
    }
    report = check("$AAPL 今晚财报 我先看下指引",
                   persona="finance_neutral", stance_strength=2, lexicons=lex)
    assert report.passed is True


def test_high_stance_without_stock_code_passes() -> None:
    lex = {
        "a_kill": [], "b_warn": [], "political": [],
        "slop": [], "stock_threshold": 3,
    }
    report = check("我坚信下半年风格切回价值",
                   persona="finance_contrarian", stance_strength=5, lexicons=lex)
    assert report.passed is True


def test_a_share_ticker_format_detected() -> None:
    lex = {"a_kill": [], "b_warn": [], "political": [], "slop": [], "stock_threshold": 3}
    report = check("sh600519 涨停 我看好", persona="x", stance_strength=4, lexicons=lex)
    assert report.passed is False


# ---------- B-warn behavior ----------

def test_single_b_warn_passes_with_penalty() -> None:
    lex = {
        "a_kill": [], "b_warn": ["抄底"], "political": [],
        "slop": [], "stock_threshold": 3,
    }
    report = check("现在能不能抄底", persona="x", stance_strength=1, lexicons=lex)
    assert report.passed is True
    assert report.severity is Severity.B_WARN
    assert report.score_penalty == 2


def test_multiple_b_warn_rejects() -> None:
    lex = {
        "a_kill": [], "b_warn": ["抄底", "梭哈"], "political": [],
        "slop": [], "stock_threshold": 3,
    }
    report = check("现在抄底 还是梭哈", persona="x", stance_strength=1, lexicons=lex)
    assert report.passed is False
    assert report.severity is Severity.A_KILL


def test_repeated_same_b_warn_rejects() -> None:
    lex = {
        "a_kill": [], "b_warn": ["抄底"], "political": [],
        "slop": [], "stock_threshold": 3,
    }
    report = check("抄底 抄底 抄底", persona="x", stance_strength=1, lexicons=lex)
    assert report.passed is False
    assert report.severity is Severity.A_KILL


# ---------- Exhaustion / circuit ----------

def test_guardrails_exhausted_is_runtime_error() -> None:
    assert issubclass(GuardrailsExhausted, RuntimeError)
    with pytest.raises(GuardrailsExhausted):
        raise GuardrailsExhausted("test")


def test_report_default_fields() -> None:
    report = GuardrailReport(passed=True)
    assert report.matched == []
    assert report.reasons == []
    assert report.score_penalty == 0
    assert report.severity is None
