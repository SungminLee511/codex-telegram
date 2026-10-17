"""Tests for counting how often a session has been summarised."""

import pytest

from src.bot.utils import compactions


@pytest.fixture(autouse=True)
def clean_cache():
    compactions.reset_cache()
    yield
    compactions.reset_cache()


SUMMARY = '{"timestamp":"t","type":"compacted","payload":{"message":"%s"}}\n'
PLAIN = '{"timestamp":"t","type":"response_item","payload":{"type":"message"}}\n'


def transcript(tmp_path, text, name="s.jsonl"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_a_fresh_session_has_no_compactions(tmp_path):
    assert compactions.count(transcript(tmp_path, PLAIN * 5)) == 0


def test_each_summary_is_counted(tmp_path):
    text = PLAIN + SUMMARY % "a" + PLAIN + SUMMARY % "b" + PLAIN
    assert compactions.count(transcript(tmp_path, text)) == 2


def test_spacing_in_the_flag_does_not_matter(tmp_path):
    text = '{"type" : "compacted"}\n'
    assert compactions.count(transcript(tmp_path, text)) == 1


def test_a_context_compacted_event_is_not_counted_twice(tmp_path):
    text = '{"type":"event_msg","payload":{"type":"context_compacted"}}\n'
    assert compactions.count(transcript(tmp_path, text)) == 0


def test_no_path_means_no_answer():
    assert compactions.count(None) is None
    assert compactions.count("") is None


def test_a_missing_file_is_not_an_error(tmp_path):
    assert compactions.count(str(tmp_path / "gone.jsonl")) is None


def test_appended_summaries_are_added_to_the_running_count(tmp_path):
    path = transcript(tmp_path, SUMMARY % "a")
    assert compactions.count(path) == 1

    with open(path, "a") as handle:
        handle.write(PLAIN + SUMMARY % "b")

    assert compactions.count(path) == 2


def test_only_new_bytes_are_read(tmp_path):
    """Transcripts reach tens of megabytes and the banner is drawn constantly."""
    path = transcript(tmp_path, SUMMARY % "a")
    compactions.count(path)

    # Rewriting the already-counted part with nonsense must not change the
    # answer: proof that those bytes are never looked at again.
    size = len(SUMMARY % "a")
    with open(path, "r+") as handle:
        handle.write("Z" * (size - 1) + "\n")

    assert compactions.count(path) == 1


def test_a_half_written_line_is_not_counted_until_it_is_complete(tmp_path):
    """Otherwise it would be counted again once the rest of it arrives."""
    partial = '{"timestamp":"t","type":"compac'
    path = transcript(tmp_path, partial)
    assert compactions.count(path) is None

    with open(path, "a") as handle:
        handle.write('ted","payload":{}}\n')

    assert compactions.count(path) == 1


def test_a_different_file_at_the_same_path_starts_over(tmp_path):
    path = transcript(tmp_path, SUMMARY % "a" + SUMMARY % "b")
    assert compactions.count(path) == 2

    transcript(tmp_path, PLAIN)  # shorter file, same name

    assert compactions.count(path) == 0
