#!/bin/bash
# Container-side unified command dispatcher.
# Usage: run.sh <observe|post|mine|review|remine|discord_poll|self_monitor>
#
# All cron rows call this script. It just normalizes env and shells out to
# `python -m src.main <command>`. If src/main.py is not yet implemented
# (early phases), we print a placeholder line and exit 0 so cron stays clean.
set -euo pipefail

CMD="${1:-}"
if [ -z "${CMD}" ]; then
    echo "usage: run.sh <observe|post|mine|review|remine|discord_poll|self_monitor>" >&2
    exit 2
fi

case "${CMD}" in
    observe|post|batch_post|mine|review|remine|discord_poll|self_monitor) ;;
    *)
        echo "run.sh: unknown command '${CMD}'" >&2
        exit 2
        ;;
esac

cd /app

# Load env (cron jobs do not inherit container env; entrypoint wrote it here)
if [ -f /etc/environment ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/environment
    set +a
fi

STAMP="$(date -u +%FT%TZ)"
echo "[run.sh ${STAMP}] command=${CMD} pid=$$"

if [ ! -f /app/src/main.py ]; then
    echo "[run.sh ${STAMP}] src/main.py not present yet — placeholder no-op for '${CMD}'"
    exit 0
fi

exec python -m src.main "${CMD}"
