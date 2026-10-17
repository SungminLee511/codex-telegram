"""Fork a Codex session by copying its rollout under a new thread id.

`codex exec` has no fork flag, but a rollout is just the session's history:
copying it to a new file whose ``session_meta.payload.id`` (and file name)
carry a fresh UUID gives a session `codex exec resume <new_id>` continues
from the same point, while the original file is never touched again.

Only the ``session_meta`` record names the id, so only that line is
rewritten; every other line is copied byte for byte. The new file is dated
now, so it sorts as the newest session.

Assumption (codex-rs rollout layout): resume locates a thread by scanning
``sessions/**/rollout-*-<id>.jsonl`` and trusts ``session_meta.payload.id``.
Verify with a live `codex exec resume` after the first real branch.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import structlog

logger = structlog.get_logger()


class ForkError(Exception):
    """The rollout could not be copied."""


def _rewrite_meta(line: bytes, old_id: str, new_id: str) -> bytes:
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return line
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return line
    payload = record.get("payload")
    if isinstance(payload, dict):
        payload["id"] = new_id
        # Recorded so a human (or a later tool) can see where it came from.
        payload["forked_from_id"] = old_id
    return json.dumps(record, ensure_ascii=False).encode("utf-8")


def fork_rollout(
    source: Path,
    sessions_dir: Path,
    old_id: str,
    now: Optional[datetime] = None,
    new_id: Optional[str] = None,
) -> Tuple[str, Path]:
    """Copy `source` to a new rollout. Returns (new thread id, new path)."""
    source = Path(source)
    if not source.is_file():
        raise ForkError(f"no rollout at {source}")
    now = now or datetime.now(timezone.utc)
    new_id = new_id or str(uuid.uuid4())

    folder = Path(sessions_dir) / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
    target = folder / f"rollout-{stamp}-{new_id}.jsonl"

    try:
        data = source.read_bytes()
    except OSError as exc:
        raise ForkError(str(exc)) from exc

    lines = data.split(b"\n")
    out = []
    rewritten = False
    for line in lines:
        if not rewritten and b"session_meta" in line:
            new_line = _rewrite_meta(line, old_id, new_id)
            rewritten = new_line is not line
            out.append(new_line)
        else:
            out.append(line)
    if not rewritten:
        raise ForkError("rollout has no session_meta record")

    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(b"\n".join(out))
        os.replace(tmp, target)
    except OSError as exc:
        raise ForkError(str(exc)) from exc

    logger.info("Forked rollout", old_id=old_id, new_id=new_id, path=str(target))
    return new_id, target
