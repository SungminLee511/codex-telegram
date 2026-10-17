"""Session banner appended to Codex's replies.

Purely cosmetic and purely bot-side: the banner is spliced in *after* Codex
has produced its answer, so it never enters the prompt, the transcript, or the
model's context. It exists so a human reading Telegram can tell which of the
many sessions (and which bot) a reply came from.

Shape (style A, as a footer):

    <codex's reply>
    ──────────────
    Analyze the Telegram bot repo architecture
       27 msg · gpt-6-sol · auto · bot2
       5h 8%: 13:40 (38%)
       week 25%: Sun 11:00 (28%)
"""

from __future__ import annotations

import html
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import structlog

from .durations import format_elapsed

logger = structlog.get_logger()

_RULE = "──────────────"
_MAX_TITLE = 58
# Telegram hard-caps a message at 4096 chars; the formatter already splits at
# 4000, so a banner only fits inline when there is headroom.
_TELEGRAM_LIMIT = 4096

_KST = timezone(timedelta(hours=9))
_MAX_WAKE_LINES = 3
_MAX_WAKE_MESSAGE = 44
# A wake whose timer has just run out is still a real process for a moment
# while it writes its file. Keep showing it rather than blinking it away.
_WAKE_GRACE = 5.0


def _esc(text: str) -> str:
    return html.escape(text or "")


def _info_for(context, session_id: str):
    """The indexed transcript record, or None if it cannot be looked up."""
    try:
        from ..handlers.session_picker import get_index

        return get_index(context).get(session_id)
    except Exception as exc:  # index is best-effort decoration
        logger.debug("session_header: index lookup failed", error=str(exc))
        return None


def _started_text(info, now: Optional[float] = None) -> Optional[str]:
    """When this session began, in the user's timezone.

    The date is added once the session is no longer from today — a bare
    `05:33` on a conversation three days old reads as this morning.
    """
    if info is None:
        return None
    try:
        from .session_start import started_at

        begun = started_at(getattr(info, "path", None))
    except Exception as exc:  # decoration only
        logger.debug("session_header: start lookup failed", error=str(exc))
        return None
    if begun is None:
        return None

    moment = datetime.fromtimestamp(begun, tz=_KST)
    today = datetime.fromtimestamp(
        time.time() if now is None else now, tz=_KST
    ).date()
    if moment.date() == today:
        return moment.strftime("%H:%M")
    return moment.strftime("%b %d %H:%M")


def _usage_line() -> Optional[str]:
    """How much of the 5-hour and weekly limits is spent, as meters.

    Account-wide rather than per-chat: the limit is on the subscription, so
    every bot and every session draws from the same pool.
    """
    try:
        from . import usage_limits

        return usage_limits.line()
    except Exception as exc:  # decoration only
        logger.debug("session_header: usage lookup failed", error=str(exc))
        return None


_TOKENS_KEY = "context_tokens"
_TOKENS_STORE_KEY = "_tokens_store"


def _tokens_store(context):
    """Last known context size per session, on disk.

    `user_data` is memory only, so every restart used to drop the count and
    the banner fell back to a message count — a number from a different scale
    appearing without warning. Sessions survive restarts; their size should
    too.
    """
    from pathlib import Path

    from .choice_store import ChoiceStore

    found = context.bot_data.get(_TOKENS_STORE_KEY)
    if found is None:
        settings = context.bot_data.get("settings")
        bot_id = getattr(settings, "bot_id", "main") if settings else "main"
        found = ChoiceStore(Path("data") / f"session_tokens_{bot_id}.json")
        context.bot_data[_TOKENS_STORE_KEY] = found
    return found


def _context_tokens(context, session_id=None, codex_response=None) -> Optional[int]:
    """Context size for this reply, remembered for the ones in between.

    The count arrives with a reply, but the banner is also drawn for messages
    that did not come from Codex, and for the first reply after a restart.
    Both read the remembered value rather than showing nothing.
    """
    tokens = getattr(codex_response, "context_tokens", None)
    if isinstance(tokens, int) and tokens > 0:
        context.user_data[_TOKENS_KEY] = tokens
        if session_id:
            try:
                _tokens_store(context).set(session_id, str(tokens))
            except Exception as exc:
                logger.debug("session_header: token save failed", error=str(exc))
        return tokens

    remembered = context.user_data.get(_TOKENS_KEY)
    if isinstance(remembered, int) and remembered > 0:
        return remembered

    try:
        stored = _tokens_store(context).get(session_id)
        return int(stored) if stored else None
    except Exception as exc:
        logger.debug("session_header: token lookup failed", error=str(exc))
        return None


def _compaction_suffix(info) -> str:
    """` (3)` when the session has been summarised, empty when it has not.

    It rides on the token count because the two are the same story: this is
    how much context is held *now*, and that is how many times the rest was
    thrown away to make room for it.
    """
    if info is None:
        return ""
    try:
        from .compactions import count

        total = count(getattr(info, "path", None))
    except Exception as exc:  # decoration only
        logger.debug("session_header: compaction count failed", error=str(exc))
        return ""
    return f" ({total})" if total else ""


def format_tokens(tokens: int) -> str:
    """`441700` -> `442k tok`; below a thousand the exact number still fits.

    Shared with the session list so both places say the size the same way.
    """
    if tokens < 1000:
        return f"{tokens} tok"
    return f"{tokens / 1000:.0f}k tok"


_format_tokens = format_tokens


def detail_line(
    size: str, model: Optional[str], effort: Optional[str], bot: str
) -> str:
    """`442k tok (3) · gpt-6-sol · auto · bot2` — the banner's detail line.

    One function so the reply banner and the `/sessions` list cannot drift
    apart: both build this line from it. The result is HTML-safe.
    """
    bits = []
    if size:
        bits.append(_esc(size))
    if model:
        bits.append(_esc(model))
    if effort:
        bits.append(_esc(effort))
    bits.append(_esc(bot))
    return " · ".join(bits)


def started_suffix(info, now: Optional[float] = None) -> str:
    """` (14:33)` for a session's start, or empty when it cannot be read."""
    started = _started_text(info, now)
    return f" ({started})" if started else ""


def _effort_label(context) -> Optional[str]:
    """How hard this session thinks — always shown, `auto` included.

    An omitted setting is indistinguishable from a broken one, and `auto` is
    a real choice rather than the absence of one.
    """
    try:
        from ..handlers.effort_command import SETTING
        from .efforts import label_for

        return label_for(SETTING.effective(context))
    except Exception as exc:  # decoration only
        logger.debug("session_header: effort lookup failed", error=str(exc))
        return None


def _model_label(context, codex_response=None) -> Optional[str]:
    """Which model answered, as a short name.

    Priority: what Codex reports actually produced this reply (the rollout's
    turn_context names the real model), else the session's `/model`
    choice, else the configured default. Reported wins so the banner shows
    the truth, not just the alias that was requested.
    """
    try:
        from ..handlers.model_command import current_model_id

        settings = context.bot_data.get("settings")
        configured = getattr(settings, "codex_model", None) if settings else None
        reported = getattr(codex_response, "model_id", None)
        from .models import label_for

        return label_for(reported or current_model_id(context) or configured)
    except Exception as exc:  # decoration only
        logger.debug("session_header: model lookup failed", error=str(exc))
        return None


def build_header(context, codex_response=None) -> Optional[str]:
    """Build the HTML banner for the chat's current session, or None."""
    session_id = context.user_data.get("codex_session_id")
    if not session_id and codex_response is not None:
        session_id = getattr(codex_response, "session_id", None)
    if not session_id:
        return None

    settings = context.bot_data.get("settings")
    bot_id = getattr(settings, "bot_id", "main") if settings else "main"

    # Message count lives on the SessionManager's cached session. Cost is
    # deliberately not shown: Codex only reports the cost of turns this bot
    # process ran, so for a resumed session it is wrong by an unknown margin.
    message_count = None
    codex_integration = context.bot_data.get("codex_integration")
    manager = getattr(codex_integration, "session_manager", None)
    session = None
    if manager is not None:
        session = manager.active_sessions.get(session_id)
    if session is not None:
        message_count = session.message_count

    info = _info_for(context, session_id)
    title = (getattr(info, "display_name", None) if info else None) or (
        "(untitled session)"
    )
    if len(title) > _MAX_TITLE:
        title = title[: _MAX_TITLE - 1] + "…"
    title = f"{title}{started_suffix(info)}"

    # The session id and the working directory are deliberately absent: the
    # title already says which conversation this is, and the directory is
    # almost always the same one, so both were noise on every single reply.
    size = ""
    tokens = _context_tokens(context, session_id, codex_response)
    if tokens:
        size = _format_tokens(tokens) + _compaction_suffix(info)
    elif message_count:
        # Before the first reply of a restarted process there is no token
        # count yet; the turn count is better than an empty space.
        size = f"{message_count} msg"
    model = _model_label(context, codex_response)
    effort = _effort_label(context)

    header = f"<b>{_esc(title)}</b>\n   {detail_line(size, model, effort, bot_id)}"
    usage = _usage_line()
    if usage:
        # One limit per line, each indented to sit under the detail line.
        body = "\n".join(f"   {_esc(line)}" for line in usage.splitlines())
        header = f"{header}\n{body}"
    try:
        wakes = _wake_lines(bot_id)
    except Exception as exc:  # the wake list must never cost us the banner
        logger.debug("session_header: wake scan failed", error=str(exc))
        wakes = None
    return f"{header}\n{wakes}" if wakes else header


def _wake_lines(bot_id: str, now: Optional[float] = None) -> Optional[str]:
    """The wake-ups this bot still has queued, or None if there are none.

    Only this bot's wakes: another bot's queue says nothing about what will
    happen in *this* chat, and the point of the line is to answer "is anything
    going to wake me, and when".
    """
    from . import pending_wakes

    now = time.time() if now is None else now
    found = pending_wakes.scan_quietly(bot_id)
    wakes = [w for w in found if w.seconds_left(now) > -_WAKE_GRACE]
    if not wakes:
        return None

    timed = [w for w in wakes if w.kind != "conditional"]
    watching = [w for w in wakes if w.kind == "conditional"]
    blocks = []
    if timed:
        plural = "wake" if len(timed) == 1 else "wakes"
        lines = [f"   ⏰ {len(timed)} {plural}"]
        for wake in timed[:_MAX_WAKE_LINES]:
            when = _kst_clock(wake.fires_at)
            left = format_elapsed(max(0, wake.seconds_left(now)))
            suffix = _message_suffix(wake.message)
            lines.append(f"      {when} KST ({left}){suffix}")
        hidden = len(timed) - _MAX_WAKE_LINES
        if hidden > 0:
            lines.append(f"      +{hidden} more")
        blocks.append("\n".join(lines))
    if watching:
        # A different symbol: these fire when something becomes true, and the
        # time shown is only the latest they can fire (their timeout).
        lines = [f"   👀 {len(watching)} watching"]
        for wake in watching[:_MAX_WAKE_LINES]:
            left = format_elapsed(max(0, wake.seconds_left(now)))
            name = Path(wake.flag).name if wake.flag else "flag"
            suffix = _message_suffix(wake.message)
            lines.append(f"      {_esc(name)} · timeout {left}{suffix}")
        hidden = len(watching) - _MAX_WAKE_LINES
        if hidden > 0:
            lines.append(f"      +{hidden} more")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _message_suffix(message: str) -> str:
    """` · <message>` shortened to one banner line, or nothing if empty."""
    text = _one_line(message)
    if len(text) > _MAX_WAKE_MESSAGE:
        text = text[: _MAX_WAKE_MESSAGE - 1] + "…"
    return f" · {_esc(text)}" if text else ""


def _one_line(text: str) -> str:
    """Collapse a multi-line wake prompt so it cannot break the banner."""
    return " ".join((text or "").split())


def _kst_clock(unix_seconds: float) -> str:
    """HH:MM in the user's timezone; the server runs on UTC."""
    moment = datetime.fromtimestamp(unix_seconds, tz=_KST)
    return moment.strftime("%H:%M")


def attach_header(messages: List, context, codex_response=None) -> List:
    """Append the banner to the last message of a formatted reply.

    It sits at the end so the answer itself is what you read first; the
    banner is a footer answering "which session was that?". Only the final
    part of a multi-part reply carries it. If that part is already close to
    Telegram's limit the banner is sent as its own short message instead of
    risking a send failure.
    """
    if not messages:
        return messages

    settings = context.bot_data.get("settings")
    if settings is not None and not getattr(settings, "session_header", True):
        return messages

    try:
        header = build_header(context, codex_response)
    except Exception as exc:  # never let decoration break a reply
        logger.debug("session_header: build failed", error=str(exc))
        return messages
    if not header:
        return messages

    footer = f"\n\n{_RULE}\n{header}"
    last = messages[-1]
    if last.parse_mode != "HTML":
        # The formatter emits HTML; anything else is unexpected, so keep the
        # banner separate rather than mixing parse modes.
        return _as_separate_message(messages, header)

    if len(last.text) + len(footer) <= _TELEGRAM_LIMIT:
        last.text = last.text + footer
        return messages

    return _as_separate_message(messages, header)


def _as_separate_message(messages: List, header: str) -> List:
    from .formatting import FormattedMessage

    return messages + [FormattedMessage(header, parse_mode="HTML")]
