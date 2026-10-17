"""Tests for the /sessions picker."""

from pathlib import Path
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.bot.handlers import session_picker as sp
from src.codex.session import CodexSession
from src.codex.session_index import SessionInfo


@pytest.fixture(autouse=True)
def isolated_db_dir(tmp_path, monkeypatch):
    """Never let the owner scan touch the host's real bot DBs."""
    monkeypatch.setattr(sp, "DB_DIR", tmp_path / "data")
    return tmp_path / "data"


def make_info(idx, title=None, turns=3, cwd="/home"):
    return SessionInfo(
        session_id=f"{idx:08d}-1111-2222-3333-444444444444",
        path=f"/tmp/{idx}.jsonl",
        title=title or f"Session {idx}",
        first_prompt=f"prompt {idx}",
        last_prompt=f"last {idx}",
        user_turns=turns,
        cwd=cwd,
        size=1024,
        mtime=1_700_000_000 - idx,
    )


class FakeIndex:
    def __init__(self, sessions):
        self.sessions = sessions

    def refresh(self):
        return self.sessions

    def search(self, query):
        return [s for s in self.sessions if query.lower() in s.display_name.lower()]


class FakeStorage:
    def __init__(self):
        self.saved = []
        self.known = {}

    async def load_session(self, session_id, user_id):
        return self.known.get(session_id)

    async def save_session(self, session):
        self.saved.append(session)
        self.known[session.session_id] = session

    async def get_all_sessions(self):
        return list(self.known.values())


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.edited = None
        self.markup = None
        self.answered = False

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        self.edited = text
        self.markup = reply_markup


def make_context(sessions, current=None):
    storage = FakeStorage()
    manager = SimpleNamespace(storage=storage, active_sessions={})
    return SimpleNamespace(
        args=[],
        user_data={"codex_session_id": current} if current else {},
        bot_data={
            "settings": SimpleNamespace(bot_id="main", approved_directory=Path("/home")),
            "codex_integration": SimpleNamespace(session_manager=manager),
            "_session_index": FakeIndex(sessions),
        },
    )


def make_update(query):
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=777),
    )


# --- rendering ---------------------------------------------------------------


def test_page_lists_at_most_page_size_entries():
    sessions = [make_info(i) for i in range(20)]
    text = sp._page_text(sessions, 0, 2, None, "")

    assert "page 1/2" in text
    assert "Session 0" in text
    assert "Session 9" in text
    assert "Session 10" not in text


def test_second_page_shows_the_next_slice():
    sessions = [make_info(i) for i in range(20)]
    text = sp._page_text(sessions, 1, 2, None, "")

    assert "Session 10" in text
    assert "Session 0\n" not in text


def test_owning_bot_is_shown_per_entry():
    sessions = [make_info(i) for i in range(2)]
    owners = {sessions[0].session_id: "bot2"}
    text = sp._page_text(sessions, 0, 1, None, "", owners)

    assert "· bot2" in text
    assert "· ?" in text  # unknown owner still renders


def test_current_session_is_starred():
    sessions = [make_info(i) for i in range(3)]
    text = sp._page_text(sessions, 0, 1, sessions[1].session_id, "")

    starred = [line for line in text.splitlines() if "⭐" in line]
    assert len(starred) == 1
    assert "Session 1" in starred[0]


def test_filter_is_shown_in_the_header():
    assert "filter:" in sp._page_text([make_info(1)], 0, 1, None, "iasbs")


def test_empty_list_says_so():
    assert "No sessions found" in sp._page_text([], 0, 0, None, "")


def test_html_in_a_title_is_escaped():
    text = sp._page_text([make_info(1, title="<i>sneaky</i>")], 0, 1, None, "")
    assert "&lt;i&gt;" in text


def test_keyboard_numbers_map_to_absolute_positions():
    sessions = [make_info(i) for i in range(20)]
    markup = sp._page_keyboard(sessions, 1, 2)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]

    assert "sess:open:10" in data  # first button on page 2
    assert "sess:open:19" in data
    assert "sess:page:0" in data


def test_first_page_has_no_back_button():
    markup = sp._page_keyboard([make_info(i) for i in range(20)], 0, 2)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "sess:page:-1" not in data
    assert any(d == "sess:page:1" for d in data)


def test_last_page_has_no_forward_button():
    markup = sp._page_keyboard([make_info(i) for i in range(20)], 1, 2)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "sess:page:2" not in data


# --- callbacks ---------------------------------------------------------------


async def test_open_shows_the_detail_card():
    sessions = [make_info(i) for i in range(3)]
    context = make_context(sessions)
    query = FakeQuery("sess:open:1")

    await sp.sessions_callback(make_update(query), context)

    assert "Session 1" in query.edited
    assert sessions[1].session_id in query.edited
    assert "last 1" in query.edited
    data = [b.callback_data for row in query.markup.inline_keyboard for b in row]
    assert "sess:go:1" in data and "sess:prev:1" in data and "sess:page:0" in data


async def test_resume_points_the_chat_at_the_session():
    sessions = [make_info(i) for i in range(3)]
    context = make_context(sessions)
    query = FakeQuery("sess:go:2")

    await sp.sessions_callback(make_update(query), context)

    assert context.user_data["codex_session_id"] == sessions[2].session_id
    assert context.user_data["force_new_session"] is False
    assert context.user_data["current_directory"] == Path("/home")
    assert "Resumed" in query.edited


async def test_resume_writes_a_db_row_so_the_sdk_can_resume():
    """Without a stored row SessionManager would mint a fresh empty session."""
    sessions = [make_info(5)]
    context = make_context(sessions)
    storage = context.bot_data["codex_integration"].session_manager.storage

    await sp.sessions_callback(make_update(FakeQuery("sess:go:0")), context)

    assert len(storage.saved) == 1
    saved = storage.saved[0]
    assert saved.session_id == sessions[0].session_id
    assert saved.user_id == 777
    assert saved.project_path == Path("/home")


async def test_resume_refreshes_an_existing_stale_row():
    """Picking an idle (past-timeout) session must revive it, not drop it."""
    sessions = [make_info(5)]
    context = make_context(sessions)
    manager = context.bot_data["codex_integration"].session_manager
    stale = datetime.now(UTC) - timedelta(days=30)
    row = CodexSession(
        session_id=sessions[0].session_id,
        user_id=777,
        project_path=Path("/home"),
        created_at=stale,
        last_used=stale,
        total_cost=1.5,
    )
    manager.storage.known[row.session_id] = row

    await sp.sessions_callback(make_update(FakeQuery("sess:go:0")), context)

    assert manager.storage.saved == [row]  # same row updated, no new one
    assert not row.is_expired(24)
    assert row.total_cost == 1.5
    assert manager.active_sessions[row.session_id] is row
    assert context.user_data["codex_session_id"] == sessions[0].session_id


async def test_resume_uses_the_session_cwd():
    sessions = [make_info(1, cwd="/home/RESEARCH/iasbs")]
    context = make_context(sessions)

    await sp.sessions_callback(make_update(FakeQuery("sess:go:0")), context)

    assert context.user_data["current_directory"] == Path("/home/RESEARCH/iasbs")


async def test_page_callback_redraws_the_list():
    context = make_context([make_info(i) for i in range(20)])
    context.user_data[sp._ALL_KEY] = True
    query = FakeQuery("sess:page:1")

    await sp.sessions_callback(make_update(query), context)

    assert "page 2/2" in query.edited


async def test_default_listing_is_capped_at_ten():
    context = make_context([make_info(i) for i in range(20)])
    sessions = sp._load_sessions(context, "")

    assert len(sessions) == sp.MAX_SESSIONS == 10
    assert len(sp._load_sessions(context, "", show_all=True)) == 20


async def test_out_of_range_position_is_reported_not_crashed():
    context = make_context([make_info(1)])
    query = FakeQuery("sess:open:99")

    await sp.sessions_callback(make_update(query), context)

    assert "gone" in query.edited.lower()


async def test_malformed_callback_data_is_handled():
    context = make_context([make_info(1)])
    query = FakeQuery("sess:open:notanumber")

    await sp.sessions_callback(make_update(query), context)

    assert "Bad session action" in query.edited


# --- owner lookup -------------------------------------------------------------


def _make_db(path, session_ids):
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("create table sessions (session_id text primary key)")
    conn.executemany("insert into sessions values (?)", [(s,) for s in session_ids])
    conn.commit()
    conn.close()


def test_owner_scan_reads_every_bot_db(isolated_db_dir):
    _make_db(isolated_db_dir / "bot.db", ["aaa"])
    _make_db(isolated_db_dir / "bot_bot2.db", ["bbb"])
    _make_db(isolated_db_dir / "bot_bot3.db", ["ccc"])

    owners = sp._scan_owners()

    assert owners == {"aaa": "main", "bbb": "bot2", "ccc": "bot3"}


def test_owner_scan_survives_a_junk_db(isolated_db_dir):
    _make_db(isolated_db_dir / "bot.db", ["aaa"])
    (isolated_db_dir / "bot_broken.db").write_text("not a database")

    assert sp._scan_owners() == {"aaa": "main"}


def test_owner_scan_on_missing_dir_is_empty(isolated_db_dir):
    assert sp._scan_owners() == {}


def test_owner_map_is_cached_between_calls(isolated_db_dir):
    _make_db(isolated_db_dir / "bot.db", ["aaa"])
    context = make_context([])

    assert sp.session_owners(context) == {"aaa": "main"}

    # A later write is not picked up until the TTL expires.
    _make_db(isolated_db_dir / "bot_bot2.db", ["bbb"])
    assert sp.session_owners(context) == {"aaa": "main"}


async def test_detail_card_names_the_owning_bot(isolated_db_dir):
    sessions = [make_info(1)]
    _make_db(isolated_db_dir / "bot_bot2.db", [sessions[0].session_id])
    context = make_context(sessions)
    query = FakeQuery("sess:open:0")

    await sp.sessions_callback(make_update(query), context)

    assert "bot <code>bot2</code>" in query.edited


async def test_preview_renders_transcript_tail(tmp_path, monkeypatch):
    info = make_info(1)
    context = make_context([info])
    monkeypatch.setattr(
        sp,
        "tail_messages",
        lambda path, limit=8: [
            {"role": "user", "text": "ping", "ts": ""},
            {"role": "assistant", "text": "pong", "ts": ""},
        ],
    )
    query = FakeQuery("sess:prev:0")

    await sp.sessions_callback(make_update(query), context)

    assert "ping" in query.edited and "pong" in query.edited


# --- size and start time, matching the reply banner -------------------------


def _info(tmp_path, lines, turns=12):
    import json as _json

    from src.codex.session_index import SessionInfo

    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return SessionInfo(
        session_id="abcdef12-1111", path=str(path), title="T", user_turns=turns
    )


def _assistant(tokens):
    import json as _json

    return _json.dumps(
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": tokens, "output_tokens": 0}}}}
    )


def _summary():
    return '{"type":"compacted","payload":{"message":"summary"}}'


def test_the_list_shows_tokens_and_compactions(tmp_path):
    from src.bot.handlers.session_picker import _size_text
    from src.bot.utils import compactions, transcript_stats

    compactions.reset_cache()
    transcript_stats.reset_cache()
    info = _info(tmp_path, [_summary(), _summary(), _assistant(442_000)])

    assert _size_text(info) == "442k tok (2)"


def test_a_session_that_never_compacted_shows_only_tokens(tmp_path):
    from src.bot.handlers.session_picker import _size_text
    from src.bot.utils import compactions, transcript_stats

    compactions.reset_cache()
    transcript_stats.reset_cache()

    assert _size_text(_info(tmp_path, [_assistant(9_000)])) == "9k tok"


def test_an_unreadable_transcript_falls_back_to_the_turn_count(tmp_path):
    """An entry with no second line at all would read as broken."""
    from src.bot.handlers.session_picker import _size_text
    from src.bot.utils import compactions, transcript_stats

    compactions.reset_cache()
    transcript_stats.reset_cache()
    info = _info(tmp_path, ['{"type":"user"}'], turns=7)

    assert _size_text(info) == "7 msg"


def test_the_list_shows_when_the_session_started(tmp_path):
    from src.bot.handlers.session_picker import _started_suffix
    from src.bot.utils import session_start

    session_start.reset_cache()
    info = _info(tmp_path, ['{"timestamp":"2026-10-12T05:33:33Z"}', _assistant(1)])

    # 05:33 UTC is 14:33 KST.
    assert "14:33" in _started_suffix(info)


# --- hiding -------------------------------------------------------------------


def _ids(sessions):
    return [s.session_id for s in sessions]


async def test_hidden_session_leaves_list_and_next_one_moves_up():
    infos = [make_info(i) for i in range(12)]
    context = make_context(infos)
    sp.set_hidden(infos[0].session_id, True)

    sessions = sp._load_sessions(context, "")

    assert infos[0].session_id not in _ids(sessions)
    assert len(sessions) == 10
    assert infos[10].session_id in _ids(sessions)
    assert infos[0].session_id not in _ids(sp._load_sessions(context, "", True))


async def test_hidden_view_lists_only_hidden():
    infos = [make_info(i) for i in range(5)]
    context = make_context(infos)
    sp.set_hidden(infos[2].session_id, True)

    assert _ids(sp._load_sessions(context, "", hidden_mode=True)) == [
        infos[2].session_id
    ]


async def test_hide_asks_then_confirm_hides():
    infos = [make_info(i) for i in range(3)]
    context = make_context(infos)
    sp._load_sessions(context, "")

    q1 = FakeQuery("sess:hide:1")
    await sp.sessions_callback(make_update(q1), context)
    assert "Hide" in q1.edited
    assert sp.load_hidden() == set()

    q2 = FakeQuery("sess:hideyes:1")
    await sp.sessions_callback(make_update(q2), context)
    assert sp.load_hidden() == {infos[1].session_id}
    assert "Sessions" in q2.edited


async def test_confirm_without_matching_pending_does_nothing():
    infos = [make_info(i) for i in range(3)]
    context = make_context(infos)
    q = FakeQuery("sess:hideyes:1")

    await sp.sessions_callback(make_update(q), context)

    assert sp.load_hidden() == set()
    assert "changed" in q.edited


async def test_unhide_from_hidden_view():
    infos = [make_info(i) for i in range(3)]
    context = make_context(infos)
    sp.set_hidden(infos[1].session_id, True)
    sp._load_sessions(context, "", hidden_mode=True)

    q = FakeQuery("sess:unhide:0")
    await sp.sessions_callback(make_update(q), context)

    assert sp.load_hidden() == set()
    assert "Hidden sessions" in q.edited


async def test_detail_offers_unhide_in_hidden_view():
    infos = [make_info(i) for i in range(2)]
    context = make_context(infos)
    sp.set_hidden(infos[0].session_id, True)
    sp._load_sessions(context, "", hidden_mode=True)

    q = FakeQuery("sess:open:0")
    await sp.sessions_callback(make_update(q), context)

    labels = [b.text for row in q.markup.inline_keyboard for b in row]
    assert "👀 Unhide" in labels and "🙈 Hide" not in labels


def test_corrupt_hidden_file_reads_as_empty(isolated_db_dir):
    isolated_db_dir.mkdir(parents=True, exist_ok=True)
    (isolated_db_dir / sp.HIDDEN_FILE_NAME).write_text("{not json")
    assert sp.load_hidden() == set()


# --- the list says what the reply banner says --------------------------------


def _assistant_with_model(tokens, model):
    import json as _json

    turn = {"type": "turn_context", "payload": {"cwd": "/home", "model": model}}
    return _json.dumps(turn) + "\n" + _assistant(tokens)


def _fresh(monkeypatch, tmp_path):
    from src.bot.utils import compactions, transcript_stats

    compactions.reset_cache()
    transcript_stats.reset_cache()
    # No saved choices anywhere unless a test writes some.
    monkeypatch.setattr(sp, "DB_DIR", tmp_path / "data")


def test_the_list_line_matches_the_banner_line(tmp_path, monkeypatch):
    """size (compactions) · model · effort · bot — the same four, same order."""
    from src.bot.utils.session_header import detail_line

    _fresh(monkeypatch, tmp_path)
    info = _info(
        tmp_path,
        [_summary(), _assistant_with_model(442_000, "gpt-6-sol")],
    )

    line = sp._detail_bits(info, {info.session_id: "bot2"})

    assert line == detail_line("442k tok (1)", "gpt-6-sol", "auto", "bot2")
    assert line == "442k tok (1) · gpt-6-sol · auto · bot2"


def test_the_model_is_the_one_that_wrote_the_last_reply(tmp_path, monkeypatch):
    _fresh(monkeypatch, tmp_path)
    info = _info(
        tmp_path,
        [
            _assistant_with_model(1_000, "gpt-6-luna"),
            _assistant_with_model(3_000, "gpt-6-astra"),
        ],
    )

    assert "gpt-6-astra" in sp._detail_bits(info, {})


def test_with_no_model_in_the_transcript_the_saved_choice_then_the_default(
    tmp_path, monkeypatch
):
    _fresh(monkeypatch, tmp_path)
    info = _info(tmp_path, [_assistant(5_000)])

    saved = {info.session_id: "gpt-6-luna"}
    assert "gpt-6-luna" in sp._detail_bits(info, {}, {"model": "gpt-6-astra"}, saved)
    assert "gpt-6-astra" in sp._detail_bits(info, {}, {"model": "gpt-6-astra"})


def test_effort_is_the_sessions_saved_choice_else_default_else_auto(
    tmp_path, monkeypatch
):
    _fresh(monkeypatch, tmp_path)
    info = _info(tmp_path, [_assistant(5_000)])

    assert "· high ·" in sp._detail_bits(
        info, {}, None, None, {info.session_id: "high"}
    )
    assert "· xhigh ·" in sp._detail_bits(info, {}, {"effort": "xhigh"})
    assert "· auto ·" in sp._detail_bits(info, {})


def test_saved_choices_are_read_from_every_bots_file(tmp_path, monkeypatch):
    from src.bot.utils.choice_store import ChoiceStore

    _fresh(monkeypatch, tmp_path)
    data = tmp_path / "data"
    ChoiceStore(data / "session_efforts_main.json").set("s1", "low")
    ChoiceStore(data / "session_efforts_bot2.json").set("s2", "xhigh")

    assert sp._saved_choices("effort") == {"s1": "low", "s2": "xhigh"}


def test_the_list_no_longer_carries_file_size_and_age(tmp_path, monkeypatch):
    _fresh(monkeypatch, tmp_path)
    sessions = [make_info(0)]

    text = sp._page_text(sessions, 0, 1, None, "")

    assert "1 KB" not in text and "ago" not in text


def test_the_detail_view_leads_with_the_same_line(tmp_path, monkeypatch):
    _fresh(monkeypatch, tmp_path)
    info = _info(tmp_path, [_assistant_with_model(9_000, "gpt-6-sol")])

    text = sp._detail_text(info, None, "bot bot2", {info.session_id: "bot2"})

    assert "9k tok · gpt-6-sol · auto · bot2" in text
