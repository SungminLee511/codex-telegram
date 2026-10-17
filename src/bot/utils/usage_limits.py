"""How much of the ChatGPT plan's Codex rate limits is already spent.

The Codex CLI learns the limits from response headers and records them in its
own rollouts: every ``token_count`` event carries a ``rate_limits`` object::

    {"type": "event_msg", "timestamp": "...",
     "payload": {"type": "token_count", "info": {...},
                 "rate_limits": {
                     "primary":   {"used_percent": 7.0, "window_minutes": 300,
                                   "resets_in_seconds": 9000},
                     "secondary": {"used_percent": 25.0, "window_minutes": 10080,
                                   "resets_at": 1760000000}}}}

The limits are account-wide, so the newest such event across *all* rollouts
on the host is the current answer — whichever session or bot made the last
call. Nothing is fetched over the network and no credentials are read.

Both reset spellings are accepted (``resets_at`` as unix seconds or ISO text,
or ``resets_in_seconds`` relative to the event timestamp); field names follow
codex-rs and should be checked against a live rollout.

A window whose reset moment has passed is reported as 0%: had anything been
spent since, a newer event would exist.

Nothing here is allowed to fail loudly: no rollouts, an older CLI that does
not record limits, a malformed line — each simply means no usage line.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import structlog

from src.codex.session_index import DEFAULT_SESSIONS_DIR

logger = structlog.get_logger()

SESSIONS_DIR = DEFAULT_SESSIONS_DIR

_DISPLAY_TZ = timezone(timedelta(hours=9))  # KST

FIVE_HOURS = 5 * 60 * 60
SEVEN_DAYS = 7 * 24 * 60 * 60

# How many of the most recently written rollouts to search, and how much of
# each tail. The newest file almost always answers on its own.
_FILES_TO_CHECK = 5
_TAIL_BYTES = 512 * 1024
_CACHE_SECONDS = 10.0


@dataclass(frozen=True)
class Window:
    """One rate-limit window: spend, length and reset moment."""

    percent: float
    window_seconds: Optional[float] = None
    resets_at: Optional[float] = None


@dataclass(frozen=True)
class Usage:
    primary: Optional[Window] = None
    secondary: Optional[Window] = None

    @property
    def known(self) -> bool:
        return self.primary is not None or self.secondary is not None


_cache: Optional[Tuple[float, Usage]] = None


def _iso(raw) -> Optional[float]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _window(raw, event_time: Optional[float]) -> Optional[Window]:
    if not isinstance(raw, dict):
        return None
    percent = raw.get("used_percent")
    if not isinstance(percent, (int, float)):
        return None
    minutes = raw.get("window_minutes")
    length = float(minutes) * 60 if isinstance(minutes, (int, float)) and minutes > 0 else None

    resets: Optional[float] = None
    at = raw.get("resets_at")
    if isinstance(at, (int, float)) and at > 0:
        resets = float(at)
    elif isinstance(at, str):
        resets = _iso(at)
    if resets is None:
        delta = raw.get("resets_in_seconds")
        if isinstance(delta, (int, float)) and event_time is not None:
            resets = event_time + float(delta)
    return Window(percent=float(percent), window_seconds=length, resets_at=resets)


def parse_event(line: bytes) -> Optional[Usage]:
    """Usage from one rollout line, or None if it carries none."""
    if b"rate_limits" not in line:
        return None
    try:
        entry = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(entry, dict):
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        # The exec --json stream may put the object at top level.
        payload = entry
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    event_time = _iso(entry.get("timestamp"))
    usage = Usage(
        primary=_window(limits.get("primary"), event_time),
        secondary=_window(limits.get("secondary"), event_time),
    )
    return usage if usage.known else None


def _newest_rollouts(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    found = []
    for path in root.rglob("rollout-*.jsonl"):
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(reverse=True)
    return [p for _, p in found[:_FILES_TO_CHECK]]


def _from_file(path: Path) -> Optional[Usage]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _TAIL_BYTES:
                handle.seek(size - _TAIL_BYTES)
                handle.readline()
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        usage = parse_event(line)
        if usage is not None:
            return usage
    return None


def read(root: Optional[Path] = None) -> Usage:
    """Newest recorded usage across the host's rollouts, uncached. Never raises."""
    root = SESSIONS_DIR if root is None else root
    try:
        for path in _newest_rollouts(root):
            usage = _from_file(path)
            if usage is not None:
                return usage
    except Exception as exc:
        logger.debug("usage_limits: read failed", error=str(exc))
    return Usage()


def current(
    root: Optional[Path] = None, now: Optional[float] = None, max_age: float = _CACHE_SECONDS
) -> Usage:
    """Cached `read`. Short cache: reading a tail is cheap and replies are slow."""
    global _cache
    now = time.time() if now is None else now
    if _cache is not None and now - _cache[0] < max_age:
        return _cache[1]
    usage = read(root)
    _cache = (now, usage)
    return usage


def remember(usage: Optional[Usage], now: Optional[float] = None) -> None:
    """Seed the cache from a live stream event, so the banner is fresh."""
    global _cache
    if usage is not None and usage.known:
        _cache = (time.time() if now is None else now, usage)


def reset_cache() -> None:
    global _cache
    _cache = None


def _label(window: Window, fallback: str) -> str:
    seconds = window.window_seconds
    if seconds is None:
        return fallback
    if abs(seconds - FIVE_HOURS) < 60:
        return "5h"
    if abs(seconds - SEVEN_DAYS) < 60:
        return "week"
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 60)}m"


def _clock(unix_seconds: float, with_weekday: bool) -> str:
    moment = datetime.fromtimestamp(unix_seconds, tz=_DISPLAY_TZ)
    return moment.strftime("%a %H:%M" if with_weekday else "%H:%M")


def _render_window(window: Optional[Window], fallback: str, now: float) -> Optional[str]:
    """`5h 7%: 13:39 (36%)` — spend, reset time, and pace through the window."""
    if window is None:
        return None
    label = _label(window, fallback)
    resets = window.resets_at
    if resets is not None and window.window_seconds and resets <= now:
        # Rolled over since the event: nothing spent in the new window yet.
        resets += window.window_seconds * (1 + int((now - resets) // window.window_seconds))
        percent = 0.0
    else:
        percent = window.percent
    text = f"{label} {percent:.0f}%"
    if resets is None:
        return text
    with_weekday = (window.window_seconds or 0) >= 86400
    tail = ""
    if window.window_seconds:
        started = resets - window.window_seconds
        fraction = max(0.0, min(1.0, (now - started) / window.window_seconds))
        tail = f" ({fraction * 100:.0f}%)"
    return f"{text}: {_clock(resets, with_weekday)}{tail}"


def render(usage: Usage, now: Optional[float] = None) -> Optional[str]:
    """One limit per line, each with its reset time and pace; None if unknown."""
    if not usage.known:
        return None
    now = time.time() if now is None else now
    lines = [
        line
        for line in (
            _render_window(usage.primary, "5h", now),
            _render_window(usage.secondary, "week", now),
        )
        if line
    ]
    return "\n".join(lines) or None


def line(root: Optional[Path] = None) -> Optional[str]:
    """The banner's usage line, or None when it cannot be worked out."""
    try:
        return render(current(root))
    except Exception as exc:  # decoration must never cost a reply
        logger.debug("usage_limits: render failed", error=str(exc))
        return None
