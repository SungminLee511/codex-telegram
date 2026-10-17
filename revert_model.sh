#!/usr/bin/env bash
# Emergency revert: put every Codex bot back on one model and restart them.
# Usage (via SSH if Telegram is dead):
#   bash <codex_bot_repo>/revert_model.sh            # -> gpt-6-sol
#   bash <codex_bot_repo>/revert_model.sh gpt-6-astra
# Touches only this repo's .env / <BOT_ID>.env files (CODEX_MODEL) and restarts
# only Codex bots; the Claude bots are never affected.
set -euo pipefail
cd "$(dirname "$0")"

MODEL="${1:-gpt-6-sol}"

for f in .env *.env; do
  [ -f "$f" ] || continue
  case "$f" in *.example) continue ;; esac
  if grep -q '^CODEX_MODEL=' "$f"; then
    sed -i "s/^CODEX_MODEL=.*/CODEX_MODEL=${MODEL}/" "$f"
  else
    echo "CODEX_MODEL=${MODEL}" >> "$f"
  fi
  echo "[revert] $f -> CODEX_MODEL=${MODEL}"
  if [ "$f" = ".env" ]; then
    b=main
  else
    b="${f%.env}"
  fi
  echo "[revert] restarting $b ..."
  bash restart_bot.sh "$b" || echo "[revert] WARN: $b restart returned nonzero"
done

echo "[revert] done. Model=${MODEL}. Send a Telegram message to verify."
