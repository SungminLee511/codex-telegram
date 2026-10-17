"""One remembered setting per session — the model it runs on, its effort.

The choice is per *session*, not per chat and not per process: a session is
the thing that has a style of work attached to it, and the same chat moves
between sessions freely (`/new`, `/sessions`, a thread switch). Storing it on
the chat would silently carry an Opus choice into a cheap one-off session.

It lives on disk because `context.user_data` does not survive a restart, and
a choice outliving a restart is the whole point — sessions auto-resume across
restarts, so the setting has to come back with them.

The file is a small JSON object, session id -> value, rewritten atomically.
Old entries are trimmed from the front once there are too many; a session
whose entry is dropped simply falls back to the configured default.

One store per setting, each with its own file, so a model choice and an
effort choice cannot overwrite one another.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

import structlog

logger = structlog.get_logger()

_MAX_ENTRIES = 200


class ChoiceStore:
    """Remembers the model chosen for each session id."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # --- persistence ------------------------------------------------------

    def _read(self) -> Dict[str, str]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            key: value
            for key, value in data.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def _write(self, entries: Dict[str, str]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(entries))
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.debug("choice_store: write failed", error=str(exc))

    # --- api --------------------------------------------------------------

    def get(self, session_id: Optional[str]) -> Optional[str]:
        """The model id chosen for this session, or None for the default."""
        if not session_id:
            return None
        return self._read().get(session_id)

    def all(self) -> Dict[str, str]:
        """Every session's choice, session id -> value."""
        return self._read()

    def set(self, session_id: str, model_id: str) -> None:
        """Pin a session to a model from now on."""
        if not session_id:
            return
        entries = self._read()
        # Re-insert at the end so an actively used session is never the one
        # trimmed away.
        entries.pop(session_id, None)
        entries[session_id] = model_id
        while len(entries) > _MAX_ENTRIES:
            entries.pop(next(iter(entries)))
        self._write(entries)

    def clear(self, session_id: str) -> None:
        """Drop a session's choice, returning it to the configured default."""
        entries = self._read()
        if entries.pop(session_id, None) is not None:
            self._write(entries)
