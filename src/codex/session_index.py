"""On-disk Codex session index.

The bot's SQLite DB only knows about sessions *this* bot process created.
The real transcripts ("rollouts") live in
``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl``
(``CODEX_HOME`` defaults to ``~/.codex``) and are shared by every bot on the
host, so a disk scan is the only way to offer a complete session list.

Each rollout is an append-only JSONL stream of ``{"timestamp", "type",
"payload"}`` records. Useful ones:

* ``session_meta``  -> ``payload.id`` (thread id, what ``codex exec resume``
  takes) and ``payload.cwd``.
* ``turn_context``  -> ``payload.cwd`` / ``payload.model`` for each turn.
* ``event_msg`` with ``payload.type == "user_message"`` -> what the human
  typed (``payload.message``). Codex's injected context (AGENTS.md,
  environment) arrives as ``response_item`` records instead, so reading only
  ``user_message`` events gives clean previews.

Codex writes no generated title, so the display name is a name set with
``/rename`` (kept in a bot-side store, never written into the rollout so the
CLI's own parser is never surprised), else the first prompt.

The scan is **incremental**: the cache keeps the byte offset consumed so far
per file and only parses the tail on rescan.

Format assumptions follow codex-rs ``RolloutItem`` serialisation; verify on a
live ``~/.codex/sessions`` tree.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import structlog

logger = structlog.get_logger()


def codex_home() -> Path:
    """``$CODEX_HOME`` or ``~/.codex`` — where the CLI keeps its state."""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


DEFAULT_SESSIONS_DIR = codex_home() / "sessions"

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_FILE_RE = re.compile(rf"^rollout-.*-({_UUID})\.jsonl$")

# Pre-filter on raw bytes so big tool-output lines are never json-parsed.
_TYPE_RE = re.compile(rb'"type"\s*:\s*"(session_meta|turn_context|user_message)"')

_MAX_LINE_BYTES = 2 * 1024 * 1024
_PREVIEW_CHARS = 300


def session_id_from_path(path: Path) -> Optional[str]:
    """The thread id embedded in a rollout file name, or None."""
    match = _FILE_RE.match(Path(path).name)
    return match.group(1).lower() if match else None


@dataclass
class SessionInfo:
    """One rollout on disk."""

    session_id: str
    path: str
    title: Optional[str] = None
    # A name set by hand with `/rename`; outranks everything else.
    custom_title: Optional[str] = None
    first_prompt: str = ""
    last_prompt: str = ""
    user_turns: int = 0
    cwd: str = ""
    model: str = ""
    size: int = 0
    mtime: float = 0.0
    offset: int = 0
    bot_id: str = ""

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def display_name(self) -> str:
        if self.custom_title:
            return self.custom_title
        if self.title:
            return self.title
        if self.first_prompt:
            return self.first_prompt[:60]
        return f"(untitled {self.short_id})"

    def age_text(self, now: Optional[float] = None) -> str:
        now = now if now is not None else time.time()
        delta = max(0.0, now - self.mtime)
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{int(delta // 60)}m ago"
        if delta < 86400:
            return f"{int(delta // 3600)}h ago"
        return f"{int(delta // 86400)}d ago"

    def size_text(self) -> str:
        if self.size < 1024:
            return f"{self.size} B"
        if self.size < 1024 * 1024:
            return f"{self.size / 1024:.0f} KB"
        return f"{self.size / 1024 / 1024:.1f} MB"


def _clean_preview(text: str) -> str:
    text = text.strip()
    if text.startswith("<"):
        closing = text.find(">")
        if 0 < closing < 120:
            text = text[closing + 1 :].strip()
    return " ".join(text.split())[:_PREVIEW_CHARS]


def _user_text(payload: dict) -> str:
    message = payload.get("message")
    return message if isinstance(message, str) else ""


def _consume(path: Path, info: SessionInfo) -> SessionInfo:
    """Parse the unread tail of `path` into `info` (mutates and returns it)."""
    try:
        stat = path.stat()
    except OSError:
        return info

    if stat.st_size < info.offset:
        info.offset = 0
        info.user_turns = 0
        info.first_prompt = ""
        info.last_prompt = ""
        info.cwd = ""

    try:
        with open(path, "rb") as handle:
            handle.seek(info.offset)
            data = handle.read()
    except OSError as exc:
        logger.debug("session_index: unreadable rollout", path=str(path), error=str(exc))
        return info

    if data:
        lines = data.split(b"\n")
        complete = lines[:-1]
        consumed = len(data) - len(lines[-1])

        for raw in complete:
            if not raw or len(raw) > _MAX_LINE_BYTES or not _TYPE_RE.search(raw):
                continue
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            rtype = record.get("type")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue

            if rtype == "session_meta":
                cwd = payload.get("cwd")
                # First cwd wins: the workspace the session started in.
                if isinstance(cwd, str) and cwd and not info.cwd:
                    info.cwd = cwd
            elif rtype == "turn_context":
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd and not info.cwd:
                    info.cwd = cwd
                model = payload.get("model")
                if isinstance(model, str) and model:
                    info.model = model
            elif rtype == "event_msg" and payload.get("type") == "user_message":
                text = _clean_preview(_user_text(payload))
                if text:
                    info.user_turns += 1
                    if not info.first_prompt:
                        info.first_prompt = text
                    info.last_prompt = text

        info.offset += consumed

    info.size = stat.st_size
    info.mtime = stat.st_mtime
    return info


TitleLookup = Callable[[str], Optional[str]]


class SessionIndex:
    """Disk-backed, incrementally-updated index of Codex rollouts."""

    def __init__(
        self,
        cache_path: Path,
        sessions_dir: Path = DEFAULT_SESSIONS_DIR,
        custom_titles: Optional[TitleLookup] = None,
    ):
        self.cache_path = Path(cache_path)
        self.sessions_dir = Path(sessions_dir)
        self._custom_titles = custom_titles
        self._cache: Dict[str, SessionInfo] = {}
        self._loaded = False

    # Kept for parity with the Claude bot's attribute name.
    @property
    def projects_dir(self) -> Path:
        return self.sessions_dir

    def _load_cache(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for sid, payload in (raw.get("sessions") or {}).items():
            try:
                self._cache[sid] = SessionInfo(**payload)
            except TypeError:
                continue

    def _save_cache(self) -> None:
        payload = {
            "version": 1,
            "sessions": {sid: asdict(info) for sid, info in self._cache.items()},
        }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            logger.debug("session_index: cache write failed", error=str(exc))

    def _apply_title(self, info: SessionInfo) -> SessionInfo:
        if self._custom_titles is not None:
            try:
                info.custom_title = self._custom_titles(info.session_id) or None
            except Exception as exc:  # decoration only
                logger.debug("session_index: title lookup failed", error=str(exc))
        return info

    def _rollout_paths(self) -> List[Path]:
        if not self.sessions_dir.is_dir():
            return []
        return sorted(self.sessions_dir.rglob("rollout-*.jsonl"))

    def refresh(self) -> List[SessionInfo]:
        """Rescan rollouts (incrementally) and return them, newest first."""
        self._load_cache()
        seen = set()
        changed = False

        for path in self._rollout_paths():
            session_id = session_id_from_path(path)
            if not session_id:
                continue
            seen.add(session_id)
            try:
                stat = path.stat()
            except OSError:
                continue

            info = self._cache.get(session_id)
            if info is None:
                info = SessionInfo(session_id=session_id, path=str(path))
                self._cache[session_id] = info
            else:
                info.path = str(path)
                if stat.st_size == info.size and stat.st_mtime == info.mtime:
                    continue
            _consume(path, info)
            changed = True

        for stale in [sid for sid in self._cache if sid not in seen]:
            del self._cache[stale]
            changed = True

        if changed:
            self._save_cache()

        for info in self._cache.values():
            self._apply_title(info)
        return sorted(self._cache.values(), key=lambda i: i.mtime, reverse=True)

    def find_path(self, session_id: str) -> Optional[Path]:
        """Locate the rollout file for a thread id without a full refresh."""
        if not self.sessions_dir.is_dir():
            return None
        matches = list(self.sessions_dir.rglob(f"rollout-*-{session_id}.jsonl"))
        return matches[0] if matches else None

    def get(self, session_id: str, refresh: bool = True) -> Optional[SessionInfo]:
        """Look up one session, refreshing just that rollout."""
        self._load_cache()
        info = self._cache.get(session_id)
        if info is None:
            path = self.find_path(session_id)
            if path is None:
                return None
            info = SessionInfo(session_id=session_id, path=str(path))
            self._cache[session_id] = info
            _consume(path, info)
            self._save_cache()
            return self._apply_title(info)

        if refresh:
            path = Path(info.path)
            try:
                stat = path.stat()
            except OSError:
                return self._apply_title(info)
            if stat.st_size != info.size or stat.st_mtime != info.mtime:
                _consume(path, info)
                self._save_cache()
        return self._apply_title(info)

    def search(self, query: str) -> List[SessionInfo]:
        """Case-insensitive match over names and prompt previews."""
        sessions = self.refresh()
        if not query:
            return sessions
        needle = query.lower()
        return [
            s
            for s in sessions
            if needle in (s.custom_title or "").lower()
            or needle in s.first_prompt.lower()
            or needle in s.last_prompt.lower()
            or needle in s.session_id.lower()
        ]


def tail_messages(path: Path, limit: int = 10) -> List[Dict[str, str]]:
    """The last `limit` user/assistant text turns of a rollout.

    User turns come from ``user_message`` events and replies from
    ``agent_message`` events — the same text the human saw.
    """
    chunk = 512 * 1024
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            handle.seek(max(0, size - chunk))
            data = handle.read()
    except OSError:
        return []

    out: List[Dict[str, str]] = []
    for raw in data.split(b"\n"):
        if not raw or len(raw) > _MAX_LINE_BYTES:
            continue
        if b"user_message" not in raw and b"agent_message" not in raw:
            continue
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind == "user_message":
            role = "user"
        elif kind == "agent_message":
            role = "assistant"
        else:
            continue
        text = _clean_preview(_user_text(payload))
        if text:
            out.append({"role": role, "text": text, "ts": record.get("timestamp", "")})

    return out[-limit:]
