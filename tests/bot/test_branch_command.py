"""/branch forks immediately; /rename names the session bot-side."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bot.handlers import branch_command as bc
from src.bot.handlers import rename_command as rc
from src.bot.handlers import session_picker as sp
from src.bot.utils import session_rename
from src.bot.utils.choice_store import ChoiceStore
from src.codex.session_index import SessionIndex

OLD = "aaaaaaaa-1111-2222-3333-444444444444"


def write_old(root: Path):
    folder = root / "2026" / "10" / "12"
    folder.mkdir(parents=True)
    path = folder / f"rollout-2026-10-12T05-33-33-{OLD}.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": OLD, "cwd": "/work"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "build it"}},
    ]
    path.write_text("".join(json.dumps(l) + "\n" for l in lines))
    return path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "DB_DIR", tmp_path / "data")
    titles = tmp_path / "data" / "session_titles.json"
    monkeypatch.setattr(session_rename, "TITLES_PATH", titles)
    sessions = tmp_path / "sessions"
    write_old(sessions)
    return SimpleNamespace(tmp=tmp_path, sessions=sessions)


class Storage:
    def __init__(self):
        self.known = {}

    async def load_session(self, sid, uid):
        return self.known.get(sid)

    async def save_session(self, s):
        self.known[s.session_id] = s


class Msg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def make(env, session_id=OLD, args=None):
    manager = SimpleNamespace(storage=Storage(), active_sessions={})
    index = SessionIndex(env.tmp / "cache.json", env.sessions,
                         custom_titles=session_rename.get_title)
    context = SimpleNamespace(
        args=args or [],
        user_data={"codex_session_id": session_id},
        bot_data={
            "settings": SimpleNamespace(bot_id="main", approved_directory=Path("/home"),
                                        codex_model=None, codex_reasoning_effort=None),
            "codex_integration": SimpleNamespace(session_manager=manager),
            "_session_index": index,
            "_model_store": ChoiceStore(env.tmp / "m.json"),
            "_effort_store": ChoiceStore(env.tmp / "e.json"),
        },
    )
    msg = Msg()
    update = SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=7))
    return context, update, msg, manager


@pytest.mark.asyncio
async def test_branch_switches_to_a_named_copy(env):
    context, update, msg, manager = make(env, args=["fast", "test"])
    context.bot_data["_model_store"].set(OLD, "gpt-6-luna")

    await bc.branch_command(update, context)

    new_id = context.user_data["codex_session_id"]
    assert new_id != OLD
    assert "Branched" in msg.replies[0]
    assert session_rename.get_title(new_id) == "build it_(branch: fast test)"
    assert context.bot_data["_model_store"].get(new_id) == "gpt-6-luna"
    assert new_id in manager.storage.known
    assert context.user_data["current_directory"] == Path("/work")
    assert len(list(env.sessions.rglob("rollout-*.jsonl"))) == 2


@pytest.mark.asyncio
async def test_branch_without_a_session(env):
    context, update, msg, _ = make(env, session_id=None)
    await bc.branch_command(update, context)
    assert "no session" in msg.replies[0]


@pytest.mark.asyncio
async def test_branch_with_missing_rollout(env):
    context, update, msg, _ = make(env, session_id="dddddddd-1111-2222-3333-444444444444")
    await bc.branch_command(update, context)
    assert "not found" in msg.replies[0]


@pytest.mark.asyncio
async def test_branch_cancel_explains(env):
    context, update, msg, _ = make(env, args=["cancel"])
    await bc.branch_command(update, context)
    assert "nothing to cancel" in msg.replies[0]
    assert context.user_data["codex_session_id"] == OLD


@pytest.mark.asyncio
async def test_rename_sets_and_shows(env):
    context, update, msg, _ = make(env, args=["My", "  work"])
    await rc.rename_command(update, context)
    assert session_rename.get_title(OLD) == "My work"
    assert context.bot_data["_session_index"].get(OLD).display_name == "My work"

    context.args = []
    await rc.rename_command(update, context)
    assert "My work" in msg.replies[-1]


@pytest.mark.asyncio
async def test_rename_without_session(env):
    context, update, msg, _ = make(env, session_id=None)
    await rc.rename_command(update, context)
    assert "No session" in msg.replies[0]


def test_set_title_needs_an_existing_rollout(env):
    assert not session_rename.set_title(str(env.tmp / "nope.jsonl"), OLD, "x")
    assert not session_rename.set_title(None, OLD, "   ")
    assert session_rename.set_title(None, OLD, "ok")


def test_branch_name_stacks():
    assert session_rename.branch_name("a_(branch: b)", "c") == "a_(branch: b)_(branch: c)"
    assert session_rename.branch_name("a", "") == "a_(branch: temp)"
