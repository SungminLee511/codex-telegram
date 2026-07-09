#!/usr/bin/env bash
# start_bot.sh — launch one Codex bot WITHOUT killing anything (use
# restart_bot.sh to replace a running instance). Multi-bot aware.
#
# Usage:
#     ./start_bot.sh            # start the 'main' bot (legacy, no config file)
#     ./start_bot.sh work       # start the 'work' bot via work.env
#     BOT_ID=work ./start_bot.sh
#
# Uses `env -i` for the same token-isolation reason as restart_bot.sh.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "[start_bot] ERROR: cannot cd to $SCRIPT_DIR"; exit 1; }

BOT_ID="${1:-${BOT_ID:-main}}"

if [ "$BOT_ID" = "main" ]; then
    LOG_FILE="$SCRIPT_DIR/bot.log"
    LAUNCH_ARGS=""
else
    LOG_FILE="$SCRIPT_DIR/bot_${BOT_ID}.log"
    ENV_FILE="${BOT_ID}.env"
    LAUNCH_ARGS="--config-file ${ENV_FILE}"
    if [ ! -f "$SCRIPT_DIR/$ENV_FILE" ]; then
        echo "[start_bot] ERROR: config file $ENV_FILE not found for bot '$BOT_ID'"
        exit 1
    fi
fi

echo "[start_bot] Starting '$BOT_ID' (log=$LOG_FILE)..."
PYBIN="$(which python)"
unset TELEGRAM_BOT_TOKEN TELEGRAM_BOT_USERNAME ANTHROPIC_API_KEY OPENAI_API_KEY
# shellcheck disable=SC2086
nohup env -i HOME="$HOME" PATH="$HOME/.local/bin:$(dirname "$PYBIN"):/usr/bin:/bin" \
    "$PYBIN" -m src.main_codex $LAUNCH_ARGS > "$LOG_FILE" 2>&1 &
NEW_PID=$!
disown $NEW_PID 2>/dev/null || true

sleep 2
if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "[start_bot] Bot '$BOT_ID' started. PID=$NEW_PID, log=$LOG_FILE"
else
    echo "[start_bot] ERROR: bot '$BOT_ID' failed to start. Check $LOG_FILE"
    exit 1
fi
