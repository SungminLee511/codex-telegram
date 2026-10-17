# Codex Telegram Bot

Telegram bot for remote OpenAI Codex CLI access. Talk to your Codex agent
from anywhere, with full session resume, image attachments, voice input,
streamed tool-use updates, and per-user authorization.

This is a **Codex port** of [claude-code-telegram](https://github.com/richardatkinson/claude-code-telegram):
the orchestrator, security layer, session storage and Telegram glue are
preserved; the Claude Agent SDK integration was replaced with a thin async
wrapper around `codex exec --json`.

## How it works

Each user message becomes one `codex exec [resume <id>] --json -` invocation:

- prompt is sent on stdin
- JSONL events (`thread.started`, `item.started/updated/completed`,
  `turn.completed`, `turn.failed`) are parsed line-by-line
- session ID from `thread.started.thread_id` is persisted, so the next
  message resumes the same Codex thread
- token usage from `turn.completed.usage` is recorded per session
- `command_execution`, `mcp_tool_call`, `web_search`, `file_change`,
  `todo_list`, and `reasoning` items are streamed back to Telegram as
  inline progress updates

Codex `exec` mode runs with `approval_policy=Never`, so the sandbox flag
(`-s read-only|workspace-write|danger-full-access`) is the only thing
gating what the agent is allowed to touch — set it appropriately.

## Prerequisites

- Python 3.11+
- [Codex CLI](https://github.com/openai/codex) installed and on `PATH`
  (or set `CODEX_CLI_PATH`)
- Either `codex login` already run, **or** `OPENAI_API_KEY` exported

```bash
# install codex
npm install -g @openai/codex
# OR grab a binary from https://github.com/openai/codex/releases

# log in once (writes ~/.codex/auth.json)
codex login
```

## Setup

```bash
git clone https://github.com/SungminLee511/codex-telegram
cd codex-telegram
pip install -e .
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN / APPROVED_DIRECTORY / ALLOWED_USERS
```

## Run

```bash
# Foreground
python -m src.main_codex

# Background (nohup)
nohup python -m src.main_codex > bot.log 2>&1 &

# Restart
bash restart_bot.sh

# View logs
tail -f bot.log
```

## Multiple bots (multi-bot)

You can run several isolated Codex bots (one per token) on one host. Everything
is keyed by a `BOT_ID` slug (default `main`); each bot gets its own DB, inject
spool, relay-state file, log, and scoped process match. `main` keeps legacy
paths byte-for-byte. See `MULTI_BOT_PLAN.md` for the full design.

```bash
# Add a second bot
cp bot2.env.example bot2.env        # set BOT_ID=bot2 + a distinct token/username
./start_bot.sh bot2                 # start it (kills nothing)
./restart_bot.sh bot2               # restart ONLY bot2
./supervise.sh status               # status of all enabled bots in bots.yaml

# Self-wake a specific bot (3rd arg = BOT_ID)
./wake_after.sh 30 "RELAY: next step" bot2
```

Derived per-bot paths (`bot2` shown): DB `data/bot_bot2.db`, inject spool
`/tmp/codex_inject/bot2/`, relay-state `/tmp/codex_relay_state_bot2.json`,
log `bot_bot2.log`. Codex paths are separate from the Claude bot's `/tmp/claude_*`
and match only `src.main_codex`, so the two families never interfere.

## .env reference

See `.env.example`. Required fields:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_BOT_USERNAME` | Bot username (no `@`) |
| `APPROVED_DIRECTORY` | Working directory root for Codex |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs |

Codex-specific knobs (all optional):

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | unset | Skip `codex login` if set |
| `CODEX_CLI_PATH` | `which codex` | Path to the `codex` binary |
| `CODEX_MODEL` | CLI default | default model, e.g. `gpt-6-sol`; `/model` overrides per session |
| `CODEX_REASONING_EFFORT` (or `BOT_DEFAULT_EFFORT`) | unset | `minimal` / `low` / `medium` / `high` / `xhigh`; `/effort` overrides per session |
| `SESSION_HEADER` | `true` | footer banner on replies |
| `SESSION_TIMEOUT_HOURS` | `168` | idle time before a session is not auto-resumed |
| `CODEX_SANDBOX_MODE` | `workspace-write` | `read-only` / `workspace-write` / `danger-full-access` |
| `CODEX_SKIP_GIT_REPO_CHECK` | `true` | Pass `--skip-git-repo-check` |
| `CODEX_DANGEROUSLY_BYPASS` | `false` | Pass `--dangerously-bypass-approvals-and-sandbox` |
| `CODEX_ADD_DIRS` | unset | CSV of `--add-dir` paths |
| `CODEX_CONFIG_OVERRIDES` | unset | CSV of `-c key=value` overrides |
| `CODEX_TIMEOUT_SECONDS` | `900` | Per-run timeout |

## Telegram commands

Same command set as the Claude Code bot:

| Command | What it does |
|---|---|
| `/start` | Welcome |
| `/new` | Start a fresh Codex thread (skip auto-resume) |
| `/sessions [all \| hidden \| text]` | Browse every Codex session on the host (`~/.codex/sessions`), preview, hide/unhide, resume — even past the 7-day timeout |
| `/rename <name>` | Name the current session (stored bot-side in `data/session_titles.json`) |
| `/model [id]` | Per-session model: buttons `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-terra`, or any typed id (unverified) → `-m` |
| `/effort [level\|auto]` | Per-session reasoning effort `minimal`…`xhigh` → `-c model_reasoning_effort=…` |
| `/branch [label]` | Fork now: copy the rollout under a new thread id and switch to it; the original is untouched |
| `/restart` | Restart the bot |

Hidden but typable: `/status`, `/verbose 0|1|2`, `/repo [name]`.
Unknown `/commands` are forwarded to Codex as text. Photos, voice notes and
files go to Codex too (uploads land in `uploads/<date>/` and Codex gets the path).

Every reply carries a footer banner: session name and start time, context
size (+ compactions), model, effort, bot, the 5h/weekly rate-limit meters
(read from `rate_limits` in the newest rollouts — no network, no credentials)
and any pending wakes (⏰ timed, 👀 conditional).

Other behaviour ported from the Claude bot:

- **Stop button** on every request (text, file, photo, voice) — interrupts the
  codex process.
- **Crash-safe progress**: "Working..." messages left by a dead process are
  retired on startup.
- **Outbox**: files Codex drops in `outbox/` are sent after the reply, then
  deleted (images as photos, ≤50 MB, 10 per sweep).
- **Empty-reply nudge**: a reply with no text schedules a 5 s wake telling
  Codex to answer (max 3 in a row).
- **Resume after kill**: a codex process killed by a signal is re-resumed on
  the same thread (up to 3 tries) instead of starting over.
- **Self-wake**: `wake_after.sh` (timed) and `wake_when.sh` (flag file or
  timeout). `AGENTS_EXAMPLE.md` is a ready-made `AGENTS.md` describing them.

Deliberately not ported: question buttons (Codex has no AskUserQuestion
tool) and the interactive tool-permission gate (`codex exec` never asks).

## Project context

Codex loads `AGENTS.md` natively. On a new thread the bot also prepends a
working-directory boundary note and, only when no `AGENTS.md` exists, the
directory's `CLAUDE.md` as a legacy fallback. Resumed turns send just the new
message.

## Notes & limitations

- **No live tool approval prompts.** `codex exec` is strict
  `approval_policy=Never`, so dangerous tool gating is the sandbox flag,
  not user confirmation. If you need approvals, run with
  `CODEX_SANDBOX_MODE=read-only` and explicitly grant write dirs via
  `CODEX_ADD_DIRS`.
- **One-shot turns.** Each user message spawns a new `codex exec`
  process; multi-turn is emulated via `codex exec resume`. For most chat
  workflows the startup cost is fine; for tight loops you'd want the
  `app-server` protocol (not implemented here).
- **Cost is estimated.** Codex emits token counts, not USD. The bot
  multiplies by built-in OpenAI pricing tables for a rough estimate;
  verify against your OpenAI dashboard for billing accuracy.

## License

MIT. Original `claude-code-telegram` © Richard Atkinson; Codex port © Sungmin Lee.
