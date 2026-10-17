"""Telegram file uploads -> on-disk inbox Codex can read.

Previously an uploaded document was decoded to UTF-8 and *inlined* into the
prompt (truncated at 50 kB), which meant PDFs, images, archives and anything
binary were rejected outright. Saving the bytes to a real path instead lets
Codex use its normal file tools on them, and removes the size-of-prompt
ceiling.

Files land in `<repo>/uploads/<YYYY-MM-DD>/<msg_id>_<name>` — inside the
approved directory so no permission prompt is needed, and gitignored so
uploads never end up in a commit.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

# src/bot/utils/uploads.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
UPLOAD_ROOT = REPO_ROOT / "uploads"

# Small text files are also inlined so Codex sees the content without
# spending a tool call on a Read.
INLINE_TEXT_LIMIT = 20 * 1024
RETENTION_DAYS = 30

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: Optional[str]) -> str:
    name = (name or "upload.bin").strip() or "upload.bin"
    name = Path(name).name  # defeat any path traversal in the client-sent name
    name = _SAFE_NAME.sub("_", name)
    return name[:120]


def upload_dir(now: Optional[float] = None) -> Path:
    stamp = time.strftime("%Y-%m-%d", time.localtime(now or time.time()))
    target = UPLOAD_ROOT / stamp
    target.mkdir(parents=True, exist_ok=True)
    return target


def destination(filename: Optional[str], message_id: int) -> Path:
    """Collision-free path for an incoming file."""
    base = _safe_filename(filename)
    target = upload_dir() / f"{message_id}_{base}"
    counter = 1
    while target.exists():
        target = upload_dir() / f"{message_id}_{counter}_{base}"
        counter += 1
    return target


async def save_telegram_file(tg_file, filename: Optional[str], message_id: int) -> Path:
    """Download a `telegram.File` into the inbox and return its path."""
    path = destination(filename, message_id)
    await tg_file.download_to_drive(str(path))
    logger.info("Saved upload", path=str(path), size=path.stat().st_size)
    return path


def save_bytes(data: bytes, filename: Optional[str], message_id: int) -> Path:
    path = destination(filename, message_id)
    path.write_bytes(data)
    logger.info("Saved upload", path=str(path), size=len(data))
    return path


def human_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.0f} KB"
    return f"{num / 1024 / 1024:.1f} MB"


def read_inline_text(path: Path) -> Optional[str]:
    """Return the file's text if it is small and decodable, else None."""
    try:
        if path.stat().st_size > INLINE_TEXT_LIMIT:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def build_prompt(path: Path, caption: Optional[str], kind: str = "file") -> str:
    """Prompt text describing an uploaded file by path (plus inline preview)."""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    caption = (caption or "").strip()
    lines = [
        caption or f"I uploaded a {kind}.",
        "",
        f"[uploaded {kind}] `{path}` ({human_size(size)})",
    ]

    inline = read_inline_text(path)
    if inline is not None:
        lines += ["", f"```\n{inline}\n```"]
    else:
        lines += ["", "Read it from that path when you need its contents."]

    return "\n".join(lines)


def cleanup(retention_days: int = RETENTION_DAYS, now: Optional[float] = None) -> int:
    """Delete day-folders older than the retention window. Returns count."""
    if not UPLOAD_ROOT.is_dir():
        return 0
    now = now or time.time()
    cutoff = now - retention_days * 86400
    removed = 0
    for day_dir in UPLOAD_ROOT.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            if day_dir.stat().st_mtime >= cutoff:
                continue
            for child in day_dir.rglob("*"):
                if child.is_file():
                    child.unlink()
                    removed += 1
            for child in sorted(
                day_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True
            ):
                if child.is_dir():
                    child.rmdir()
            day_dir.rmdir()
        except OSError as exc:
            logger.debug("upload cleanup failed", dir=str(day_dir), error=str(exc))
    if removed:
        logger.info("Cleaned old uploads", files=removed)
    return removed
