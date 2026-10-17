"""`/rename <name>` — name the chat's current session.

In the Claude bot this is forwarded to the CLI's own `/rename`. Codex exec has
no such command, so the bot stores the name itself (`session_rename`), where
`/sessions` and the reply banner read it. Bare `/rename` shows the name.
"""

from __future__ import annotations

import html

import structlog
from telegram import Update
from telegram.ext import ContextTypes

logger = structlog.get_logger()


async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from ..utils.session_rename import get_title, set_title
    from .session_picker import get_index

    session_id = context.user_data.get("codex_session_id")
    if not session_id:
        await update.message.reply_text(
            "✏️ No session yet — send a message first, then /rename it."
        )
        return

    name = " ".join(context.args or []).strip()
    if not name:
        current = get_title(session_id)
        shown = html.escape(current) if current else "<i>(none — first prompt is shown)</i>"
        await update.message.reply_text(
            f"✏️ Name: {shown}\nUsage: <code>/rename &lt;new name&gt;</code>",
            parse_mode="HTML",
        )
        return

    info = None
    try:
        info = get_index(context).get(session_id)
    except Exception as exc:  # naming does not need the index
        logger.debug("rename: index lookup failed", error=str(exc))

    if set_title(info.path if info else None, session_id, name):
        await update.message.reply_text(
            f"✏️ Renamed to <b>{html.escape(' '.join(name.split()))}</b>.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("✏️ Could not save the name.")
