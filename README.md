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
| `CODEX_MODEL` | CLI default | e.g. `gpt-5-codex`, `gpt-5`, `gpt-5-mini` |
| `CODEX_REASONING_EFFORT` | unset | `minimal` / `low` / `medium` / `high` |
| `CODEX_SANDBOX_MODE` | `workspace-write` | `read-only` / `workspace-write` / `danger-full-access` |
| `CODEX_SKIP_GIT_REPO_CHECK` | `true` | Pass `--skip-git-repo-check` |
| `CODEX_DANGEROUSLY_BYPASS` | `false` | Pass `--dangerously-bypass-approvals-and-sandbox` |
| `CODEX_ADD_DIRS` | unset | CSV of `--add-dir` paths |
| `CODEX_CONFIG_OVERRIDES` | unset | CSV of `-c key=value` overrides |
| `CODEX_TIMEOUT_SECONDS` | `900` | Per-run timeout |

## Telegram commands

- `/start`, `/help` — usage
- `/new` — start a fresh Codex thread (skip auto-resume)
- `/cd <subdir>`, `/ls`, `/pwd` — navigate inside `APPROVED_DIRECTORY`
- `/projects` — list available projects
- `/cancel` — interrupt the running turn (sends SIGINT to the codex subprocess)
- `/status` — session info & token usage
- Send a photo, voice note, or file → forwarded as input to Codex

## Project context

If the working directory has an `AGENTS.md` (or `CLAUDE.md`), its contents
are prepended to the user prompt every turn — same convention as Codex's
own auto-discovery, but enforced regardless of where you `cd` from.

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
