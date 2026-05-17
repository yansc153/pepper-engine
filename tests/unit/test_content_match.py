"""Unit tests for src.content_match — normalize, hash, similarity."""
from __future__ import annotations

from src.content_match import (
    SIMILARITY_THRESHOLD,
    content_hash,
    normalize_text,
    similarity,
)


def test_normalize_is_idempotent() -> None:
    s = "盘前 快评：A股 今日 大涨 3.5%！"
    assert normalize_text(normalize_text(s)) == normalize_text(s)


def test_normalize_strips_whitespace_and_punctuation() -> None:
    a = "盘前快评：A股今日大涨3.5%！"
    b = "盘前快评 A股 今日 大涨 3.5"
    assert normalize_text(a) == normalize_text(b)


def test_normalize_folds_full_width_punctuation() -> None:
    # ! vs ！, , vs ， — NFKC should fold these
    assert normalize_text("hello！") == normalize_text("hello!")
    assert normalize_text("a，b") == normalize_text("a,b")


def test_normalize_strips_zero_width_spaces() -> None:
    # Hidden zero-width and full-width spaces a user might paste from a phone
    a = "盘​前　快评"
    b = "盘前快评"
    assert normalize_text(a) == normalize_text(b)


def test_hash_stable_under_punctuation_edits() -> None:
    # User adds a comma in the X client — hash must still match
    original = "盘前快评：今日A股开盘走势分歧"
    edited = "盘前快评:今日A股开盘走势 分歧"
    assert content_hash(original) == content_hash(edited)


def test_hash_changes_when_actual_content_changes() -> None:
    a = "今日A股大涨"
    b = "今日A股大跌"
    assert content_hash(a) != content_hash(b)


def test_similarity_high_on_minor_edits() -> None:
    a = "盘前快评：今日A股开盘走势分歧，看好科技板块"
    b = "盘前快评：今日A股开盘走势分歧，看好科技板块。"  # added period
    assert similarity(a, b) >= SIMILARITY_THRESHOLD


def test_similarity_low_on_different_text() -> None:
    a = "今日A股大涨3.5%"
    b = "美联储加息25基点"
    assert similarity(a, b) < SIMILARITY_THRESHOLD


def test_similarity_zero_when_empty() -> None:
    assert similarity("", "anything") == 0.0
    assert similarity("anything", "") == 0.0


def test_hash_is_hex_digest() -> None:
    h = content_hash("test")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
