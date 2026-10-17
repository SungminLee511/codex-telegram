"""-m / -c model_reasoning_effort reach the codex argv, resume included."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.codex.cli_integration import CodexCLIManager


def manager(**config):
    base = dict(codex_cli_path="/bin/codex", codex_model=None,
                codex_reasoning_effort=None, codex_skip_git_repo_check=True,
                codex_sandbox_mode=None, sandbox_enabled=True, codex_add_dirs=None,
                codex_dangerously_bypass=False, codex_config_overrides=None)
    base.update(config)
    mgr = CodexCLIManager.__new__(CodexCLIManager)
    mgr.config = SimpleNamespace(**base)
    return mgr


def argv(mgr, resume=False, **kw):
    return mgr._build_command(
        prompt_via_stdin=True, working_directory=Path("/w"),
        session_id="sid" if resume else None, continue_session=resume,
        image_files=[], **kw)


def pair(cmd, flag):
    return [cmd[i + 1] for i, x in enumerate(cmd) if x == flag]


@pytest.mark.parametrize("resume", [False, True])
def test_session_choice_overrides_config(resume):
    cmd = argv(manager(codex_model="gpt-6-sol", codex_reasoning_effort="low"),
               resume=resume, model="gpt-6-astra", effort="xhigh")
    assert pair(cmd, "-m") == ["gpt-6-astra"]
    assert "model_reasoning_effort=xhigh" in pair(cmd, "-c")


def test_config_default_when_session_has_none():
    cmd = argv(manager(codex_model="gpt-6-luna", codex_reasoning_effort="high"))
    assert pair(cmd, "-m") == ["gpt-6-luna"]
    assert "model_reasoning_effort=high" in pair(cmd, "-c")


def test_nothing_set_passes_nothing():
    cmd = argv(manager())
    assert "-m" not in cmd
    assert not any(c.startswith("model_reasoning_effort") for c in pair(cmd, "-c"))


def test_resume_keeps_session_id_last_before_stdin():
    cmd = argv(manager(), resume=True, model="gpt-6-sol")
    assert cmd[1:3] == ["exec", "resume"]
    assert cmd[-2:] == ["sid", "-"]
    assert "--cd" not in cmd and "-s" not in cmd
