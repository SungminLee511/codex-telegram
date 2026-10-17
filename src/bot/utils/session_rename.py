"""Give a session a name, the way `/rename` does.

Codex rollouts have no title record, and appending a foreign record type to
a file the CLI also parses on resume is a risk not worth taking. So names
live bot-side in one small JSON map shared by every Codex bot on the host
(`data/session_titles.json`, keyed by thread id); the session index reads it.

Writes go tmp-then-rename so a reader never sees half a file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

import structlog

logger = structlog.get_logger()

TITLES_PATH = Path("data") / "session_titles.json"


def _read(path: Path) -> Dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v}


def get_title(session_id: Optional[str], path: Optional[Path] = None) -> Optional[str]:
    """The name set for this session, or None."""
    if not session_id:
        return None
    return _read(path or TITLES_PATH).get(session_id)


def set_title(
    transcript_path: Optional[str],
    session_id: str,
    name: str,
    path: Optional[Path] = None,
) -> bool:
    """Name a session. Returns whether the name was stored.

    `transcript_path` is kept for parity with the Claude bot's signature: a
    name is only stored for a session whose rollout exists, so a typo'd id
    never shows up in the list as a session that never was.
    """
    cleaned = " ".join((name or "").split())
    if not session_id or not cleaned:
        return False
    if transcript_path is not None and not Path(transcript_path).is_file():
        logger.debug("session_rename: no such rollout", path=transcript_path)
        return False

    target = path or TITLES_PATH
    titles = _read(target)
    titles[session_id] = cleaned
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(titles, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        logger.warning("session_rename: could not store title", error=str(exc))
        return False
    logger.info("Session renamed", session_id=session_id, name=cleaned)
    return True


def branch_name(original: str, label: str) -> str:
    """`telegram 마개조` + `fast test` -> `telegram 마개조_(branch: fast test)`.

    A branch of a branch keeps stacking, which is honest: the name says how
    far from the original you are.
    """
    label = " ".join((label or "").split()) or "temp"
    return f"{original}_(branch: {label})"
