"""Stop button on file, photo and voice requests."""

import asyncio
from types import SimpleNamespace

import pytest

from src.bot.orchestrator import ActiveRequest, MessageOrchestrator


def make_orch():
    orch = MessageOrchestrator.__new__(MessageOrchestrator)
    orch._active_requests = {}
    orch.settings = SimpleNamespace(approved_directory="/tmp", bot_id="main",
                                    inject_dir="/tmp/x")
    return orch


def test_the_stop_keyboard_targets_the_user():
    kb = MessageOrchestrator._stop_keyboard(7)

    (button,) = kb.inline_keyboard[0]
    assert button.text == "Stop"
    assert button.callback_data == "stop:7"


class Progress:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


@pytest.mark.asyncio
async def test_a_stop_before_the_run_starts_ends_it_and_clears_the_request():
    orch = make_orch()
    progress = Progress()
    event = asyncio.Event()
    event.set()
    orch._active_requests[7] = ActiveRequest(user_id=7, interrupt_event=event)
    failed = []

    async def fake_fail(msg, text, **kwargs):
        failed.append(text)

    orch._fail_progress = fake_fail
    context = SimpleNamespace(
        bot_data={"codex_integration": object()}, user_data={}
    )

    await orch._handle_agentic_media_message(
        update=SimpleNamespace(),
        context=context,
        prompt="x",
        progress_msg=progress,
        user_id=7,
        chat=SimpleNamespace(),
        interrupt_event=event,
    )

    assert failed == ["Stopped."]
    assert 7 not in orch._active_requests


@pytest.mark.asyncio
async def test_a_missing_integration_clears_the_request_too():
    orch = make_orch()
    orch._active_requests[7] = ActiveRequest(user_id=7)

    async def fake_fail(msg, text, **kwargs):
        pass

    orch._fail_progress = fake_fail
    context = SimpleNamespace(bot_data={}, user_data={})

    await orch._handle_agentic_media_message(
        update=SimpleNamespace(),
        context=context,
        prompt="x",
        progress_msg=Progress(),
        user_id=7,
        chat=SimpleNamespace(),
    )

    assert 7 not in orch._active_requests
