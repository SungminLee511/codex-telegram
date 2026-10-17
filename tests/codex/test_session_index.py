"""Tests for the on-disk Codex rollout index."""

import os

from conftest import agent, injected, meta, rollout_path, turn, user, write_rollout

from src.codex.session_index import SessionIndex, session_id_from_path, tail_messages

SID = "aaaaaaaa-1111-2222-3333-444444444444"
SID2 = "bbbbbbbb-1111-2222-3333-444444444444"


def index(sessions, tmp_path, titles=None):
    return SessionIndex(tmp_path / "cache.json", sessions_dir=sessions, custom_titles=titles)


def test_id_comes_from_the_file_name(sessions):
    assert session_id_from_path(rollout_path(sessions, SID)) == SID
    assert session_id_from_path(sessions / "notes.jsonl") is None


def test_scan_extracts_prompts_turns_cwd_and_model(sessions, tmp_path):
    write_rollout(sessions, SID, [meta(SID, cwd="/work"), turn("gpt-6-astra"),
                                  user("first question"), agent("an answer"),
                                  user("second question")])
    [info] = index(sessions, tmp_path).refresh()
    assert info.session_id == SID
    assert info.cwd == "/work"
    assert info.model == "gpt-6-astra"
    assert info.user_turns == 2
    assert info.first_prompt == "first question"
    assert info.last_prompt == "second question"
    assert info.display_name == "first question"


def test_injected_context_is_not_a_user_turn(sessions, tmp_path):
    write_rollout(sessions, SID, [meta(SID), injected("<user_instructions>AGENTS</user_instructions>"),
                                  user("real one")])
    [info] = index(sessions, tmp_path).refresh()
    assert info.user_turns == 1
    assert info.first_prompt == "real one"


def test_custom_title_wins(sessions, tmp_path):
    write_rollout(sessions, SID, [meta(SID), user("hello")])
    [info] = index(sessions, tmp_path, titles={SID: "Named"}.get).refresh()
    assert info.display_name == "Named"


def test_untitled_falls_back_to_short_id(sessions, tmp_path):
    write_rollout(sessions, SID, [meta(SID)])
    [info] = index(sessions, tmp_path).refresh()
    assert info.display_name == f"(untitled {SID[:8]})"


def test_rescan_is_incremental(sessions, tmp_path):
    idx = index(sessions, tmp_path)
    write_rollout(sessions, SID, [meta(SID), user("one")])
    idx.refresh()
    write_rollout(sessions, SID, [user("two")])
    [info] = idx.refresh()
    assert info.user_turns == 2
    assert info.last_prompt == "two"


def test_partial_line_waits_for_the_rest(sessions, tmp_path):
    path = write_rollout(sessions, SID, [meta(SID)])
    with open(path, "a") as handle:
        handle.write('{"type":"event_msg","payload":{"type":"user_message","mess')
    idx = index(sessions, tmp_path)
    assert idx.refresh()[0].user_turns == 0
    with open(path, "a") as handle:
        handle.write('age":"late"}}\n')
    assert idx.refresh()[0].user_turns == 1


def test_cache_survives_a_new_index(sessions, tmp_path):
    write_rollout(sessions, SID, [meta(SID), user("hi")])
    index(sessions, tmp_path).refresh()
    again = index(sessions, tmp_path)
    assert again.get(SID, refresh=False).first_prompt == "hi"


def test_newest_first_and_deleted_ones_forgotten(sessions, tmp_path):
    old = write_rollout(sessions, SID, [meta(SID), user("old")], day="2026/10/01")
    write_rollout(sessions, SID2, [meta(SID2), user("new")])
    os.utime(old, (1, 1))
    idx = index(sessions, tmp_path)
    assert [s.session_id for s in idx.refresh()] == [SID2, SID]
    old.unlink()
    assert [s.session_id for s in idx.refresh()] == [SID2]


def test_get_finds_an_unindexed_rollout(sessions, tmp_path):
    write_rollout(sessions, SID, [meta(SID), user("hello")])
    info = index(sessions, tmp_path).get(SID)
    assert info is not None and info.first_prompt == "hello"
    assert index(sessions, tmp_path).get(SID2) is None


def test_search_matches_prompts_names_and_ids(sessions, tmp_path):
    write_rollout(sessions, SID, [meta(SID), user("fix the parser")])
    write_rollout(sessions, SID2, [meta(SID2), user("other")])
    idx = index(sessions, tmp_path, titles={SID2: "Release prep"}.get)
    assert [s.session_id for s in idx.search("PARSER")] == [SID]
    assert [s.session_id for s in idx.search("release")] == [SID2]
    assert [s.session_id for s in idx.search("bbbbbbbb")] == [SID2]


def test_missing_dir_is_empty(tmp_path):
    assert SessionIndex(tmp_path / "c.json", sessions_dir=tmp_path / "nope").refresh() == []


def test_tail_messages_reads_both_sides(sessions):
    path = write_rollout(sessions, SID, [meta(SID), user("q1"), agent("a1"), user("q2")])
    assert tail_messages(path) == [
        {"role": "user", "text": "q1", "ts": "2026-10-12T05:34:00Z"},
        {"role": "assistant", "text": "a1", "ts": "2026-10-12T05:34:05Z"},
        {"role": "user", "text": "q2", "ts": "2026-10-12T05:34:00Z"},
    ]
    assert len(tail_messages(path, limit=1)) == 1
