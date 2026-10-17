"""Tests for reading a session's start time out of its transcript."""

import json

import pytest

from src.bot.utils import session_start


@pytest.fixture(autouse=True)
def clean_cache():
    session_start.reset_cache()
    yield
    session_start.reset_cache()


def transcript(tmp_path, *entries, name="s.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return str(path)


def test_the_first_timestamp_is_the_start(tmp_path):
    path = transcript(
        tmp_path,
        {"type": "queue-operation", "timestamp": "2026-10-12T05:33:33.919Z"},
        {"type": "user", "timestamp": "2026-10-12T06:00:00.000Z"},
    )

    assert session_start.started_at(path) == 1791783213.919


def test_a_timezone_offset_is_respected(tmp_path):
    utc = transcript(tmp_path, {"timestamp": "2026-10-12T05:33:33Z"}, name="a.jsonl")
    offset = transcript(
        tmp_path, {"timestamp": "2026-10-12T14:33:33+09:00"}, name="b.jsonl"
    )

    assert session_start.started_at(utc) == session_start.started_at(offset)


def test_entries_without_a_timestamp_are_skipped(tmp_path):
    path = transcript(
        tmp_path,
        {"type": "mode"},
        {"type": "atis-latch"},
        {"type": "user", "timestamp": "2026-10-12T05:33:33Z"},
    )

    assert session_start.started_at(path) is not None


def test_a_broken_line_does_not_stop_the_search(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{not json\n{"timestamp":"2026-10-12T05:33:33Z"}\n')

    assert session_start.started_at(str(path)) is not None


def test_a_transcript_with_no_timestamps_gives_nothing(tmp_path):
    assert session_start.started_at(transcript(tmp_path, {"type": "mode"})) is None


def test_an_unparsable_timestamp_is_ignored(tmp_path):
    path = transcript(tmp_path, {"timestamp": "the other day"})

    assert session_start.started_at(path) is None


def test_a_missing_file_is_not_an_error(tmp_path):
    assert session_start.started_at(str(tmp_path / "gone.jsonl")) is None


def test_no_path_means_no_answer():
    assert session_start.started_at(None) is None
    assert session_start.started_at("") is None


def test_only_the_top_of_a_huge_transcript_is_read(tmp_path):
    """A session's start is at the beginning; reading 30 MB to find it is not ok."""
    path = tmp_path / "s.jsonl"
    filler = [json.dumps({"type": "mode"})] * 500
    filler.append(json.dumps({"timestamp": "2026-10-12T05:33:33Z"}))
    path.write_text("\n".join(filler) + "\n")

    assert session_start.started_at(str(path)) is None


def test_the_answer_is_read_once(tmp_path):
    path = transcript(tmp_path, {"timestamp": "2026-10-12T05:33:33Z"})
    first = session_start.started_at(path)

    (tmp_path / "s.jsonl").unlink()

    assert session_start.started_at(path) == first


def test_a_failure_is_remembered_too(tmp_path):
    """Otherwise every reply pays for a transcript that will never be there."""
    missing = str(tmp_path / "gone.jsonl")
    assert session_start.started_at(missing) is None

    transcript(tmp_path, {"timestamp": "2026-10-12T05:33:33Z"}, name="gone.jsonl")

    assert session_start.started_at(missing) is None
