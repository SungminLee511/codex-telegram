"""Tests for reading pending wake-ups out of /proc."""

import os

import pytest

from src.bot.utils.pending_wakes import PendingWake, scan, scan_quietly

CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
BOOT = 1_700_000_000


def write_proc(root, pid, *, delay=600, bot="main", message="RELAY: next step",
               started_after_boot=100.0, cmdline=None):
    """Build a fake /proc entry shaped like a wake_after.sh worker."""
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    if cmdline is None:
        cmdline = (
            f'bash\0-c\0sleep {delay}\necho "[$(date -u)] firing codex wake after '
            f'{delay}s (bot={bot})" >> /tmp/wake_after.log\0'
        )
    (d / "cmdline").write_text(cmdline)
    ticks = int(started_after_boot * CLOCK_TICKS)
    # stat is "pid (comm) state <fields 4..21> starttime ...". The comm holds a
    # space on purpose, to prove the parser splits from the last ')'.
    fields = " ".join(str(i) for i in range(4, 22))
    (d / "stat").write_text(f"{pid} (bash worker) S {fields} {ticks} 0 0\n")
    (d / "environ").write_text(f"PATH=/usr/bin\0WAKE_MESSAGE={message}\0HOME=/root\0")
    return d


@pytest.fixture()
def proc(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "stat").write_text(f"cpu 1 2 3\nbtime {BOOT}\nprocesses 99\n")
    return root


def test_a_scheduled_wake_is_found(proc):
    write_proc(proc, 4242, delay=600, started_after_boot=100.0)

    (wake,) = scan(proc=proc)

    assert wake.pid == 4242
    assert wake.bot_id == "main"
    assert wake.fires_at == BOOT + 100 + 600


def test_the_message_comes_from_the_environment_not_the_command_line(proc):
    """wake_after.sh exports it, so ps cannot see it."""
    write_proc(proc, 1, message="RELAY: check the log")

    assert scan(proc=proc)[0].message == "RELAY: check the log"


def test_unrelated_processes_are_ignored(proc):
    write_proc(proc, 7, cmdline="sleep\0600\0")
    write_proc(proc, 8, cmdline="python\0-m\0src.main\0")

    assert scan(proc=proc) == []


def test_non_numeric_proc_entries_are_skipped(proc):
    (proc / "self").mkdir()
    (proc / "meminfo").write_text("MemTotal: 1 kB")

    assert scan(proc=proc) == []


def test_wakes_are_sorted_soonest_first(proc):
    write_proc(proc, 1, delay=900, started_after_boot=10.0)
    write_proc(proc, 2, delay=60, started_after_boot=10.0)

    assert [w.pid for w in scan(proc=proc)] == [2, 1]


def test_filtering_by_bot_keeps_only_that_bots_wakes(proc):
    write_proc(proc, 1, bot="main")
    write_proc(proc, 2, bot="bot4")

    assert [w.bot_id for w in scan(bot_id="bot4", proc=proc)] == ["bot4"]


def test_no_filter_returns_every_bot(proc):
    write_proc(proc, 1, bot="main")
    write_proc(proc, 2, bot="bot4")

    assert len(scan(proc=proc)) == 2


def test_a_process_that_exits_mid_scan_is_skipped_not_raised(proc):
    """Listing /proc then reading it is inherently racy."""
    d = write_proc(proc, 1)
    (d / "stat").unlink()

    assert scan(proc=proc) == []


def test_a_missing_environ_leaves_the_message_empty(proc):
    d = write_proc(proc, 1)
    (d / "environ").unlink()

    assert scan(proc=proc)[0].message == ""


def test_an_unreadable_proc_yields_nothing_rather_than_failing(tmp_path):
    assert scan(proc=tmp_path / "nope") == []


def test_seconds_left_counts_down_from_the_fire_time():
    wake = PendingWake(pid=1, bot_id="main", message="x", fires_at=1000.0)

    assert wake.seconds_left(now=940.0) == 60.0


def test_scan_quietly_swallows_errors(monkeypatch):
    import src.bot.utils.pending_wakes as pw

    monkeypatch.setattr(pw, "scan", lambda *a, **k: 1 / 0)

    assert scan_quietly("main") == []


# --- conditional wakes (wake_when.sh) ---------------------------------------


def write_conditional(root, pid, *, timeout=3600, bot="main", flag="/tmp/job.done",
                      message="RELAY: done?", started_after_boot=100.0):
    d = write_proc(root, pid, message=message, started_after_boot=started_after_boot,
                   cmdline=f"bash\0-c\0while :; do sleep 10; done\0"
                           f"codex cond-wake (bot={bot} timeout={timeout}s)\0")
    (d / "environ").write_text(
        f"PATH=/usr/bin\0WAKE_MESSAGE={message}\0WAKE_FLAG={flag}\0"
    )
    return d


def test_a_conditional_wake_is_found_with_its_flag_and_timeout(proc):
    write_conditional(proc, 9, timeout=3600, flag="/tmp/train.done",
                      started_after_boot=100.0)

    (wake,) = scan(proc=proc)

    assert wake.kind == "conditional"
    assert wake.flag == "/tmp/train.done"
    assert wake.bot_id == "main"
    assert wake.fires_at == BOOT + 100 + 3600  # the latest it can fire


def test_timed_and_conditional_wakes_are_listed_together(proc):
    write_proc(proc, 1, delay=60, started_after_boot=10.0)
    write_conditional(proc, 2, timeout=120, started_after_boot=10.0)

    kinds = {w.pid: w.kind for w in scan(proc=proc)}

    assert kinds == {1: "timed", 2: "conditional"}


def test_a_conditional_wake_respects_the_bot_filter(proc):
    write_conditional(proc, 1, bot="bot4")

    assert scan(bot_id="main", proc=proc) == []
    assert len(scan(bot_id="bot4", proc=proc)) == 1
