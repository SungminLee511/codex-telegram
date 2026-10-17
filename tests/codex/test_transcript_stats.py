"""Context size and model read from a rollout's tail."""

import pytest
from conftest import agent, meta, tokens, turn, user, write_rollout

from src.bot.utils import transcript_stats as ts

SID = "aaaaaaaa-1111-2222-3333-444444444444"


@pytest.fixture(autouse=True)
def clean():
    ts.reset_cache()
    yield
    ts.reset_cache()


def test_latest_token_count_wins(sessions):
    path = write_rollout(sessions, SID, [meta(SID), tokens(total=1000), user("x"),
                                         tokens(total=4200), agent("y")])
    assert ts.context_tokens(str(path)) == 4200


def test_input_plus_output_without_total(sessions):
    path = write_rollout(sessions, SID, [meta(SID), tokens(inp=300, out=20)])
    assert ts.context_tokens(str(path)) == 320


def test_no_token_events_is_unknown(sessions):
    path = write_rollout(sessions, SID, [meta(SID), user("x")])
    assert ts.context_tokens(str(path)) is None


def test_answer_refreshes_when_the_file_grows(sessions):
    path = write_rollout(sessions, SID, [meta(SID), tokens(total=10)])
    assert ts.context_tokens(str(path)) == 10
    write_rollout(sessions, SID, [tokens(total=99)])
    assert ts.context_tokens(str(path)) == 99


def test_last_model_comes_from_turn_context(sessions):
    path = write_rollout(sessions, SID, [meta(SID), turn("gpt-6-luna"), turn("gpt-6-astra")])
    assert ts.last_model(str(path)) == "gpt-6-astra"


def test_missing_or_empty_paths(tmp_path):
    assert ts.context_tokens(None) is None
    assert ts.context_tokens(str(tmp_path / "gone")) is None
    assert ts.last_model("") is None
