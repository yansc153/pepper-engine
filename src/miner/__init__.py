"""Pattern Miner: distill, weave, retrieve techniques (UNIFIED_SPEC §3.1, §6).

Public API frozen at Phase A — downstream subagents (S9 writer, S10 reviewer,
S14 selector) consume only these symbols.
"""

from __future__ import annotations

from src.miner.distiller import full_distill, light_distill
from src.miner.retriever import retrieve
from src.miner.types import RetrievalContext, TechniqueEntry
from src.miner.weaver import weave_full, weave_nightly

__all__ = [
    "retrieve",
    "light_distill",
    "full_distill",
    "weave_nightly",
    "weave_full",
    "TechniqueEntry",
    "RetrievalContext",
]
