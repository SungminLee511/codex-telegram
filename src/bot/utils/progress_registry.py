"""Crash-safe record of live "Working..." messages.

A progress message is deleted in the handler's `finally` block, which covers
every *in-process* failure. It does not cover the process itself going away:
a restart, a kill, an OOM. The handler never resumes, so the message sits in
the chat saying "Working... (10s)" forever and the Stop button under it is
wired to a request that no longer exists.

So the live ones are written to disk as they are created and removed as they
are deleted. Whatever is still in the file at startup belongs to a request
that died with the previous process, and gets struck through on sight.

The file is a plain JSON list, rewritten atomically. It is tiny (usually
empty, at most a handful of entries) so rewriting beats appending.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List

import structlog

logger = structlog.get_logger()


class ProgressRegistry:
    """Tracks (chat_id, message_id) pairs for progress messages in flight."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # --- persistence ------------------------------------------------------

    def _read(self) -> List[Dict[str, int]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [
            entry
            for entry in data
            if isinstance(entry, dict)
            and isinstance(entry.get("chat_id"), int)
            and isinstance(entry.get("message_id"), int)
        ]

    def _write(self, entries: List[Dict[str, int]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(entries))
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.debug("progress_registry: write failed", error=str(exc))

    # --- api --------------------------------------------------------------

    def add(self, chat_id: int, message_id: int) -> None:
        entries = self._read()
        if not any(
            e["chat_id"] == chat_id and e["message_id"] == message_id for e in entries
        ):
            entries.append({"chat_id": chat_id, "message_id": message_id})
            self._write(entries)

    def discard(self, chat_id: int, message_id: int) -> None:
        entries = self._read()
        remaining = [
            e
            for e in entries
            if not (e["chat_id"] == chat_id and e["message_id"] == message_id)
        ]
        if len(remaining) != len(entries):
            self._write(remaining)

    def drain(self) -> List[Dict[str, int]]:
        """Return everything still recorded and forget it."""
        entries = self._read()
        if entries:
            self._write([])
        return entries


STALE_TEXT = (
    "⚠️ <b>Interrupted</b>\n"
    "The bot restarted while this was running, so the reply was lost. "
    "Send the message again to retry."
)


async def sweep_stale(bot, registry: ProgressRegistry) -> int:
    """Mark every progress message orphaned by the last shutdown.

    The message is edited rather than deleted so the chat still shows that
    something was asked and did not finish — a silently vanishing message
    looks like the question was never sent.
    """
    entries = registry.drain()
    cleared = 0
    for entry in entries:
        try:
            await bot.edit_message_text(
                chat_id=entry["chat_id"],
                message_id=entry["message_id"],
                text=STALE_TEXT,
                parse_mode="HTML",
                reply_markup=None,
            )
            cleared += 1
        except Exception as exc:
            # Already deleted, too old to edit, or unchanged — nothing to do.
            logger.debug(
                "progress_registry: could not clear stale message",
                chat_id=entry.get("chat_id"),
                message_id=entry.get("message_id"),
                error=str(exc),
            )
    if cleared:
        logger.info("Cleared stale progress messages", count=cleared)
    return cleared
