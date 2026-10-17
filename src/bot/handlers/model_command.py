"""`/model` — choose which model answers in this session.

Scope is the session, so switching to Luna for a cheap chore does not quietly
follow you into the next piece of real work. The chosen model is remembered on
disk and comes back with the session after a restart, and is passed to Codex
as ``-m <model>`` on every turn (resumed ones included).
"""

from __future__ import annotations

import html
from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..utils import models
from .session_choice import SessionSetting

logger = structlog.get_logger()

SETTING = SessionSetting("model", "codex_model")
PENDING_KEY = SETTING.pending_key


def get_store(context):
    return SETTING.store(context)


def current_model_id(context) -> Optional[str]:
    return SETTING.current(context)


def bind_pending(context, session_id: Optional[str]) -> None:
    SETTING.bind_pending(context, session_id)


def _listing(context) -> str:
    """The menu, marking whichever model is in force right now."""
    active = SETTING.effective(context)
    lines = ["🧠 <b>Model</b>", ""]
    for choice in models.CHOICES:
        mark = "▸" if choice.alias == active else " "
        lines.append(
            f"{mark} <code>{choice.alias}</code> — {html.escape(choice.note)}"
        )
    if active and active not in {c.alias for c in models.CHOICES}:
        lines.append(f"▸ <code>{html.escape(active)}</code> — typed name")
    lines.append("")
    lines.append(
        "Tap one, or send <code>/model &lt;exact-name&gt;</code> for any "
        "other model."
    )
    return "\n".join(lines)


def _keyboard(context) -> InlineKeyboardMarkup:
    """One button per family, the active one marked."""
    active = SETTING.effective(context)
    rows = []
    for choice in models.CHOICES:
        mark = "▸ " if choice.alias == active else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{choice.alias}", callback_data=f"model:{choice.alias}"
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch model from a tapped button."""
    query = update.callback_query
    if query is None or not query.data:
        return
    alias = query.data.split(":", 1)[1]
    choice = models.resolve(alias)
    if choice is None:
        await query.answer("Unknown model.")
        return

    session_id = SETTING.choose(context, choice.alias)
    await query.answer(f"Model: {choice.alias}")
    try:
        await query.edit_message_text(_listing(context), parse_mode="HTML",
                                      reply_markup=_keyboard(context))
    except Exception:
        logger.debug("model: could not redraw the list")
    logger.info("Model switched by button", model=choice.alias,
                session_id=session_id)


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the model list, or switch the current session to one of them."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            _listing(context), parse_mode="HTML", reply_markup=_keyboard(context)
        )
        return

    wanted = args[0].strip()
    checking = None
    choice = models.resolve(wanted)
    if choice is not None:
        model_id, label, warning = choice.alias, choice.alias, ""
    else:
        if not models.is_wellformed(wanted):
            await update.message.reply_text(
                f"❓ <code>{html.escape(wanted[:60])}</code> is not a model name.\n\n"
                + _listing(context),
                parse_mode="HTML",
            )
            return
        # No live probe: checking would spend a request. Say so instead.
        model_id, label = wanted, models.label_for(wanted) or wanted
        warning = (
            "\n⚠️ Not one of the listed models, so it is unverified; "
            "if the next reply errors, pick another."
        )

    session_id = SETTING.choose(context, model_id)
    where = (
        f"session <code>{html.escape(session_id[:8])}</code>"
        if session_id
        else "the session your next message starts"
    )

    logger.info(
        "Model switched",
        model=model_id,
        session_id=session_id,
        user_id=update.effective_user.id if update.effective_user else None,
    )
    await _say(
        checking,
        update,
        f"🧠 Now <b>{html.escape(label)}</b> for {where}.{warning}",
    )


async def _say(placeholder, update: Update, text: str) -> None:
    """Edit the placeholder message if there is one, else reply."""
    if placeholder is not None:
        try:
            await placeholder.edit_text(text, parse_mode="HTML")
            return
        except Exception:
            pass
    await update.message.reply_text(text, parse_mode="HTML")
