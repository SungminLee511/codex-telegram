"""wake_when.sh: wake when a flag file turns true, or when the timeout hits."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "wake_when.sh"


@pytest.fixture()
def env(tmp_path):
    e = dict(os.environ)
    e.update(INJECT_DIR=str(tmp_path / "spool"), WAKE_CHAT_ID="42", WAKE_WHEN_POLL="1")
    return e


def run(env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True
    )


def wakes(tmp_path, bot="testbot", wait=6.0):
    spool = tmp_path / "spool" / bot
    end = time.time() + wait
    while time.time() < end:
        found = sorted(spool.glob("*.json")) if spool.is_dir() else []
        if found:
            return [json.loads(p.read_text()) for p in found]
        time.sleep(0.2)
    return []


def test_the_timeout_is_required(env):
    out = run(env, "--flag", "/tmp/x", "--message", "m")

    assert out.returncode == 2
    assert "--timeout is required" in out.stderr


def test_a_bad_timeout_is_refused(env):
    out = run(env, "--flag", "/tmp/x", "--timeout", "soon", "--message", "m")

    assert out.returncode == 2


def test_a_zero_timeout_is_refused(env):
    out = run(env, "--flag", "/tmp/x", "--timeout", "0", "--message", "m")

    assert out.returncode == 2


def test_a_true_flag_fires_the_wake_with_the_message(env, tmp_path):
    flag = tmp_path / "ok.flag"
    out = run(env, "--flag", str(flag), "--timeout", "30", "--message",
              "RELAY: done?", "--bot", "testbot")
    assert out.returncode == 0
    flag.write_text("TRUE \n")

    (wake,) = wakes(tmp_path)

    assert wake["chat_id"] == 42
    assert wake["text"].startswith("[condition met")
    assert "RELAY: done?" in wake["text"]


def test_a_false_flag_does_not_fire_before_the_timeout(env, tmp_path):
    flag = tmp_path / "ok.flag"
    flag.write_text("false")
    run(env, "--flag", str(flag), "--timeout", "30", "--message", "m",
        "--bot", "testbot")

    assert wakes(tmp_path, wait=3.0) == []
    flag.write_text("1")  # let the background worker finish
    wakes(tmp_path)


def test_the_timeout_fires_the_wake_regardless(env, tmp_path):
    run(env, "--flag", str(tmp_path / "never"), "--timeout", "2", "--message",
        "RELAY: stalled", "--bot", "testbot")

    (wake,) = wakes(tmp_path)

    assert wake["text"].startswith("[TIMED OUT")
    assert "RELAY: stalled" in wake["text"]


def test_the_log_tail_is_included(env, tmp_path):
    log = tmp_path / "job.log"
    log.write_text("\n".join(f"line{i}" for i in range(50)) + "\n")
    flag = tmp_path / "ok.flag"
    flag.write_text("true")
    run(env, "--flag", str(flag), "--timeout", "30", "--message", "m",
        "--log", str(log), "--bot", "testbot")

    (wake,) = wakes(tmp_path)

    assert "line49" in wake["text"]
    assert "line30" in wake["text"]
    assert "line29" not in wake["text"]  # only the last 20 lines


def test_it_fires_once_and_the_worker_exits(env, tmp_path):
    flag = tmp_path / "ok.flag"
    flag.write_text("true")
    run(env, "--flag", str(flag), "--timeout", "30", "--message", "m",
        "--bot", "testbot")

    assert len(wakes(tmp_path)) == 1
    time.sleep(2.5)

    assert len(wakes(tmp_path, wait=0.5)) == 1


def test_quotes_and_newlines_in_the_message_survive(env, tmp_path):
    flag = tmp_path / "ok.flag"
    flag.write_text("true")
    message = "it's \"quoted\" $(not run)\nsecond line"
    run(env, "--flag", str(flag), "--timeout", "30", "--message", message,
        "--bot", "testbot")

    (wake,) = wakes(tmp_path)

    assert message in wake["text"]
