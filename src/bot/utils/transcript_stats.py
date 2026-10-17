"""Context size and model of a session the bot is not currently running.

For the live session the numbers arrive with the reply. For every *other*
session — the ones listed by `/sessions` — they have to come from the
rollout file.

Only the tail is read. Codex appends a ``token_count`` event after each model
call whose ``info.last_token_usage`` is what the model held on that call, and
a ``turn_context`` record naming the model at the start of each turn, so both
answers sit near the end of the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()

_TAIL_BYTES = 512 * 1024

# path -> (size when read, answer)
_cache: Dict[str, Tuple[int, Optional[int]]] = {}
_model_cache: Dict[str, Tuple[int, Optional[str]]] = {}


def tokens_from_usage(usage) -> Optional[int]:
    """Context held on one model call, from a Codex usage dict.

    ``total_tokens`` is what Codex itself measures against the context window;
    input + output is the fallback for payloads that omit it. Cached input is
    already counted inside ``input_tokens``.
    """
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and total > 0:
        return int(total)
    found = 0
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            found += int(value)
    return found or None


def _tail_lines(path: Path, size: int) -> List[bytes]:
    with path.open("rb") as handle:
        if size > _TAIL_BYTES:
            handle.seek(size - _TAIL_BYTES)
            handle.readline()  # drop the partial line we landed inside
        return handle.read().splitlines()


def context_tokens(transcript_path: Optional[str]) -> Optional[int]:
    """Context the model last held in this session, or None if unknown."""
    if not transcript_path:
        return None
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
    except OSError:
        return None

    cached = _cache.get(transcript_path)
    if cached is not None and cached[0] == size:
        return cached[1]

    found: Optional[int] = None
    try:
        lines = _tail_lines(path, size)
    except OSError as exc:
        logger.debug("transcript_stats: unreadable", path=transcript_path, error=str(exc))
        _cache[transcript_path] = (size, None)
        return None

    for line in reversed(lines):
        if b"token_count" not in line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        found = tokens_from_usage(info.get("last_token_usage"))
        if found is not None:
            break

    _cache[transcript_path] = (size, found)
    return found


def last_model(transcript_path: Optional[str]) -> Optional[str]:
    """The model named by the session's latest ``turn_context``, or None."""
    if not transcript_path:
        return None
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
    except OSError:
        return None

    cached = _model_cache.get(transcript_path)
    if cached is not None and cached[0] == size:
        return cached[1]

    found: Optional[str] = None
    try:
        lines = _tail_lines(path, size)
    except OSError:
        _model_cache[transcript_path] = (size, None)
        return None

    for line in reversed(lines):
        if b"turn_context" not in line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "turn_context":
            continue
        payload = entry.get("payload")
        model = payload.get("model") if isinstance(payload, dict) else None
        if isinstance(model, str) and model:
            found = model
            break

    _model_cache[transcript_path] = (size, found)
    return found


def reset_cache() -> None:
    """Forget every remembered answer — for tests."""
    _cache.clear()
    _model_cache.clear()
