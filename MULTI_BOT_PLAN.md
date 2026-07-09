# Multi-Bot Support — run N Codex Telegram bots concurrently, crash-free

**Goal:** allow multiple independent Codex Telegram bot processes (one per
token) to run on the same host **simultaneously**, with **zero** cross-talk,
**zero** crashes, and **fully independent** self-wake / relay / restart
behaviour — and never interfering with the sibling Claude-Code bots.

This mirrors the Claude bot's multi-bot design, adapted to Codex namespaces
(`src.main_codex`, `/tmp/codex_inject`, `data/codex_inject_message.json`,
`/tmp/codex_relay_state_<id>.json`).

---

## 0. Shared-singleton inventory (root cause of collisions)

| # | Shared resource (before) | Where | Failure if 2 bots share it |
|---|--------------------------|-------|----------------------------|
| C1 | **Inject file** `data/codex_inject_message.json` | `inject_watcher.py`, `wake_after.sh` | A wake meant for bot A eaten by whichever watcher polls first → wrong bot wakes / rename race. |
| C2 | **Relay file clobber** (truncating write) | `wake_after.sh` | Two near-simultaneous wakes overwrite each other → lost wake. |
| C3 | **SQLite DB** `sqlite:///data/bot.db` | `constants.py` `DEFAULT_DATABASE_URL` | Shared DB → session_id/auth interleave, `database is locked`. |
| C4 | **restart_bot.sh** kills all `src.main_codex` | `restart_bot.sh` | Restarting bot A kills bot B too. |
| C5 | **API / webhook ports** `8080` / `8443` | `settings.py` | Only if servers enabled; second bot fails to bind. |
| C6 | **Relay-state file** (kill-switch counter) | CLAUDE.md convention | Two relays share one counter → wrong accounting. |

Non-issues (no change needed):
- **Telegram polling**: distinct tokens = distinct `getUpdates` → no 409.
- **Codex CLI integration**: each turn is a fresh `codex exec` subprocess; session
  continuity is `codex exec resume <thread_id>`, stored per-user in the DB. Once
  each bot has its own DB (C3) there is no shared in-memory state.
- **Claude bots are safe**: they run `src.main`; every Codex pgrep matches only
  `src.main_codex`, so the two families never touch each other.

---

## Design principle

Everything keyed by a single **`BOT_ID`** slug (default `main`). One `.env` per
bot → `<BOT_ID>.env`, passed via `--config-file`. All per-bot paths derive from
`BOT_ID`. Default `bot_id="main"` reproduces legacy single-bot behaviour
byte-for-byte (no DB migration).

---

## What was implemented

| Part | Where | Verified |
|------|-------|----------|
| **A. Inject isolation** (C1,C2) | `settings.py` (`bot_id`, `inject_dir`, `inject_spool_dir`, `relay_state_path`); `inject_watcher.py` (spool-dir FIFO + atomic `_claim_file` + legacy shim for `main`); `core.py` (wires per-bot spool, legacy file only for `main`) | FIFO order + at-most-once claim unit-checked |
| **B. Wake tooling** (C2,C6) | `wake_after.sh` (`[BOT_ID]` 3rd arg; atomic tmp→mv; unique `<ns>-<rand>.json`; per-bot `WAKE_CHAT_ID_<id>`); `relay_state_path` per bot | named-bot spool + main legacy both verified, quote-safe |
| **C. Per-bot DB** (C3) | `settings.py` after-validator: `bot_id!="main"` + default URL → `sqlite:///data/bot_<id>.db`; startup log echoes `bot_id` | `main`→`bot.db`, `bot2`→`bot_bot2.db`, explicit URL respected |
| **D. Launcher isolation** (C4) | `restart_bot.sh` (scoped `src.main_codex` match, main vs `--config-file`); `start_bot.sh` | syntax OK; isolation proven vs 6 live claude procs |
| **E. Port distinctness** (C5) | `main_codex.py` `_assert_ports_available` (only checks enabled servers) | ported from claude bot |
| **F. Supervisor** | `bots.yaml` (`{bot_id, env_file, enabled}`), `supervise.sh` (status/start/restart/stop enabled bots) | `status` detects live `main` |

---

## How to run a second Codex bot

1. `cp bot2.env.example bot2.env`; set `BOT_ID=bot2`, a DISTINCT
   `TELEGRAM_BOT_TOKEN` + `TELEGRAM_BOT_USERNAME`, and `ALLOWED_USERS`.
   (Only if API/webhook enabled: distinct `API_SERVER_PORT`/`WEBHOOK_PORT`.)
2. `./start_bot.sh bot2` (kills nothing) — or set `enabled: true` in `bots.yaml`
   then `./supervise.sh start`.
3. Wake it: `./wake_after.sh 30 "RELAY: ..." bot2`.
4. Restart only it: `./restart_bot.sh bot2` — never touches `main` or the
   Claude bots.

Per-bot derived paths for `bot2`: DB `data/bot_bot2.db`, spool
`/tmp/codex_inject/bot2/`, relay-state `/tmp/codex_relay_state_bot2.json`,
log `bot_bot2.log`.

## Crash-safety argument

1. Different tokens → no Telegram 409.
2. Per-bot spool dir + unique filenames + atomic mv/rename → no inject
   collision/clobber; FIFO ordering.
3. Per-bot DB → no SQLite lock contention or session cross-talk.
4. Per-bot restart match (`src.main_codex` + `--config-file <id>.env`) → killing
   one never kills another (or any Claude bot).
5. Port assertion fails fast instead of crashing mid-run (ports off by default).
6. Fully backward compatible → single-bot `main` deploy behaves exactly as
   before, no DB migration.

## Rollback

All changes are additive + backward compatible. Revert any file with
`git checkout <ref> -- <file>`. No DB migration required for `main`.
