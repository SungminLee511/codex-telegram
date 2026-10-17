"""Tests for /effort: the level list, the store, and clearing back to auto."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bot.handlers import effort_command as ec
from src.bot.utils import efforts
from src.bot.utils.choice_store import ChoiceStore


# --- the fixed list ---------------------------------------------------------


def test_every_level_the_api_takes_is_offered():
    assert [c.alias for c in efforts.CHOICES] == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]


def test_a_level_resolves_to_itself():
    assert efforts.resolve("high").alias == "high"


def test_resolution_ignores_case_and_padding():
    assert efforts.resolve("  XHIGH ").alias == "xhigh"


def test_an_unknown_level_resolves_to_nothing():
    assert efforts.resolve("turbo") is None


def test_a_number_is_not_a_level():
    """The API takes words; a stray '3' should be refused, not passed on."""
    assert efforts.resolve("3") is None


def test_auto_is_recognised_as_clearing_the_choice():
    assert efforts.is_auto("auto")
    assert efforts.is_auto("DEFAULT")
    assert not efforts.is_auto("high")


def test_the_banner_label_is_the_level():
    assert efforts.label_for("xhigh") == "xhigh"


def test_nothing_set_reads_as_auto():
    """An omitted setting is indistinguishable from a broken one."""
    assert efforts.label_for(None) == "auto"


# --- the handler ------------------------------------------------------------


class Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(message_id=1)


def make_context(tmp_path, session_id=None, args=None, codex_reasoning_effort=None):
    return SimpleNamespace(
        args=args or [],
        user_data={"codex_session_id": session_id},
        bot_data={
            "settings": SimpleNamespace(bot_id="main", codex_reasoning_effort=codex_reasoning_effort),
            "_effort_store": ChoiceStore(Path(tmp_path) / "efforts.json"),
        },
    )


def make_update(message):
    return SimpleNamespace(message=message, effective_user=SimpleNamespace(id=7))


@pytest.mark.asyncio
async def test_bare_effort_lists_the_levels(tmp_path):
    msg = Message()

    await ec.effort_command(make_update(msg), make_context(tmp_path))

    for choice in efforts.CHOICES:
        assert choice.alias in msg.replies[0]
    assert "auto" in msg.replies[0]


@pytest.mark.asyncio
async def test_the_listing_marks_auto_when_nothing_is_set(tmp_path):
    msg = Message()

    await ec.effort_command(make_update(msg), make_context(tmp_path))

    marked = [ln for ln in msg.replies[0].splitlines() if ln.startswith("▸")]
    assert len(marked) == 1
    assert "auto" in marked[0]


@pytest.mark.asyncio
async def test_the_listing_marks_the_level_in_force(tmp_path):
    context = make_context(tmp_path, session_id="abc", args=["high"])
    await ec.effort_command(make_update(Message()), context)

    msg = Message()
    context.args = []
    await ec.effort_command(make_update(msg), context)

    marked = [ln for ln in msg.replies[0].splitlines() if ln.startswith("▸")]
    assert len(marked) == 1
    assert "high" in marked[0]


@pytest.mark.asyncio
async def test_choosing_pins_the_current_session(tmp_path):
    context = make_context(tmp_path, session_id="abcdef12-3456", args=["xhigh"])
    msg = Message()

    await ec.effort_command(make_update(msg), context)

    assert context.bot_data["_effort_store"].get("abcdef12-3456") == "xhigh"
    assert "xhigh" in msg.replies[0]


@pytest.mark.asyncio
async def test_another_session_is_unaffected(tmp_path):
    context = make_context(tmp_path, session_id="one", args=["minimal"])
    await ec.effort_command(make_update(Message()), context)

    context.user_data["codex_session_id"] = "two"
    assert ec.current_effort(context) is None


@pytest.mark.asyncio
async def test_choosing_before_a_session_exists_waits_for_one(tmp_path):
    context = make_context(tmp_path, session_id=None, args=["low"])

    await ec.effort_command(make_update(Message()), context)

    assert context.user_data[ec.PENDING_KEY] == "low"
    assert ec.current_effort(context) == "low"


@pytest.mark.asyncio
async def test_the_waiting_choice_is_bound_once_the_session_has_an_id(tmp_path):
    context = make_context(tmp_path, session_id=None, args=["low"])
    await ec.effort_command(make_update(Message()), context)

    ec.bind_pending(context, "fresh-session")

    assert context.bot_data["_effort_store"].get("fresh-session") == "low"
    assert not context.user_data[ec.PENDING_KEY]


@pytest.mark.asyncio
async def test_auto_clears_the_choice(tmp_path):
    context = make_context(tmp_path, session_id="abc", args=["high"])
    await ec.effort_command(make_update(Message()), context)

    context.args = ["auto"]
    msg = Message()
    await ec.effort_command(make_update(msg), context)

    assert ec.current_effort(context) is None
    assert context.bot_data["_effort_store"].get("abc") is None
    assert "auto" in msg.replies[0]


@pytest.mark.asyncio
async def test_auto_also_clears_a_choice_still_waiting(tmp_path):
    context = make_context(tmp_path, session_id=None, args=["high"])
    await ec.effort_command(make_update(Message()), context)

    context.args = ["auto"]
    await ec.effort_command(make_update(Message()), context)

    assert not context.user_data[ec.PENDING_KEY]


@pytest.mark.asyncio
async def test_an_unknown_level_is_refused_with_the_list(tmp_path):
    msg = Message()
    context = make_context(tmp_path, session_id="abc", args=["turbo"])

    await ec.effort_command(make_update(msg), context)

    assert "No effort level called" in msg.replies[0]
    assert "xhigh" in msg.replies[0]
    assert context.bot_data["_effort_store"].get("abc") is None


@pytest.mark.asyncio
async def test_the_configured_default_applies_when_the_session_is_silent(tmp_path):
    context = make_context(tmp_path, session_id="abc", codex_reasoning_effort="medium")

    assert ec.current_effort(context) is None  # nothing chosen for the session
    assert ec.SETTING.effective(context) == "medium"


def test_the_store_is_created_once_per_bot(tmp_path):
    context = make_context(tmp_path)
    del context.bot_data["_effort_store"]

    first = ec.get_store(context)

    assert ec.get_store(context) is first
    assert first.path.name == "session_efforts_main.json"


def test_effort_and_model_do_not_share_a_file(tmp_path):
    """Two settings, two files — one must not overwrite the other."""
    from src.bot.handlers import model_command as mc

    context = make_context(tmp_path)
    del context.bot_data["_effort_store"]
    context.bot_data["settings"] = SimpleNamespace(
        bot_id="main", codex_reasoning_effort=None, codex_model=None
    )

    assert ec.get_store(context).path != mc.get_store(context).path


def test_a_broken_store_does_not_block_the_request(tmp_path):
    context = make_context(tmp_path, session_id="abc")

    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("disk on fire")

    context.bot_data["_effort_store"] = Boom()

    assert ec.current_effort(context) is None
