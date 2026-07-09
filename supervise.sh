#!/usr/bin/env bash
# supervise.sh — start / stop / restart / status all ENABLED Codex bots in
# bots.yaml.
#
# Usage:
#     ./supervise.sh start      # start every enabled bot (skips already-running)
#     ./supervise.sh restart    # restart every enabled bot
#     ./supervise.sh stop       # stop every enabled bot
#     ./supervise.sh status     # show running/stopped per enabled bot
#
# Pure convenience over start_bot.sh / restart_bot.sh — those still work
# standalone. Each bot stays isolated (spool dir, DB, log, process match) and
# nothing here ever matches the Claude bots (`src.main` vs `src.main_codex`).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "[supervise] ERROR: cannot cd to $SCRIPT_DIR"; exit 1; }

BOTS_YAML="${BOTS_YAML:-$SCRIPT_DIR/bots.yaml}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python || echo /home/sky/miniconda3/envs/Telegram-bot-env/bin/python)}"
ACTION="${1:-status}"

if [ ! -f "$BOTS_YAML" ]; then
    echo "[supervise] ERROR: $BOTS_YAML not found"
    exit 1
fi

# Emit one enabled bot_id per line.
enabled_bot_ids() {
    "$PYTHON_BIN" - "$BOTS_YAML" <<'PY'
import sys, yaml
with open(sys.argv[1]) as fh:
    doc = yaml.safe_load(fh) or {}
for b in (doc.get("bots") or []):
    if b.get("enabled"):
        print(b.get("bot_id", "").strip())
PY
}

# Is a given codex bot running? (mirror restart_bot.sh matching logic)
is_running() {
    local bid="$1"
    if [ "$bid" = "main" ]; then
        pgrep -af "[s]rc\.main_codex" 2>/dev/null | grep -v -- "--config-file" | grep -q .
    else
        pgrep -f "[s]rc\.main_codex.*--config-file[ =]*${bid}\.env" >/dev/null 2>&1
    fi
}

mapfile -t BOT_IDS < <(enabled_bot_ids)
if [ "${#BOT_IDS[@]}" -eq 0 ]; then
    echo "[supervise] No enabled bots in $BOTS_YAML"
    exit 0
fi

case "$ACTION" in
    start)
        for bid in "${BOT_IDS[@]}"; do
            if is_running "$bid"; then
                echo "[supervise] '$bid' already running — skip"
            else
                ./start_bot.sh "$bid"
            fi
        done
        ;;
    restart)
        for bid in "${BOT_IDS[@]}"; do
            ./restart_bot.sh "$bid"
        done
        ;;
    stop)
        for bid in "${BOT_IDS[@]}"; do
            if [ "$bid" = "main" ]; then
                PIDS=$(pgrep -af "[s]rc\.main_codex" 2>/dev/null | grep -v -- "--config-file" | awk '{print $1}')
            else
                PIDS=$(pgrep -f "[s]rc\.main_codex.*--config-file[ =]*${bid}\.env" 2>/dev/null || true)
            fi
            if [ -n "$PIDS" ]; then
                echo "[supervise] stopping '$bid' (PIDs: $PIDS)"
                kill $PIDS 2>/dev/null || true
            else
                echo "[supervise] '$bid' not running"
            fi
        done
        ;;
    status)
        for bid in "${BOT_IDS[@]}"; do
            if is_running "$bid"; then
                echo "[supervise] $bid : RUNNING"
            else
                echo "[supervise] $bid : stopped"
            fi
        done
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
