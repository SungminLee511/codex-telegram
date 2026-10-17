"""Tests for the crash-safe progress-message registry."""

import json

import pytest

from src.bot.utils import progress_registry as pr
from src.bot.utils.progress_registry import ProgressRegistry, sweep_stale


@pytest.fixture()
def registry(tmp_path):
    return ProgressRegistry(tmp_path / "data" / "pending.json")


class FakeBot:
    def __init__(self, fail=False):
        self.edits = []
        self.fail = fail

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("message to edit not found")
        self.edits.append((chat_id, message_id, text))


def test_add_then_drain_returns_the_entry(registry):
    registry.add(10, 99)

    assert registry.drain() == [{"chat_id": 10, "message_id": 99}]


def test_drain_empties_the_file(registry):
    registry.add(10, 99)
    registry.drain()

    assert registry.drain() == []


def test_discard_removes_only_that_message(registry):
    registry.add(10, 1)
    registry.add(10, 2)

    registry.discard(10, 1)

    assert registry.drain() == [{"chat_id": 10, "message_id": 2}]


def test_adding_twice_does_not_duplicate(registry):
    registry.add(10, 1)
    registry.add(10, 1)

    assert len(registry.drain()) == 1


def test_a_clean_run_leaves_nothing_behind(registry):
    """The normal path: open then close -> no stale entry at next startup."""
    registry.add(5, 50)
    registry.discard(5, 50)

    assert registry.drain() == []


def test_missing_file_reads_as_empty(registry):
    assert registry.drain() == []


def test_corrupt_file_is_ignored_not_raised(registry):
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_text("{not json")

    assert registry.drain() == []
    registry.add(1, 2)  # still usable afterwards
    assert registry.drain() == [{"chat_id": 1, "message_id": 2}]


def test_malformed_entries_are_dropped(registry):
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_text(
        json.dumps([{"chat_id": "x", "message_id": 1}, {"chat_id": 7, "message_id": 8}])
    )

    assert registry.drain() == [{"chat_id": 7, "message_id": 8}]


async def test_sweep_edits_every_orphan_and_clears_them(registry):
    registry.add(10, 1)
    registry.add(10, 2)
    bot = FakeBot()

    cleared = await sweep_stale(bot, registry)

    assert cleared == 2
    assert [e[1] for e in bot.edits] == [1, 2]
    assert "Interrupted" in bot.edits[0][2]
    assert registry.drain() == []  # not swept twice on the next restart


async def test_sweep_survives_messages_that_cannot_be_edited(registry):
    """Deleted or too-old messages must not abort the sweep."""
    registry.add(10, 1)
    bot = FakeBot(fail=True)

    assert await sweep_stale(bot, registry) == 0
    assert registry.drain() == []


async def test_sweep_on_a_clean_shutdown_does_nothing(registry):
    bot = FakeBot()

    assert await sweep_stale(bot, registry) == 0
    assert bot.edits == []
