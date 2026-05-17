#!/bin/bash
# Sync local secrets/ to the VPS repo's secrets/ directory.
# Usage: ./scripts/cookie_sync.sh user@vps.example.com:/opt/pepperbot
#
# secrets/ is mounted read-only into the container, so we sync at the host
# level and then `docker compose restart pepperbot` on the VPS to pick up
# the new cookies.
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 user@host:/path/to/repo" >&2
    exit 2
fi

DEST="$1"
LOCAL_SECRETS="$(cd "$(dirname "$0")/.." && pwd)/secrets/"

if [ ! -d "${LOCAL_SECRETS}" ]; then
    echo "error: ${LOCAL_SECRETS} does not exist locally" >&2
    exit 1
fi

# --chmod=600 keeps remote permissions tight for cookie/api-key files
# --delete is intentionally omitted so a partial local secrets/ does not wipe VPS
rsync -avz --chmod=600 \
    --exclude '.DS_Store' \
    "${LOCAL_SECRETS}" "${DEST%/}/secrets/"

echo
echo "Done. On the VPS run:"
echo "  cd ${DEST#*:}  &&  docker compose restart pepperbot"
