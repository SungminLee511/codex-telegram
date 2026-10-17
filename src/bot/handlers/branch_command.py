"""`/branch` — continue this conversation on a copy, leaving the original be.

The copy has the whole history; the original stops where it was. That turns
an experiment you might regret into something you can walk away from.

Unlike the Claude bot (whose CLI forks while handling the next turn), Codex
has no fork flag, so the fork is done here, immediately and for free: the
rollout is copied under a new thread id (`src/codex/fork.py`) and the chat is
pointed at the copy. Nothing is sent to the model, so there is no armed
one-shot flag to forget about and nothing to cancel — `/sessions` goes back.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..utils.session_rename import branch_name

logger = structlog.get_logger()

DEFAULT_LABEL = "temp"


def _carry_settings(context, old_id: str, new_id: str) -> None:
    """Start the branch configured the way its original was.

    A fork that silently reverted to the default model would be a surprise in
    the one place you least want one: a copy you made to try something.
    """
    from .effort_command import SETTING as EFFORT
    from .model_command import SETTING as MODEL

    for setting in (MODEL, EFFORT):
        try:
            chosen = setting.store(context).get(old_id)
            if chosen:
                setting.store(context).set(new_id, chosen)
        except Exception as exc:
            logger.debug(f"branch: could not carry {setting.name}", error=str(exc))


async def fork_current(context, user_id: int, label: str) -> Optional[str]:
    """Fork the chat's session. Returns the reply text (HTML)."""
    from ...codex.fork import ForkError, fork_rollout
    from ..utils.session_rename import set_title
    from .session_picker import adopt_session, get_index

    old_id = context.user_data.get("codex_session_id")
    if not old_id:
        return "🌿 Nothing to branch from yet — this chat has no session."

    index = get_index(context)
    old = index.get(old_id)
    if old is None:
        return (
            "🌿 Can't branch: the rollout for "
            f"<code>{html.escape(old_id[:8])}</code> was not found on disk."
        )

    try:
        new_id, new_path = fork_rollout(Path(old.path), index.sessions_dir, old_id)
    except ForkError as exc:
        logger.warning("branch: fork failed", error=str(exc))
        return f"🌿 Branch failed: {html.escape(str(exc))}"

    name = branch_name(old.display_name, label)
    set_title(str(new_path), new_id, name)
    _carry_settings(context, old_id, new_id)

    settings = context.bot_data.get("settings")
    project = Path(old.cwd) if old.cwd else getattr(settings, "approved_directory", Path("."))
    await adopt_session(context, user_id, new_id, project, old.user_turns)

    logger.info("Branched", old_session=old_id, new_session=new_id, label=label)
    return (
        f"🌿 Branched to <b>{html.escape(name)}</b>\n"
        f"Now on <code>{html.escape(new_id[:8])}</code>; "
        f"<code>{html.escape(old_id[:8])}</code> is untouched — "
        "<code>/sessions</code> to go back."
    )


async def branch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fork the current session now and switch this chat to the copy."""
    label = " ".join(context.args or []).strip() or DEFAULT_LABEL
    if label.lower() in ("cancel", "off", "abort"):
        await update.message.reply_text(
            "🌿 Branches happen immediately, so there is nothing to cancel — "
            "use /sessions to go back to the original."
        )
        return
    user_id = update.effective_user.id if update.effective_user else 0
    text = await fork_current(context, user_id, label)
    await update.message.reply_text(text, parse_mode="HTML")
