"""Builders for fake Codex rollout files."""

import json
from pathlib import Path

import pytest


def rollout_path(root: Path, session_id: str, day="2026/10/12") -> Path:
    folder = root.joinpath(*day.split("/"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"rollout-2026-10-12T05-33-33-{session_id}.jsonl"


def write_rollout(root: Path, session_id: str, records, day="2026/10/12") -> Path:
    path = rollout_path(root, session_id, day)
    with open(path, "a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def meta(session_id, cwd="/home", ts="2026-10-12T05:33:33Z"):
    return {"timestamp": ts, "type": "session_meta",
            "payload": {"id": session_id, "cwd": cwd, "timestamp": ts}}


def turn(model="gpt-6-sol", cwd="/home"):
    return {"timestamp": "2026-10-12T05:34:00Z", "type": "turn_context",
            "payload": {"cwd": cwd, "model": model, "approval_policy": "never"}}


def user(text):
    return {"timestamp": "2026-10-12T05:34:00Z", "type": "event_msg",
            "payload": {"type": "user_message", "message": text, "images": []}}


def injected(text):
    """Context Codex adds itself (AGENTS.md etc.) — never a human turn."""
    return {"timestamp": "2026-10-12T05:34:00Z", "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}


def agent(text):
    return {"timestamp": "2026-10-12T05:34:05Z", "type": "event_msg",
            "payload": {"type": "agent_message", "message": text}}


def tokens(total=None, inp=0, out=0, rate_limits=None, ts="2026-10-12T05:34:06Z"):
    last = {"input_tokens": inp, "cached_input_tokens": 0, "output_tokens": out}
    if total is not None:
        last["total_tokens"] = total
    payload = {"type": "token_count", "info": {"last_token_usage": last,
                                               "total_token_usage": last}}
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    return {"timestamp": ts, "type": "event_msg", "payload": payload}


@pytest.fixture()
def sessions(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    return root
