"""OpenAI Codex CLI subprocess integration.

Wraps ``codex exec --json`` (and ``codex exec resume``) as an async subprocess,
parses the JSONL event stream, and exposes the same surface (`CodexResponse`,
`StreamUpdate`) the rest of the bot consumes.

The Codex CLI in non-interactive ``exec`` mode is one-shot: a single prompt
in, a stream of JSONL events out, then the process exits. Multi-turn is
emulated by chaining ``codex exec resume <session_id>`` calls between user
messages, which is a perfect fit for a Telegram request/response loop.
"""

import asyncio
import base64
import json
import os
import shutil
import signal
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from ..security.validators import SecurityValidator
from .exceptions import (
    CodexParsingError,
    CodexProcessError,
    CodexTimeoutError,
)

logger = structlog.get_logger()

# Fallback message when Codex produces no agent_message but did execute tools.
TASK_COMPLETED_MSG = "✅ Task completed. Tools used: {tools_summary}"

# Rough per-token pricing in USD (input, output) — used only to populate
# ``CodexResponse.cost`` for legacy bookkeeping. Real source of truth is
# ``CodexResponse.usage``. Update as OpenAI pricing changes.
_TOKEN_PRICES_USD: Dict[str, Dict[str, float]] = {
    "gpt-5": {"input": 1.25e-6, "output": 10.0e-6},
    "gpt-5-codex": {"input": 1.25e-6, "output": 10.0e-6},
    "gpt-5-mini": {"input": 0.25e-6, "output": 2.0e-6},
    "default": {"input": 1.0e-6, "output": 5.0e-6},
}


def _estimate_cost(model: Optional[str], usage: Dict[str, int]) -> float:
    """Best-effort USD cost estimate from token counts."""
    if not usage:
        return 0.0
    key = (model or "default").lower()
    prices = _TOKEN_PRICES_USD.get(key, _TOKEN_PRICES_USD["default"])
    return (
        usage.get("input_tokens", 0) * prices["input"]
        + usage.get("output_tokens", 0) * prices["output"]
    )


@dataclass
class CodexResponse:
    """Response from a Codex CLI run."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False
    usage: Dict[str, int] = field(default_factory=dict)


@dataclass
class StreamUpdate:
    """Streaming update emitted while Codex runs."""

    type: str  # 'assistant', 'reasoning', 'tool', 'system', 'result', 'error'
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    progress: Optional[Dict[str, Any]] = None

    def get_tool_names(self) -> List[str]:
        names: List[str] = []
        if self.tool_calls:
            for tc in self.tool_calls:
                if isinstance(tc, dict):
                    name = tc.get("name")
                    if isinstance(name, str) and name:
                        names.append(name)
        if self.metadata:
            tn = self.metadata.get("tool_name")
            if isinstance(tn, str) and tn:
                names.append(tn)
        return list(dict.fromkeys(names))

    def is_error(self) -> bool:
        if self.type == "error":
            return True
        if self.metadata:
            if self.metadata.get("is_error") is True:
                return True
            status = self.metadata.get("status")
            if isinstance(status, str) and status.lower() in {"error", "failed"}:
                return True
        return False

    def get_error_message(self) -> str:
        if self.metadata:
            for key in ("error_message", "error", "message"):
                value = self.metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        if isinstance(self.content, str) and self.content.strip():
            return self.content
        return "Unknown error"

    def get_progress_percentage(self) -> Optional[int]:
        if self.progress:
            for key in ("percentage", "percent", "progress"):
                v = self.progress.get(key)
                if isinstance(v, (int, float)):
                    return max(0, min(100, int(v)))
        return None


class CodexCLIManager:
    """Spawn ``codex exec --json`` and stream its events back."""

    def __init__(
        self,
        config: Settings,
        security_validator: Optional[SecurityValidator] = None,
    ):
        self.config = config
        self.security_validator = security_validator

        # Codex auth normally lives in ~/.codex/auth.json (via `codex login`).
        # If the user provided OPENAI_API_KEY, surface it to subprocesses.
        api_key = getattr(config, "openai_api_key_str", None)
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
            logger.info("Using provided OPENAI_API_KEY for Codex authentication")
        else:
            logger.info(
                "No OPENAI_API_KEY provided; relying on existing `codex login` credentials"
            )

    # ---------------------------------------------------------------- helpers

    def _resolve_codex_binary(self) -> str:
        """Find the codex CLI binary."""
        explicit = getattr(self.config, "codex_cli_path", None)
        if explicit:
            return str(explicit)
        env_path = os.environ.get("CODEX_CLI_PATH")
        if env_path:
            return env_path
        binary = shutil.which("codex")
        if binary:
            return binary
        return "codex"

    def _build_command(
        self,
        prompt_via_stdin: bool,
        working_directory: Path,
        session_id: Optional[str],
        continue_session: bool,
        image_files: List[str],
    ) -> List[str]:
        """Build the codex exec argv."""
        cmd: List[str] = [self._resolve_codex_binary(), "exec"]
        is_resume = bool(continue_session and session_id)

        if is_resume:
            cmd.append("resume")

        cmd.append("--json")

        # `codex exec resume` does NOT accept --cd, -s, or --add-dir;
        # the resumed session keeps its original cwd and sandbox settings.
        if not is_resume:
            cmd.extend(["--cd", str(working_directory)])

            sandbox_mode = getattr(self.config, "codex_sandbox_mode", None) or (
                "workspace-write"
                if getattr(self.config, "sandbox_enabled", True)
                else "danger-full-access"
            )
            cmd.extend(["-s", sandbox_mode])

        if getattr(self.config, "codex_skip_git_repo_check", True):
            cmd.append("--skip-git-repo-check")

        model = getattr(self.config, "codex_model", None)
        if model:
            cmd.extend(["-m", model])

        effort = getattr(self.config, "codex_reasoning_effort", None)
        if effort:
            cmd.extend(["-c", f"model_reasoning_effort={effort}"])

        if not is_resume:
            for extra in getattr(self.config, "codex_add_dirs", None) or []:
                cmd.extend(["--add-dir", str(extra)])

        if getattr(self.config, "codex_dangerously_bypass", False):
            cmd.append("--dangerously-bypass-approvals-and-sandbox")

        for img_path in image_files:
            cmd.extend(["-i", img_path])

        for override in getattr(self.config, "codex_config_overrides", None) or []:
            cmd.extend(["-c", str(override)])

        # Positional SESSION_ID must come AFTER options for `exec resume`.
        if is_resume:
            cmd.append(session_id)

        if prompt_via_stdin:
            cmd.append("-")

        return cmd

    def _build_legacy_claude_addendum(self, working_directory: Path) -> Optional[str]:
        """Read CLAUDE.md only when Codex has no native AGENTS.md context."""
        if (Path(working_directory) / "AGENTS.md").exists():
            return None

        p = Path(working_directory) / "CLAUDE.md"
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except OSError:
                return None
        return None

    # ---------------------------------------------------------------- execute

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], Any]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> CodexResponse:
        """Run a single Codex turn via ``codex exec [resume]``."""
        loop = asyncio.get_event_loop()
        start_time = loop.time()

        logger.info(
            "Starting Codex CLI command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        # Keep repeated turns like Claude SDK sessions: only the user's new
        # prompt is sent on resume. Codex loads AGENTS.md natively, so we only
        # inject CLAUDE.md once as a legacy fallback when no AGENTS.md exists.
        is_resume = bool(continue_session and session_id)
        if is_resume:
            full_prompt = prompt
        else:
            sys_addendum = self._build_legacy_claude_addendum(working_directory)
            boundary_note = (
                f"All file operations must stay within {working_directory}. "
                "Use relative paths."
            )
            full_prompt_parts: List[str] = [boundary_note]
            if sys_addendum:
                full_prompt_parts.append(sys_addendum)
            full_prompt_parts.append(prompt)
            full_prompt = "\n\n".join(full_prompt_parts)

        # Materialize images to temp files (codex CLI takes file paths).
        tmp_image_paths: List[str] = []
        try:
            for img in images or []:
                media_type = img.get("media_type", "image/png")
                ext = "png"
                if "/" in media_type:
                    ext = media_type.split("/", 1)[1].split(";", 1)[0] or "png"
                fd, path = tempfile.mkstemp(prefix="codex_img_", suffix=f".{ext}")
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(base64.b64decode(img["data"]))
                    tmp_image_paths.append(path)
                except Exception:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    raise

            cmd = self._build_command(
                prompt_via_stdin=True,
                working_directory=working_directory,
                session_id=session_id,
                continue_session=continue_session,
                image_files=tmp_image_paths,
            )

            logger.debug("Codex CLI argv", cmd=cmd)

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(working_directory),
                    start_new_session=True,
                )
            except FileNotFoundError as e:
                raise CodexProcessError(
                    "Codex CLI not found. Please install:\n"
                    "  npm install -g @openai/codex\n"
                    "or grab a release binary from github.com/openai/codex/releases.\n"
                    "Set CODEX_CLI_PATH if installed elsewhere.\n"
                    f"Underlying error: {e}"
                )

            if proc.stdin is not None:
                try:
                    proc.stdin.write(full_prompt.encode("utf-8"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            stderr_lines: List[str] = []
            interrupted = False
            captured_session_id: Optional[str] = session_id
            tools_used: List[Dict[str, Any]] = []
            text_chunks: List[str] = []
            usage: Dict[str, int] = {}
            turn_error: Optional[str] = None
            num_turns = 0

            async def _drain_stderr() -> None:
                assert proc.stderr is not None
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                    stderr_lines.append(decoded)
                    logger.debug("codex stderr", line=decoded)

            async def _drain_stdout() -> None:
                nonlocal captured_session_id, num_turns, turn_error
                assert proc.stdout is not None
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    raw = line.decode("utf-8", errors="replace").rstrip("\n")
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("Skipping non-JSON stdout line", line=raw)
                        continue

                    etype = event.get("type")

                    if etype == "thread.started":
                        tid = event.get("thread_id")
                        if isinstance(tid, str) and tid:
                            captured_session_id = tid
                        continue

                    if etype == "turn.started":
                        num_turns += 1
                        continue

                    if etype == "turn.completed":
                        u = event.get("usage") or {}
                        if isinstance(u, dict):
                            for k in (
                                "input_tokens",
                                "cached_input_tokens",
                                "output_tokens",
                                "reasoning_output_tokens",
                            ):
                                if isinstance(u.get(k), (int, float)):
                                    usage[k] = usage.get(k, 0) + int(u[k])
                        continue

                    if etype == "turn.failed":
                        err = event.get("error") or {}
                        msg = err.get("message") if isinstance(err, dict) else None
                        turn_error = msg or "turn failed"
                        if stream_callback:
                            try:
                                await stream_callback(
                                    StreamUpdate(
                                        type="error",
                                        content=turn_error,
                                        metadata={"is_error": True, "error": turn_error},
                                    )
                                )
                            except Exception as cb_err:
                                logger.warning("Stream callback error", error=str(cb_err))
                        continue

                    if etype == "error":
                        msg = event.get("message") or "unknown error"
                        turn_error = msg
                        if stream_callback:
                            try:
                                await stream_callback(
                                    StreamUpdate(
                                        type="error",
                                        content=msg,
                                        metadata={"is_error": True, "error": msg},
                                    )
                                )
                            except Exception as cb_err:
                                logger.warning("Stream callback error", error=str(cb_err))
                        continue

                    if etype in {"item.started", "item.updated", "item.completed"}:
                        await self._handle_item_event(
                            event,
                            etype,
                            stream_callback,
                            tools_used,
                            text_chunks,
                        )
                        continue

                    logger.debug("Unhandled codex event type", event_type=etype)

            stdout_task = asyncio.create_task(_drain_stdout())
            stderr_task = asyncio.create_task(_drain_stderr())

            interrupt_watcher: Optional["asyncio.Task[None]"] = None
            if interrupt_event is not None:

                async def _watch_interrupt() -> None:
                    nonlocal interrupted
                    await interrupt_event.wait()
                    interrupted = True
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                    except (ProcessLookupError, PermissionError):
                        pass

                interrupt_watcher = asyncio.create_task(_watch_interrupt())

            timeout = self.config.codex_timeout_seconds
            try:
                exit_code = await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.error("Codex CLI timed out", timeout_seconds=timeout)
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
                raise CodexTimeoutError(f"Codex CLI timed out after {timeout}s")
            finally:
                if interrupt_watcher is not None:
                    interrupt_watcher.cancel()
                try:
                    await asyncio.wait_for(stdout_task, timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    stdout_task.cancel()
                try:
                    await asyncio.wait_for(stderr_task, timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    stderr_task.cancel()

            if exit_code != 0 and not interrupted:
                stderr_blob = "\n".join(stderr_lines[-30:]) if stderr_lines else ""
                err_msg = turn_error or f"codex exec exited with code {exit_code}"
                if stderr_blob:
                    err_msg = f"{err_msg}\nStderr:\n{stderr_blob}"
                logger.error(
                    "Codex CLI process failed",
                    exit_code=exit_code,
                    stderr=stderr_blob,
                )
                raise CodexProcessError(err_msg)

            content = "\n\n".join(c for c in text_chunks if c).strip()
            if not content and tools_used:
                names = list(
                    dict.fromkeys(
                        t.get("name", "")
                        for t in tools_used
                        if isinstance(t.get("name"), str) and t.get("name")
                    )
                )
                content = TASK_COMPLETED_MSG.format(
                    tools_summary=", ".join(names) or "unknown"
                )

            duration_ms = int((loop.time() - start_time) * 1000)
            cost = _estimate_cost(getattr(self.config, "codex_model", None), usage)

            return CodexResponse(
                content=content,
                session_id=captured_session_id or session_id or "",
                cost=cost,
                duration_ms=duration_ms,
                num_turns=max(1, num_turns),
                is_error=bool(turn_error) and not interrupted,
                error_type="turn_error" if turn_error else None,
                tools_used=tools_used,
                interrupted=interrupted,
                usage=usage,
            )

        except (CodexTimeoutError, CodexProcessError, CodexParsingError):
            raise
        except Exception as e:
            logger.exception("Unexpected error in Codex CLI integration", error=str(e))
            raise CodexProcessError(f"Unexpected error: {e}")
        finally:
            for p in tmp_image_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass

    # ---------------------------------------------------------------- events

    async def _handle_item_event(
        self,
        event: Dict[str, Any],
        etype: str,
        stream_callback: Optional[Callable[[StreamUpdate], Any]],
        tools_used: List[Dict[str, Any]],
        text_chunks: List[str],
    ) -> None:
        """Translate a codex item.* event into StreamUpdate(s)."""
        item = event.get("item") or {}
        if not isinstance(item, dict):
            return

        item_type = item.get("type")
        item_id = item.get("id")

        if item_type == "agent_message":
            if etype == "item.completed":
                text = item.get("text")
                if isinstance(text, str) and text:
                    text_chunks.append(text)
                    if stream_callback:
                        try:
                            await stream_callback(
                                StreamUpdate(type="assistant", content=text)
                            )
                        except Exception as cb_err:
                            logger.warning("Stream callback error", error=str(cb_err))
            return

        if item_type == "reasoning":
            if etype == "item.completed":
                text = item.get("text")
                if isinstance(text, str) and text and stream_callback:
                    try:
                        await stream_callback(
                            StreamUpdate(type="reasoning", content=text)
                        )
                    except Exception as cb_err:
                        logger.warning("Stream callback error", error=str(cb_err))
            return

        # Tool-like items.
        tool_name_map = {
            "command_execution": "Bash",
            "mcp_tool_call": None,
            "web_search": "WebSearch",
            "file_change": "FileChange",
            "todo_list": "TodoList",
            "collab_tool_call": "CollabAgent",
        }
        if item_type in tool_name_map:
            if item_type == "mcp_tool_call":
                server = item.get("server") or "mcp"
                tool = item.get("tool") or "tool"
                tname = f"{server}.{tool}"
            else:
                tname = tool_name_map[item_type] or item_type

            tool_input: Dict[str, Any] = {}
            if item_type == "command_execution":
                tool_input["command"] = item.get("command")
                if etype == "item.completed":
                    tool_input["exit_code"] = item.get("exit_code")
                    tool_input["status"] = item.get("status")
            elif item_type == "mcp_tool_call":
                tool_input["arguments"] = item.get("arguments")
                if etype == "item.completed":
                    tool_input["status"] = item.get("status")
            elif item_type == "web_search":
                tool_input["query"] = item.get("query")
            elif item_type == "file_change":
                tool_input["changes"] = item.get("changes")
            elif item_type == "todo_list":
                tool_input["items"] = item.get("items")

            if etype == "item.started":
                tools_used.append(
                    {
                        "id": item_id,
                        "name": tname,
                        "input": tool_input,
                        "timestamp": asyncio.get_event_loop().time(),
                    }
                )

            if stream_callback:
                try:
                    await stream_callback(
                        StreamUpdate(
                            type="tool",
                            tool_calls=[
                                {
                                    "id": item_id,
                                    "name": tname,
                                    "input": tool_input,
                                    "phase": etype.split(".", 1)[1],
                                }
                            ],
                            metadata={
                                "tool_name": tname,
                                "phase": etype.split(".", 1)[1],
                                "status": item.get("status"),
                            },
                        )
                    )
                except Exception as cb_err:
                    logger.warning("Stream callback error", error=str(cb_err))
            return

        if item_type == "error":
            msg = item.get("message") or "error"
            if stream_callback and etype == "item.completed":
                try:
                    await stream_callback(
                        StreamUpdate(
                            type="error",
                            content=msg,
                            metadata={"is_error": True, "error": msg},
                        )
                    )
                except Exception as cb_err:
                    logger.warning("Stream callback error", error=str(cb_err))
            return

        # Unknown item.
        if stream_callback and etype == "item.completed":
            try:
                await stream_callback(
                    StreamUpdate(
                        type="system",
                        content=str(item)[:500],
                        metadata={"item_type": item_type},
                    )
                )
            except Exception as cb_err:
                logger.warning("Stream callback error", error=str(cb_err))
