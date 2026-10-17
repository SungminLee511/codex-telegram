"""When a session began, read from the first line of its rollout (session_meta carries a timestamp).

Nothing in the bot records this: the session row is created when the bot first
hears about a session, which for a resumed one is long after it started. The
transcript is the only honest source — its first line is the first thing that
ever happened in that conversation, and it carries a timestamp.

Only the opening of the file is read, and the answer is remembered: a session's
start does not change, so one small read per session is the whole cost.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import structlog

logger = structlog.get_logger()

# Entries at the top of a transcript that may precede any timestamped one.
_MAX_LINES_SCANNED = 20

_cache: Dict[str, Optional[float]] = {}


def _parse(raw) -> Optional[float]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def started_at(transcript_path: Optional[str]) -> Optional[float]:
    """Unix seconds of the first timestamped entry, or None.

    A failure is cached as well as a success: a transcript that cannot be read
    now will not become readable on the next reply either, and the banner must
    not pay for the attempt every time.
    """
    if not transcript_path:
        return None
    if transcript_path in _cache:
        return _cache[transcript_path]

    found: Optional[float] = None
    try:
        with Path(transcript_path).open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= _MAX_LINES_SCANNED:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    found = _parse(entry.get("timestamp"))
                    if found is not None:
                        break
    except OSError as exc:
        logger.debug(
            "session_start: unreadable transcript",
            path=transcript_path,
            error=str(exc),
        )

    _cache[transcript_path] = found
    return found


def reset_cache() -> None:
    """Forget what has been read — for tests."""
    _cache.clear()
