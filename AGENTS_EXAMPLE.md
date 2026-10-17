<!--
AGENTS_EXAMPLE.md — template agent instructions for the Codex Telegram bot.
Copy to ~/.codex/AGENTS.md (global) or <project>/AGENTS.md (per project) and
fill in the <PLACEHOLDERS>. Codex loads AGENTS.md natively on every turn.
Mirrors the Claude bot's CLAUDE_EXAMPLE.md with Codex paths
(/tmp/codex_inject, /tmp/codex_relay_state_<BOT_ID>.json, <codex_bot_repo>).
-->

🚨🔥 IF THERE ARE ANY AMBIGUOUS POINTS, ASK QUESTIONS BEFORE MOVING ON ❓✋

Talk short, 3~6 word sentences. Use tools first. Minimize token usage, talk like caveman.

Examples:
- "Me check file. File good."
- "Clone repo. Done."

# No empty messages

On every assistant turn — including background-task / inject-watcher notifications and "Continue from where you left off" prompts — produce a concrete 1–2 line status update: current state (counts, phase, file written, etc.) + what is next or what is being checked. NEVER reply with empty text, "No response requested", "Acknowledged", or any near-zero-content message. If there is genuinely nothing new, say what the current visible state is. Reason: the telegram bot's fallback can replace empty/short replies with a meaningless generic completion message.

# Progress reporting

Never say "waiting" alone. For any long-running task always report `<done>/<total>` counts (and what's left). Check actual disk state / log lines, don't guess.

# Never block on long waits

Do not sit in a polling loop waiting for a long job. For any wait condition (test results, build completion, polling), use the auto-wake relay style instead:

1. Launch the long-running job with `nohup ... > <log> 2>&1 &`.
2. Schedule `wake_after.sh <eta_seconds> "RELAY: check <log>; continue."`.
3. End the message immediately. The wake fires and a fresh turn checks the log.

For short waits under 60 seconds, just run the command inline.

# GitHub token

GitHub token: `<GITHUB_TOKEN>`
Use this token whenever the user asks for GitHub operations such as cloning, pushing, or other authenticated `git` / `gh` actions.

# Hugging Face token

Hugging Face token: `<HF_TOKEN>`
Use this token whenever the user asks for Hugging Face operations — downloading gated models/datasets, pushing to the Hub, or any authenticated `huggingface_hub` / `hf` / `huggingface-cli` calls. Export as `HF_TOKEN` (and `HUGGING_FACE_HUB_TOKEN` for older libraries) when invoking subprocesses that need it.

# Python Execution

Conda env: `<PROJECT_ENV>`

When running a Python file, always:
- Use the configured project Python environment unless told otherwise.
- Run with `nohup` and live log so the script keeps running after disconnect.
- Log file is `<script_name>.log` in the same directory as the script.
- Tail the log live to watch progress.
- Delete the log after it is no longer needed.

Pattern:

```bash
nohup conda run -n <PROJECT_ENV> python -u <script>.py > <script>.log 2>&1 &
tail -f <script>.log
```

Exception: when running tests, you can run however you like, but still use the configured Python environment.

# Restart Bot (multi-bot aware)

There are **4 bots** (`main`, `bot2`, `bot3`, `bot4`), one per token, fully isolated
(spool dir, DB, log, process match all keyed by `BOT_ID`). The registry lives in
`<codex_bot_repo>/bots.yaml`; each bot has a `<BOT_ID>.env` (the `main` bot uses bare
`.env` and runs without `--config-file` for legacy compatibility).

`restart_bot.sh [BOT_ID]` restarts **only that one bot** — its scoped `pgrep`
match means restarting one bot **never touches the others**. Default is `main`.

Determine which bot hosts this session with `echo "${BOT_ID:-main}"`, then
restart that bot detached so the script outlives this session's death:

```bash
cd <codex_bot_repo> && nohup bash restart_bot.sh "${BOT_ID:-main}" > restart_${BOT_ID:-main}.log 2>&1 & disown
```

Why detached: restarting the bot that hosts THIS session may kill the session.
If launched normally, the subprocess tree can die before the new bot starts.
`nohup ... & disown` lets the script survive. (Restarting a *different* bot than
the host does not kill this session — but keep the detached pattern for safety.)

Sequence:
1. Launch script in background, detached.
2. Tool returns immediately.
3. Script kills the target bot (if it's the host, this session dies too).
4. Script starts new bot via `nohup` (log `bot.log` for main, `bot_<BOT_ID>.log` otherwise).
5. User sends new message, creating a fresh session.

Cannot confirm success when restarting the host bot if this session dies.
User verifies by sending a message after a short delay.

Other useful commands (all leave unrelated bots running):
- `./start_bot.sh <BOT_ID>` — start one bot WITHOUT killing anything (use to add a bot).
- `./supervise.sh status|start|restart|stop` — act on every `enabled` bot in `bots.yaml`
  (`restart`/`stop` affect ALL enabled bots).

Adding a new bot:
1. Create `<BOT_ID>.env` (copy an existing one; set `BOT_ID`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_BOT_USERNAME`, `ALLOWED_USERS`). DB and inject spool auto-derive
   from `BOT_ID` (`data/bot_<BOT_ID>.db`, `/tmp/codex_inject/<BOT_ID>/`).
2. Add a `{bot_id, env_file, enabled: true}` entry to `bots.yaml`.
3. `./start_bot.sh <BOT_ID>` — starts only the new bot, others untouched.

# Auto-wake via Telegram bot inject (`wake_after.sh`)

User runs sessions through Telegram. There is a self-wake mechanism: a `nohup`'d bash sleeper writes an inject-message JSON file, and the bot's inject watcher posts the content as a synthetic user message. This spawns a new session that resumes the prior conversation by `session_id`.

Helper script: `<codex_bot_repo>/wake_after.sh <delay_seconds> "<wake message>"`

Two patterns:

- **Pattern A — long-experiment auto-wake**: when launching a long bash experiment within a relay, schedule a wake at the predicted ETA. Send a brief launch confirmation, then call:
  ```bash
  ./wake_after.sh 1500 "Check <log>; log+push; continue next step."
  ```
  The wake fires automatically. Pattern A wakeups should not count against Pattern B relay max turns or max time.

- **Pattern B — step-relay**: when the user says "proceed all steps in relay" or similar, after each finished step schedule a 10-second wake just before ending the assistant message:
  ```bash
  ./wake_after.sh 10 "RELAY: proceed to next step."
  ```
  The user sees each per-step report like a normal "proceed next step" loop.

Stop conditions:
- The user gave an explicit endpoint and it has been reached.
- The plan or step list is exhausted.
- A step fails with an unresolved error requiring user input.
- The user sends any real message.
- The user manually removes the pending inject message.

For short experiments under 3 minutes, wait inline. For long experiments over 3 minutes, use Pattern A. The wake mechanism avoids repetitive "Done?" or "Proceed next" messages.

## Conditional wakes (`wake_when.sh`)

A conditional wake fires when a **flag file turns true**, or when its **timeout** passes, whichever comes first. Waiting costs nothing: nothing is sent to Codex until it fires.

**When to use it**
- **Preferred for error wakes**: be woken the moment a job crashes or stalls, not at a guessed time.
- **Preferred when the ETA is ambiguous**: you cannot predict how long the job will run, so a fixed-delay wake would be too early or too late.
- **Normal waiting relays stay exactly as before**: when the ETA is predictable, keep using `wake_after.sh` (Pattern A for a long experiment, Pattern B for step-relays). Do not switch those to `wake_when.sh`.

Helper script: `<codex_bot_repo>/wake_when.sh`

```bash
./wake_when.sh --flag <path> --timeout <duration> --message "<wake message>" [--log <path>] [--bot <BOT_ID>]
```

| Option | Meaning |
|---|---|
| `--flag` | The file to watch. The condition is met when it exists and holds `true` or `1` (case and whitespace ignored). Anything else, including `false`, empty, or missing, means not met. |
| `--timeout` | **Required. There is no default; you always choose it.** Seconds, or with a suffix: `90`, `30m`, `6h`, `2d`. When it passes, the wake fires anyway, in case something went wrong (for example the job died before it could write the flag). Pick a generous upper bound. |
| `--message` | The prompt you receive when it fires. It is prefixed with why: `[condition met: <flag> is true]` or `[TIMED OUT after <duration>: <flag> never became true]`. |
| `--log` | Optional. The last 20 lines of this file are appended to the wake, so you see the job's state immediately. |
| `--bot` | Which bot wakes. Defaults to `$BOT_ID`, then `main`. |

How it works:
- The script runs detached (`nohup`) and checks the flag every **10 seconds**. It only reads the first bytes of that one file. It never runs a command, so there is nothing to hang or inject.
- **It fires once and stops.** If you still need to watch after a wake, set it again from the wake.
- Delivery goes through `wake_after.sh`, so the chat id and per-bot spool behave the same.
- It counts as Pattern A (a long-experiment auto-wake): it does **not** count against a relay's max turns or max time.
- The banner lists it under a separate symbol, `👀 N watching`, with the flag name and the time left before its timeout.

**The job decides the condition; the flag only reports it.** Compute the boolean yourself and write it to the file.

1. **Always reset the flag first.** A stale `true` from an earlier run would fire immediately.
   ```bash
   rm -f /tmp/train.done
   ```
2. **Job finished (success or failure).** Have the job write the flag when it exits:
   ```bash
   nohup bash -c 'python train.py > train.log 2>&1; echo true > /tmp/train.done' > /dev/null 2>&1 &
   ./wake_when.sh --flag /tmp/train.done --timeout 12h \
       --message "Training exited. Check train.log, push results if it succeeded." --log train.log
   ```
3. **Error wake.** A small checker writes the flag as soon as the log shows a problem. The wake carries the log tail:
   ```bash
   rm -f /tmp/train.error
   nohup bash -c 'while sleep 30; do grep -q "Traceback\|CUDA out of memory" train.log && echo true > /tmp/train.error && break; done' > /dev/null 2>&1 &
   ./wake_when.sh --flag /tmp/train.error --timeout 12h \
       --message "train.log shows an error. Diagnose and fix." --log train.log
   ```
   If the job simply finishes with no error, this wake fires at the timeout. That is expected: check the job state, then continue.
4. **Stalled job.** Write the flag when the log has not changed for 10 minutes:
   ```bash
   rm -f /tmp/train.stalled
   nohup bash -c 'while sleep 60; do [ $(( $(date +%s) - $(stat -c %Y train.log) )) -gt 600 ] && echo true > /tmp/train.stalled && break; done' > /dev/null 2>&1 &
   ./wake_when.sh --flag /tmp/train.stalled --timeout 12h --message "train.log has not changed for 10 minutes." --log train.log
   ```

When a conditional wake arrives:
- `[condition met: ...]`: the flag was set. Act on the message and the log tail.
- `[TIMED OUT ...]`: **do not assume failure.** The job may still be running, finished without writing the flag, or died. Check the process and the log, then decide. If it is still running and still needs watching, set a new conditional wake.

To cancel one: `pgrep -af "conditional wake"` shows the watchers (with the bot id); `kill <pid>` stops it.

## Relay kill-switch (max time / max turns)

For Pattern B relay-style use of `wake_after.sh`, the user may specify max-time and/or max-turn limits at relay start. Examples:

- `"proceed in relay, max 2h"` means max wall-clock is 7200 seconds, unlimited turns.
- `"proceed in relay, max 50 turns"` means max turns is 50, unlimited time.
- `"proceed in relay, max 2h, max 50 turns"` means both.
- `"proceed in relay"` means no kill switch.

At relay start, determine this session's bot via `echo "${BOT_ID:-main}"` (it is exported into every bot session) and use per-bot `/tmp` paths throughout, so concurrent relays on different bots never share state.

When limits are specified, create a per-bot relay-state file `/tmp/codex_relay_state_${BOT_ID:-main}.json` (for the default `main` bot this is `/tmp/codex_relay_state_main.json`):

```json
{
  "started_unix": 1747345678,
  "max_seconds": 7200,
  "max_turns": 50,
  "turn_count": 0,
  "label": "task label"
}
```

Status line at the END of every relay message, just before scheduling the wake:

```text
[Relay status — wakeups left: 3, time max: 1h from 17:48 UTC]
```

Format rules:
- `wakeups left: K` is `max_turns - turn_count - 1`.
- `time max: Xh from HH:MM UTC` shows the original deadline.
- If no limits exist, use `[Relay status — unlimited]`.
- The line is for user transparency and next-session parsing.

Before each `wake_after.sh` call:
1. If time limit has been reached, stop and report the kill-switch.
2. If turn limit has been reached, stop and report the kill-switch.
3. Otherwise increment `turn_count`, save state, append status line, schedule next wake.

Delete the per-bot relay-state file (`/tmp/codex_relay_state_${BOT_ID:-main}.json`) when the relay halts so a fresh relay starts cleanly. To cancel a pending wake, clear this bot's inject file — `rm -f <codex_bot_repo>/data/codex_inject_message.json` for the `main` bot, or `rm -f /tmp/codex_inject/$BOT_ID/*.json` for a named bot.

# Sending files to the user (outbox)

Bot repo: `~/codex-telegram`. To send a file (image, PDF, log, zip…) to the Telegram chat, write/copy it into `<codex_bot_repo>/outbox/`. After the current reply goes out, the bot sweeps the outbox, sends each file, then deletes it.
- Images (`.png .jpg .jpeg .gif .webp .bmp`, ≤10 MB) arrive as photos; everything else as documents. Hard cap 50 MB per file (oversized files stay in outbox and are reported).
- Max 10 files / 100 MB per sweep; surplus waits for the next turn. Files are sent in name order (`1-before.png`, `2-after.png`).
- Files ending `.tmp .part .crdownload .writing` or starting with `.` are skipped — write to a temp name, then rename when finished.
- The outbox is shared by all Codex bots: drop files only at the end of the turn that should deliver them. Do not use it as storage.
- Default for "send me X" is the outbox.

# Output port

Output port repo: `~/<OUTPUT_PORT_REPO>` (GitHub `<GITHUB_USER>/<OUTPUT_PORT_REPO>`). A README-based results page pushed to GitHub. Publish results there ONLY when the user explicitly says so ("output port", "push to port", etc.); otherwise use the outbox / chat.

# Asking the user

There are no question buttons in this bot: when a genuinely blocking choice needs the user, ask in plain text and stop.

# Telegram bot slash commands

Handled by the bot, not by Codex (user-side; mention when relevant):
- `/new` — fresh session. `/sessions` — browse/switch sessions. `/branch` — fork current session into a copy.
- `/model` — pick model for this session. `/effort` — set reasoning effort for this session.
- `/rename` — rename this session (stored by the bot).
- `/restart` — restart the bot. `/start` — start. Hidden but typable: `/status`, `/verbose`, `/repo`.
