"""Rate-limit meters read from Codex rollouts."""

import json
import os
from datetime import datetime, timezone

import pytest
from conftest import meta, tokens, write_rollout

from src.bot.utils import usage_limits as ul

SID = "aaaaaaaa-1111-2222-3333-444444444444"
SID2 = "bbbbbbbb-1111-2222-3333-444444444444"
T0 = datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def clean():
    ul.reset_cache()
    yield
    ul.reset_cache()


def limits(p=7.0, s=25.0, p_in=9000, s_at=None):
    secondary = {"used_percent": s, "window_minutes": 10080}
    secondary["resets_at"] = s_at if s_at is not None else T0 + 3 * 86400
    return {"primary": {"used_percent": p, "window_minutes": 300, "resets_in_seconds": p_in},
            "secondary": secondary}


def test_reads_both_windows(sessions):
    write_rollout(sessions, SID, [meta(SID), tokens(total=1, rate_limits=limits(),
                                                    ts="2026-10-12T05:00:00Z")])
    usage = ul.read(sessions)
    assert usage.primary.percent == 7.0
    assert usage.primary.window_seconds == 5 * 3600
    assert usage.primary.resets_at == T0 + 9000
    assert usage.secondary.resets_at == T0 + 3 * 86400


def test_latest_event_in_the_newest_file_wins(sessions):
    old = write_rollout(sessions, SID, [tokens(total=1, rate_limits=limits(p=90))])
    write_rollout(sessions, SID2, [tokens(total=1, rate_limits=limits(p=10)),
                                   tokens(total=1, rate_limits=limits(p=12))])
    os.utime(old, (1, 1))
    assert ul.read(sessions).primary.percent == 12


def test_iso_reset_and_top_level_stream_shape():
    line = json.dumps({"type": "token_count", "rate_limits": {
        "primary": {"used_percent": 3, "window_minutes": 300,
                    "resets_at": "2026-10-12T08:00:00Z"}}}).encode()
    usage = ul.parse_event(line)
    assert usage.primary.resets_at == T0 + 3 * 3600
    assert usage.secondary is None


def test_nothing_recorded_is_unknown(sessions):
    write_rollout(sessions, SID, [meta(SID), tokens(total=5)])
    assert not ul.read(sessions).known
    assert ul.read(sessions / "missing").known is False


def test_render_shows_spend_reset_and_pace():
    usage = ul.Usage(primary=ul.Window(7, 5 * 3600, T0 + 3 * 3600),
                     secondary=ul.Window(25, 7 * 86400, T0 + 5 * 86400))
    text = ul.render(usage, now=T0)
    first, second = text.splitlines()
    # T0 is 14:00 KST; 5h window ends 3h later -> 17:00, 40% elapsed.
    assert first == "5h 7%: 17:00 (40%)"
    assert second.startswith("week 25%: ")
    assert second.endswith("(29%)")


def test_a_rolled_over_window_reads_zero():
    usage = ul.Usage(primary=ul.Window(80, 5 * 3600, T0 - 60))
    assert ul.render(usage, now=T0).startswith("5h 0%: ")


def test_unusual_window_lengths_get_a_generic_label():
    usage = ul.Usage(primary=ul.Window(1, 3600, None))
    assert ul.render(usage, now=T0) == "1h 1%"


def test_line_never_raises(monkeypatch):
    monkeypatch.setattr(ul, "current", lambda root=None: 1 / 0)
    assert ul.line() is None


def test_remember_seeds_the_cache(sessions):
    ul.remember(ul.Usage(primary=ul.Window(42)))
    assert ul.current(sessions).primary.percent == 42
