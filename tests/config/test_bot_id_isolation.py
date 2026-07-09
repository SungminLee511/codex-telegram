"""Multi-bot config-derivation tests (BOT_ID namespacing)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config.settings import Settings

_BASE = dict(
    telegram_bot_token="x:y",
    telegram_bot_username="u",
    approved_directory="/tmp",
)


def _settings(**over) -> Settings:
    return Settings(**{**_BASE, **over})


def test_main_keeps_legacy_paths():
    s = _settings(bot_id="main")
    assert s.database_url == "sqlite:///data/bot.db"
    assert s.inject_spool_dir == Path("/tmp/codex_inject/main")
    assert s.relay_state_path == Path("/tmp/codex_relay_state_main.json")


def test_named_bot_derives_isolated_paths():
    s = _settings(bot_id="bot2")
    assert s.database_url == "sqlite:///data/bot_bot2.db"
    assert s.inject_spool_dir == Path("/tmp/codex_inject/bot2")
    assert s.relay_state_path == Path("/tmp/codex_relay_state_bot2.json")


def test_explicit_database_url_is_respected():
    s = _settings(bot_id="bot2", database_url="sqlite:///data/custom.db")
    assert s.database_url == "sqlite:///data/custom.db"


def test_inject_dir_override():
    s = _settings(bot_id="bot2", inject_dir="/var/codex")
    assert s.inject_spool_dir == Path("/var/codex/bot2")
