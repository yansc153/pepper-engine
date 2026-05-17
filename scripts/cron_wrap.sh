#!/bin/bash
# Wrap a single cron command with:
#   1. per-slot per-date log file (pepperbot-<slot>-YYYY-MM-DD.log)
#   2. flock contention visibility (logged + alerted, NOT silent)
#   3. non-zero exit visibility (alert via webhook)
#   4. rotation: prune logs older than 14 days
#
# Usage: cron_wrap.sh <slot_name> <command_args...>
# Example: cron_wrap.sh observe /app/scripts/run.sh observe
set -uo pipefail

SLOT="${1:?slot name required}"
shift

DATE_TAG="$(date -u +%Y-%m-%d)"
LOG_DIR="/app/logs"
LOG_FILE="${LOG_DIR}/pepperbot-${SLOT}-${DATE_TAG}.log"
LOCK_FILE="/tmp/pepperbot-${SLOT}.lock"
mkdir -p "${LOG_DIR}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] ${SLOT}: $*" >> "${LOG_FILE}"; }

# 1. Prune logs older than 14 days for this slot prefix
find "${LOG_DIR}" -name "pepperbot-${SLOT}-*.log" -mtime +14 -delete 2>/dev/null || true

# Discord bot REST alert helper (reuses DISCORD_BOT_TOKEN + ALERT_CHANNEL_ID
# or DISCORD_DRAFT_CHANNEL_ID). Silent if either is unset.
discord_alert() {
    local msg="$1"
    local ch="${ALERT_CHANNEL_ID:-${DISCORD_DRAFT_CHANNEL_ID:-}}"
    [ -z "${DISCORD_BOT_TOKEN:-}" ] && return 0
    [ -z "${ch}" ] && return 0
    local url="https://discord.com/api/v10/channels/${ch}/messages"
    [ -n "${ALERT_THREAD_ID:-}" ] && url="${url}?thread_id=${ALERT_THREAD_ID}"
    # Use python for JSON-safe payload escaping: stderr/log tails can contain
    # backslashes, quotes, control chars that break naive printf templates.
    local payload
    payload="$(MSG="${msg}" python3 -c 'import json, os; print(json.dumps({"content": os.environ["MSG"]}))' 2>/dev/null)" \
        || return 0
    curl -fsS -X POST \
        -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "${payload}" \
        "${url}" >/dev/null 2>&1 || true
}

# 2. Try to acquire the lock; if held, log + alert + exit 0 (not a failure).
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    log "SKIPPED: lock held by another invocation (PID=$(cat ${LOCK_FILE}.pid 2>/dev/null || echo unknown))"
    discord_alert ":lock: pepperbot ${SLOT} lock contention at $(ts)"
    exit 0
fi
echo $$ > "${LOCK_FILE}.pid"

# 3. Run the wrapped command; capture exit code without killing the wrapper.
log "START: $*"
start_epoch=$(date +%s)
"$@" >> "${LOG_FILE}" 2>&1
rc=$?
duration=$(( $(date +%s) - start_epoch ))
log "END: exit=${rc} duration=${duration}s"

# 4. If non-zero exit, alert via Discord bot REST.
if [ "${rc}" -ne 0 ]; then
    # Pull last 10 log lines for context, escape quotes/newlines for JSON.
    tail_excerpt="$(tail -n 10 "${LOG_FILE}" \
        | tr '\n' ' ' \
        | sed 's/"/\\"/g' \
        | head -c 800)"
    discord_alert ":rotating_light: pepperbot ${SLOT} exited ${rc} at $(ts)\\n\`\`\`\\n${tail_excerpt}\\n\`\`\`"
fi

rm -f "${LOCK_FILE}.pid"
exit "${rc}"
