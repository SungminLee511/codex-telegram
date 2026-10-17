"""Files Codex leaves for the user, swept into the chat after each reply.

Codex has no way to hand you a file: it can write one to disk, but the bot is
what talks to Telegram. So there is a drop box. Anything Codex puts in
`<repo>/outbox/` is sent to the chat once the reply has gone out, and then
deleted — the directory is a queue, not a folder you browse.

Codex never sends or deletes anything itself. It writes a file and stops; the
sweep below is ordinary bot code running after the turn, which is why a crashed
or interrupted turn still leaves the file to be picked up by the next one.

Two things that look like over-engineering and are not:

*Claiming by rename.* Several bots share this repo and so share the directory.
Renaming a file to a private `.sending-<pid>` name before uploading means two
bots sweeping at the same moment cannot both send it — the loser's rename
fails. A file is only unlinked once Telegram has accepted it; a failed send
puts the original name back, so nothing is lost by a dropped connection.

*Leaving the oversized behind.* Telegram refuses a document over 50 MB. Such a
file stays where it is, with its name reported in the chat, rather than being
silently dropped — the user asked for it, so the failure has to be visible and
the bytes have to survive it.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger()

# src/bot/utils/outbox.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
OUTBOX = REPO_ROOT / "outbox"

# Telegram's own ceilings for a bot. Anything above the document ceiling is
# refused by the API, so it is checked here rather than discovered as an
# upload that stalls and then fails.
PHOTO_MAX_BYTES = 10 * 1024 * 1024
DOCUMENT_MAX_BYTES = 50 * 1024 * 1024

# Limits on one sweep, not on one file. A loop that writes a thousand frames
# into the outbox should not turn into a thousand uploads and a flood-ban; the
# surplus stays on disk and the chat is told what was held back.
MAX_FILES_PER_SWEEP = 10
MAX_TOTAL_BYTES_PER_SWEEP = 100 * 1024 * 1024

PHOTO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# A file still being written should not be sent half-finished. These suffixes
# are the usual marks of work in progress.
_IGNORED_SUFFIXES = (".tmp", ".part", ".crdownload", ".writing")
_CLAIM_MARK = ".sending-"


def pending(root: Path = OUTBOX) -> List[Path]:
    """Files waiting to be sent, oldest name first.

    Sorted by name so a numbered series (`1-before.png`, `2-after.png`) arrives
    in the order it was meant to be read.
    """
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []

    out: List[Path] = []
    for path in entries:
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name.endswith(_IGNORED_SUFFIXES) or _CLAIM_MARK in path.name:
            continue
        out.append(path)
    return out


def is_photo(path: Path, size: Optional[int] = None) -> bool:
    """Whether Telegram will take this as a photo rather than a document.

    Over the photo ceiling it still goes through, as a document — which keeps
    the original resolution anyway, so a big render arrives intact instead of
    being refused.
    """
    if path.suffix.lower() not in PHOTO_EXTENSIONS:
        return False
    if size is None:
        try:
            size = path.stat().st_size
        except OSError:
            return False
    return size <= PHOTO_MAX_BYTES


@dataclass
class Delivery:
    """What the sweep did, for the note that follows it."""

    sent: List[str]
    too_big: List[str]
    failed: List[str]
    held_back: int = 0

    @property
    def anything_happened(self) -> bool:
        return bool(self.sent or self.too_big or self.failed or self.held_back)

    def note(self) -> Optional[str]:
        """A short line for the chat, or None when there is nothing to say."""
        if not (self.too_big or self.failed or self.held_back):
            return None
        parts = []
        if self.too_big:
            cap = DOCUMENT_MAX_BYTES // (1024 * 1024)
            names = ", ".join(self.too_big)
            parts.append(
                f"Too big for Telegram ({cap} MB cap), left in outbox: {names}"
            )
        if self.failed:
            parts.append(f"Failed to send: {', '.join(self.failed)}")
        if self.held_back:
            parts.append(
                f"{self.held_back} more file(s) still in outbox — "
                f"at most {MAX_FILES_PER_SWEEP} go per message."
            )
        return "\n".join(parts)


def _claim(path: Path) -> Optional[Path]:
    """Take exclusive ownership of a file, or None if someone else has it."""
    claimed = path.with_name(f"{path.name}{_CLAIM_MARK}{os.getpid()}")
    try:
        os.rename(path, claimed)
    except OSError:
        return None
    return claimed


def _release(claimed: Path, original: Path) -> None:
    """Put a file back under its real name after a failed send."""
    try:
        os.rename(claimed, original)
    except OSError as exc:
        logger.warning(
            "outbox: could not restore name", path=str(claimed), error=str(exc)
        )


async def deliver(message, root: Path = OUTBOX) -> Delivery:
    """Send everything waiting, then delete what went through.

    `message` is the Telegram message to reply to. Never raises: a file that
    cannot be sent is reported, not escalated.
    """
    result = Delivery(sent=[], too_big=[], failed=[])
    budget = MAX_TOTAL_BYTES_PER_SWEEP

    waiting = pending(root)
    for index, path in enumerate(waiting):
        if len(result.sent) >= MAX_FILES_PER_SWEEP:
            result.held_back = len(waiting) - index
            break

        try:
            size = path.stat().st_size
        except OSError:
            continue

        if size > DOCUMENT_MAX_BYTES:
            result.too_big.append(path.name)
            continue

        if size > budget:
            # Enough has gone this turn. The rest keeps until the next one
            # rather than being dropped.
            result.held_back = len(waiting) - index
            break

        claimed = _claim(path)
        if claimed is None:
            continue  # another bot got there first

        try:
            with open(claimed, "rb") as handle:
                if is_photo(path, size):
                    # The handle is open on the claimed name, so the real one
                    # has to be given explicitly or Telegram shows the marker.
                    await message.reply_photo(
                        photo=handle, caption=path.name, filename=path.name
                    )
                else:
                    await message.reply_document(
                        document=handle, filename=path.name
                    )
        except Exception as exc:
            logger.warning(
                "outbox: send failed", path=str(path), error=str(exc)
            )
            result.failed.append(path.name)
            _release(claimed, path)
            continue

        try:
            claimed.unlink()
        except OSError as exc:
            logger.warning(
                "outbox: sent but could not delete", path=str(claimed), error=str(exc)
            )
        result.sent.append(path.name)
        budget -= size
        await asyncio.sleep(0.3)  # stay under Telegram's per-chat rate limit

    return result
