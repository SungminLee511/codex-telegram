"""Tests for the display-only session banner."""

from types import SimpleNamespace

import pytest

from src.bot.utils.formatting import FormattedMessage
from src.bot.utils.pending_wakes import PendingWake
from src.bot.utils.session_header import attach_header, build_header
from src.codex.session_index import SessionInfo


@pytest.fixture(autouse=True)
def no_wakes(monkeypatch):
    """The host may really have wakes queued; don't let that leak into tests."""
    import src.bot.utils.pending_wakes as pw

    monkeypatch.setattr(pw, "scan_quietly", lambda bot_id=None: [])


@pytest.fixture(autouse=True)
def no_usage(monkeypatch):
    """Keep the banner off the network; usage has its own tests."""
    import src.bot.utils.usage_limits as ul

    monkeypatch.setattr(ul, "line", lambda path=None: None)


@pytest.fixture()
def usage(monkeypatch):
    """Install a fixed usage line for the banner to find."""
    import src.bot.utils.usage_limits as ul

    def install(text):
        monkeypatch.setattr(ul, "line", lambda path=None: text)

    return install


@pytest.fixture()
def wakes(monkeypatch):
    """Install a fixed set of pending wakes for the banner to find."""
    import src.bot.utils.pending_wakes as pw

    def install(*entries):
        monkeypatch.setattr(pw, "scan_quietly", lambda bot_id=None: list(entries))

    return install


class _MemoryStore:
    """Stands in for the on-disk token store."""

    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class FakeIndex:
    def __init__(self, info=None):
        self._info = info

    def get(self, session_id, refresh=True):
        return self._info


def make_context(
    session_id="abcdef12-3456-7890-aaaa-bbbbccccdddd",
    title="A tidy session",
    message_count=27,
    total_cost=1.42,
    bot_id="main",
    header_enabled=True,
    cwd="/home",
    codex_model=None,
):
    settings = SimpleNamespace(
        bot_id=bot_id,
        approved_directory="/home",
        session_header=header_enabled,
        codex_model=codex_model,
    )
    info = (
        SessionInfo(session_id=session_id, path="/x.jsonl", title=title)
        if title is not None
        else None
    )
    session = (
        SimpleNamespace(message_count=message_count, total_cost=total_cost)
        if message_count is not None
        else None
    )
    manager = SimpleNamespace(
        active_sessions={session_id: session} if session else {}
    )
    return SimpleNamespace(
        user_data={"codex_session_id": session_id, "current_directory": cwd},
        bot_data={
            # Kept off the real data/ directory: the banner now remembers
            # token counts on disk, and a stale one there would answer these.
            "_tokens_store": _MemoryStore(),
            "settings": settings,
            "codex_integration": SimpleNamespace(session_manager=manager),
            "_session_index": FakeIndex(info),
        },
    )


def test_header_contains_the_title_count_and_bot():
    header = build_header(make_context())

    assert "A tidy session" in header
    assert "27 msg" in header
    assert "main" in header


def test_the_session_id_is_not_shown():
    """The title already says which conversation this is."""
    assert "abcdef12" not in build_header(make_context())


def test_the_working_directory_is_not_shown():
    """It is the same directory on nearly every reply, so it was only noise."""
    assert "/home" not in build_header(make_context(cwd="/home"))


def test_the_bot_name_stands_alone():
    assert "bot main" not in build_header(make_context())


def kst(unix_seconds, fmt):
    from datetime import datetime, timedelta, timezone

    zone = timezone(timedelta(hours=9))
    return datetime.fromtimestamp(unix_seconds, tz=zone).strftime(fmt)


def test_the_context_size_replaces_the_message_count():
    context = make_context()
    response = SimpleNamespace(session_id="x", cost=0.0, context_tokens=441_700)

    header = build_header(context, response)

    assert "442k tok" in header
    assert "msg" not in header


def test_the_compaction_count_rides_on_the_token_count(monkeypatch):
    import src.bot.utils.compactions as comp

    monkeypatch.setattr(comp, "count", lambda path: 3)
    response = SimpleNamespace(session_id="x", cost=0.0, context_tokens=475_000)

    assert "475k tok (3)" in build_header(make_context(), response)


def test_a_session_that_has_never_compacted_shows_no_number(monkeypatch):
    import src.bot.utils.compactions as comp

    monkeypatch.setattr(comp, "count", lambda path: 0)
    response = SimpleNamespace(session_id="x", cost=0.0, context_tokens=475_000)

    header = build_header(make_context(), response)
    assert "475k tok" in header
    assert "(0)" not in header


def test_a_failing_compaction_count_does_not_break_the_banner(monkeypatch):
    import src.bot.utils.compactions as comp

    monkeypatch.setattr(comp, "count", lambda path: 1 / 0)
    response = SimpleNamespace(session_id="x", cost=0.0, context_tokens=475_000)

    assert "475k tok" in build_header(make_context(), response)


def test_a_small_context_is_shown_exactly():
    context = make_context()
    response = SimpleNamespace(session_id="x", cost=0.0, context_tokens=840)

    assert "840 tok" in build_header(context, response)


def test_the_last_known_size_is_kept_between_replies():
    """The banner is drawn for messages that did not come from Codex."""
    context = make_context()
    build_header(context, SimpleNamespace(session_id="x", cost=0.0, context_tokens=9000))

    assert "9k tok" in build_header(context)


def test_the_message_count_stands_in_until_a_reply_arrives():
    context = make_context()

    header = build_header(context)

    assert "27 msg" in header
    assert "tok" not in header


def test_a_response_without_a_token_count_does_not_blank_the_banner():
    context = make_context()
    response = SimpleNamespace(session_id="x", cost=0.0, context_tokens=None)

    assert "27 msg" in build_header(context, response)


def test_the_start_time_follows_the_title(monkeypatch):
    import time

    import src.bot.utils.session_start as ss

    begun = time.time() - 3600  # an hour ago, so still "today" in KST
    monkeypatch.setattr(ss, "started_at", lambda path: begun)

    header = build_header(make_context())

    assert f"A tidy session ({kst(begun, '%H:%M')})" in header


def test_an_older_session_also_shows_its_date(monkeypatch):
    """A bare 05:33 on a days-old session reads as this morning."""
    import time

    import src.bot.utils.session_start as ss

    begun = time.time() - 5 * 86400
    monkeypatch.setattr(ss, "started_at", lambda path: begun)

    assert kst(begun, "%b %d %H:%M") in build_header(make_context())


def test_an_unreadable_transcript_leaves_the_title_alone(monkeypatch):
    import src.bot.utils.session_start as ss

    monkeypatch.setattr(ss, "started_at", lambda path: None)

    header = build_header(make_context())
    assert "A tidy session" in header
    assert "(" not in header.splitlines()[0]


def test_a_failing_start_lookup_does_not_break_the_banner(monkeypatch):
    import src.bot.utils.session_start as ss

    monkeypatch.setattr(ss, "started_at", lambda path: 1 / 0)

    assert "A tidy session" in build_header(make_context())


def test_cost_is_never_shown():
    """The SDK only reports this process's spend, so the number would mislead."""
    assert "$" not in build_header(make_context())


def test_no_session_means_no_header():
    context = make_context()
    context.user_data["codex_session_id"] = None
    assert build_header(context) is None


def test_a_session_id_from_the_response_is_enough_to_render():
    """The id is no longer printed, but it is still what gates the banner."""
    context = make_context()
    context.user_data["codex_session_id"] = None
    response = SimpleNamespace(
        session_id="feedface-0000-1111-2222-333344445555", cost=0.5
    )

    assert build_header(context, response) is not None


def test_untitled_session_still_renders():
    context = make_context(title=None)
    assert "(untitled session)" in build_header(context)


def test_long_title_is_truncated():
    context = make_context(title="x" * 200)
    header = build_header(context)
    assert "…" in header
    assert len(header) < 400


def test_html_in_title_is_escaped():
    context = make_context(title="<b>not bold</b>")
    header = build_header(context)
    assert "&lt;b&gt;" in header
    assert "<b>not bold</b>" not in header


def test_banner_is_appended_to_the_last_message_only():
    context = make_context()
    messages = [
        FormattedMessage("first part", parse_mode="HTML"),
        FormattedMessage("second part", parse_mode="HTML"),
    ]

    out = attach_header(messages, context)

    assert out[0].text == "first part"
    assert out[1].text.startswith("second part")
    assert out[1].text.rstrip().endswith("main")
    assert "<b>" in out[1].text


def test_banner_becomes_its_own_message_when_body_is_near_the_limit():
    context = make_context()
    messages = [FormattedMessage("y" * 4090, parse_mode="HTML")]

    out = attach_header(messages, context)

    assert len(out) == 2
    assert out[0].text == "y" * 4090
    assert out[1].text.startswith("<b>")
    assert all(len(m.text) <= 4096 for m in out)


def test_disabled_by_setting():
    context = make_context(header_enabled=False)
    messages = [FormattedMessage("body", parse_mode="HTML")]
    assert attach_header(messages, context) == messages


def test_empty_message_list_is_untouched():
    assert attach_header([], make_context()) == []


def test_index_failure_does_not_break_the_reply():
    context = make_context()

    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("index exploded")

    context.bot_data["_session_index"] = Boom()
    messages = [FormattedMessage("body", parse_mode="HTML")]

    out = attach_header(messages, context)
    # Title lookup failed, but the banner (and the reply) still render.
    assert out[0].text.startswith("body")
    assert "(untitled session)" in out[0].text


def test_missing_session_manager_still_renders():
    context = make_context()
    context.bot_data["codex_integration"] = None
    context.bot_data["_session_index"] = FakeIndex(
        SessionInfo(session_id="x", path="/x", title="T")
    )
    header = build_header(context, SimpleNamespace(session_id="x", cost=0.25))
    assert "T" in header
    assert "msg" not in header  # count unknown without the manager
    assert "$" not in header


# --- usage limits -----------------------------------------------------------


def test_the_usage_numbers_sit_in_the_banner(usage):
    usage("5h 19% · week 22% · 1d 0%")

    header = build_header(make_context())

    assert "5h 19%" in header
    assert "week 22%" in header
    assert "1d 0%" in header


def test_each_usage_line_is_indented_under_the_banner(usage):
    usage("5h 7%: 13:39 (36%)\nweek 25%: Sun 10:59 (29%)")

    lines = build_header(make_context()).splitlines()

    assert lines[-2].startswith("   5h 7%")
    assert lines[-1].startswith("   week 25%")


def test_unknown_usage_leaves_the_banner_as_it_was(usage):
    usage(None)

    header = build_header(make_context())

    assert "%" not in header
    assert "A tidy session" in header


def test_a_failing_usage_lookup_does_not_break_the_banner(monkeypatch):
    import src.bot.utils.usage_limits as ul

    monkeypatch.setattr(ul, "line", lambda path=None: 1 / 0)

    assert "A tidy session" in build_header(make_context())


# --- model ------------------------------------------------------------------


def test_a_pinned_effort_is_shown(monkeypatch):
    context = make_context(codex_model=None)
    monkeypatch.setitem(context.bot_data, "_model_store", _NoModels())
    monkeypatch.setitem(context.bot_data, "_effort_store", _FixedModel("xhigh"))

    assert "xhigh" in build_header(context)


def test_auto_effort_is_still_shown(monkeypatch):
    """An omitted setting looks like a broken one; auto is a real state."""
    context = make_context(codex_model=None)
    monkeypatch.setitem(context.bot_data, "_model_store", _NoModels())
    monkeypatch.setitem(context.bot_data, "_effort_store", _NoModels())

    assert "auto" in build_header(context)


def test_the_configured_model_is_shown(monkeypatch):
    context = make_context(codex_model="gpt-6-astra")
    monkeypatch.setitem(context.bot_data, "_model_store", _NoModels())

    assert "gpt-6-astra" in build_header(context)


def test_a_session_override_wins_over_the_configured_model(monkeypatch):
    context = make_context(codex_model="gpt-6-astra")
    monkeypatch.setitem(context.bot_data, "_model_store", _FixedModel("gpt-6-luna"))

    header = build_header(context)
    assert "gpt-6-luna" in header
    assert "gpt-6-astra" not in header


def test_the_reported_model_wins_over_the_requested_alias(monkeypatch):
    from types import SimpleNamespace

    context = make_context(codex_model=None)
    monkeypatch.setitem(context.bot_data, "_model_store", _FixedModel("gpt-6-luna"))
    response = SimpleNamespace(model_id="gpt-6-sol-2026-09-22", session_id=None)

    header = build_header(context, response)
    assert "gpt-6-sol" in header


def test_no_model_anywhere_means_no_model_in_the_banner(monkeypatch):
    context = make_context(codex_model=None)
    monkeypatch.setitem(context.bot_data, "_model_store", _NoModels())

    header = build_header(context)
    assert "A tidy session" in header


class _NoModels:
    def get(self, session_id):
        return None


class _FixedModel:
    def __init__(self, model_id):
        self.model_id = model_id

    def get(self, session_id):
        return self.model_id


# --- pending wakes ----------------------------------------------------------


def wake(fires_in, message="RELAY: next step", bot_id="main", pid=1):
    """A wake firing `fires_in` seconds after the frozen now of 1000.0."""
    return PendingWake(
        pid=pid, bot_id=bot_id, message=message, fires_at=1000.0 + fires_in
    )


def lines(*entries, now=1000.0):
    from src.bot.utils.session_header import _wake_lines

    return _wake_lines("main", now=now)


def test_no_wakes_means_no_wake_section():
    assert build_header(make_context()).count("\n") == 1  # title + one detail line


def test_a_queued_wake_shows_its_countdown_and_message(wakes):
    wakes(wake(167, "RELAY: check the log"))

    out = lines()

    assert "1 wake" in out
    assert "(2min 47s)" in out
    assert "RELAY: check the log" in out


def test_the_fire_time_is_shown_in_kst(wakes):
    # 1000.0 + 3600 = 1970-01-01 01:00 UTC -> 10:00 KST
    wakes(wake(2600))

    assert "10:00 KST" in lines()


def test_several_wakes_are_counted_and_listed(wakes):
    wakes(wake(60, "first", pid=1), wake(120, "second", pid=2))

    out = lines()

    assert "2 wakes" in out
    assert "first" in out and "second" in out


def test_only_the_first_three_are_listed(wakes):
    wakes(*[wake(60 * i, f"msg{i}", pid=i) for i in range(1, 6)])

    out = lines()

    assert "5 wakes" in out
    assert "msg4" not in out
    assert "+2 more" in out


def test_a_wake_that_already_fired_is_dropped(wakes):
    wakes(wake(-30))

    assert lines() is None


def test_a_wake_firing_right_now_is_kept(wakes):
    """Its timer is up but it is still writing the file."""
    wakes(wake(-1))

    assert "1 wake" in lines()


def test_a_long_message_is_truncated(wakes):
    wakes(wake(60, "RELAY: " + "x" * 200))

    out = lines()

    assert "…" in out
    assert len(max(out.splitlines(), key=len)) < 90


def test_a_multiline_message_becomes_one_line(wakes):
    wakes(wake(60, "RELAY:\ncheck\nthe log"))

    out = lines()

    assert "RELAY: check the log" in out
    assert len(out.splitlines()) == 2  # header line + one wake line


def test_html_in_a_wake_message_is_escaped(wakes):
    wakes(wake(60, "run <b>now</b>"))

    assert "&lt;b&gt;" in lines()


def test_the_wake_section_is_part_of_the_banner(wakes):
    import time

    # build_header reads the real clock, so this one must be genuinely future.
    wakes(PendingWake(pid=1, bot_id="main", message="RELAY: soon",
                      fires_at=time.time() + 60))

    header = build_header(make_context())

    assert "A tidy session" in header
    assert "RELAY: soon" in header


def test_a_failing_wake_scan_does_not_break_the_banner(monkeypatch):
    import src.bot.utils.pending_wakes as pw

    monkeypatch.setattr(pw, "scan_quietly", lambda bot_id=None: 1 / 0)

    header = build_header(make_context())

    assert "A tidy session" in header


def test_the_context_size_survives_a_restart():
    """user_data is memory only; the count must not fall back to a msg count."""
    store = _MemoryStore()
    first = make_context()
    first.bot_data["_tokens_store"] = store
    build_header(first, SimpleNamespace(session_id="x", cost=0.0, context_tokens=441_700))

    # A fresh process: same session on disk, empty user_data.
    after = make_context()
    after.bot_data["_tokens_store"] = store
    after.user_data.pop("context_tokens", None)

    header = build_header(after)
    assert "442k tok" in header
    assert "msg" not in header


def cond(fires_in, flag="/tmp/train.done", message="RELAY: finished?", pid=2):
    return PendingWake(
        pid=pid, bot_id="main", message=message, fires_at=1000.0 + fires_in,
        kind="conditional", flag=flag,
    )


def test_a_conditional_wake_has_its_own_symbol_and_shows_the_flag_and_timeout(wakes):
    wakes(cond(3600))

    out = lines()

    assert "👀 1 watching" in out
    assert "train.done" in out
    assert "timeout" in out
    assert "⏰" not in out


def test_timed_and_conditional_wakes_get_separate_sections(wakes):
    wakes(wake(60, "later"), cond(3600))

    out = lines()

    assert "⏰ 1 wake" in out
    assert "👀 1 watching" in out
    assert out.index("⏰") < out.index("👀")
