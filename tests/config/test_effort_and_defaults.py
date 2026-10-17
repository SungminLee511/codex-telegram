"""Settings parity with the Claude bot: effort default, banner flag, 7-day sessions."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config.settings import Settings

_BASE = dict(
    telegram_bot_token="x:y",
    telegram_bot_username="u",
    approved_directory="/tmp",
)


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **over})


def test_sessions_last_a_week_by_default():
    assert _settings().session_timeout_hours == 24 * 7


def test_session_header_is_on_by_default():
    assert _settings().session_header is True


def test_effort_unset_means_model_decides():
    assert _settings().codex_reasoning_effort is None


@pytest.mark.parametrize("value", ["minimal", "low", "medium", "high", "xhigh"])
def test_every_codex_effort_is_accepted(value):
    assert _settings(codex_reasoning_effort=value).codex_reasoning_effort == value


def test_effort_is_normalised():
    assert _settings(codex_reasoning_effort=" HIGH ").codex_reasoning_effort == "high"


def test_blank_effort_is_unset():
    assert _settings(codex_reasoning_effort="").codex_reasoning_effort is None


def test_unknown_effort_is_rejected():
    with pytest.raises(ValidationError):
        _settings(codex_reasoning_effort="max")


@pytest.mark.parametrize("var", ["CODEX_REASONING_EFFORT", "BOT_DEFAULT_EFFORT"])
def test_effort_reads_either_env_name(monkeypatch, var):
    monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("BOT_DEFAULT_EFFORT", raising=False)
    monkeypatch.setenv(var, "low")
    assert Settings(_env_file=None, **_BASE).codex_reasoning_effort == "low"
