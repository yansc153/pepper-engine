"""Unit tests for src.miner.weave_rules — 5 edge types + IoU + cross-domain."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.miner.types import TechniqueEntry
from src.miner.weave_rules import compute_edges, iou, is_cross_domain


def _entry(
    *,
    eid: int = 1,
    hook: str = "反共识开场",
    lane: str = "pre_market",
    sx: str = "short_comma_no_period",
    emotions: list[str] | None = None,
    author: str = "alice",
    posted_at: datetime | None = None,
) -> TechniqueEntry:
    return TechniqueEntry(
        id=eid,
        observation_id=eid,
        hook_pattern=hook,
        hook_example="x",
        syntax_signature=sx,
        sentence_len_avg=18.0,
        sentence_len_p90=26.0,
        stance_strength=3,
        emotion_triggers=emotions or [],
        image_style="none",
        post_hour_utc=10,
        topic_lane=lane,
        applicable_personas=["finance_neutral"],
        content_mode="insight",
        optimal_length="short",
        distilled_at=None,
        success_score=50.0,
        times_retrieved=0,
        times_used_in_post=0,
        recency_weight=1.0,
        author_handle=author,
        posted_at=posted_at,
    )


# ----------- iou edge cases -----------


def test_iou_both_empty() -> None:
    assert iou([], []) == 0.0
    assert iou(None, None) == 0.0


def test_iou_one_empty() -> None:
    assert iou(["FOMO"], []) == 0.0
    assert iou([], ["FOMO"]) == 0.0


def test_iou_disjoint() -> None:
    assert iou(["FOMO"], ["共情"]) == 0.0


def test_iou_full_overlap() -> None:
    assert iou(["FOMO", "嘲讽"], ["FOMO", "嘲讽"]) == 1.0


def test_iou_partial() -> None:
    # |A ∩ B|=1, |A ∪ B|=3 → 1/3
    assert abs(iou(["FOMO", "嘲讽"], ["FOMO", "焦虑"]) - 1 / 3) < 1e-9


# ----------- is_cross_domain -----------


def test_cross_domain_true_finance_to_general() -> None:
    assert is_cross_domain("pre_market", "general_tech_ai") is True
    assert is_cross_domain("general_meme_career", "intraday") is True


def test_cross_domain_false_within_family() -> None:
    assert is_cross_domain("pre_market", "intraday") is False
    assert is_cross_domain("general_tech_ai", "general_meme_career") is False


def test_cross_domain_false_with_other() -> None:
    assert is_cross_domain("pre_market", "other") is False


# ----------- compute_edges truth table -----------


def test_compute_edges_same_hook() -> None:
    a = _entry(eid=1, hook="数字暴击", lane="pre_market")
    b = _entry(eid=2, hook="数字暴击", lane="intraday")
    types = {t for t, _ in compute_edges(a, b)}
    assert "same_hook" in types
    assert "same_lane_diff_angle" not in types


def test_compute_edges_same_lane_diff_angle() -> None:
    a = _entry(eid=1, hook="数字暴击", lane="pre_market")
    b = _entry(eid=2, hook="反共识开场", lane="pre_market")
    edges = dict(compute_edges(a, b))
    assert edges.get("same_lane_diff_angle") == 0.7
    assert "same_hook" not in edges


def test_compute_edges_co_occurring_emotion_above_threshold() -> None:
    a = _entry(eid=1, emotions=["FOMO", "嘲讽", "焦虑"])
    b = _entry(eid=2, emotions=["FOMO", "嘲讽", "猎奇"])
    edges = dict(compute_edges(a, b))
    # iou = 2/4 = 0.5 → NOT strict greater than 0.5
    assert "co_occurring_emotion" not in edges


def test_compute_edges_co_occurring_emotion_strict_greater() -> None:
    a = _entry(eid=1, emotions=["FOMO", "嘲讽"])
    b = _entry(eid=2, emotions=["FOMO", "嘲讽", "猎奇"])
    edges = dict(compute_edges(a, b))
    # iou = 2/3 ≈ 0.667 > 0.5
    assert "co_occurring_emotion" in edges
    assert abs(edges["co_occurring_emotion"] - 2 / 3) < 1e-9


def test_compute_edges_temporal_chain_within_48h() -> None:
    t = datetime(2026, 5, 1, tzinfo=timezone.utc)
    a = _entry(eid=1, author="alice", posted_at=t)
    b = _entry(eid=2, author="alice", posted_at=t + timedelta(hours=47))
    edges = dict(compute_edges(a, b))
    assert edges.get("temporal_chain") == 0.8


def test_compute_edges_temporal_chain_outside_48h() -> None:
    t = datetime(2026, 5, 1, tzinfo=timezone.utc)
    a = _entry(eid=1, author="alice", posted_at=t)
    b = _entry(eid=2, author="alice", posted_at=t + timedelta(hours=49))
    edges = dict(compute_edges(a, b))
    assert "temporal_chain" not in edges


def test_compute_edges_cross_domain_bridge() -> None:
    a = _entry(eid=1, lane="pre_market", sx="stacked_short")
    b = _entry(eid=2, lane="general_tech_ai", sx="stacked_short")
    edges = dict(compute_edges(a, b))
    assert edges.get("cross_domain_bridge") == 0.9


def test_compute_edges_cross_domain_bridge_requires_same_syntax() -> None:
    a = _entry(eid=1, lane="pre_market", sx="stacked_short")
    b = _entry(eid=2, lane="general_tech_ai", sx="long_run_on")
    edges = dict(compute_edges(a, b))
    assert "cross_domain_bridge" not in edges


def test_compute_edges_temporal_chain_requires_same_author() -> None:
    t = datetime(2026, 5, 1, tzinfo=timezone.utc)
    a = _entry(eid=1, author="alice", posted_at=t)
    b = _entry(eid=2, author="bob", posted_at=t + timedelta(hours=1))
    edges = dict(compute_edges(a, b))
    assert "temporal_chain" not in edges


def test_compute_edges_multiple_simultaneous() -> None:
    t = datetime(2026, 5, 1, tzinfo=timezone.utc)
    a = _entry(
        eid=1, hook="数字暴击", lane="pre_market",
        emotions=["FOMO", "嘲讽"], author="alice", posted_at=t,
    )
    b = _entry(
        eid=2, hook="数字暴击", lane="pre_market",
        emotions=["FOMO", "嘲讽", "猎奇"], author="alice",
        posted_at=t + timedelta(hours=2),
    )
    types = {t for t, _ in compute_edges(a, b)}
    # same_hook + co_occurring_emotion + temporal_chain (NOT same_lane_diff_angle
    # because hooks match)
    assert "same_hook" in types
    assert "co_occurring_emotion" in types
    assert "temporal_chain" in types
    assert "same_lane_diff_angle" not in types
