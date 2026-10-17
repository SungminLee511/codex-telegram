"""Message orchestrator — single entry point for all Telegram updates.

Routes every message straight to Codex. There is one mode and it is
conversational; commands exist only for the things a conversation cannot do
— switching session, model, effort, or restarting the bot.
"""

import asyncio
import re
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..codex.cli_integration import StreamUpdate
from ..config.settings import Settings
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.durations import format_elapsed
from .utils.html_format import escape_html
from .utils import outbox, uploads
from .handlers import effort_command, model_command
from .utils.progress_registry import ProgressRegistry
from .utils.session_header import attach_header

logger = structlog.get_logger()

_MEDIA_TYPE_MAP = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
    # Codex item types, as cli_integration names them.
    "FileChange": "\u270f\ufe0f",
    "TodoList": "\u2611\ufe0f",
    "CollabAgent": "\U0001f9e0",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")




@dataclass
class ActiveRequest:
    """Tracks an in-flight Codex request so it can be interrupted."""

    user_id: int
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    progress_msg: Any = None  # telegram Message object


# An empty reply from Codex is retried with a wake after this many seconds.
EMPTY_REPLY_WAKE_SECONDS = 5
# Consecutive empty replies that get retried before giving up (a model that
# keeps answering with nothing would otherwise be woken forever, at your cost).
MAX_EMPTY_REPLY_RETRIES = 3
EMPTY_REPLY_WAKE_TEXT = (
    "DO NOT REPLY WITH NO REPLY MOTHERFUCKER I TOLD YOU IN AGENTS.MD. "
    "Answer my last message now."
)
_WAKE_SCRIPT = Path(__file__).resolve().parents[2] / "wake_after.sh"


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        self._active_requests: Dict[int, ActiveRequest] = {}
        self._known_commands: frozenset[str] = frozenset()
        self.progress_registry = ProgressRegistry(
            Path("data") / f"pending_progress_{settings.bot_id}.json"
        )

    async def _open_progress(self, update: Update, text: str, **kwargs: Any):
        """Send a progress message and record it as in-flight."""
        msg = await update.message.reply_text(text, **kwargs)
        try:
            self.progress_registry.add(msg.chat_id, msg.message_id)
        except Exception:  # pragma: no cover - bookkeeping must never block
            logger.debug("Failed to record progress message")
        return msg

    def _forget_progress(self, msg: Any) -> None:
        """Stop tracking a progress message that reached a final state.

        Used when the message is *edited* into an error instead of deleted:
        it already says what happened, so a later restart must not overwrite
        it with "interrupted".
        """
        if msg is None:
            return
        try:
            self.progress_registry.discard(msg.chat_id, msg.message_id)
        except Exception:  # pragma: no cover
            logger.debug("Failed to clear progress record")

    async def _close_progress(self, msg: Any) -> None:
        """Delete a progress message and drop it from the in-flight record."""
        if msg is None:
            return
        self._forget_progress(msg)
        try:
            await msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

    async def _fail_progress(
        self, msg: Any, text: str, parse_mode: Optional[str] = None
    ) -> None:
        """Turn a progress message into its own error message, and stop tracking."""
        self._forget_progress(msg)
        try:
            await msg.edit_text(text, parse_mode=parse_mode, reply_markup=None)
        except Exception:
            logger.debug("Failed to edit progress message into an error")

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            await handler(update, context)

        return wrapped

    def register_handlers(self, app: Application) -> None:
        """Register the bot's handlers."""
        self._register_agentic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import (
            admin_command,
            branch_command,
            rename_command,
            session_picker,
        )

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("sessions", session_picker.sessions_command),
            ("verbose", self.agentic_verbose),
            ("repo", self.agentic_repo),
            ("model", model_command.model_command),
            ("effort", effort_command.effort_command),
            ("branch", branch_command.branch_command),
            # Codex exec has no /rename of its own, so the bot names sessions.
            ("rename", rename_command.rename_command),
            ("restart", admin_command.restart_command),
        ]

        # Derive known commands dynamically — avoids drift when new commands are added
        self._known_commands: frozenset[str] = frozenset(cmd for cmd, _ in handlers)

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Codex
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # Unknown slash commands -> Codex (passthrough in agentic mode).
        # Registered commands are handled by CommandHandlers in group 0
        # (higher priority). This catches any /command not matched there
        # and forwards it to Codex, while skipping known commands to
        # avoid double-firing.
        app.add_handler(
            MessageHandler(
                filters.COMMAND,
                self._inject_deps(self._handle_unknown_command),
            ),
            group=10,
        )

        # File uploads -> Codex
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Codex
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice messages -> transcribe -> Codex
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # Setting pickers: /model and /effort draw tappable lists.
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(model_command.model_callback),
                pattern=r"^model:",
            )
        )
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(effort_command.effort_callback),
                pattern=r"^effort:",
            )
        )

        # Stop button callback (must be before cd: handler)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_stop_callback),
                pattern=r"^stop:",
            )
        )

        # Session picker callbacks (/sessions list -> detail -> resume)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(session_picker.sessions_callback),
                pattern=r"^sess:",
            )
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        logger.info("Agentic handlers registered")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        commands = [
                # The menu is what you scroll before typing, so it holds the
                # commands worth choosing from. `status`, `verbose` and `repo`
                # still work when typed — they are hidden, not removed.
            BotCommand("start", "Start the bot"),
            BotCommand("new", "Start a fresh session"),
            BotCommand("sessions", "Browse and switch sessions"),
            BotCommand("rename", "Rename this session"),
            BotCommand("model", "Choose the model for this session"),
            BotCommand("effort", "Set reasoning effort for this session"),
            BotCommand("branch", "Continue on a copy, leaving this one intact"),
            BotCommand("restart", "Restart the bot"),
        ]
        return commands
    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) · /status",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["codex_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact one-line status, no buttons."""
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = str(current_dir)

        session_id = context.user_data.get("codex_session_id")
        session_status = "active" if session_id else "none"

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass

        await update.message.reply_text(
            f"📂 {dir_display} · Session: {session_status}{cost_str}"
        )

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({format_elapsed(elapsed)})\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Codex's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        if tool_name == "FileChange":
            changes = tool_input.get("changes") or []
            paths = [
                str(c.get("path", "")).rsplit("/", 1)[-1]
                for c in changes
                if isinstance(c, dict) and c.get("path")
            ]
            if paths:
                return ", ".join(paths)[:80]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    @staticmethod
    def _stop_keyboard(user_id: int) -> InlineKeyboardMarkup:
        """The Stop button shown on a running request's progress message."""
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]]
        )

    def _wake_if_empty(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, codex_response: Any
    ) -> None:
        """Schedule a nudge when Codex answered with nothing.

        The chat shows "(No content to display)" for an empty reply. Instead of
        leaving it there, a wake fires a few seconds later telling Codex not to
        do that. Skipped when you pressed Stop, and capped at
        MAX_EMPTY_REPLY_RETRIES in a row; any non-empty reply resets the count.
        Never raises: this is a convenience and must not cost the reply.
        """
        try:
            empty = bool(getattr(codex_response, "no_text", False)) or not (
                getattr(codex_response, "content", "") or ""
            ).strip()
            if not empty:
                context.user_data["empty_reply_retries"] = 0
                return
            if getattr(codex_response, "interrupted", False):
                return
            tries = int(context.user_data.get("empty_reply_retries", 0))
            if tries >= MAX_EMPTY_REPLY_RETRIES:
                logger.warning("Empty replies kept coming; no more auto-wakes")
                context.user_data["empty_reply_retries"] = 0
                return
            context.user_data["empty_reply_retries"] = tries + 1

            chat = update.effective_chat
            env = dict(os.environ)
            if chat is not None:
                env["WAKE_CHAT_ID"] = str(chat.id)
            env["INJECT_DIR"] = str(self.settings.inject_dir)
            subprocess.Popen(
                [
                    "bash",
                    str(_WAKE_SCRIPT),
                    str(EMPTY_REPLY_WAKE_SECONDS),
                    EMPTY_REPLY_WAKE_TEXT,
                    str(self.settings.bot_id),
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info("Empty reply: wake scheduled", attempt=tries + 1)
        except Exception as exc:
            logger.warning("Could not schedule the empty-reply wake", error=str(exc))

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        draft_streamer: Optional[DraftStreamer] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.


        Returns None when verbose_level is 0 **and** no MCP image
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        if verbose_level == 0 and draft_streamer is None:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Stop all streaming activity after interrupt
            if interrupt_event is not None and interrupt_event.is_set():
                return


            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    # Codex reports each item twice (started, completed);
                    # log it once, when it starts.
                    if tc.get("phase") not in (None, "started"):
                        continue
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture assistant text (reasoning / commentary)
            # Codex streams reasoning summaries separately from replies;
            # both are the running commentary the progress message shows.
            if update_obj.type in ("assistant", "reasoning") and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits
            if not draft_streamer and verbose_level >= 1:
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    try:
                        await progress_msg.edit_text(
                            new_text, reply_markup=reply_markup
                        )
                    except Exception:
                        pass

        return _on_stream

    async def _flush_outbox(self, update: Update) -> None:
        """Send whatever Codex left in the outbox, after its reply.

        Runs after the text so the file arrives as an attachment to an answer
        that has already explained it, and runs on every turn — including one
        that failed — so a file written before an error still gets out.
        """
        try:
            result = await outbox.deliver(update.message)
        except Exception as exc:  # the drop box must never break a reply
            logger.warning("Outbox sweep failed", error=str(exc))
            return
        note = result.note()
        if note:
            try:
                await update.message.reply_text(note)
            except Exception:
                logger.debug("Outbox note could not be sent")

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Codex passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)

        # Create Stop button and interrupt event
        interrupt_event = asyncio.Event()
        stop_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]]
        )
        progress_msg = await self._open_progress(
            update, "Working...", reply_markup=stop_kb
        )

        # Register active request for stop callback
        active_request = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )
        self._active_requests[user_id] = active_request

        codex_integration = context.bot_data.get("codex_integration")
        if not codex_integration:
            self._active_requests.pop(user_id, None)
            await self._fail_progress(
                progress_msg, "Codex integration not available. Check configuration."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("codex_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()

        # Stream drafts (private chats only)
        draft_streamer: Optional[DraftStreamer] = None
        if self.settings.enable_stream_drafts and chat.type == "private":
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
            )

        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            reply_markup=stop_kb,
            draft_streamer=draft_streamer,
            interrupt_event=interrupt_event,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        success = True
        try:
            codex_response = await codex_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                interrupt_event=interrupt_event,
                model=model_command.current_model_id(context),
                effort=effort_command.current_effort(context),
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["codex_session_id"] = codex_response.session_id
            # A model picked before the session existed now has an id to
            # attach to.
            model_command.bind_pending(context, codex_response.session_id)
            effort_command.bind_pending(context, codex_response.session_id)

            # Track directory changes
            from .handlers.common import _update_working_directory_from_codex_response

            _update_working_directory_from_codex_response(
                codex_response, context, self.settings, user_id
            )

            # Store interaction
            storage = context.bot_data.get("storage")
            if storage:
                try:
                    await storage.save_codex_interaction(
                        user_id=user_id,
                        session_id=codex_response.session_id,
                        prompt=message_text,
                        response=codex_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)

            response_content = codex_response.content
            if codex_response.interrupted:
                response_content = (
                    response_content or ""
                ) + "\n\n_(Interrupted by user)_"

            formatted_messages = formatter.format_codex_response(response_content)
            formatted_messages = attach_header(
                formatted_messages, context, codex_response
            )
            self._wake_if_empty(update, context, codex_response)

        except Exception as e:
            success = False
            logger.error("Codex integration failed", error=str(e), user_id=user_id)
            from .handlers.common import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)

        await self._close_progress(progress_msg)

        for i, message in enumerate(formatted_messages):
            if not message.text or not message.text.strip():
                continue
            try:
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,  # No keyboards in agentic mode
                    reply_to_message_id=(
                        update.message.message_id if i == 0 else None
                    ),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)
            except Exception as send_err:
                logger.warning(
                    "Failed to send HTML response, retrying as plain text",
                    error=str(send_err),
                    message_index=i,
                )
                try:
                    await update.message.reply_text(
                        message.text,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                except Exception as plain_err:
                    await update.message.reply_text(
                        f"Failed to deliver response "
                        f"(Telegram error: {str(plain_err)[:150]}). "
                        f"Please try again.",
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )


        await self._flush_outbox(update)

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Codex, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check — Telegram's bot API caps downloads at 20 MB.
        max_size = 20 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 20MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        interrupt_event = asyncio.Event()
        stop_kb = self._stop_keyboard(user_id)
        progress_msg = await self._open_progress(
            update, "Working...", reply_markup=stop_kb
        )
        self._active_requests[user_id] = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )

        # Save the upload to the inbox and hand Codex the *path*. Inlining the
        # bytes would cap us at text files small enough to fit in a prompt;
        # a path works for PDFs, archives and multi-MB data alike.
        prompt: Optional[str] = None
        try:
            tg_file = await document.get_file()
            saved_path = await uploads.save_telegram_file(
                tg_file, document.file_name, update.message.message_id
            )
            prompt = uploads.build_prompt(
                saved_path, update.message.caption, kind="file"
            )
            uploads.cleanup()
        except Exception as exc:
            logger.error("Upload save failed", error=str(exc), user_id=user_id)
            self._active_requests.pop(user_id, None)
            await self._fail_progress(
                progress_msg, f"Could not save the file: {exc}"
            )
            return

        # Process with Codex
        codex_integration = context.bot_data.get("codex_integration")
        if not codex_integration:
            self._active_requests.pop(user_id, None)
            await self._fail_progress(
                progress_msg, "Codex integration not available. Check configuration."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("codex_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            reply_markup=stop_kb,
            interrupt_event=interrupt_event,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            codex_response = await codex_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                interrupt_event=interrupt_event,
                model=model_command.current_model_id(context),
                effort=effort_command.current_effort(context),
            )

            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["codex_session_id"] = codex_response.session_id
            model_command.bind_pending(context, codex_response.session_id)
            effort_command.bind_pending(context, codex_response.session_id)

            from .handlers.common import _update_working_directory_from_codex_response

            _update_working_directory_from_codex_response(
                codex_response, context, self.settings, user_id
            )

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            response_content = codex_response.content
            if codex_response.interrupted:
                response_content = (
                    response_content or ""
                ) + "\n\n_(Interrupted by user)_"
            formatted_messages = formatter.format_codex_response(response_content)
            formatted_messages = attach_header(
                formatted_messages, context, codex_response
            )
            self._wake_if_empty(update, context, codex_response)

            await self._close_progress(progress_msg)

            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(
                        update.message.message_id if i == 0 else None
                    ),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)


        except Exception as e:
            from .handlers.common import _format_error_message

            await self._fail_progress(
                progress_msg, _format_error_message(e), parse_mode="HTML"
            )
            logger.error("Codex file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)
            await self._flush_outbox(update)

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Codex, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        interrupt_event = asyncio.Event()
        stop_kb = self._stop_keyboard(user_id)
        progress_msg = await self._open_progress(
            update, "Working...", reply_markup=stop_kb
        )
        self._active_requests[user_id] = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )
            fmt = processed_image.metadata.get("format", "png")
            images = [
                {
                    "data": processed_image.base64_data,
                    "media_type": _MEDIA_TYPE_MAP.get(fmt, "image/png"),
                }
            ]

            # Codex sees the pixels inline (base64 block), but also keep a
            # copy on disk so it can be re-opened, cropped or referenced by
            # path later in the conversation.
            prompt = processed_image.prompt
            try:
                tg_file = await photo.get_file()
                saved_path = await uploads.save_telegram_file(
                    tg_file,
                    f"photo_{photo.file_unique_id}.{fmt}",
                    update.message.message_id,
                )
                prompt = f"{prompt}\n\n[uploaded image] `{saved_path}`"
                uploads.cleanup()
            except Exception as exc:
                logger.warning("Photo copy to inbox failed", error=str(exc))

            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
                images=images,
                interrupt_event=interrupt_event,
                stop_kb=stop_kb,
            )

        except Exception as e:
            from .handlers.common import _format_error_message

            self._active_requests.pop(user_id, None)
            await self._fail_progress(
                progress_msg, _format_error_message(e), parse_mode="HTML"
            )
            logger.error(
                "Codex photo processing failed", error=str(e), user_id=user_id
            )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Codex, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        voice_handler = features.get_voice_handler() if features else None

        if not voice_handler:
            await update.message.reply_text(self._voice_unavailable_message())
            return

        chat = update.message.chat
        await chat.send_action("typing")
        interrupt_event = asyncio.Event()
        stop_kb = self._stop_keyboard(user_id)
        progress_msg = await self._open_progress(
            update, "Transcribing...", reply_markup=stop_kb
        )
        self._active_requests[user_id] = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )

        try:
            voice = update.message.voice
            processed_voice = await voice_handler.process_voice_message(
                voice, update.message.caption
            )

            if not interrupt_event.is_set():
                await progress_msg.edit_text("Working...", reply_markup=stop_kb)
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_voice.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
                interrupt_event=interrupt_event,
                stop_kb=stop_kb,
            )

        except Exception as e:
            from .handlers.common import _format_error_message

            self._active_requests.pop(user_id, None)
            await self._fail_progress(
                progress_msg, _format_error_message(e), parse_mode="HTML"
            )
            logger.error(
                "Codex voice processing failed", error=str(e), user_id=user_id
            )

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
        images: Optional[List[Dict[str, str]]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        stop_kb: Optional[InlineKeyboardMarkup] = None,
    ) -> None:
        """Run a media-derived prompt through Codex and send responses."""
        codex_integration = context.bot_data.get("codex_integration")
        if not codex_integration:
            self._active_requests.pop(user_id, None)
            await self._fail_progress(
                progress_msg, "Codex integration not available. Check configuration."
            )
            return

        # Stop pressed while the photo/voice was still being prepared.
        if interrupt_event is not None and interrupt_event.is_set():
            self._active_requests.pop(user_id, None)
            await self._fail_progress(progress_msg, "Stopped.")
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("codex_session_id")
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            reply_markup=stop_kb,
            interrupt_event=interrupt_event,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            codex_response = await codex_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                images=images,
                interrupt_event=interrupt_event,
                model=model_command.current_model_id(context),
                effort=effort_command.current_effort(context),
            )
        finally:
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)

        if force_new:
            context.user_data["force_new_session"] = False

        context.user_data["codex_session_id"] = codex_response.session_id
        model_command.bind_pending(context, codex_response.session_id)
        effort_command.bind_pending(context, codex_response.session_id)

        from .handlers.common import _update_working_directory_from_codex_response

        _update_working_directory_from_codex_response(
            codex_response, context, self.settings, user_id
        )

        from .utils.formatting import ResponseFormatter

        formatter = ResponseFormatter(self.settings)
        response_content = codex_response.content
        if codex_response.interrupted:
            response_content = (response_content or "") + "\n\n_(Interrupted by user)_"
        formatted_messages = formatter.format_codex_response(response_content)
        formatted_messages = attach_header(formatted_messages, context, codex_response)
        self._wake_if_empty(update, context, codex_response)

        await self._close_progress(progress_msg)

        # Use MCP-collected images (from send_image_to_user tool calls).
        for i, message in enumerate(formatted_messages):
            if not message.text or not message.text.strip():
                continue
            await update.message.reply_text(
                message.text,
                parse_mode=message.parse_mode,
                reply_markup=None,
                reply_to_message_id=(update.message.message_id if i == 0 else None),
            )
            if i < len(formatted_messages) - 1:
                await asyncio.sleep(0.5)


        await self._flush_outbox(update)

    async def _handle_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forward unknown slash commands to Codex in agentic mode.

        Known commands are handled by their own CommandHandlers (group 0);
        this handler fires for *every* COMMAND message in group 10 but
        returns immediately when the command is registered, preventing
        double execution.
        """
        msg = update.effective_message
        if not msg or not msg.text:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in self._known_commands:
            return  # let the registered CommandHandler take care of it
        # Forward unrecognised /commands to Codex as natural language
        await self.agentic_text(update, context)

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        if self.settings.voice_provider == "local":
            return (
                "Voice processing is not available. "
                "Ensure whisper.cpp is installed and the model file exists. "
                "Check WHISPER_CPP_BINARY_PATH and WHISPER_CPP_MODEL_PATH settings."
            )
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            'voice extras with: pip install "codex-telegram[voice]"'
        )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = base / target_name
            if not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Try to find a resumable session
            codex_integration = context.bot_data.get("codex_integration")
            session_id = None
            if codex_integration:
                existing = await codex_integration._find_resumable_session(
                    update.effective_user.id, target_path
                )
                if existing:
                    session_id = existing.session_id
            context.user_data["codex_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        current_name = current_dir.name if current_dir != base else None

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def _handle_stop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle stop: callbacks — interrupt a running Codex request."""
        query = update.callback_query
        target_user_id = int(query.data.split(":", 1)[1])

        # Only the requesting user can stop their own request
        if query.from_user.id != target_user_id:
            await query.answer(
                "Only the requesting user can stop this.", show_alert=True
            )
            return

        active = self._active_requests.get(target_user_id)
        if not active:
            await query.answer("Already completed.", show_alert=False)
            return
        if active.interrupted:
            await query.answer("Already stopping...", show_alert=False)
            return

        active.interrupt_event.set()
        active.interrupted = True
        await query.answer("Stopping...", show_alert=False)

        try:
            await active.progress_msg.edit_text("Stopping...", reply_markup=None)
        except Exception:
            pass

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        codex_integration = context.bot_data.get("codex_integration")
        session_id = None
        if codex_integration:
            existing = await codex_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["codex_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )
