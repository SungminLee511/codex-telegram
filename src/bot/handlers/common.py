"""Helpers the reply path needs, independent of any one handler.

Lifted out of the old classic-mode `message.py` when that mode was deleted:
these two were the only parts of it the agentic path actually used, and
leaving them there would have meant keeping 1,300 lines of dead code to host
them.
"""

from __future__ import annotations

import structlog

from ...codex.exceptions import (
    CodexError,
    CodexMCPError,
    CodexParsingError,
    CodexProcessError,
    CodexSessionError,
    CodexTimeoutError,
)
from ..utils.html_format import escape_html

logger = structlog.get_logger()


def _update_working_directory_from_codex_response(
    codex_response, context, settings, user_id
):
    """Update the working directory based on Codex's response content."""
    import re
    from pathlib import Path

    # Look for directory changes in Codex's response
    # This searches for common patterns that indicate directory changes
    patterns = [
        r"(?:^|\n).*?cd\s+([^\s\n]+)",  # cd command
        r"(?:^|\n).*?Changed directory to:?\s*([^\s\n]+)",  # explicit directory change
        r"(?:^|\n).*?Current directory:?\s*([^\s\n]+)",  # current directory indication
        r"(?:^|\n).*?Working directory:?\s*([^\s\n]+)",  # working directory indication
    ]

    content = codex_response.content.lower()
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    for pattern in patterns:
        matches = re.findall(pattern, content, re.MULTILINE | re.IGNORECASE)
        for match in matches:
            try:
                # Clean up the path
                new_path = match.strip().strip("\"'`")

                # Handle relative paths
                if new_path.startswith("./") or new_path.startswith("../"):
                    new_path = (current_dir / new_path).resolve()
                elif not new_path.startswith("/"):
                    # Relative path without ./
                    new_path = (current_dir / new_path).resolve()
                else:
                    # Absolute path
                    new_path = Path(new_path).resolve()

                # Validate that the new path is within the approved directory
                if (
                    new_path.is_relative_to(settings.approved_directory)
                    and new_path.exists()
                ):
                    context.user_data["current_directory"] = new_path
                    logger.info(
                        "Updated working directory from Codex response",
                        old_dir=str(current_dir),
                        new_dir=str(new_path),
                        user_id=user_id,
                    )
                    return  # Take the first valid match

            except (ValueError, OSError) as e:
                # Invalid path, skip this match
                logger.debug(
                    "Invalid path in Codex response", path=match, error=str(e)
                )
                continue


def _format_error_message(error: Exception | str) -> str:
    """Format error messages for user-friendly display.

    Accepts an exception object (preferred) or a string for backward
    compatibility.  When an exception is provided, the error type is used
    to produce a specific, actionable message.
    """
    # Normalise: keep both the object and a string representation.
    if isinstance(error, str):
        error_str = error
        error_obj: Exception | None = None
    else:
        error_str = str(error)
        error_obj = error

    # --- Dispatch on exception type first (most specific) ---

    if isinstance(error_obj, CodexTimeoutError):
        return (
            "⏰ <b>Request Timeout</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Try breaking your request into smaller parts\n"
            "• Avoid asking for very large file operations in one go\n"
            "• Try again — transient slowdowns happen"
        )

    if isinstance(error_obj, CodexMCPError):
        server_hint = ""
        if error_obj.server_name:
            server_hint = f" (<code>{escape_html(error_obj.server_name)}</code>)"
        return (
            f"🔌 <b>MCP Server Error</b>{server_hint}\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Check that the MCP server is running and reachable\n"
            "• Verify <code>MCP_CONFIG_PATH</code> points to a valid config\n"
            "• Ask the administrator to check MCP server logs"
        )

    if isinstance(error_obj, CodexParsingError):
        return (
            "📄 <b>Response Parsing Error</b>\n\n"
            f"Codex returned a response that could not be parsed:\n"
            f"<code>{escape_html(error_str[:300])}</code>\n\n"
            "<b>What you can do:</b>\n"
            "• Try your request again\n"
            "• Rephrase your prompt if the problem persists"
        )

    if isinstance(error_obj, CodexSessionError):
        return (
            "🔄 <b>Session Error</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Use /new to start a fresh session\n"
            "• Try your request again\n"
            "• Use /status to check your current session"
        )

    if isinstance(error_obj, CodexProcessError):
        return _format_process_error(error_str)

    # Any future CodexError subtypes not explicitly handled above —
    # preserve their existing message as-is rather than downgrading
    # to a generic "process error".
    if isinstance(error_obj, CodexError):
        safe_error = escape_html(error_str)
        if len(safe_error) > 500:
            safe_error = safe_error[:500] + "..."
        return (
            f"❌ <b>Codex Error</b>\n\n"
            f"{safe_error}\n\n"
            f"Try again or use /new to start a fresh session."
        )

    # --- Fall back to keyword matching (for string-only callers) --------
    # These patterns match the known error prefixes produced by
    # cli_integration.py and facade.py, NOT arbitrary user content.

    error_lower = error_str.lower()

    if "usage limit reached" in error_lower or "usage limit" in error_lower:
        return error_str  # Already user-friendly

    if "tool not allowed" in error_lower:
        return error_str  # Already formatted by facade.py

    if "no conversation found" in error_lower:
        return (
            "🔄 <b>Session Not Found</b>\n\n"
            "The previous Codex session could not be found or has expired.\n\n"
            "<b>What you can do:</b>\n"
            "• Use /new to start a fresh session\n"
            "• Try your request again\n"
            "• Use /status to check your current session"
        )

    if "rate limit" in error_lower:
        return (
            "⏱️ <b>Rate Limit Reached</b>\n\n"
            "Too many requests in a short time period.\n\n"
            "<b>What you can do:</b>\n"
            "• Wait a moment before trying again\n"
            "• Use simpler requests\n"
            "• Check your current usage with /status"
        )

    if "timed out after" in error_lower or "codex timed out" in error_lower:
        return (
            "⏰ <b>Request Timeout</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Try breaking your request into smaller parts\n"
            "• Avoid asking for very large file operations in one go\n"
            "• Try again — transient slowdowns happen"
        )

    if "overloaded" in error_lower:
        return (
            "🏗️ <b>Codex is Overloaded</b>\n\n"
            "The Codex API is currently experiencing high demand.\n\n"
            "<b>What you can do:</b>\n"
            "• Wait a moment and try again\n"
            "• Shorter prompts may succeed more easily"
        )

    if (
        "invalid api key" in error_lower
        or "authentication_error" in error_lower
        or "401 unauthorized" in error_lower
    ):
        return (
            "🔑 <b>API Authentication Error</b>\n\n"
            "Codex is not logged in, or its API key is invalid or expired.\n\n"
            "<b>What you can do:</b>\n"
            "• Run <code>codex login</code> on the host, or\n"
            "• Ask the administrator to verify <code>OPENAI_API_KEY</code>"
        )

    # Match known CLI prefixes: "Failed to connect to Codex: ..."
    # and "MCP server connection failed: ..."
    if error_lower.startswith("failed to connect to codex"):
        return (
            "🌐 <b>Connection Error</b>\n\n"
            f"Could not connect to Codex:\n"
            f"<code>{escape_html(error_str[:300])}</code>\n\n"
            "<b>What you can do:</b>\n"
            "• Check your network / firewall settings\n"
            "• Verify the Codex CLI is installed and accessible\n"
            "• Try again in a moment"
        )

    # Match known CLI prefix: "Codex CLI not found. ..."
    if error_lower.startswith("codex cli not found"):
        return (
            "🔍 <b>Codex CLI Not Found</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Ensure Codex is installed: "
            "<code>npm install -g @openai/codex</code>\n"
            "• Set the <code>CODEX_CLI_PATH</code> environment variable"
        )

    # Match known CLI prefixes: "MCP server error: ..." and
    # "MCP server connection failed: ..."
    if error_lower.startswith("mcp server"):
        return (
            "🔌 <b>MCP Server Error</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Check that the MCP server is running\n"
            "• Verify MCP configuration\n"
            "• Ask the administrator to check MCP server logs"
        )

    # --- No match — show the raw error as-is ---
    safe_error = escape_html(error_str)
    if len(safe_error) > 500:
        safe_error = safe_error[:500] + "..."

    return f"❌ {safe_error}"


def _format_process_error(error_str: str) -> str:
    """Format a Codex process/CLI error with the actual details."""
    safe_error = escape_html(error_str)
    if len(safe_error) > 500:
        safe_error = safe_error[:500] + "..."

    return (
        f"❌ <b>Codex Process Error</b>\n\n"
        f"{safe_error}\n\n"
        "<b>What you can do:</b>\n"
        "• Try your request again\n"
        "• Use /new to start a fresh session if the problem persists\n"
        "• Check /status for current session state"
    )
