"""Tests for the progress-message duration format."""

from src.bot.utils.durations import format_elapsed


def test_under_a_minute_is_seconds_only():
    assert format_elapsed(10) == "10s"


def test_a_minute_switches_to_minutes():
    assert format_elapsed(60) == "1min 0s"


def test_the_last_second_before_a_minute_is_still_seconds():
    assert format_elapsed(59) == "59s"


def test_minutes_and_seconds_are_both_shown():
    assert format_elapsed(167) == "2min 47s"


def test_seconds_are_truncated_not_rounded_up():
    """So the counter never shows a time that has not elapsed yet."""
    assert format_elapsed(59.9) == "59s"


def test_zero_is_fine():
    assert format_elapsed(0) == "0s"


def test_a_negative_clock_skew_does_not_show_a_negative_time():
    assert format_elapsed(-5) == "0s"


def test_long_runs_keep_counting_minutes():
    assert format_elapsed(3725) == "62min 5s"
