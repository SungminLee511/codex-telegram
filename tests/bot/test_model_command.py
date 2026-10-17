"""Tests for /model: the choice list, the store, and the per-session binding."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bot.handlers import model_command as mc
from src.bot.utils import models
from src.bot.utils.choice_store import ChoiceStore


# --- the list ---------------------------------------------------------------


def test_the_buttons_are_the_four_models():
    assert [c.alias for c in models.CHOICES] == [
        "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-terra"]


def test_an_alias_resolves_case_and_space_insensitively():
    assert models.resolve("  GPT-6-SOL  ").alias == "gpt-6-sol"


def test_a_pinned_id_is_not_a_button():
    assert models.resolve("gpt-6-sol-2026-09-22") is None


def test_wellformed_names():
    assert models.is_wellformed("gpt-5.6-terra")
    assert models.is_wellformed("org/model:tag")
    assert not models.is_wellformed("rm -rf /")
    assert not models.is_wellformed("")
    assert not models.is_wellformed("-flag")


def test_label_drops_snapshot_date():
    assert models.label_for("gpt-6-sol") == "gpt-6-sol"
    assert models.label_for("gpt-6-sol-2026-09-22") == "gpt-6-sol"
    assert models.label_for("gpt-6-luna-20260922") == "gpt-6-luna"
    assert models.label_for(None) is None


# --- the store --------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    return ChoiceStore(tmp_path / "models.json")


def test_an_unset_session_has_no_model(store):
    assert store.get("abc") is None


def test_a_choice_survives_a_new_store_on_the_same_file(store):
    """The whole reason it is on disk: restarts."""
    store.set("abc", "gpt-6-luna")

    assert ChoiceStore(store.path).get("abc") == "gpt-6-luna"


def test_sessions_do_not_share_a_choice(store):
    store.set("abc", "gpt-6-luna")
    store.set("def", "gpt-5.6-terra")

    assert store.get("abc") == "gpt-6-luna"
    assert store.get("def") == "gpt-5.6-terra"


def test_setting_again_replaces(store):
    store.set("abc", "gpt-6-luna")
    store.set("abc", "gpt-5.6-terra")

    assert store.get("abc") == "gpt-5.6-terra"


def test_clearing_returns_a_session_to_the_default(store):
    store.set("abc", "gpt-6-luna")
    store.clear("abc")

    assert store.get("abc") is None


def test_an_empty_session_id_is_not_stored(store):
    store.set("", "gpt-6-luna")

    assert store.get("") is None


def test_the_file_is_trimmed_but_keeps_the_recent_ones(store, monkeypatch):
    monkeypatch.setattr("src.bot.utils.choice_store._MAX_ENTRIES", 3)
    for i in range(5):
        store.set(f"s{i}", "gpt-5.6-terra")

    assert store.get("s0") is None
    assert store.get("s4") == "gpt-5.6-terra"


def test_a_corrupt_file_reads_as_empty_rather_than_raising(store):
    store.path.write_text("{not json")

    assert store.get("abc") is None


def test_touching_a_session_keeps_it_from_being_trimmed(store, monkeypatch):
    monkeypatch.setattr("src.bot.utils.choice_store._MAX_ENTRIES", 2)
    store.set("old", "gpt-5.6-terra")
    store.set("other", "gpt-5.6-terra")
    store.set("old", "gpt-6-luna")  # used again -> now the newest
    store.set("new", "gpt-5.6-terra")

    assert store.get("old") == "gpt-6-luna"
    assert store.get("other") is None


# --- the handler ------------------------------------------------------------


class Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(message_id=1)


def make_context(tmp_path, session_id=None, args=None, codex_model="gpt-6-astra"):
    context = SimpleNamespace(
        args=args or [],
        user_data={"codex_session_id": session_id},
        bot_data={
            "settings": SimpleNamespace(bot_id="main", codex_model=codex_model),
            "_model_store": ChoiceStore(Path(tmp_path) / "models.json"),
        },
    )
    return context


def make_update(message):
    return SimpleNamespace(message=message, effective_user=SimpleNamespace(id=7))


@pytest.mark.asyncio
async def test_bare_model_lists_the_choices(tmp_path):
    msg = Message()
    await mc.model_command(make_update(msg), make_context(tmp_path))

    (text,) = msg.replies
    for choice in models.CHOICES:
        assert choice.alias in text


@pytest.mark.asyncio
async def test_the_listing_marks_the_model_in_force(tmp_path):
    msg = Message()
    context = make_context(tmp_path, codex_model="gpt-6-luna")
    await mc.model_command(make_update(msg), context)

    marked = [ln for ln in msg.replies[0].splitlines() if ln.startswith("▸")]
    assert len(marked) == 1
    assert "gpt-6-luna" in marked[0]


@pytest.mark.asyncio
async def test_choosing_pins_the_current_session(tmp_path):
    context = make_context(tmp_path, session_id="abcdef12-3456", args=["gpt-6-luna"])
    msg = Message()

    await mc.model_command(make_update(msg), context)

    assert context.bot_data["_model_store"].get("abcdef12-3456") == "gpt-6-luna"
    assert "gpt-6-luna" in msg.replies[0]


@pytest.mark.asyncio
async def test_another_session_is_unaffected(tmp_path):
    context = make_context(tmp_path, session_id="one", args=["gpt-5.6-terra"])
    await mc.model_command(make_update(Message()), context)

    context.user_data["codex_session_id"] = "two"
    assert mc.current_model_id(context) is None


@pytest.mark.asyncio
async def test_choosing_before_a_session_exists_waits_for_one(tmp_path):
    context = make_context(tmp_path, session_id=None, args=["gpt-5.6-terra"])

    await mc.model_command(make_update(Message()), context)

    assert context.user_data[mc.PENDING_KEY] == "gpt-5.6-terra"
    assert mc.current_model_id(context) == "gpt-5.6-terra"


@pytest.mark.asyncio
async def test_the_waiting_choice_is_bound_once_the_session_has_an_id(tmp_path):
    context = make_context(tmp_path, session_id=None, args=["gpt-5.6-terra"])
    await mc.model_command(make_update(Message()), context)

    mc.bind_pending(context, "fresh-session")

    assert context.bot_data["_model_store"].get("fresh-session") == "gpt-5.6-terra"
    assert not context.user_data[mc.PENDING_KEY]


@pytest.mark.asyncio
async def test_binding_without_a_waiting_choice_does_nothing(tmp_path):
    context = make_context(tmp_path, session_id="abc")

    mc.bind_pending(context, "abc")

    assert context.bot_data["_model_store"].get("abc") is None


@pytest.mark.asyncio
async def test_a_malformed_name_is_refused(tmp_path):
    msg = Message()
    context = make_context(tmp_path, session_id="abc", args=["rm;-rf"])

    await mc.model_command(make_update(msg), context)

    assert "not a model name" in msg.replies[0]
    assert context.bot_data["_model_store"].get("abc") is None


@pytest.mark.asyncio
async def test_a_typed_name_is_pinned_with_an_unverified_warning(tmp_path):
    msg = Message()
    context = make_context(tmp_path, session_id="abc", args=["o4-mini"])

    await mc.model_command(make_update(msg), context)

    assert context.bot_data["_model_store"].get("abc") == "o4-mini"
    assert "unverified" in msg.replies[-1]


@pytest.mark.asyncio
async def test_a_button_model_has_no_warning(tmp_path):
    msg = Message()
    context = make_context(tmp_path, session_id="abc", args=["gpt-6-astra"])

    await mc.model_command(make_update(msg), context)

    assert context.bot_data["_model_store"].get("abc") == "gpt-6-astra"
    assert "unverified" not in msg.replies[-1]


@pytest.mark.asyncio
async def test_switching_clears_a_stale_waiting_choice(tmp_path):
    context = make_context(tmp_path, session_id=None, args=["gpt-5.6-terra"])
    await mc.model_command(make_update(Message()), context)

    context.user_data["codex_session_id"] = "now-exists"
    context.args = ["gpt-6-luna"]
    await mc.model_command(make_update(Message()), context)

    assert not context.user_data[mc.PENDING_KEY]
    assert mc.current_model_id(context) == "gpt-6-luna"


def test_the_store_is_created_once_per_bot(tmp_path):
    context = make_context(tmp_path)
    del context.bot_data["_model_store"]

    first = mc.get_store(context)

    assert mc.get_store(context) is first
    assert first.path.name == "session_models_main.json"


def test_a_broken_store_does_not_block_the_request(tmp_path):
    context = make_context(tmp_path, session_id="abc")

    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("disk on fire")

    context.bot_data["_model_store"] = Boom()

    assert mc.current_model_id(context) is None
