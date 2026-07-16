from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import decode_stdout, wait_until, write_mock_config


def _run_args(config: Path, cwd: Path, *extra: str) -> tuple[str, ...]:
    return ("run", "mock", "--config", str(config), "--cwd", str(cwd), *extra)


@pytest.mark.parametrize(
    ("mode", "output_format", "output_extra"),
    [
        ("text", "text", ""),
        ("json", "json", 'field = "result.text"'),
        (
            "jsonl",
            "jsonl",
            'match = { type = "result" }\nfield = "result.text"\ncollect = "last"',
        ),
    ],
)
def test_run_parses_text_json_and_jsonl(
    tmp_path: Path,
    invoke_cli,
    mode: str,
    output_format: str,
    output_extra: str,
) -> None:
    config = write_mock_config(
        tmp_path / "config.toml",
        mode=mode,
        output_format=output_format,
        output_extra=output_extra,
    )

    result = invoke_cli(*_run_args(config, tmp_path), input="review this")

    payload = decode_stdout(result)
    assert result.returncode == 0, result.stderr
    assert payload["ok"] is True
    data = payload["data"]
    assert data["state"] == "completed"
    assert data["succeeded"] is True
    assert data["final_text"] == "final:review this"


def test_argv_input_substitutes_prompt_as_one_argument(tmp_path: Path, invoke_cli) -> None:
    config = write_mock_config(
        tmp_path / "config.toml",
        mode="argv",
        input_mode="argv",
    )

    result = invoke_cli(*_run_args(config, tmp_path), input="spaces ; $(are literal)")

    payload = decode_stdout(result)
    assert result.returncode == 0, result.stderr
    assert payload["data"]["final_text"] == "argv:spaces ; $(are literal)"


@pytest.mark.parametrize("mode", ["malformed-json", "empty"])
def test_protocol_failure_is_terminal(tmp_path: Path, invoke_cli, mode: str) -> None:
    config = write_mock_config(
        tmp_path / "config.toml",
        mode=mode,
        output_format="json",
        output_extra='field = "result.text"',
    )

    result = invoke_cli(*_run_args(config, tmp_path), input="prompt")

    payload = decode_stdout(result)
    assert result.returncode == 1
    assert payload["data"]["state"] == "failed"
    assert payload["data"]["error"]["code"] == "PROTOCOL_ERROR"


def test_nonzero_exit_preserves_partial_text(tmp_path: Path, invoke_cli) -> None:
    config = write_mock_config(
        tmp_path / "config.toml",
        command_args=["--exit-code", "7"],
    )

    result = invoke_cli(*_run_args(config, tmp_path), input="keep me")

    payload = decode_stdout(result)
    data = payload["data"]
    assert result.returncode == 1
    assert data["state"] == "failed"
    assert data["exit_code"] == 7
    assert data["partial_text"] == "final:keep me"
    assert data["error"]["code"] == "NONZERO_EXIT"


def test_timeout_sets_terminal_state_and_kills_process_tree(tmp_path: Path, invoke_cli) -> None:
    agent_pid = tmp_path / "timeout-agent.pid"
    child_pid = tmp_path / "timeout-child.pid"
    config = write_mock_config(
        tmp_path / "config.toml",
        mode="spawn-child",
        command_args=[
            "--sleep-seconds",
            "30",
            "--pid-file",
            str(agent_pid),
            "--child-pid-file",
            str(child_pid),
        ],
    )

    result = invoke_cli(*_run_args(config, tmp_path, "--timeout", "0.2s"), input="prompt")

    payload = decode_stdout(result)
    assert result.returncode == 1
    assert payload["data"]["state"] == "timed_out"
    assert payload["data"]["error"]["code"] == "TIMEOUT"
    assert not _process_is_running(int(agent_pid.read_text()))
    assert not _process_is_running(int(child_pid.read_text()))


def test_cancel_terminates_entire_process_group(tmp_path: Path, invoke_cli) -> None:
    agent_pid = tmp_path / "agent.pid"
    child_pid = tmp_path / "child.pid"
    config = write_mock_config(
        tmp_path / "config.toml",
        mode="spawn-child",
        command_args=[
            "--sleep-seconds",
            "30",
            "--pid-file",
            str(agent_pid),
            "--child-pid-file",
            str(child_pid),
        ],
    )
    start = invoke_cli(
        "start",
        "mock",
        "--config",
        str(config),
        "--cwd",
        str(tmp_path),
        input="prompt",
    )
    start_payload = decode_stdout(start)
    assert start.returncode == 0, start.stderr
    run_id = start_payload["data"]["run_id"]
    wait_until(lambda: child_pid.exists())

    cancelled = invoke_cli("cancel", run_id)

    assert cancelled.returncode == 0, cancelled.stderr
    wait_until(lambda: not _process_is_running(int(agent_pid.read_text())))
    wait_until(lambda: not _process_is_running(int(child_pid.read_text())))
    status = invoke_cli("status", run_id)
    assert decode_stdout(status)["data"]["state"] == "cancelled"


def test_environment_is_allowlisted(
    tmp_path: Path,
    invoke_cli,
    isolated_env: dict[str, str],
) -> None:
    config = write_mock_config(
        tmp_path / "config.toml",
        mode="environment",
        command_args=["--env-name", "PASSED_VALUE", "--env-name", "SECRET_VALUE"],
        env_pass=["PASSED_VALUE"],
    )
    env = {**isolated_env, "PASSED_VALUE": "visible", "SECRET_VALUE": "hidden"}

    result = invoke_cli(*_run_args(config, tmp_path), input="ignored", env=env)

    data = decode_stdout(result)["data"]
    values = json.loads(data["final_text"])
    assert values == {"PASSED_VALUE": "visible", "SECRET_VALUE": None}


def test_output_limit_fails_without_unbounded_capture(tmp_path: Path, invoke_cli) -> None:
    config = write_mock_config(
        tmp_path / "config.toml",
        mode="large-output",
        command_args=["--output-bytes", "65536"],
        max_output_bytes=4096,
    )

    result = invoke_cli(*_run_args(config, tmp_path), input="ignored")

    payload = decode_stdout(result)
    assert result.returncode == 1
    assert payload["data"]["state"] == "failed"
    assert payload["data"]["error"]["code"] == "OUTPUT_LIMIT"


def test_global_concurrency_limit_rejects_instead_of_queueing(tmp_path: Path, invoke_cli) -> None:
    config = write_mock_config(
        tmp_path / "config.toml",
        mode="sleep",
        command_args=["--sleep-seconds", "30"],
        max_concurrency=1,
    )
    first_cwd = tmp_path / "one"
    second_cwd = tmp_path / "two"
    first_cwd.mkdir()
    second_cwd.mkdir()
    first = invoke_cli(
        "start",
        "mock",
        "--config",
        str(config),
        "--cwd",
        str(first_cwd),
        input="one",
    )
    assert first.returncode == 0, first.stderr
    first_id = decode_stdout(first)["data"]["run_id"]

    second = invoke_cli(
        "start",
        "mock",
        "--config",
        str(config),
        "--cwd",
        str(second_cwd),
        input="two",
    )

    second_payload = decode_stdout(second)
    assert second.returncode == 2
    assert second_payload["error"]["code"] == "CONCURRENCY_LIMIT"
    invoke_cli("cancel", first_id)


def test_nested_delegation_fails_fast(
    tmp_path: Path,
    invoke_cli,
    isolated_env: dict[str, str],
) -> None:
    config = write_mock_config(tmp_path / "config.toml")
    env = {**isolated_env, "CLIEXEC_DEPTH": "1"}

    result = invoke_cli(*_run_args(config, tmp_path), input="nested", env=env)

    payload = decode_stdout(result)
    assert result.returncode == 2
    assert payload["error"]["code"] == "NESTED_DELEGATION"


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        fields = stat.read_text(encoding="utf-8").split()
        return len(fields) > 2 and fields[2] != "Z"
    return True
