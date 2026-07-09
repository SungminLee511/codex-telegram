"""Multi-bot isolation tests for the Codex inject watcher.

Covers:
- spool-dir FIFO ordering + already-processed exclusion (_pending_files)
- atomic single-fire claim (_claim_file): exactly one of two racers wins
- a watcher bound to bot A never consumes bot B's spool files (isolation)
- legacy single-file fallback still fires for the 'main' bot (back-compat)
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.bot.inject_watcher import (
    DEFAULT_INJECT_PATH,
    _claim_file,
    _pending_files,
    inject_watcher_loop,
)


def _write_wake(spool_dir, name, chat_id=42, text="hi"):
    spool_dir.mkdir(parents=True, exist_ok=True)
    p = spool_dir / name
    p.write_text(json.dumps({"chat_id": chat_id, "text": text}))
    return p


# ---- fakes -----------------------------------------------------------------

class _FakeMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return _FakeMessage(message_id=1000 + len(self.sent))


class _FakeApp:
    """Minimal stand-in for telegram.ext.Application used by the watcher."""

    def __init__(self):
        self.bot = _FakeBot()
        self.update_queue: asyncio.Queue = asyncio.Queue()


# ---- spool ordering & atomic claim -----------------------------------------

def test_pending_files_fifo_and_excludes_processed(tmp_path):
    spool = tmp_path / "main"
    p1 = _write_wake(spool, "100-a.json")
    time.sleep(0.01)
    p2 = _write_wake(spool, "200-b.json")
    # an already-processed file must be ignored
    (spool / "050-old.json.processed-123").write_text("{}")

    pending = _pending_files(spool)
    assert pending == [p1, p2], "must be oldest-first and exclude .processed-*"


def test_pending_files_missing_dir(tmp_path):
    assert _pending_files(tmp_path / "nope") == []
    assert _pending_files(None) == []


def test_claim_file_single_fire(tmp_path):
    spool = tmp_path / "main"
    p = _write_wake(spool, "1.json", text="claim-me")

    raw1 = _claim_file(p)
    raw2 = _claim_file(p)  # second claim: file already renamed away
    assert raw1 is not None and "claim-me" in raw1
    assert raw2 is None, "a wake file must fire at most once"
    # original gone, a .processed-* sibling exists
    assert not p.exists()
    assert any(".processed-" in f.name for f in spool.iterdir())


# ---- end-to-end watcher behaviour ------------------------------------------

async def _run_one_pass(app, **kwargs):
    """Run the watcher loop briefly then stop it via stop_event."""
    stop = asyncio.Event()
    task = asyncio.create_task(
        inject_watcher_loop(app, poll_seconds=0.02, stop_event=stop, **kwargs)
    )
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_watcher_consumes_only_its_own_spool(tmp_path):
    base = tmp_path / "inject"
    spool_a = base / "botA"
    spool_b = base / "botB"
    _write_wake(spool_a, "1.json", chat_id=111, text="for-A")
    _write_wake(spool_b, "1.json", chat_id=222, text="for-B")

    app = _FakeApp()
    # Watcher bound to botA, legacy disabled.
    await _run_one_pass(app, spool_dir=spool_a, legacy_path=None)

    # Exactly one wake fired, and it was botA's (chat_id 111).
    assert app.update_queue.qsize() == 1
    upd = app.update_queue.get_nowait()
    assert upd.message.chat.id == 111
    # botB's file untouched.
    assert (spool_b / "1.json").exists()
    # botA's file consumed.
    assert not (spool_a / "1.json").exists()


@pytest.mark.asyncio
async def test_legacy_single_file_fires_for_main(tmp_path):
    legacy = tmp_path / "codex_inject_message.json"
    legacy.write_text(json.dumps({"chat_id": 777, "text": "legacy-wake"}))

    app = _FakeApp()
    await _run_one_pass(app, spool_dir=None, legacy_path=legacy)

    assert app.update_queue.qsize() == 1
    upd = app.update_queue.get_nowait()
    assert upd.message.chat.id == 777
    assert not legacy.exists()  # claimed/renamed


def test_default_legacy_path_is_codex_specific():
    # Regression: the Codex legacy default must be the Codex inject file, not the
    # Claude bot's /tmp/claude_inject_message.json.
    s = str(DEFAULT_INJECT_PATH)
    assert s.endswith("codex_inject_message.json")
    assert "claude_inject_message.json" not in s
