"""Topic Selector (S14) — choose what to write about.

Public surface kept tiny on purpose so writer/observer integrations stay decoupled
from the internal scoring + clustering logic.
"""

from __future__ import annotations

from selector.db import expire_old_candidates
from selector.topic_scorer import pick_top_topic, score_topics

__all__ = ["score_topics", "pick_top_topic", "expire_old_candidates"]
