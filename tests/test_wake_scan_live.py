"""Real wake processes are listed by the Codex scanner — and only by it."""

import os
import re
import signal
import subprocess
import time
from pathlib import Path

from src.bot.utils import pending_wakes

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_TIMED = re.compile(r"(?<!codex )firing wake after (\d+)s \(bot=")
CLAUDE_COND = re.compile(r"conditional wake \(bot=([A-Za-z0-9_-]+) timeout=")


def _env(tmp_path):
    env = dict(os.environ)
    env.update(INJECT_DIR=str(tmp_path / "spool"), WAKE_CHAT_ID="42",
               CODEX_INJECT_PATH=str(tmp_path / "legacy.json"))
    return env


def _kill(bot):
    for wake in pending_wakes.scan(bot):
        # Only the worker itself: it shares our process group, so killpg
        # would take the test runner down with it.
        try:
            os.kill(wake.pid, signal.SIGTERM)
        except OSError:
            pass


def test_timed_and_conditional_wakes_are_found(tmp_path):
    bot = f"scan{os.getpid()}"
    env = _env(tmp_path)
    try:
        subprocess.run(["bash", str(ROOT / "wake_after.sh"), "900", "timed one", bot],
                       env=env, check=True, capture_output=True)
        subprocess.run(["bash", str(ROOT / "wake_when.sh"), "--flag",
                        str(tmp_path / "f"), "--timeout", "1h", "--message",
                        "cond one", "--bot", bot], env=env, check=True,
                       capture_output=True)
        found = []
        for _ in range(30):
            found = pending_wakes.scan(bot)
            if len(found) >= 2:
                break
            time.sleep(0.1)
        kinds = {w.kind: w for w in found}
        assert set(kinds) == {"timed", "conditional"}
        assert kinds["timed"].message == "timed one"
        assert kinds["conditional"].flag == str(tmp_path / "f")
        assert 3000 < kinds["conditional"].seconds_left() <= 3600
    finally:
        _kill(bot)


def test_codex_markers_do_not_match_the_claude_patterns():
    assert not CLAUDE_COND.search("codex cond-wake (bot=main timeout=60s)")
    assert pending_wakes._COND_RE.search("codex cond-wake (bot=main timeout=60s)")
    assert pending_wakes._WAKE_RE.search("firing codex wake after 5s (bot=main)")
    assert not pending_wakes._WAKE_RE.search("firing wake after 5s (bot=main)")
