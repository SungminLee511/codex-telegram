#!/bin/bash
# wake_when.sh — wake Codex when a flag file turns true, or when a timeout hits.
#
# Usage:
#     ./wake_when.sh --flag PATH --timeout DURATION --message "TEXT" [--log PATH] [--bot BOT_ID]
#     ./wake_when.sh --flag /tmp/train.done --timeout 6h \
#         --message "RELAY: training finished, push the results." --log train.log
#
# The flag file is the whole condition. The *job* (or a tiny checker script that
# Codex writes) decides whether the condition holds and writes "true" or "1"
# into the file. This script never runs a user-supplied command: it only reads
# the first few bytes of one file, every 10 seconds, so there is nothing to
# hang, crash, or inject into.
#
#   --flag     file to watch. The condition is met when it exists and holds
#              `true` or `1` (case-insensitive, whitespace ignored).
#   --timeout  REQUIRED, no default. Seconds, or with an s/m/h/d suffix. When it
#              passes, the wake fires anyway ("TIMED OUT"), in case the job died
#              before it could write the flag.
#   --message  the prompt Codex receives. It is prefixed with why it fired:
#              [condition met: ...] or [TIMED OUT after ...: ...].
#   --log      optional. Its last 20 lines are appended to the wake.
#   --bot      which bot wakes (default $BOT_ID, then "main").
#
# Fires once and stops. To keep watching, set it again from the wake.
# Runs detached (nohup), so it outlives the calling shell. Delivery goes through
# wake_after.sh, so the chat id and per-bot spool behave exactly as they do there.
#
# WAKE_WHEN_POLL (seconds, default 10) exists for tests only.

set -euo pipefail

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

FLAG=""
TIMEOUT_RAW=""
MESSAGE=""
LOG=""
BOT="${BOT_ID:-main}"

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --flag|--timeout|--message|--log|--bot)
            [ $# -ge 2 ] || { echo "wake_when: $1 needs a value" >&2; exit 2; }
            case "$1" in
                --flag) FLAG="$2" ;;
                --timeout) TIMEOUT_RAW="$2" ;;
                --message) MESSAGE="$2" ;;
                --log) LOG="$2" ;;
                --bot) BOT="$2" ;;
            esac
            shift 2
            ;;
        *) echo "wake_when: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$FLAG" ]        || { echo "wake_when: --flag is required" >&2; exit 2; }
[ -n "$TIMEOUT_RAW" ] || { echo "wake_when: --timeout is required (no default)" >&2; exit 2; }
[ -n "$MESSAGE" ]     || { echo "wake_when: --message is required" >&2; exit 2; }

parse_duration() {
    local raw="$1" n u
    [[ "$raw" =~ ^([0-9]+)([smhd]?)$ ]] || return 1
    n="${BASH_REMATCH[1]}"; u="${BASH_REMATCH[2]}"
    case "$u" in
        ""|s) echo "$n" ;;
        m) echo $(( n * 60 )) ;;
        h) echo $(( n * 3600 )) ;;
        d) echo $(( n * 86400 )) ;;
    esac
}

TIMEOUT="$(parse_duration "$TIMEOUT_RAW")" \
    || { echo "wake_when: bad --timeout '$TIMEOUT_RAW' (use e.g. 90, 30m, 6h)" >&2; exit 2; }
[ "$TIMEOUT" -gt 0 ] || { echo "wake_when: --timeout must be greater than 0" >&2; exit 2; }
[[ "$BOT" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "wake_when: bad --bot '$BOT'" >&2; exit 2; }

FLAG="$(realpath -m -- "$FLAG")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Everything dynamic travels in the environment, so quotes or newlines in the
# message can never break the worker's shell. WAKE_MESSAGE and WAKE_FLAG are also
# what the bot reads from /proc to show this wait in the banner.
export WAKE_MESSAGE="$MESSAGE"
export WAKE_FLAG="$FLAG"
export WAKE_LOG="$LOG"
export WAKE_TIMEOUT="$TIMEOUT"
case "$TIMEOUT_RAW" in *[smhd]) LABEL="$TIMEOUT_RAW" ;; *) LABEL="${TIMEOUT_RAW}s" ;; esac
export WAKE_TIMEOUT_LABEL="$LABEL"
export WAKE_BOT="$BOT"
export WAKE_SCRIPT_DIR="$SCRIPT_DIR"
export WAKE_POLL="${WAKE_WHEN_POLL:-10}"
mkdir -p "$SCRIPT_DIR/data"
export WAKE_LOGFILE="$SCRIPT_DIR/data/codex_wake_after.log"

WORKER="$(cat <<'EOF'
flag_true() {
    [ -f "$WAKE_FLAG" ] || return 1
    local v
    v="$(head -c 64 -- "$WAKE_FLAG" 2>/dev/null | tr -d '[:space:]' | tr 'A-Z' 'a-z')"
    [ "$v" = "true" ] || [ "$v" = "1" ]
}

deadline=$(( $(date +%s) + WAKE_TIMEOUT ))
while :; do
    if flag_true; then
        reason="condition met: $WAKE_FLAG is true"
        break
    fi
    remaining=$(( deadline - $(date +%s) ))
    if [ "$remaining" -le 0 ]; then
        reason="TIMED OUT after $WAKE_TIMEOUT_LABEL: $WAKE_FLAG never became true"
        break
    fi
    nap="$WAKE_POLL"
    [ "$remaining" -lt "$nap" ] && nap="$remaining"
    sleep "$nap"
done

text="[$reason] $WAKE_MESSAGE"
if [ -n "${WAKE_LOG:-}" ] && [ -f "$WAKE_LOG" ]; then
    tail_text="$(tail -n 20 -- "$WAKE_LOG" | tail -c 2500)"
    text="$text"$'\n\n'"Last 20 lines of $WAKE_LOG:"$'\n'"$tail_text"
fi
echo "[$(date -u)] codex conditional wake fired (bot=$WAKE_BOT): $reason" >> "$WAKE_LOGFILE"
bash "$WAKE_SCRIPT_DIR/wake_after.sh" 0 "$text" "$WAKE_BOT" > /dev/null
EOF
)"

# The extra argument becomes $0 of the worker. It sits on the process command
# line, which is how the bot finds this wait (see pending_wakes.py). The
# "codex cond-wake" wording keeps the Claude bot's scanner from listing it.
nohup bash -c "$WORKER" "codex cond-wake (bot=${BOT} timeout=${TIMEOUT}s)" > /dev/null 2>&1 &
disown
echo "codex conditional wake set: flag=${FLAG} timeout=${TIMEOUT_RAW} (pid=$!, bot=${BOT})"
