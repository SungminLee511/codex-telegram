#!/bin/bash
# wake_after.sh - schedule a synthetic Telegram message for Codex.
#
# Usage:
#     ./wake_after.sh <delay_seconds> "<wake-up message>"
#
# Writes data/codex_inject_message.json by default. This is intentionally
# separate from the Claude bot's inject file.

set -euo pipefail

DELAY="${1:-300}"
MESSAGE="${2:-Wake up}"
CHAT_ID="${WAKE_CHAT_ID:-8610757705}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INJECT_FILE="${CODEX_INJECT_PATH:-$SCRIPT_DIR/data/codex_inject_message.json}"
LOG_FILE="$SCRIPT_DIR/data/codex_wake_after.log"

nohup bash -c '
    delay="$1"
    message="$2"
    inject_file="$3"
    log_file="$4"
    chat_id="$5"

    sleep "$delay"
    echo "[$(date -u)] firing codex wake after ${delay}s" >> "$log_file"
    python3 - "$message" "$inject_file" "$chat_id" <<'"'"'PY'"'"'
import json
import sys
from pathlib import Path

message, inject_file, chat_id = sys.argv[1], sys.argv[2], int(sys.argv[3])
Path(inject_file).write_text(
    json.dumps({"chat_id": chat_id, "text": message}, ensure_ascii=True, indent=2)
    + "\n",
    encoding="utf-8",
)
PY
    echo "[$(date -u)] wrote ${inject_file}" >> "$log_file"
' wake_after "$DELAY" "$MESSAGE" "$INJECT_FILE" "$LOG_FILE" "$CHAT_ID" \
    > /dev/null 2>&1 &

disown
echo "codex wake scheduled in ${DELAY}s (pid=$!)"
