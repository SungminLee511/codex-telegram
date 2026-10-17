"""How many times a session has been summarised to fit in the context window.

When the window fills, Codex compacts: it writes a summary of the
conversation so far and continues from that instead of the full history. Each
one discards detail, so the count is worth seeing — a session on its fifth
compaction is a session that has forgotten most of what it did.

The marker is a top-level `{"type":"compacted", ...}` rollout item in the
Codex session file (~/.codex/sessions/.../rollout-*.jsonl); Codex writes one
per compaction. The leading quote in the pattern keeps `context_compacted`
event messages from being counted a second time. (Format assumed from the
Codex RolloutItem::Compacted serialisation; verify on a live rollout.)

Transcripts reach tens of megabytes, and the banner is drawn after every
reply, so the file is never re-read from the top. Only the bytes appended
since the last look are scanned, and the running count is kept per path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import structlog

logger = structlog.get_logger()

_MARKER = re.compile(rb'"type"\s*:\s*"compacted"')


@dataclass
class _Progress:
    """Where the last scan of one transcript stopped, and what it had found."""

    offset: int = 0
    count: int = 0


_seen: Dict[str, _Progress] = {}


def count(transcript_path: Optional[str]) -> Optional[int]:
    """Compactions so far in this transcript, or None if it cannot be read."""
    if not transcript_path:
        return None

    progress = _seen.get(transcript_path)
    if progress is None:
        progress = _Progress()
        _seen[transcript_path] = progress

    try:
        path = Path(transcript_path)
        size = path.stat().st_size
        if size < progress.offset:
            # The file shrank, so it is not the one we were counting: start
            # again rather than report a count for a file that no longer is.
            progress.offset = 0
            progress.count = 0
        if size > progress.offset:
            with path.open("rb") as handle:
                handle.seek(progress.offset)
                chunk = handle.read(size - progress.offset)
            # A trailing partial line is left for next time; counting it now
            # would risk counting it twice once the rest arrives.
            cut = chunk.rfind(b"\n")
            if cut == -1:
                return progress.count or None
            progress.count += len(_MARKER.findall(chunk[: cut + 1]))
            progress.offset += cut + 1
    except OSError as exc:
        logger.debug(
            "compactions: unreadable transcript",
            path=transcript_path,
            error=str(exc),
        )
        return progress.count or None

    return progress.count


def reset_cache() -> None:
    """Forget every running count — for tests."""
    _seen.clear()
