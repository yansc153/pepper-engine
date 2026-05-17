"""Deterministic guardrails for finance-account drafts.

Zero LLM tokens. All rules are regex / lexicon driven.

Lexicons:
- config/compliance_lexicon.yaml — A_kill / B_warn / compliance_named_stock_threshold
- config/political_lexicon.yaml   — A_kill (political / religion / sex / etc.)
- voice/slop_words.md             — additional A-class slop words (one per line)

Hard rules:
- A_kill lexicon hit                                  → reject
- political lexicon hit                               → reject
- B_warn hit (single)                                 → soft (-2 score, warn)
- B_warn hit (>=2 distinct OR same hit twice)         → reject
- stance_strength > threshold AND named stock code    → reject
- voice slop_words hit                                → reject

`check(draft, persona, stance_strength)` returns `GuardrailReport`.
`MAX_REWRITE_ATTEMPTS = 3`. Writer is responsible for the rewrite loop and for
raising `GuardrailsExhausted` (+ tripping circuit_breaker) when exhausted.

Missing lexicon files → FileNotFoundError. Never silently pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

__all__ = [
    "GuardrailReport",
    "GuardrailsExhausted",
    "Severity",
    "MAX_REWRITE_ATTEMPTS",
    "check",
    "trip_circuit_breaker",
    "load_lexicons",
]

MAX_REWRITE_ATTEMPTS = 3

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_COMPLIANCE_PATH = _PROJECT_ROOT / "config" / "compliance_lexicon.yaml"
_POLITICAL_PATH = _PROJECT_ROOT / "config" / "political_lexicon.yaml"
_SLOP_PATH = _PROJECT_ROOT / "voice" / "slop_words.md"

# Stock-code patterns: A-share 6-digit (600519, sh600519), HK (00700.HK), US ($AAPL, NASDAQ:TSLA)
_STOCK_CODE_RE = re.compile(
    r"(?:\b[shz]{2}\d{6}\b)"           # sh600519 / sz000001
    r"|(?:\b\d{6}\.(?:SH|SZ|HK)\b)"     # 600519.SH
    r"|(?:\b\d{5}\.HK\b)"               # 00700.HK
    r"|(?:\$[A-Z]{1,5}\b)"              # $AAPL
    r"|(?:\b[A-Z]{2,5}:[A-Z]{1,5}\b)",  # NASDAQ:TSLA
    re.IGNORECASE,
)


class Severity(Enum):
    A_KILL = "A_kill"
    B_WARN = "B_warn"


class GuardrailsExhausted(RuntimeError):
    """Raised by writer when rewrite loop exceeds MAX_REWRITE_ATTEMPTS."""


@dataclass
class GuardrailReport:
    """Outcome of a single guardrail check on a draft."""

    passed: bool
    severity: Severity | None = None
    matched: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    score_penalty: int = 0


def _read_lexicon_yaml(path: Path) -> dict[str, list[str] | int]:
    """Load a compliance-style YAML. Raises FileNotFoundError if missing."""
    if not path.exists():
        raise FileNotFoundError(f"lexicon missing: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"lexicon must be a mapping: {path}")
    return data


def _read_slop_words(path: Path) -> list[str]:
    """Parse voice/slop_words.md. Skip blank / comment lines."""
    if not path.exists():
        raise FileNotFoundError(f"slop words file missing: {path}")
    words: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("<!--"):
            continue
        words.append(line)
    return words


def load_lexicons() -> dict[str, object]:
    """Load all three lexicons; cached at module level via lru-style dict.

    Returns dict with keys: a_kill, b_warn, political, slop, stock_threshold.
    """
    compliance = _read_lexicon_yaml(_COMPLIANCE_PATH)
    political = _read_lexicon_yaml(_POLITICAL_PATH)
    return {
        "a_kill": list(compliance.get("A_kill") or []),
        "b_warn": list(compliance.get("B_warn") or []),
        "political": list(political.get("A_kill") or []),
        "slop": _read_slop_words(_SLOP_PATH),
        "stock_threshold": int(compliance.get("compliance_named_stock_threshold", 3)),
    }


def _scan(text: str, terms: list[str]) -> list[str]:
    """Return distinct lexicon terms that appear in text (case-insensitive)."""
    if not text or not terms:
        return []
    lowered = text.lower()
    hits: list[str] = []
    for term in terms:
        if not term:
            continue
        if term.lower() in lowered:
            hits.append(term)
    return hits


def _count_hits(text: str, terms: list[str]) -> int:
    """Total occurrences (not distinct) across all listed terms."""
    if not text or not terms:
        return 0
    lowered = text.lower()
    return sum(lowered.count(term.lower()) for term in terms if term)


def _has_named_stock(text: str) -> bool:
    return bool(_STOCK_CODE_RE.search(text or ""))


def check(
    draft: str,
    persona: str,
    stance_strength: int = 0,
    lexicons: dict[str, object] | None = None,
) -> GuardrailReport:
    """Run the full guardrail pipeline on `draft`.

    Returns:
        GuardrailReport — passed=True if no severities triggered.
        On reject, severity=A_KILL; on warn-only, severity=B_WARN with penalty.
    """
    lex = lexicons if lexicons is not None else load_lexicons()
    a_kill = list(lex["a_kill"])  # type: ignore[arg-type]
    b_warn = list(lex["b_warn"])  # type: ignore[arg-type]
    political = list(lex["political"])  # type: ignore[arg-type]
    slop = list(lex["slop"])  # type: ignore[arg-type]
    stock_threshold = int(lex["stock_threshold"])  # type: ignore[arg-type]

    matched: list[str] = []
    reasons: list[str] = []

    political_hits = _scan(draft, political)
    if political_hits:
        matched.extend(political_hits)
        reasons.append(f"political lexicon: {political_hits}")
        return GuardrailReport(False, Severity.A_KILL, matched, reasons)

    a_hits = _scan(draft, a_kill)
    if a_hits:
        matched.extend(a_hits)
        reasons.append(f"A_kill compliance: {a_hits}")
        return GuardrailReport(False, Severity.A_KILL, matched, reasons)

    slop_hits = _scan(draft, slop)
    if slop_hits:
        matched.extend(slop_hits)
        reasons.append(f"slop words: {slop_hits}")
        return GuardrailReport(False, Severity.A_KILL, matched, reasons)

    if stance_strength > stock_threshold and _has_named_stock(draft):
        reasons.append(
            f"stance_strength={stance_strength} > {stock_threshold} with named stock code"
        )
        return GuardrailReport(False, Severity.A_KILL, matched, reasons)

    b_distinct = _scan(draft, b_warn)
    b_total = _count_hits(draft, b_warn)
    if len(b_distinct) >= 2 or b_total >= 2:
        matched.extend(b_distinct)
        reasons.append(f"B_warn repeated/multiple: distinct={b_distinct} total={b_total}")
        return GuardrailReport(False, Severity.A_KILL, matched, reasons)

    if b_distinct:
        # Single B-warn hit → soft penalty, draft still passable.
        return GuardrailReport(
            passed=True,
            severity=Severity.B_WARN,
            matched=b_distinct,
            reasons=[f"B_warn single: {b_distinct}"],
            score_penalty=2,
        )

    return GuardrailReport(passed=True)


def trip_circuit_breaker(
    scope: str,
    reason: str,
    reset_after_seconds: int = 3600,
) -> None:
    """Write a circuit_breaker row so downstream cron knows to back off.

    Lazy-import database to keep guardrails import-cheap and side-effect free
    in tests that monkey-patch the DB.
    """
    from datetime import datetime, timedelta, timezone

    from src.database import get_conn, with_retry

    reset_after = datetime.now(timezone.utc) + timedelta(seconds=reset_after_seconds)

    def _write() -> None:
        conn = get_conn()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO circuit_breaker "
                    "(scope, tripped_at, reason, reset_after) VALUES (?, ?, ?, ?)",
                    (scope, datetime.now(timezone.utc).isoformat(), reason,
                     reset_after.isoformat()),
                )
        finally:
            conn.close()

    with_retry(_write)
