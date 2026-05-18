"""Pepperbot orchestrator — single entry point for cron + manual invocation.

Usage: ``python -m src.main <command> [--dry-run]``

Eight commands map 1:1 to the rows in ``crontab.txt`` plus a dev-only ``test``
smoke. Each command is a thin orchestration layer over the subagent modules;
all heavy logic lives in observers / selector / writer / discord / miner.

Design notes:
- ``--dry-run`` exports ``DRY_RUN=1`` early so downstream modules (publisher,
  discord bot) short-circuit. Must happen before any module imports that read
  the env at import time, so all module imports are deferred into the command
  functions.
- Every command emits one structured JSON log line on completion (cmd /
  duration_s / success / extra fields) so ``grep`` on logs/*.log can drive
  dashboards without parsing free-text.
- A command that has nothing to do (no fresh topic, owner unset, etc.) exits
  0 with ``success=true`` and a descriptive note — those are not failures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

# Mirror conftest.py: subpackages (selector, observers, etc.) use bare imports
# that resolve via src/ on sys.path. When invoked as `python -m src.main`,
# only the project root is on sys.path, so inject src/ here BEFORE deferred
# command imports execute.
_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

LOGGER = logging.getLogger("pepperbot.main")

CommandResult = tuple[int, dict[str, Any]]
CommandFn = Callable[[], Awaitable[CommandResult]]


# ---------------------------------------------------------------------------
# 8 commands
# ---------------------------------------------------------------------------


async def _cmd_observe() -> CommandResult:
    """Run all external adapters, then score new observations into topic_candidates."""
    from src.database import get_conn
    from src.observers.runner import run_once
    from selector import expire_old_candidates, score_topics

    report = await run_once()
    score_created = 0
    score_top = 0.0
    conn = get_conn()
    try:
        expire_old_candidates(conn, older_than_hours=6)
        score_result = score_topics(conn)
        score_created = int(score_result.created)
        score_top = float(score_result.top_score)
    finally:
        conn.close()

    extras = {
        "observations_inserted": int(report.observations_inserted),
        "adapter_success": int(report.success_count),
        "adapter_errors": int(report.error_count),
        "topics_created": score_created,
        "top_virality_score": score_top,
    }
    return 0, extras


async def _cmd_post() -> CommandResult:
    """Pick a fresh topic → write a draft → push to Discord. No direct publish."""
    from src.database import get_conn
    from src.discord.bot import push_draft_to_discord
    from src.writer import write_draft
    from selector import pick_top_topic

    conn = get_conn()
    try:
        topic = pick_top_topic(conn)
    finally:
        conn.close()

    if topic is None:
        LOGGER.warning("no fresh topic available; skipping post cycle")
        return 0, {"skipped": "no_fresh_topic"}

    try:
        draft = await write_draft(topic)
    except Exception as exc:  # noqa: BLE001 — writer raises GuardrailsExhausted etc.
        # Chronic writer failure must be visible to cron; alert + non-zero exit.
        from src.alerting import alert
        LOGGER.exception("write_draft raised: %s", exc)
        alert(
            f"write_draft failed for topic {topic.get('id')}",
            context={"topic_lane": topic.get("predicted_topic_lane"), "error": repr(exc)},
        )
        return 1, {"error": "write_failed", "detail": str(exc)}

    if not draft.success or draft.draft_id is None:
        # Draft rejected by score/guardrails is a normal outcome (exit 0),
        # but log at INFO so it's visible.
        LOGGER.info("draft rejected: %s (score=%s)", draft.error, draft.score_total)
        return 0, {
            "skipped": "draft_rejected",
            "error": draft.error,
            "score": draft.score_total,
        }

    msg_id = await push_draft_to_discord(draft.draft_id)
    LOGGER.info("draft %s pushed to discord msg %s", draft.draft_id, msg_id)
    return 0, {
        "draft_id": draft.draft_id,
        "discord_message_id": msg_id,
        "score": draft.score_total,
    }


async def _cmd_batch_post() -> CommandResult:
    """Generate up to BATCH_N drafts in one shot — used by the morning cron.

    For each iteration: pick top fresh topic → write_draft → push_draft_to_discord.
    Continues until BATCH_N pushed OR no fresh topics left OR all rejected.
    """
    from src.database import get_conn
    from src.discord.bot import push_draft_to_discord
    from src.writer import write_draft
    from selector import pick_top_topic

    BATCH_N = int(os.environ.get("BATCH_POST_N", "20"))
    pushed: list[dict[str, Any]] = []
    rejected = 0
    no_image = 0
    skipped_no_topic = 0
    write_errors = 0

    for i in range(BATCH_N):
        conn = get_conn()
        try:
            topic = pick_top_topic(conn)
        finally:
            conn.close()
        if topic is None:
            skipped_no_topic += 1
            break

        try:
            draft = await write_draft(topic)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("batch write_draft #%d failed: %s", i, exc)
            write_errors += 1
            continue

        if not draft.success or draft.draft_id is None:
            if draft.error and "no source image" in draft.error:
                no_image += 1
            else:
                rejected += 1
            continue

        try:
            msg_id = await push_draft_to_discord(draft.draft_id)
            pushed.append({"draft_id": draft.draft_id, "msg_id": msg_id})
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("batch push #%d failed: %s", i, exc)

    return 0, {
        "batch_target": BATCH_N,
        "pushed_count": len(pushed),
        "rejected": rejected,
        "no_image": no_image,
        "write_errors": write_errors,
        "stopped_no_topic": skipped_no_topic > 0,
        "pushed": pushed,
    }


async def _cmd_discord_poll() -> CommandResult:
    """Sweep Discord reactions on outstanding drafts."""
    from src.discord.bot import poll_reactions

    advanced = await poll_reactions()
    return 0, {"drafts_advanced": int(advanced)}


async def _cmd_self_monitor() -> CommandResult:
    """Cross-reference the main account timeline back into drafts / wild_posts."""
    from src.observers.self_monitor_adapter import SelfMonitorAdapter

    adapter = SelfMonitorAdapter()
    try:
        result = await adapter.reconcile()
    except RuntimeError as exc:
        # Missing config (TWITTER_HANDLE etc.) is a misconfig, not "nothing to do".
        # Exit non-zero so cron_wrap.sh fires an alert.
        LOGGER.error("self_monitor disabled: %s", exc)
        try:
            from src.alerting import alert
            alert("self_monitor cannot run", context={"error": str(exc)})
        except Exception as alert_exc:  # noqa: BLE001
            LOGGER.error("alerting failed: %s", alert_exc)
        return 1, {"error": "self_monitor_misconfig", "detail": str(exc)}

    return 0, {
        "scanned": result.scanned,
        "bound": result.bound,
        "wild": result.wild,
        "errors": result.errors,
    }


async def _cmd_mine() -> CommandResult:
    """Daily distill of today's viral observations + nightly weave."""
    from datetime import datetime, timezone

    from src.miner import full_distill, weave_nightly

    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    new_ids = full_distill(since)
    edges = weave_nightly(new_ids)
    return 0, {"entries_distilled": len(new_ids), "edges_created": int(edges)}


async def _cmd_review() -> CommandResult:
    """Reviewer pass: metrics + 4 feedback channels + weight update + stale check.

    Reviewer module (S10) is not yet implemented. Until it lands this command is
    a structured no-op so the cron row stays clean and exits 0.
    """
    try:
        from src import reviewer  # type: ignore[attr-defined]
    except ImportError:
        LOGGER.warning("reviewer module not yet implemented; review is a no-op")
        return 0, {"skipped": "reviewer_not_implemented"}

    fn = getattr(reviewer, "review_and_update_weights", None)
    if fn is None:
        LOGGER.warning("reviewer.review_and_update_weights missing; review is a no-op")
        return 0, {"skipped": "reviewer_entry_missing"}

    summary = fn()
    if asyncio.iscoroutine(summary):
        summary = await summary
    return 0, {"reviewer_summary": summary if isinstance(summary, dict) else str(summary)}


async def _cmd_remine() -> CommandResult:
    """Weekly full re-weave: recompute all edges + recency decay + prune."""
    from src.miner import weave_full

    decayed, pruned = weave_full()
    return 0, {"entries_decayed": int(decayed), "entries_pruned": int(pruned)}


async def _cmd_test() -> CommandResult:
    """Smoke test: import every command module, verify they load. No external calls."""
    os.environ["DRY_RUN"] = "1"
    modules_ok: list[str] = []
    errors: list[str] = []
    for mod_name in (
        "src.database",
        "src.observers.runner",
        "src.observers.self_monitor_adapter",
        "selector",
        "src.writer",
        "src.discord.bot",
        "src.miner",
    ):
        try:
            __import__(mod_name)
            modules_ok.append(mod_name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{mod_name}: {exc}")

    # Verify DB is reachable (migrations may not have run yet, that's fine).
    try:
        from src.database import get_conn

        conn = get_conn()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        errors.append(f"db: {exc}")

    if errors:
        return 1, {"modules_ok": modules_ok, "db_ok": db_ok, "errors": errors}
    return 0, {"modules_ok": modules_ok, "db_ok": db_ok}


COMMANDS: dict[str, CommandFn] = {
    "observe": _cmd_observe,
    "post": _cmd_post,
    "batch_post": _cmd_batch_post,
    "discord_poll": _cmd_discord_poll,
    "self_monitor": _cmd_self_monitor,
    "mine": _cmd_mine,
    "review": _cmd_review,
    "remine": _cmd_remine,
    "test": _cmd_test,
}


# ---------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=os.environ.get("PEPPERBOT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _emit_summary(cmd: str, duration_s: float, success: bool, extras: dict[str, Any]) -> None:
    payload: dict[str, Any] = {
        "cmd": cmd,
        "duration_s": round(duration_s, 3),
        "success": success,
    }
    payload.update(extras)
    # One JSON line per run — grep-friendly.
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pepperbot",
        description="Pepperbot orchestrator: one entry per cron row.",
    )
    parser.add_argument("command", choices=sorted(COMMANDS.keys()))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Set DRY_RUN=1 so publisher/discord bot short-circuit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = _parse_args(argv)
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"

    cmd_fn = COMMANDS[args.command]
    start = time.monotonic()
    LOGGER.info("cmd=%s start dry_run=%s", args.command, args.dry_run)

    exit_code = 0
    extras: dict[str, Any] = {}
    try:
        exit_code, extras = asyncio.run(cmd_fn())
    except KeyboardInterrupt:
        LOGGER.warning("cmd=%s interrupted", args.command)
        exit_code = 130
        extras = {"error": "interrupted"}
    except Exception as exc:  # noqa: BLE001 — top-level safety net
        LOGGER.exception("cmd=%s failed: %s", args.command, exc)
        exit_code = 1
        extras = {"error": repr(exc)}
        # Alert ops on any uncaught cron failure
        try:
            from src.alerting import alert
            alert(f"cmd={args.command} crashed", context={"error": repr(exc)})
        except Exception as alert_exc:  # noqa: BLE001 — alert must never recurse
            LOGGER.error("alerting failed: %s", alert_exc)

    duration = time.monotonic() - start
    _emit_summary(args.command, duration, exit_code == 0, extras)
    LOGGER.info("cmd=%s done exit=%d duration=%.2fs", args.command, exit_code, duration)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
