"""An empty reply from Codex triggers a short wake telling it not to do that."""

from types import SimpleNamespace

from src.bot import orchestrator as orch_mod
from src.bot.orchestrator import MessageOrchestrator


class FakePopen:
    calls = []

    def __init__(self, args, **kwargs):
        FakePopen.calls.append((args, kwargs))


def make(monkeypatch):
    FakePopen.calls = []
    monkeypatch.setattr(orch_mod.subprocess, "Popen", FakePopen)
    orch = MessageOrchestrator.__new__(MessageOrchestrator)
    orch.settings = SimpleNamespace(bot_id="bot2", inject_dir="/tmp/codex_inject")
    context = SimpleNamespace(user_data={})
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=42))
    return orch, update, context


def reply(content, interrupted=False, no_text=False):
    return SimpleNamespace(content=content, interrupted=interrupted, no_text=no_text)


def test_an_empty_reply_schedules_a_five_second_wake(monkeypatch):
    orch, update, context = make(monkeypatch)

    orch._wake_if_empty(update, context, reply(""))

    (args, kwargs), = FakePopen.calls
    assert args[0] == "bash" and args[1].endswith("wake_after.sh")
    assert args[2] == "5"
    assert "DO NOT REPLY WITH NO REPLY" in args[3]
    assert args[4] == "bot2"
    assert kwargs["env"]["WAKE_CHAT_ID"] == "42"


def test_a_whitespace_only_reply_counts_as_empty(monkeypatch):
    orch, update, context = make(monkeypatch)

    orch._wake_if_empty(update, context, reply("  \n "))

    assert len(FakePopen.calls) == 1


def test_a_real_reply_schedules_nothing_and_resets_the_count(monkeypatch):
    orch, update, context = make(monkeypatch)
    context.user_data["empty_reply_retries"] = 2

    orch._wake_if_empty(update, context, reply("here you go"))

    assert FakePopen.calls == []
    assert context.user_data["empty_reply_retries"] == 0


def test_stopping_a_run_does_not_trigger_a_wake(monkeypatch):
    orch, update, context = make(monkeypatch)

    orch._wake_if_empty(update, context, reply("", interrupted=True))

    assert FakePopen.calls == []


def test_it_gives_up_after_the_cap_so_it_cannot_loop_forever(monkeypatch):
    orch, update, context = make(monkeypatch)

    for _ in range(orch_mod.MAX_EMPTY_REPLY_RETRIES + 1):
        orch._wake_if_empty(update, context, reply(""))

    # The last empty reply gets no wake, so the chain ends there...
    assert len(FakePopen.calls) == orch_mod.MAX_EMPTY_REPLY_RETRIES
    # ...and the count resets, so a later message starts fresh.
    assert context.user_data["empty_reply_retries"] == 0


def test_a_failure_to_schedule_never_raises(monkeypatch):
    orch, update, context = make(monkeypatch)

    def boom(*a, **k):
        raise OSError("no bash")

    monkeypatch.setattr(orch_mod.subprocess, "Popen", boom)

    orch._wake_if_empty(update, context, reply(""))  # must not raise


def test_tools_run_but_no_text_written_also_triggers_a_wake(monkeypatch):
    """The stand-in "Task completed. Tools used" text is not a reply."""
    orch, update, context = make(monkeypatch)

    orch._wake_if_empty(
        update,
        context,
        reply("✅ Task completed. Tools used: Bash, Read", no_text=True),
    )

    assert len(FakePopen.calls) == 1


def test_a_real_reply_with_text_is_not_flagged_even_after_tools(monkeypatch):
    orch, update, context = make(monkeypatch)

    orch._wake_if_empty(update, context, reply("Done: fixed it.", no_text=False))

    assert FakePopen.calls == []
