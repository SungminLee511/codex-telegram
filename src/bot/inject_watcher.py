"""Synthetic-message injector for Codex self-wake.

Watches a JSON file on disk and pushes its text into the Telegram update
queue as if the user sent it. This lets detached shell jobs wake the Codex
bot after long-running work without touching the Claude bot.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

import structlog
from telegram import Update
from telegram.ext import Application

logger = structlog.get_logger()


DEFAULT_INJECT_PATH = Path(
    os.environ.get(
        "CODEX_INJECT_PATH",
        Path(__file__).resolve().parents[2] / "data" / "codex_inject_message.json",
    )
)
POLL_INTERVAL_SECONDS = 2.0


def _build_synthetic_update_payload(
    chat_id: int,
    text: str,
    user_id: Optional[int] = None,
    first_name: str = "CodexWake",
    message_id: Optional[int] = None,
) -> dict:
    """Construct a Telegram Update payload for a private text message."""
    if user_id is None:
        user_id = chat_id
    if message_id is None:
        message_id = int(time.time() * 1000) % (2**31)
    now = int(time.time())
    return {
        "update_id": int(time.time() * 1000) % (2**31),
        "message": {
            "message_id": message_id,
            "date": now,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": first_name,
            },
            "text": text,
        },
    }


async def inject_watcher_loop(
    app: Application,
    inject_path: Path = DEFAULT_INJECT_PATH,
    poll_seconds: float = POLL_INTERVAL_SECONDS,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Background task: poll for the inject file, push synthetic Updates."""
    logger.info(
        "codex inject_watcher started",
        path=str(inject_path),
        poll_seconds=poll_seconds,
    )

    bot = app.bot

    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("codex inject_watcher stopping")
            return

        try:
            if inject_path.exists():
                processed_path = inject_path.with_suffix(
                    f".processed-{int(time.time())}"
                )
                try:
                    raw = inject_path.read_text()
                    os.rename(inject_path, processed_path)
                except FileNotFoundError:
                    raw = None

                if raw is not None:
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            "codex inject_watcher: bad JSON",
                            error=str(e),
                            raw=raw[:200],
                        )
                        payload = None

                    if payload is not None:
                        chat_id = int(payload.get("chat_id", 0))
                        text = str(payload.get("text", "")).strip()
                        user_id = payload.get("user_id")
                        if user_id is not None:
                            user_id = int(user_id)

                        if not text or not chat_id:
                            logger.warning(
                                "codex inject_watcher: missing chat_id or text",
                                payload=payload,
                            )
                        else:
                            placeholder = await bot.send_message(
                                chat_id=chat_id,
                                text=f"[codex auto-wake: {text[:80]}]",
                            )
                            update_dict = _build_synthetic_update_payload(
                                chat_id=chat_id,
                                text=text,
                                user_id=user_id,
                                message_id=placeholder.message_id,
                            )
                            update = Update.de_json(update_dict, bot)
                            await app.update_queue.put(update)
                            logger.info(
                                "codex inject_watcher fired synthetic message",
                                chat_id=chat_id,
                                text_preview=text[:80],
                                real_msg_id=placeholder.message_id,
                            )
        except Exception as e:
            logger.error("codex inject_watcher loop error", error=str(e))

        await asyncio.sleep(poll_seconds)
