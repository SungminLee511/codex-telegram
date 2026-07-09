# Codex Telegram Bot → Claude Bot Parity: Gap Analysis & Upgrade Plan

Reference repo (target parity): `../telegram_bot` (Claude Code bot).
This repo: `codex_telegram_bot` — an **older single-bot fork** of the same codebase.

Both share an identical package layout (`src/bot`, `src/config`, `src/security`,
`src/storage`, `src/bot/utils`, handlers, middleware). The divergence is almost
entirely in the **multi-bot / process-orchestration / self-wake** layer, plus a
few Claude-era naming artifacts. The core Telegram handling, streaming, and
session logic are structurally equivalent (Codex uses a subprocess+JSONL CLI
integration where Claude uses the Python SDK — this is a legitimate difference,
NOT a gap).

---

## PART A — EVERYTHING THE CODEX REPO LACKS

### A1. Multi-bot isolation (BOT_ID) — **completely absent** (biggest gap)

The Claude bot runs N fully-isolated bots off one codebase, keyed by a `BOT_ID`
slug (default `"main"`). Codex has none of this.

| Concern | Claude bot | Codex bot (current) |
|---|---|---|
| `BOT_ID` settings field | `settings.py` `bot_id: str = "main"` | **missing** |
| Per-bot env file | `<BOT_ID>.env` via `--config-file` | `--config-file` exists but only picks the `.env`; no `BOT_ID` derived |
| Per-bot SQLite DB | `bot_id!="main"` → `sqlite:///data/bot_<id>.db` (after-validator) | fixed `sqlite:///data/bot.db` |
| Per-bot inject spool dir | `inject_spool_dir = /tmp/claude_inject/<id>/` | fixed single file `data/codex_inject_message.json` |
| Per-bot relay-state path | `relay_state_path = /tmp/claude_relay_state_<id>.json` | **missing** |
| Per-bot log file | `bot.log` / `bot_<id>.log` (shell) | fixed `bot.log` |
| Scoped process match | `pgrep` scoped to bare `src.main` vs `--config-file <id>.env` | `pgrep -f "python -m src.main_codex"` — matches **all** instances |
| Registry | `bots.yaml` | **missing** |
| `start_bot.sh` (add a bot, kill nothing) | present | **missing** |
| `supervise.sh` (status/start/restart/stop all enabled) | present | **missing** |
| Port collision fail-fast | `_assert_ports_available` in `main.py` | **missing** (only matters if API/webhook enabled) |
| Design doc | `MULTI_BOT_PLAN.md` | **missing** |

Consequence: you cannot run two Codex bots side by side. A single
`restart_bot.sh` would kill every `main_codex` process, and both would share one
DB + one inject file.

### A2. Self-wake / relay — **present but single-instance only**

`wake_after.sh` and `inject_watcher.py` exist and work, but:
- `wake_after.sh` takes only `<delay> <message>`; **no `BOT_ID` 3rd arg**, writes
  one fixed file. Claude's version: 3rd arg selects bot, atomic tmp→mv into the
  per-bot spool, unique filename per wake (concurrent wakes never clobber).
- `inject_watcher.py` watches **one fixed file** with a `.processed-<ts>` rename.
  Claude's version: FIFO spool-directory scan (oldest-first), atomic
  `_claim_file` rename as the at-most-once claim, plus a legacy single-file shim
  for `main` only, and a placeholder-message trick to get a real `message_id`.
- No per-bot relay-state file, so the kill-switch (max-turns / max-time) state
  described in `CLAUDE.md` cannot be namespaced per bot.

### A3. Missing top-level files

`bots.yaml`, `start_bot.sh`, `supervise.sh`, `MULTI_BOT_PLAN.md`,
`CLAUDE_EXAMPLE.md`, `bot2.env`/`bot3.env` equivalents, and the `tests/`
directory (Claude has `tests/bot/test_inject_watcher_multibot.py`,
`tests/claude/test_facade_resume.py`).

### A4. Claude-era naming artifacts (cosmetic, low priority)

- `sdk_manager` variable name in `main_codex.py` / `facade.py` (Codex has no SDK).
- `codex_allowed_tools` defaults use Claude tool names (`Read/Edit/Grep`); Codex's
  real items are `command_execution`/`file_change`/etc. and the allow/deny list is
  **not actually enforced** at the CLI layer.
- `codex_binary_path` deprecated field still present (superseded by `codex_cli_path`).
- Cost caps (`codex_max_cost_*`) are driven by a **hardcoded local price table**,
  not real billing — misleading but harmless.

### A5. NOT a gap (intentional Codex differences — do not "fix")

- Subprocess `codex exec --json` + JSONL parsing instead of the Claude Python SDK.
- Session resume via `codex exec resume <thread_id>` (thread_id captured from the
  `thread.started` event) instead of SDK session objects.
- `monitor.py` is a bash-path boundary checker, not a stream monitor (same in both).

---

## PART B — IMPLEMENTATION PLAN (ordered, incremental, back-compat)

Guiding rule (from Claude's design): **every change is additive; `BOT_ID="main"`
must reproduce today's single-bot behavior byte-for-byte** (no DB migration, same
`bot.log`, same inject file honored). Naming: Codex uses its own namespaces
(`/tmp/codex_inject/<id>/`, `/tmp/codex_relay_state_<id>.json`, `data/bot_<id>.db`)
to stay fully separate from the Claude bot's `/tmp/claude_*` paths.

### Phase 1 — Config layer (`BOT_ID` plumbing)
1. `src/config/settings.py`: add
   - `bot_id: str = Field("main", ...)`
   - `inject_dir: str = Field("/tmp/codex_inject", ...)`
   - after-validator: if `bot_id != "main"` and `database_url == DEFAULT_DATABASE_URL`
     → `self.database_url = f"sqlite:///data/bot_{self.bot_id}.db"`
   - properties: `inject_spool_dir` → `Path(inject_dir)/bot_id`;
     `relay_state_path` → `/tmp/codex_relay_state_{bot_id}.json`;
     `database_path` (for logging).
2. Confirm `loader.py` already does `load_dotenv(config_file or .env, override=True)`
   before `Settings()` — it does (`loader.py:34-41,61`). No change needed.
   `override=True` is load-bearing (prevents token bleed between bots).
3. `main_codex.py` already accepts `--config-file` and forwards to `load_config`.
   No change needed for arg parsing.
4. **Test**: launch with a `bot2.env` (`BOT_ID=bot2`) and assert DB path becomes
   `data/bot_bot2.db`, spool `/tmp/codex_inject/bot2/`.

### Phase 2 — Inject watcher (spool FIFO + atomic claim)
1. Rewrite `src/bot/inject_watcher.py` to mirror Claude's:
   - `_claim_file(path)` — read then `os.rename` to `.processed-<ts>` (atomic
     at-most-once claim; loser gets `FileNotFoundError → None`).
   - `_pending_files(spool_dir)` — `glob("*.json")` sorted by `(mtime, name)`.
   - `_fire_wake(app, raw)` — parse JSON, send placeholder `[codex auto-wake: …]`
     to get a real `message_id`, build synthetic Update, `app.update_queue.put`.
   - `inject_watcher_loop(app, spool_dir=None, legacy_path=..., poll_seconds, stop_event, *, inject_path=None)`
     — scan spool FIFO then legacy file; keep `inject_path` back-compat alias.
   - Keep `CODEX_INJECT_PATH` honored as the legacy single-file path for `main`.
2. `src/bot/core.py`: wire it per-bot (mirror Claude `core.py:244-259`):
   ```python
   spool_dir = self.settings.inject_spool_dir
   legacy_path = None if self.settings.bot_id != "main" else DEFAULT_INJECT_PATH
   inject_task = asyncio.create_task(
       inject_watcher_loop(self.app, spool_dir=spool_dir, legacy_path=legacy_path))
   ```
3. **Test**: port `test_inject_watcher_multibot.py` — two spool dirs, concurrent
   wake files, assert each fires once and files end `.processed-*`.

### Phase 3 — `wake_after.sh` (BOT_ID arg + atomic spool writes)
1. Add 3rd positional `BOT_ID` (`${3:-${BOT_ID:-main}}`).
2. `main` → legacy single file `data/codex_inject_message.json` (atomic tmp→mv).
3. named bot → `mkdir -p /tmp/codex_inject/<id>/`, unique
   `$(date +%s%N)-$RANDOM.json`, atomic tmp→mv.
4. Per-bot chat-id override: `WAKE_CHAT_ID_<id>` → `WAKE_CHAT_ID` → default.
5. Pass message via env var (not string interpolation) to survive quotes/parens.

### Phase 4 — Process scripts (scoped, non-interfering)
1. Rewrite `restart_bot.sh` with a `matching_pids()` that scopes per bot:
   - `main`: `pgrep -af "[s]rc\.main_codex"` **excluding** `--config-file`.
   - named: `pgrep -f "[s]rc\.main_codex.*--config-file[ =]*<id>.env"`.
   - Launch: `main` bare `-m src.main_codex`; named
     `-m src.main_codex --config-file <id>.env`. Keep the existing `env -i` /
     token-unset hardening so the Claude bot's exported tokens never leak in.
   - Log: `bot.log` (main) / `bot_<id>.log` (named).
2. Add `start_bot.sh` — resolve BOT_ID, require `<id>.env` for non-main,
   `nohup … & disown`, verify alive after 2s. Kills nothing.
3. Add `supervise.sh` — `status|start|restart|stop`, reads enabled bots from
   `bots.yaml` (inline python yaml parse), delegates to the per-bot scripts.
4. Add `bots.yaml` (`{bot_id, env_file, enabled}` list); seed with `main`.
5. Add a `bot2.env` template (copy `.env`, set `BOT_ID`, distinct
   `TELEGRAM_BOT_TOKEN`/`TELEGRAM_BOT_USERNAME`/`ALLOWED_USERS`).

### Phase 5 — Optional hardening & cleanup
1. Port `_assert_ports_available` into `main_codex.py` (only if API/webhook are
   ever enabled — otherwise skip; Codex bot is polling-only today).
2. Rename `sdk_manager` → `codex_manager`; drop deprecated `codex_binary_path`;
   fix `codex_allowed_tools` defaults to real Codex item names OR document that
   the list is advisory (not enforced).
3. Update `README.md` + `.env.example` with the multi-bot section; add
   `MULTI_BOT_PLAN.md` (adapt Claude's, swap `/tmp/claude_*` → `/tmp/codex_*`).

### Phase 6 — Tests & verification
1. Port both Claude tests (`tests/`), adapt paths/names.
2. Manual: run `main` + `bot2` simultaneously → confirm separate DBs, separate
   inject spools, `restart_bot.sh bot2` leaves `main` (and the Claude bots)
   untouched, wakes route to the right bot.

---

## PART C — EFFORT / RISK NOTES

- Phases 1–4 are the real parity work; ~90% is mechanical porting from the
  Claude repo (files are near-identical siblings). Main care point: keep Codex
  namespaces (`/tmp/codex_*`, `src.main_codex`) distinct so nothing ever matches
  or clobbers the Claude bot's processes/files.
- Highest-risk item: `restart_bot.sh` pgrep scoping — a wrong regex could kill
  the Claude bots or all Codex instances. Test the `matching_pids()` regex in
  isolation before wiring the kill.
- No DB migration needed: `main` keeps `data/bot.db`.
- The Codex CLI integration (`cli_integration.py`) needs **no changes** for
  parity — it is functionally complete; the gaps are all orchestration-layer.
