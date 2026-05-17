"""Content normalization + hashing + fuzzy matching.

Shared by writer (writes drafts.content_hash) and self_monitor_adapter
(matches X-tweets back to drafts via 3-stage lookup):

    Stage 1: exact match on content_hash    (fast, O(1) via index)
    Stage 2: hash of normalized text        (handles whitespace/punct drift)
    Stage 3: difflib similarity ≥ 0.85      (catches single-char manual edits)

Normalization rules (intentionally aggressive, matches what humans tend to
casually edit on Twitter):
  - lowercase ASCII letters
  - strip ALL whitespace (incl. zero-width spaces, full-width spaces)
  - strip ALL ASCII + Chinese punctuation
  - preserve digits, Chinese characters, English letters

The function is idempotent: ``normalize(normalize(x)) == normalize(x)``.
"""
from __future__ import annotations

import hashlib
import unicodedata
from difflib import SequenceMatcher

__all__ = [
    "normalize_text",
    "content_hash",
    "similarity",
    "SIMILARITY_THRESHOLD",
]

SIMILARITY_THRESHOLD = 0.85

# Unicode category prefixes to STRIP during normalization.
#   Z* — separators (space, line, paragraph; includes zero-width / full-width)
#   P* — punctuation (ASCII, Chinese full-width, brackets like 「」『』【】《》, ·, etc.)
#   S* — symbols (math, currency, modifier)
#   C* — control / format codepoints
# Letters (L*), digits (N*), and CJK (treated as Lo) survive.
_STRIP_CATEGORIES = ("Z", "P", "S", "C")


def normalize_text(text: str) -> str:
    """Return a canonical form for cross-edit comparison.

    Uses Unicode category classification (not a hardcoded punct table) so any
    bracket / symbol / control codepoint that future Twitter input introduces
    gets folded away without code changes. NFKC first collapses full-width
    variants (！ → !, ＡＢ → AB) into their canonical forms.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(
        ch for ch in folded if unicodedata.category(ch)[0] not in _STRIP_CATEGORIES
    ).lower()


def content_hash(text: str) -> str:
    """sha256 of normalized text. Stable across whitespace/punct edits."""
    norm = normalize_text(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def similarity(a: str, b: str) -> float:
    """0.0–1.0 SequenceMatcher ratio on normalized text."""
    na = normalize_text(a)
    nb = normalize_text(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()
