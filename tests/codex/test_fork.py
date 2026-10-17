"""Forking a session = copying its rollout under a new thread id."""

import json
from datetime import datetime, timezone

import pytest
from conftest import meta, user, write_rollout

from src.codex.fork import ForkError, fork_rollout
from src.codex.session_index import SessionIndex, session_id_from_path

OLD = "aaaaaaaa-1111-2222-3333-444444444444"
NEW = "cccccccc-1111-2222-3333-444444444444"
NOW = datetime(2026, 10, 17, 9, 30, tzinfo=timezone.utc)


def test_copy_gets_new_id_in_name_and_meta(sessions):
    src = write_rollout(sessions, OLD, [meta(OLD), user("hello"), user("again")])
    new_id, path = fork_rollout(src, sessions, OLD, now=NOW, new_id=NEW)

    assert new_id == NEW
    assert path.parent == sessions / "2026" / "10" / "17"
    assert session_id_from_path(path) == NEW
    first = json.loads(path.read_text().splitlines()[0])
    assert first["payload"]["id"] == NEW
    assert first["payload"]["forked_from_id"] == OLD
    # Everything after the meta line is byte-identical.
    assert path.read_text().splitlines()[1:] == src.read_text().splitlines()[1:]


def test_original_is_untouched(sessions):
    src = write_rollout(sessions, OLD, [meta(OLD), user("hello")])
    before = src.read_bytes()
    fork_rollout(src, sessions, OLD, now=NOW)
    assert src.read_bytes() == before


def test_index_sees_both_with_shared_history(sessions, tmp_path):
    src = write_rollout(sessions, OLD, [meta(OLD), user("hello")])
    fork_rollout(src, sessions, OLD, now=NOW, new_id=NEW)
    found = {s.session_id: s for s in SessionIndex(tmp_path / "c.json", sessions).refresh()}
    assert set(found) == {OLD, NEW}
    assert found[NEW].first_prompt == "hello"


def test_random_id_when_not_given(sessions):
    src = write_rollout(sessions, OLD, [meta(OLD)])
    new_id, _ = fork_rollout(src, sessions, OLD)
    assert new_id != OLD and len(new_id) == 36


def test_missing_source_or_meta_fails(sessions):
    with pytest.raises(ForkError):
        fork_rollout(sessions / "nope.jsonl", sessions, OLD)
    src = write_rollout(sessions, OLD, [user("no meta")])
    with pytest.raises(ForkError):
        fork_rollout(src, sessions, OLD)
