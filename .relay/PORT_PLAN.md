# Codex parity port — relay plan (state on disk; each step = 1 commit)
Ref repo: ~/Claude-Code-Telegram-bot. Target: ~/codex-telegram. Edit+test only, never run bot/codex.
Decisions: skip ask_user + tool-permission gate; usage meters from ~/.codex/sessions rollout rate_limits;
remove /cd /ls /pwd /git /export /actions /continue /end /projects /sync_threads (mirror Claude menu:
start new sessions branch model effort rename restart; hidden status verbose repo);
/model buttons: gpt-6-astra gpt-6-sol gpt-6-luna gpt-5.6-terra (+typed); push to origin when done.
Tests: conda run -n SML_env python -m pytest tests -q

Steps (mark [x] when committed):
[ ] 1 utils: durations, outbox, uploads, progress_registry, pending_wakes, choice_store, compactions + tests
[ ] 2 settings/storage/repositories diff port (per-session model/effort, hide, 7d timeout)
[ ] 3 codex session_index (rollout jsonl) + transcript_stats + usage_limits(rate_limits) + session_header + tests
[ ] 4 models/efforts utils + /model /effort handlers + cli -m / -c model_reasoning_effort + tests
[ ] 5 session_picker /sessions (hide/unhide), session_choice, session_start, session_rename, /branch (rollout copy) + tests
[ ] 6 orchestrator port: banner, Stop button, crash-safe progress, outbox sweep, empty-reply 5s nudge, remove extra cmds, admin /restart
[ ] 7 wake_when.sh + inject/banner watcher + revert_model.sh + AGENTS_EXAMPLE.md + tests
[ ] 8 README, delete stale UPGRADE_TO_PARITY_PLAN.md, full test run, push
