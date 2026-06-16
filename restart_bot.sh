#!/usr/bin/env bash
# Restart the Codex Telegram bot.
# Kills any running `python -m src.main_codex` process, then starts a fresh one
# under nohup with logs to bot.log next to this script.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "[restart_bot] ERROR: cannot cd to $SCRIPT_DIR"; exit 1; }

LOG_FILE="$SCRIPT_DIR/bot.log"

echo "[restart_bot] Looking for existing bot process..."
PIDS=$(pgrep -f "python -m src.main_codex" || true)

if [ -n "$PIDS" ]; then
    echo "[restart_bot] Killing PIDs: $PIDS"
    kill $PIDS 2>/dev/null || true

    # Wait up to 10s for graceful shutdown
    for _ in $(seq 1 10); do
        if ! pgrep -f "python -m src.main_codex" >/dev/null; then
            break
        fi
        sleep 1
    done

    # Force kill if still alive
    REMAINING=$(pgrep -f "python -m src.main_codex" || true)
    if [ -n "$REMAINING" ]; then
        echo "[restart_bot] Force killing: $REMAINING"
        kill -9 $REMAINING 2>/dev/null || true
        sleep 1
    fi
else
    echo "[restart_bot] No existing bot process found."
fi

echo "[restart_bot] Starting new bot process..."
# Clear env vars that would shadow .env (claude bot exports its own tokens)
PYBIN="$(which python)"
unset TELEGRAM_BOT_TOKEN TELEGRAM_BOT_USERNAME ANTHROPIC_API_KEY OPENAI_API_KEY
nohup env -i HOME="$HOME" PATH="$HOME/.local/bin:$(dirname "$PYBIN"):/usr/bin:/bin" \
    "$PYBIN" -m src.main_codex > "$LOG_FILE" 2>&1 &
NEW_PID=$!
disown $NEW_PID 2>/dev/null || true

sleep 2

if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "[restart_bot] Bot started. PID=$NEW_PID, log=$LOG_FILE"
else
    echo "[restart_bot] ERROR: bot failed to start. Check $LOG_FILE"
    exit 1
fi
