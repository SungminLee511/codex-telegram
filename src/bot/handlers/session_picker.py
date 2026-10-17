"""`/sessions` — browse every Codex session on disk and switch to one.

Scope is deliberately *all* sessions, not just the ones this bot process
created: rollouts are shared across bots (`~/.codex/sessions/YYYY/MM/DD/`),
so the picker lists everything and labels which bot's DB knows each one.

Switching sessions is two steps:

1. Point `context.user_data["codex_session_id"]` at the chosen id.
2. **Insert a DB row for it** — `SessionManager.get_or_create_session()`
   falls back to creating a brand-new empty session when the id is unknown
   to storage, which would silently start a fresh conversation instead of
   resuming. Writing the row first is what makes `codex exec resume` fire.
"""

from __future__ import annotations

import html
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, List, Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...codex.session import CodexSession
from ...codex.session_index import SessionIndex, SessionInfo, tail_messages

logger = structlog.get_logger()

PAGE_SIZE = 10
# Default listing depth. The disk holds every session ever run on this host,
# which is far more than anyone wants to scroll; `/sessions all` lifts the cap.
MAX_SESSIONS = 10
_INDEX_KEY = "_session_index"
_LIST_KEY = "sessions_list"
_QUERY_KEY = "sessions_query"
_ALL_KEY = "sessions_show_all"
_HIDDEN_KEY = "sessions_show_hidden"
_PENDING_HIDE_KEY = "sessions_pending_hide"

# --- which bot owns a session -------------------------------------------------
#
# Transcripts are shared by every bot on the host, but each bot keeps its own
# SQLite DB (`data/bot.db` for `main`, `data/bot_<id>.db` otherwise). A session
# id present in one of those DBs is the only record of which bot ran it, so the
# owner map is built by reading every DB read-only.

DB_DIR = Path("data")
_OWNER_CACHE_KEY = "_session_owners"
_OWNER_TTL = 60.0


def _db_bot_id(path: Path) -> str:
    name = path.stem  # "bot" or "bot_bot2"
    return name[4:] if name.startswith("bot_") else "main"


def _scan_owners(db_dir: Optional[Path] = None) -> Dict[str, str]:
    """Map session_id -> bot_id by reading every bot DB read-only."""
    db_dir = db_dir or DB_DIR
    owners: Dict[str, str] = {}
    if not db_dir.is_dir():
        return owners
    for path in sorted(db_dir.glob("bot*.db")):
        bot_id = _db_bot_id(path)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute("select session_id from sessions").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for (session_id,) in rows:
            # First DB wins; alphabetical order makes that deterministic.
            owners.setdefault(session_id, bot_id)
    return owners


def session_owners(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, str]:
    """Cached owner map (re-scanned at most once a minute)."""
    cached = context.bot_data.get(_OWNER_CACHE_KEY)
    now = time.time()
    if cached and now - cached[0] < _OWNER_TTL:
        return cached[1]
    try:
        owners = _scan_owners()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("session owner scan failed", error=str(exc))
        owners = cached[1] if cached else {}
    context.bot_data[_OWNER_CACHE_KEY] = (now, owners)
    return owners


# --- hidden sessions -----------------------------------------------------------
#
# "Hiding" only flags an id; the transcript stays on disk and can be unhidden
# via `/sessions hidden`. The flag file is shared by every bot on the host,
# since the transcripts themselves are shared.

HIDDEN_FILE_NAME = "hidden_sessions.json"


def _hidden_path() -> Path:
    return DB_DIR / HIDDEN_FILE_NAME


def load_hidden() -> set:
    try:
        data = json.loads(_hidden_path().read_text())
    except (OSError, ValueError):
        return set()
    return {str(x) for x in data} if isinstance(data, list) else set()


def _save_hidden(ids: set) -> None:
    path = _hidden_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(ids)))
    tmp.replace(path)


def set_hidden(session_id: str, hidden: bool) -> None:
    ids = load_hidden()
    if hidden:
        ids.add(session_id)
    else:
        ids.discard(session_id)
    _save_hidden(ids)


def get_index(context: ContextTypes.DEFAULT_TYPE) -> SessionIndex:
    """One SessionIndex per bot process, cached in bot_data."""
    index = context.bot_data.get(_INDEX_KEY)
    if index is None:
        settings = context.bot_data.get("settings")
        bot_id = getattr(settings, "bot_id", "main") if settings else "main"
        cache_path = Path("data") / f"session_index_{bot_id}.json"
        from ..utils.session_rename import get_title

        index = SessionIndex(cache_path=cache_path, custom_titles=get_title)
        context.bot_data[_INDEX_KEY] = index
    return index


def _esc(text: str) -> str:
    return html.escape(text or "")


def _owner_text(owners: Optional[Dict[str, str]], session_id: str) -> str:
    if not owners:
        return "bot ?"
    return f"bot {owners.get(session_id, '?')}"


def _started_suffix(info: SessionInfo) -> str:
    """` (14:33)` — when the session began, same shape as the reply banner."""
    from ..utils.session_header import started_suffix

    return started_suffix(info)


def _size_text(info: SessionInfo) -> str:
    """`442k tok (3)`, the same measure the banner shows for the live session.

    Falls back to the turn count when the transcript cannot be read for one,
    since an entry with no second line at all reads as broken.
    """
    from ..utils.compactions import count
    from ..utils.session_header import format_tokens
    from ..utils.transcript_stats import context_tokens

    try:
        tokens = context_tokens(info.path)
        compactions = count(info.path) or 0
    except Exception as exc:  # a listing must not die over decoration
        logger.debug("session_picker: size lookup failed", error=str(exc))
        return f"{info.user_turns} msg"

    if not tokens:
        return f"{info.user_turns} msg"
    suffix = f" ({compactions})" if compactions else ""
    return f"{format_tokens(tokens)}{suffix}"


def _saved_choices(kind: str) -> Dict[str, str]:
    """Every bot's remembered per-session `model` / `effort`, merged.

    Transcripts are shared but each bot keeps its own choice files, so a
    session run by another bot is only found by reading all of them.
    """
    from ..utils.choice_store import ChoiceStore

    merged: Dict[str, str] = {}
    try:
        paths = sorted(DB_DIR.glob(f"session_{kind}s_*.json"))
    except OSError:
        return merged
    for path in paths:
        for session_id, value in ChoiceStore(path).all().items():
            merged.setdefault(session_id, value)
    return merged


def _defaults(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Optional[str]]:
    """This bot's configured model and effort, for sessions with no choice."""
    settings = context.bot_data.get("settings")
    return {
        "model": getattr(settings, "codex_model", None) if settings else None,
        "effort": getattr(settings, "codex_reasoning_effort", None) if settings else None,
    }


def _detail_bits(
    info: SessionInfo,
    owners: Optional[Dict[str, str]],
    defaults: Optional[Dict[str, Optional[str]]] = None,
    models_by_session: Optional[Dict[str, str]] = None,
    efforts_by_session: Optional[Dict[str, str]] = None,
) -> str:
    """The session's detail line, exactly as the reply banner words it.

    size (compactions) · model · effort · bot. The model is the one that wrote
    the last reply, the same thing the banner reports; failing that the
    session's saved choice, then the configured default.
    """
    from ..utils import efforts, models
    from ..utils.session_header import detail_line
    from ..utils.transcript_stats import last_model

    defaults = defaults or {}
    sid = info.session_id
    try:
        actual = last_model(info.path)
    except Exception as exc:  # a listing must not die over decoration
        logger.debug("session_picker: model lookup failed", error=str(exc))
        actual = None
    model = models.label_for(
        actual or (models_by_session or {}).get(sid) or defaults.get("model")
    )
    effort = efforts.label_for(
        (efforts_by_session or {}).get(sid) or defaults.get("effort")
    )
    bot = (owners or {}).get(sid, "?")
    return detail_line(_size_text(info), model, effort, bot)


def _page_text(
    sessions: List[SessionInfo],
    page: int,
    total_pages: int,
    current_id: Optional[str],
    query: str,
    owners: Optional[Dict[str, str]] = None,
    hidden_mode: bool = False,
    defaults: Optional[Dict[str, Optional[str]]] = None,
) -> str:
    title = "🙈 <b>Hidden sessions</b>" if hidden_mode else "🗂 <b>Sessions</b>"
    header = f"{title} — page {page + 1}/{max(1, total_pages)}"
    if query:
        header += f"  (filter: <code>{_esc(query)}</code>)"
    lines = [header, ""]

    models_by_session = _saved_choices("model")
    efforts_by_session = _saved_choices("effort")

    start = page * PAGE_SIZE
    for offset, info in enumerate(sessions[start : start + PAGE_SIZE], start=1):
        marker = "⭐ " if info.session_id == current_id else ""
        lines.append(
            f"<b>{offset}</b> {marker}{_esc(info.display_name[:60])}"
            f"{_esc(_started_suffix(info))}"
        )
        lines.append(
            "    "
            + _detail_bits(
                info, owners, defaults, models_by_session, efforts_by_session
            )
        )
    if not sessions:
        lines.append("<i>No sessions found.</i>")
    return "\n".join(lines)


def _page_keyboard(
    sessions: List[SessionInfo], page: int, total_pages: int
) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    visible = sessions[start : start + PAGE_SIZE]

    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for offset in range(len(visible)):
        row.append(
            InlineKeyboardButton(
                str(offset + 1), callback_data=f"sess:open:{start + offset}"
            )
        )
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"sess:page:{page - 1}"))
    nav.append(InlineKeyboardButton("🔄", callback_data=f"sess:page:{page}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("▶", callback_data=f"sess:page:{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


def _load_sessions(
    context: ContextTypes.DEFAULT_TYPE,
    query: str = "",
    show_all: bool = False,
    hidden_mode: bool = False,
) -> List[SessionInfo]:
    index = get_index(context)
    sessions = index.search(query) if query else index.refresh()
    hidden = load_hidden()
    if hidden_mode:
        # Hidden view: only flagged sessions, never capped (it is a short list).
        sessions = [s for s in sessions if s.session_id in hidden]
    else:
        # Filter before capping, so hiding one lets the next-oldest move up.
        sessions = [s for s in sessions if s.session_id not in hidden]
        if not show_all:
            sessions = sessions[:MAX_SESSIONS]
    context.user_data[_LIST_KEY] = [s.session_id for s in sessions]
    context.user_data[_QUERY_KEY] = query
    context.user_data[_ALL_KEY] = show_all
    context.user_data[_HIDDEN_KEY] = hidden_mode
    return sessions


def _sessions_for_page(context: ContextTypes.DEFAULT_TYPE) -> List[SessionInfo]:
    """Re-resolve the list behind the current keyboard.

    The keyboard encodes list *positions*, so the list must be rebuilt the
    same way it was built when the keyboard was drawn (same filter and cap).
    """
    return _load_sessions(
        context,
        context.user_data.get(_QUERY_KEY, ""),
        context.user_data.get(_ALL_KEY, False),
        context.user_data.get(_HIDDEN_KEY, False),
    )


async def sessions_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """`/sessions [all | hidden | search text]` — show page 1 of the list."""
    args = list(context.args or [])
    first = args[0].lower() if args else ""
    show_all = first == "all"
    hidden_mode = first == "hidden"
    if show_all or hidden_mode:
        args = args[1:]
    query = " ".join(args)

    sessions = _load_sessions(context, query, show_all, hidden_mode)
    total_pages = (len(sessions) + PAGE_SIZE - 1) // PAGE_SIZE
    current_id = context.user_data.get("codex_session_id")

    text = _page_text(
        sessions,
        0,
        total_pages,
        current_id,
        query,
        session_owners(context),
        hidden_mode,
        _defaults(context),
    )
    if not show_all and not hidden_mode and len(sessions) >= MAX_SESSIONS:
        text += f"\n<i>Showing last {MAX_SESSIONS} — /sessions all for more.</i>"

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=_page_keyboard(sessions, 0, total_pages),
    )


def _detail_text(
    info: SessionInfo,
    current_id: Optional[str],
    bot_label: str,
    owners: Optional[Dict[str, str]] = None,
    defaults: Optional[Dict[str, Optional[str]]] = None,
) -> str:
    marker = " ⭐ <i>current</i>" if info.session_id == current_id else ""
    lines = [
        f"<b>{_esc(info.display_name)}</b>"
        f"{_esc(_started_suffix(info))}{marker}",
        "",
        f"<code>{_esc(info.session_id)}</code>",
        _detail_bits(
            info,
            owners,
            defaults,
            _saved_choices("model"),
            _saved_choices("effort"),
        ),
        f"{info.size_text()} · {info.age_text()}",
        f"cwd <code>{_esc(info.cwd or '?')}</code> · {bot_label}",
    ]
    if info.last_prompt:
        lines += ["", f"<b>last prompt:</b> <i>{_esc(info.last_prompt[:200])}</i>"]
    return "\n".join(lines)


def _detail_keyboard(
    position: int, page: int, hidden: bool = False
) -> InlineKeyboardMarkup:
    if hidden:
        toggle = InlineKeyboardButton("👀 Unhide", callback_data=f"sess:unhide:{position}")
    else:
        toggle = InlineKeyboardButton("🙈 Hide", callback_data=f"sess:hide:{position}")
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶ Resume this", callback_data=f"sess:go:{position}"
                ),
                InlineKeyboardButton(
                    "👁 Preview", callback_data=f"sess:prev:{position}"
                ),
            ],
            [
                toggle,
                InlineKeyboardButton("◀ Back", callback_data=f"sess:page:{page}"),
            ],
        ]
    )


def _confirm_hide_keyboard(position: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Yes, hide", callback_data=f"sess:hideyes:{position}"
                ),
                InlineKeyboardButton("✖ Cancel", callback_data=f"sess:open:{position}"),
            ]
        ]
    )


async def _bot_label(context: ContextTypes.DEFAULT_TYPE, session_id: str) -> str:
    """Which bot's DB knows this session (best effort)."""
    owner = session_owners(context).get(session_id)
    if owner:
        return f"bot <code>{_esc(owner)}</code>"
    return "bot <i>other/unknown</i>"


async def sessions_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle every `sess:*` inline button."""
    query = update.callback_query
    await query.answer()

    try:
        _, action, raw_arg = query.data.split(":", 2)
        arg = int(raw_arg)
    except (ValueError, AttributeError):
        await query.edit_message_text("Bad session action.")
        return

    sessions = _sessions_for_page(context)
    current_id = context.user_data.get("codex_session_id")
    total_pages = (len(sessions) + PAGE_SIZE - 1) // PAGE_SIZE

    if action == "page":
        page = max(0, min(arg, max(0, total_pages - 1)))
        await query.edit_message_text(
            _page_text(
                sessions,
                page,
                total_pages,
                current_id,
                context.user_data.get(_QUERY_KEY, ""),
                session_owners(context),
                context.user_data.get(_HIDDEN_KEY, False),
                _defaults(context),
            ),
            parse_mode="HTML",
            reply_markup=_page_keyboard(sessions, page, total_pages),
        )
        return

    if arg < 0 or arg >= len(sessions):
        await query.edit_message_text("That session is gone. Run /sessions again.")
        return

    info = sessions[arg]
    page = arg // PAGE_SIZE
    hidden_mode = context.user_data.get(_HIDDEN_KEY, False)

    if action == "open":
        label = await _bot_label(context, info.session_id)
        await query.edit_message_text(
            _detail_text(
                info, current_id, label, session_owners(context), _defaults(context)
            ),
            parse_mode="HTML",
            reply_markup=_detail_keyboard(arg, page, hidden_mode),
        )
        return

    if action == "hide":
        context.user_data[_PENDING_HIDE_KEY] = info.session_id
        await query.edit_message_text(
            f"🙈 Hide <b>{_esc(info.display_name[:60])}</b>?\n\n"
            "<i>Transcript stays on disk. Undo via /sessions hidden.</i>",
            parse_mode="HTML",
            reply_markup=_confirm_hide_keyboard(arg),
        )
        return

    if action in ("hideyes", "unhide"):
        # Positions can shift if a new session appears between the confirm
        # screen and the tap; never hide a session the user did not pick.
        if action == "hideyes" and (
            context.user_data.pop(_PENDING_HIDE_KEY, None) != info.session_id
        ):
            await query.edit_message_text(
                "Session list changed. Run /sessions again."
            )
            return
        set_hidden(info.session_id, action == "hideyes")
        logger.info(
            "Session visibility changed",
            session_id=info.session_id,
            hidden=action == "hideyes",
        )
        # Rebuild: the list shrank, so redraw the page the entry was on.
        sessions = _sessions_for_page(context)
        total_pages = (len(sessions) + PAGE_SIZE - 1) // PAGE_SIZE
        page = max(0, min(page, max(0, total_pages - 1)))
        await query.edit_message_text(
            _page_text(
                sessions,
                page,
                total_pages,
                current_id,
                context.user_data.get(_QUERY_KEY, ""),
                session_owners(context),
                hidden_mode,
                _defaults(context),
            ),
            parse_mode="HTML",
            reply_markup=_page_keyboard(sessions, page, total_pages),
        )
        return

    if action == "prev":
        turns = tail_messages(Path(info.path), limit=8)
        lines = [f"👁 <b>{_esc(info.display_name[:60])}</b>", ""]
        for turn in turns:
            who = "🧑" if turn["role"] == "user" else "🤖"
            lines.append(f"{who} {_esc(turn['text'][:200])}")
            lines.append("")
        if not turns:
            lines.append("<i>No readable messages in the tail.</i>")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=_detail_keyboard(arg, page, hidden_mode),
        )
        return

    if action == "go":
        await _resume_session(update, context, info)
        return

    await query.edit_message_text("Unknown session action.")


async def adopt_session(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session_id: str,
    project_path: Path,
    message_count: int = 0,
) -> None:
    """Make `session_id` resolvable from storage and point this chat at it.

    Persists a row so SessionManager resolves the id instead of minting a
    fresh empty session (which would drop all context). An explicit pick also
    refreshes last_used, so an idle session past the timeout still resumes
    instead of being silently replaced.
    """
    codex_integration = context.bot_data.get("codex_integration")
    manager = getattr(codex_integration, "session_manager", None)

    if manager is not None:
        try:
            existing = await manager.storage.load_session(session_id, user_id)
        except Exception:
            existing = None
        now = datetime.now(UTC)
        if existing is not None:
            existing.last_used = now
            try:
                await manager.storage.save_session(existing)
                manager.active_sessions[session_id] = existing
            except Exception as exc:
                logger.warning(
                    "Failed to refresh picked session",
                    session_id=session_id,
                    error=str(exc),
                )
        else:
            session = CodexSession(
                session_id=session_id,
                user_id=user_id,
                project_path=project_path,
                created_at=now,
                last_used=now,
                message_count=message_count,
            )
            try:
                await manager.storage.save_session(session)
                manager.active_sessions[session_id] = session
            except Exception as exc:
                logger.warning(
                    "Failed to persist adopted session",
                    session_id=session_id,
                    error=str(exc),
                )

    context.user_data["codex_session_id"] = session_id
    context.user_data["force_new_session"] = False
    context.user_data["current_directory"] = project_path


async def _resume_session(
    update: Update, context: ContextTypes.DEFAULT_TYPE, info: SessionInfo
) -> None:
    """Point this chat at `info` and make the session resolvable from storage."""
    query = update.callback_query
    user_id = update.effective_user.id
    settings = context.bot_data.get("settings")

    project_path = Path(info.cwd) if info.cwd else settings.approved_directory
    await adopt_session(context, user_id, info.session_id, project_path, info.user_turns)

    logger.info(
        "Switched session via picker",
        session_id=info.session_id,
        user_id=user_id,
        cwd=str(project_path),
    )

    await query.edit_message_text(
        "▶ <b>Resumed</b>\n\n"
        f"{_esc(info.display_name)}\n"
        f"<code>{_esc(info.session_id)}</code>\n"
        f"{info.user_turns} msg · cwd <code>{_esc(str(project_path))}</code>\n\n"
        "<i>Send a message to continue this conversation.</i>",
        parse_mode="HTML",
    )
