"""The Codex orchestrator exposes the Claude bot's command set and passes
per-session model/effort into every run."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.ext import CommandHandler

from src.bot.orchestrator import MessageOrchestrator
from src.bot.utils.choice_store import ChoiceStore


def settings(tmp_path):
    return SimpleNamespace(
        bot_id="main", inject_dir=str(tmp_path), approved_directory=Path("/tmp"),
        verbose_level=0, enable_stream_drafts=False, session_header=False,
        codex_model=None, codex_reasoning_effort=None,
    )


@pytest.mark.asyncio
async def test_menu_matches_the_claude_bot(tmp_path):
    orch = MessageOrchestrator(settings(tmp_path), {})
    names = [c.command for c in await orch.get_bot_commands()]
    assert names == ["start", "new", "sessions", "rename", "model", "effort",
                     "branch", "restart"]


def test_registered_commands_drop_the_codex_extras(tmp_path):
    orch = MessageOrchestrator(settings(tmp_path), {})
    added = []
    app = SimpleNamespace(add_handler=lambda h, group=0: added.append(h))
    orch.register_handlers(app)
    commands = {c for h in added if isinstance(h, CommandHandler) for c in h.commands}
    assert commands == {"start", "new", "status", "sessions", "verbose", "repo",
                        "model", "effort", "branch", "rename", "restart"}
    for gone in ("cd", "ls", "pwd", "git", "export", "actions", "continue",
                 "end", "projects", "sync_threads"):
        assert gone not in commands


class Chat:
    type = "private"
    id = 1

    async def send_action(self, *_):
        pass


class Msg:
    def __init__(self, text):
        self.text = text
        self.chat = Chat()
        self.message_id = 5
        self.message_thread_id = None
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return SimpleNamespace(chat_id=1, message_id=len(self.replies),
                               delete=_noop, edit_text=_noop)


async def _noop(*a, **k):
    pass


@pytest.mark.asyncio
async def test_text_runs_with_the_sessions_model_and_effort(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.config.settings import Settings

    real = Settings(_env_file=None, telegram_bot_token="x:y",
                    telegram_bot_username="u", approved_directory=str(tmp_path),
                    verbose_level=0, session_header=False)
    orch = MessageOrchestrator(real, {})
    seen = {}

    async def run_command(**kw):
        seen.update(kw)
        return SimpleNamespace(session_id="sid-1", content="hi", interrupted=False,
                               no_text=False, cost=0.0)

    context = SimpleNamespace(
        user_data={"codex_session_id": "sid-1"},
        bot_data={
            "settings": orch.settings,
            "codex_integration": SimpleNamespace(run_command=run_command),
            "_model_store": ChoiceStore(tmp_path / "m.json"),
            "_effort_store": ChoiceStore(tmp_path / "e.json"),
        },
    )
    context.bot_data["_model_store"].set("sid-1", "gpt-6-astra")
    context.bot_data["_effort_store"].set("sid-1", "xhigh")
    monkeypatch.setattr("src.bot.utils.outbox.pending", lambda root=None: [])
    msg = Msg("hello")
    update = SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=7),
                             effective_chat=msg.chat)

    await orch.agentic_text(update, context)

    assert seen["model"] == "gpt-6-astra"
    assert seen["effort"] == "xhigh"
    assert "fork_session" not in seen and "ask_user" not in seen
    assert any("hi" in r for r in msg.replies)
