"""Synthetic-message injector for Codex self-wake.

Watches disk for wake files; when one appears, reads JSON of the form
`{"chat_id": int, "text": str, "user_id": int (optional)}`, builds a
synthetic Telegram Update with that text, and pushes it into the bot's
update queue. The bot then processes it exactly as if a real user sent it.

Use case: a nohup'd bash script after a long-running experiment sleeps for
N minutes, then writes the trigger file. The bot picks it up, routes the
message to the agentic-text handler, which resumes the Codex session and
acts on the "wake up" prompt.

Multi-bot model: instead of one fixed file, each bot watches its own
**spool directory** `<inject_dir>/<bot_id>/` for `*.json` wake files, processed
oldest-first (FIFO). Each wake is a uniquely-named file, so concurrent wakes
never clobber each other. After processing, a file is renamed to
`<name>.processed-<timestamp>` so it isn't re-fired and audit history is kept.

Backward compatibility: for `bot_id == "main"` the legacy single-file path
(``data/codex_inject_message.json`` / ``$CODEX_INJECT_PATH``) is also honoured
so existing tooling keeps working during migration. The Codex bot's paths are
intentionally distinct from the Claude bot's (`/tmp/codex_*` vs `/tmp/claude_*`)
so the two never clash.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

import structlog
from telegram import Update
from telegram.ext import Application

logger = structlog.get_logger()


DEFAULT_INJECT_PATH = Path(
    os.environ.get(
        "CODEX_INJECT_PATH",
        Path(__file__).resolve().parents[2] / "data" / "codex_inject_message.json",
    )
)
POLL_INTERVAL_SECONDS = 2.0


def _build_synthetic_update_payload(
    chat_id: int,
    text: str,
    user_id: Optional[int] = None,
    first_name: str = "CodexWake",
    message_id: Optional[int] = None,
) -> dict:
    """Construct a Telegram Update payload for a private text message."""
    if user_id is None:
        user_id = chat_id
    if message_id is None:
        message_id = int(time.time() * 1000) % (2**31)
    now = int(time.time())
    return {
        "update_id": int(time.time() * 1000) % (2**31),
        "message": {
            "message_id": message_id,
            "date": now,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": first_name,
            },
            "text": text,
        },
    }


def _claim_file(path: Path) -> Optional[str]:
    """Atomically claim a wake file by reading then renaming it.

    The `os.rename` is the claim: if two watcher iterations (or two bot
    processes that somehow share a spool) race, exactly one wins the rename
    and the loser gets FileNotFoundError -> returns None. Guarantees a wake
    fires at most once.
    """
    processed_path = path.with_suffix(path.suffix + f".processed-{int(time.time())}")
    try:
        raw = path.read_text()
        os.rename(path, processed_path)
        return raw
    except FileNotFoundError:
        return None


async def _fire_wake(app: Application, raw: str) -> None:
    """Parse a claimed wake payload and inject it as a synthetic Update."""
    bot = app.bot
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("codex inject_watcher: bad JSON", error=str(e), raw=raw[:200])
        return

    chat_id = int(payload.get("chat_id", 0))
    text = str(payload.get("text", "")).strip()
    user_id = payload.get("user_id")
    if user_id is not None:
        user_id = int(user_id)
    if not text or not chat_id:
        logger.warning(
            "codex inject_watcher: missing chat_id or text", payload=payload
        )
        return

    # Send a placeholder bot-message first to get a REAL message_id from
    # Telegram. The orchestrator explicitly uses `update.message.message_id`
    # as reply target in many places, so the synthetic message MUST carry a
    # real message_id.
    placeholder = await bot.send_message(
        chat_id=chat_id,
        text=f"[codex auto-wake: {text[:80]}]",
    )
    real_msg_id = placeholder.message_id

    update_dict = _build_synthetic_update_payload(
        chat_id=chat_id, text=text, user_id=user_id, message_id=real_msg_id,
    )
    update = Update.de_json(update_dict, bot)
    await app.update_queue.put(update)
    logger.info(
        "codex inject_watcher fired synthetic message",
        chat_id=chat_id, text_preview=text[:80], real_msg_id=real_msg_id,
    )


def _pending_files(spool_dir: Optional[Path]) -> list[Path]:
    """Return pending '*.json' wake files in the spool dir, oldest-first (FIFO).

    Excludes already-processed files. Sort by (mtime, name) so wakes fire in
    roughly the order they were written, with the unique filename breaking ties.
    """
    if spool_dir is None or not spool_dir.is_dir():
        return []
    files = [p for p in spool_dir.glob("*.json") if p.is_file()]
    try:
        files.sort(key=lambda p: (p.stat().st_mtime, p.name))
    except FileNotFoundError:
        files.sort(key=lambda p: p.name)
    return files


async def inject_watcher_loop(app: Application,
                               spool_dir: Optional[Path] = None,
                               legacy_path: Optional[Path] = DEFAULT_INJECT_PATH,
                               poll_seconds: float = POLL_INTERVAL_SECONDS,
                               stop_event: Optional[asyncio.Event] = None,
                               *,
                               inject_path: Optional[Path] = None) -> None:
    """Background task: poll the per-bot spool dir (and optional legacy file),
    fire synthetic Updates for each wake.

    Args:
        spool_dir: per-bot directory watched for ``*.json`` wake files. Created
            if missing. When ``None`` only ``legacy_path`` is watched.
        legacy_path: single-file fallback (``data/codex_inject_message.json``)
            for backward compatibility; pass ``None`` to disable (non-main bots).
        inject_path: deprecated alias for ``legacy_path`` (kept so old callers
            that passed a single path keep working).
    """
    # Back-compat: an old caller may pass inject_path=<file>. Treat it as the
    # legacy single-file watch and skip spool scanning unless spool_dir given.
    if inject_path is not None:
        legacy_path = inject_path

    if spool_dir is not None:
        try:
            spool_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.error("codex inject_watcher: cannot create spool dir",
                         spool_dir=str(spool_dir), error=str(e))

    logger.info("codex inject_watcher started",
                spool_dir=str(spool_dir) if spool_dir else None,
                legacy_path=str(legacy_path) if legacy_path else None,
                poll_seconds=poll_seconds)

    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("codex inject_watcher stopping (stop_event)")
            return
        try:
            # 1) Spool directory (FIFO, oldest-first), one wake per file.
            for path in _pending_files(spool_dir):
                raw = _claim_file(path)
                if raw is not None:
                    await _fire_wake(app, raw)

            # 2) Legacy single-file fallback (main bot / old tooling).
            if legacy_path is not None and legacy_path.exists():
                raw = _claim_file(legacy_path)
                if raw is not None:
                    await _fire_wake(app, raw)
        except Exception as e:  # noqa: BLE001
            logger.error("codex inject_watcher loop error", error=str(e))

        await asyncio.sleep(poll_seconds)
