#!/bin/bash
# wake_after.sh — schedule a synthetic Telegram message for Codex after a delay.
#
# Usage:
#     ./wake_after.sh <delay_seconds> "<wake-up message>" [BOT_ID]
#     ./wake_after.sh 1500 "Check build.log; push if done."
#     ./wake_after.sh 30   "RELAY: next step" work        # target the 'work' bot
#
# Runs under nohup so it outlives the calling shell. After `<delay_seconds>`,
# drops a wake file the Codex bot's inject_watcher picks up and routes to the
# agentic-text handler — resuming the Codex session and acting on the prompt.
#
# Multi-bot:
#   BOT_ID (3rd arg or $BOT_ID env, default "main") selects which bot wakes.
#   - main : writes the legacy single file (data/codex_inject_message.json,
#            overridable via $CODEX_INJECT_PATH). The 'main' bot watches both
#            this legacy file and its spool dir.
#   - other: writes a UNIQUE file into <INJECT_DIR>/<BOT_ID>/ via an atomic
#            write-tmp-then-mv, so concurrent wakes never clobber.
#   CHAT_ID resolves from $WAKE_CHAT_ID_<BOT_ID>, then $WAKE_CHAT_ID, then the
#   default allowed user.
#
# Codex paths (/tmp/codex_inject, data/codex_inject_message.json) are
# intentionally distinct from the Claude bot's so the two never clash.

set -euo pipefail

DELAY="${1:-300}"
MESSAGE="${2:-Wake up}"
BOT_ID="${3:-${BOT_ID:-main}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Per-bot chat-id override: WAKE_CHAT_ID_<BOT_ID> > WAKE_CHAT_ID > default.
_perbot_var="WAKE_CHAT_ID_${BOT_ID}"
CHAT_ID="${!_perbot_var:-${WAKE_CHAT_ID:-8610757705}}"

INJECT_BASE="${INJECT_DIR:-/tmp/codex_inject}"
SPOOL_DIR="${INJECT_BASE}/${BOT_ID}"
LEGACY_FILE="${CODEX_INJECT_PATH:-$SCRIPT_DIR/data/codex_inject_message.json}"
LOG_FILE="${SCRIPT_DIR}/data/codex_wake_after.log"
mkdir -p "$(dirname "$LOG_FILE")"

# Pass the raw message + chat id to the delayed worker via the ENVIRONMENT
# (not string interpolation) so single/double quotes, parentheses, etc. in the
# message can never break the shell quoting of the generated script. The JSON
# payload is built by python inside the worker, which is quote-safe.
export WAKE_MESSAGE="$MESSAGE"
export WAKE_CHAT_ID_RESOLVED="$CHAT_ID"

nohup bash -c "
    sleep ${DELAY}
    echo \"[\$(date -u)] firing codex wake after ${DELAY}s (bot=${BOT_ID})\" >> ${LOG_FILE}
    PAYLOAD=\"\$(python3 -c 'import json, os; print(json.dumps({\"chat_id\": int(os.environ[\"WAKE_CHAT_ID_RESOLVED\"]), \"text\": os.environ[\"WAKE_MESSAGE\"]}))')\"
    if [ -z \"\$PAYLOAD\" ]; then
        echo \"[\$(date -u)] ERROR: empty payload, aborting wake\" >> ${LOG_FILE}
        exit 1
    fi
    if [ \"${BOT_ID}\" = \"main\" ]; then
        # Legacy single file (atomic via tmp+mv to avoid half-written reads).
        mkdir -p \"\$(dirname \"${LEGACY_FILE}\")\"
        TMP=\"${LEGACY_FILE}.tmp.\$\$\"
        printf '%s' \"\$PAYLOAD\" > \"\$TMP\"
        mv -f \"\$TMP\" \"${LEGACY_FILE}\"
        echo \"[\$(date -u)] wrote ${LEGACY_FILE}\" >> ${LOG_FILE}
    else
        # Per-bot spool: unique filename, atomic mv into place.
        mkdir -p \"${SPOOL_DIR}\"
        NAME=\"\$(date +%s%N)-\$RANDOM\"
        TMP=\"${SPOOL_DIR}/.\${NAME}.tmp\"
        DST=\"${SPOOL_DIR}/\${NAME}.json\"
        printf '%s' \"\$PAYLOAD\" > \"\$TMP\"
        mv -f \"\$TMP\" \"\$DST\"
        echo \"[\$(date -u)] wrote \$DST\" >> ${LOG_FILE}
    fi
" > /dev/null 2>&1 &

disown
echo "codex wake scheduled in ${DELAY}s (pid=$!, bot=${BOT_ID})"
