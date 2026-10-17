"""The bookkeeping shared by `/model` and `/effort`.

Both commands do the same three things and differ only in what they are
choosing between: look up what this session is set to, record a new choice,
and hold a choice made before the session existed until it has an id to
attach to. That plumbing lives here once; the commands keep their own lists,
their own wording, and their own rules about what may be selected.

Picking a setting before the session exists (the very first message in a fresh
chat) has to work, because that is exactly when you would do it. The value
waits in `user_data` and is bound to the session id as soon as Codex reports
one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import structlog

from ..utils.choice_store import ChoiceStore

logger = structlog.get_logger()


class SessionSetting:
    """One per-session setting: where it is stored and how it is read.

    `name` names the setting in logs and in the data file. `fallback_attr` is
    the settings field holding the configured default, used when a session has
    made no choice of its own.
    """

    def __init__(self, name: str, fallback_attr: str):
        self.name = name
        self.fallback_attr = fallback_attr
        self._store_key = f"_{name}_store"
        self.pending_key = f"pending_{name}"

    # --- storage ----------------------------------------------------------

    def store(self, context) -> ChoiceStore:
        """The shared per-bot store, created on first use."""
        found = context.bot_data.get(self._store_key)
        if found is None:
            bot_id = self.bot_id(context)
            found = ChoiceStore(Path("data") / f"session_{self.name}s_{bot_id}.json")
            context.bot_data[self._store_key] = found
        return found

    @staticmethod
    def bot_id(context) -> str:
        settings = context.bot_data.get("settings")
        return getattr(settings, "bot_id", "main") if settings else "main"

    # --- reading ----------------------------------------------------------

    def current(self, context) -> Optional[str]:
        """What the next request should use, or None for the configured one.

        A pending choice wins: it was made more recently than anything on
        disk, and it has simply not had a session id to attach to yet.
        """
        pending = context.user_data.get(self.pending_key)
        if pending:
            return pending
        try:
            return self.store(context).get(context.user_data.get("codex_session_id"))
        except Exception as exc:  # a broken store must not block the request
            logger.debug(f"{self.name}: lookup failed", error=str(exc))
            return None

    def configured(self, context) -> Optional[str]:
        """The default from the environment, when the session has no choice."""
        settings = context.bot_data.get("settings")
        return getattr(settings, self.fallback_attr, None) if settings else None

    def effective(self, context) -> Optional[str]:
        """What is actually in force: the session's choice, else the default."""
        return self.current(context) or self.configured(context)

    # --- writing ----------------------------------------------------------

    def choose(self, context, value: str) -> Optional[str]:
        """Record a choice. Returns the session id it was attached to, if any.

        None means there is no session yet and the value is waiting instead.
        """
        session_id = context.user_data.get("codex_session_id")
        if session_id:
            self.store(context).set(session_id, value)
            context.user_data[self.pending_key] = None
            return session_id
        context.user_data[self.pending_key] = value
        return None

    def clear(self, context) -> None:
        """Forget this session's choice, back to the configured default."""
        context.user_data[self.pending_key] = None
        session_id = context.user_data.get("codex_session_id")
        if not session_id:
            return
        try:
            self.store(context).clear(session_id)
        except Exception as exc:
            logger.debug(f"{self.name}: clear failed", error=str(exc))

    def bind_pending(self, context, session_id: Optional[str]) -> None:
        """Attach a choice made before the session existed to that session."""
        pending = context.user_data.get(self.pending_key)
        if not pending or not session_id:
            return
        try:
            self.store(context).set(session_id, pending)
        except Exception as exc:
            logger.debug(f"{self.name}: bind failed", error=str(exc))
            return
        context.user_data[self.pending_key] = None
