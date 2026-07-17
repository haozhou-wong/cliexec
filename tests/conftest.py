from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOCK_AGENT = ROOT / "tests" / "helpers" / "mock_agent.py"


@pytest.fixture
def isolated_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "PYTHONPATH": str(ROOT / "src"),
        }
    )
    return env


@pytest.fixture
def invoke_cli(isolated_env: dict[str, str]) -> Callable[..., subprocess.CompletedProcess[str]]:
    def invoke(
        *args: str,
        input: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "cliexec", *args],
            cwd=ROOT,
            env=env or isolated_env,
            input=input,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    return invoke


def write_mock_config(
    path: Path,
    *,
    mode: str = "text",
    output_format: str = "text",
    command_args: list[str] | None = None,
    input_mode: str = "stdin",
    output_extra: str = "",
    env_pass: list[str] | None = None,
    max_concurrency: int = 4,
    max_output_bytes: int = 67_108_864,
    session_toml: str = "",
) -> Path:
    args = [sys.executable, str(MOCK_AGENT), "--mode", mode, *(command_args or [])]
    command = json.dumps(args)
    passed = json.dumps(env_pass or [])
    prompt_arg = '\nprompt_arg = "{prompt}"' if input_mode == "argv" else ""
    inline_result_bytes = min(262_144, max_output_bytes)
    session_block = (
        f"\n[agents.mock.session]\n{session_toml.strip()}\n" if session_toml.strip() else ""
    )
    path.write_text(
        f"""\
version = 1

[policy]
max_concurrency = {max_concurrency}
default_timeout = "30m"
max_timeout = "2h"
max_permission = "workspace_write"
retention_days = 30
inline_result_bytes = {inline_result_bytes}
max_output_bytes = {max_output_bytes}

[agents.mock]
enabled = true
command = {command}
success_exit_codes = [0]
allow_unrestricted = false

[agents.mock.input]
mode = "{input_mode}"{prompt_arg}

[agents.mock.output]
format = "{output_format}"
{output_extra}

[agents.mock.modes.read_only]
args = []

[agents.mock.modes.workspace_write]
args = []

[agents.mock.env]
pass = {passed}
{session_block}
""",
        encoding="utf-8",
    )
    return path


def decode_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.stdout, f"missing JSON output; stderr={result.stderr!r}"
    return json.loads(result.stdout)


def wait_until(predicate: Callable[[], bool], *, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition was not met before timeout")
