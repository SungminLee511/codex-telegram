"""Tests for resume-after-subprocess-kill recovery in CodexIntegration.

Regression coverage for the context-loss bug: when the `codex`
process is killed by a signal (exit 143/137) mid-turn, the facade must
re-resume the SAME session id (transcript on disk is intact) instead of
discarding the session and starting a fresh, empty one.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.codex import facade as facade_mod
from src.codex.exceptions import CodexProcessError
from src.codex.facade import CodexIntegration, _is_transient_subprocess_kill
from src.codex.cli_integration import CodexResponse


def _resp(session_id="sess-1", content="ok"):
    return CodexResponse(
        content=content,
        session_id=session_id,
        cost=0.0,
        duration_ms=1,
        num_turns=1,
    )


def _make_integration(session_id="sess-1"):
    """Build a CodexIntegration with a fake session manager.

    get_or_create_session returns an existing (resumable) session first, and a
    fresh one (different id, marked new) on the fallback path.
    """
    existing = SimpleNamespace(session_id=session_id, is_new_session=False)
    fresh = SimpleNamespace(session_id="sess-fresh", is_new_session=True)

    sm = SimpleNamespace()
    sm._calls = {"get_or_create": 0}

    async def get_or_create_session(user_id, working_directory, sid=None):
        sm._calls["get_or_create"] += 1
        # First call returns the existing session; later calls (fallback) fresh.
        return existing if sm._calls["get_or_create"] == 1 else fresh

    sm.get_or_create_session = get_or_create_session
    sm.remove_session = AsyncMock()
    sm.update_session = AsyncMock()

    config = SimpleNamespace()
    sdk = SimpleNamespace()
    integ = CodexIntegration(config=config, sdk_manager=sdk, session_manager=sm)
    return integ, sm


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Make retry backoff instantaneous so tests stay fast."""
    monkeypatch.setattr(facade_mod, "_RESUME_AFTER_KILL_BASE_DELAY", 0.0)
    monkeypatch.setattr(facade_mod, "_RESUME_AFTER_KILL_MAX_DELAY", 0.0)

    async def _instant(_):
        return None

    monkeypatch.setattr(facade_mod.asyncio, "sleep", _instant)


# --------------------------------------------------------------------------- #
# classifier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "msg, expected",
    [
        ("Unexpected error: Command failed with exit code 143", True),
        ("Codex process error: Command failed with exit code 137", True),
        ("codex exec exited with code -15", True),
        ("codex exec exited with code -9", True),
        ("codex exec exited with code -90", False),
        ("Command failed with exit code 1", False),  # session gone
        ("No conversation found with session ID abc", False),
        ("Failed to connect to Codex: timed out", False),
        ("exit code 1430", False),  # word-boundary guard
    ],
)
def test_classifier(msg, expected):
    assert _is_transient_subprocess_kill(CodexProcessError(msg)) is expected


# --------------------------------------------------------------------------- #
# transient kill -> re-resume same id, recover with context
# --------------------------------------------------------------------------- #
async def test_kill_then_recover_keeps_session():
    integ, sm = _make_integration()
    calls = []

    async def fake_execute(*, session_id, continue_session, **kw):
        calls.append((session_id, continue_session))
        if len(calls) == 1:
            raise CodexProcessError("Unexpected error: Command failed with exit code 143")
        return _resp(session_id="sess-1")

    integ._execute = fake_execute

    resp = await integ.run_command(
        prompt="hi", working_directory="/tmp", user_id=1, session_id="sess-1"
    )

    assert resp.session_id == "sess-1"
    # second call re-resumed the SAME id with continue_session=True
    assert calls[0] == ("sess-1", True)
    assert calls[1] == ("sess-1", True)
    # the session was NOT discarded
    sm.remove_session.assert_not_awaited()


# --------------------------------------------------------------------------- #
# genuine "session gone" -> fresh fallback (legacy behavior preserved)
# --------------------------------------------------------------------------- #
async def test_session_gone_falls_back_to_fresh():
    integ, sm = _make_integration()
    calls = []

    async def fake_execute(*, session_id, continue_session, **kw):
        calls.append((session_id, continue_session))
        if len(calls) == 1:
            raise CodexProcessError("Command failed with exit code 1")
        return _resp(session_id="sess-fresh")

    integ._execute = fake_execute

    resp = await integ.run_command(
        prompt="hi", working_directory="/tmp", user_id=1, session_id="sess-1"
    )

    assert resp.session_id == "sess-fresh"
    # stale session removed, fresh session started (session_id=None)
    sm.remove_session.assert_awaited_once()
    assert calls[0] == ("sess-1", True)
    assert calls[1] == (None, False)


# --------------------------------------------------------------------------- #
# persistent kill -> retries exhausted -> fresh fallback (never worse than today)
# --------------------------------------------------------------------------- #
async def test_persistent_kill_exhausts_then_fresh():
    integ, sm = _make_integration()
    calls = []

    async def fake_execute(*, session_id, continue_session, **kw):
        calls.append((session_id, continue_session))
        if continue_session:  # every resume attempt gets killed
            raise CodexProcessError("Unexpected error: Command failed with exit code 143")
        return _resp(session_id="sess-fresh")  # fresh session works

    integ._execute = fake_execute

    resp = await integ.run_command(
        prompt="hi", working_directory="/tmp", user_id=1, session_id="sess-1"
    )

    assert resp.session_id == "sess-fresh"
    # 1 initial + MAX_ATTEMPTS resume retries, all killed, then 1 fresh
    resume_calls = [c for c in calls if c[1] is True]
    assert len(resume_calls) == 1 + facade_mod._RESUME_AFTER_KILL_MAX_ATTEMPTS
    assert calls[-1] == (None, False)
    sm.remove_session.assert_awaited_once()


# --------------------------------------------------------------------------- #
# hard timeout during recovery is NOT swallowed (session preserved)
# --------------------------------------------------------------------------- #
async def test_timeout_during_recovery_propagates():
    integ, sm = _make_integration()
    calls = []

    async def fake_execute(*, session_id, continue_session, **kw):
        calls.append((session_id, continue_session))
        if len(calls) == 1:
            raise CodexProcessError("Unexpected error: Command failed with exit code 143")
        raise asyncio.TimeoutError()

    integ._execute = fake_execute

    with pytest.raises(asyncio.TimeoutError):
        await integ.run_command(
            prompt="hi", working_directory="/tmp", user_id=1, session_id="sess-1"
        )

    # never fell back to a fresh session
    sm.remove_session.assert_not_awaited()
