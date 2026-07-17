from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import decode_stdout, wait_until, write_mock_config

from cliexec.adapter import NESTED_DELEGATION_INSTRUCTION


def _run_args(config: Path, cwd: Path, *extra: str) -> tuple[str, ...]:
    return ("run", "mock", "--config", str(config), "--cwd", str(cwd), *extra)


def _worker_prompt(prompt: str) -> str:
    return f"{prompt}\n\n{NESTED_DELEGATION_INSTRUCTION}"


def _session_result(*prompts: str) -> str:
    return "final:" + "|".join(_worker_prompt(prompt) for prompt in prompts)


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
    assert data["final_text"] == _session_result("review this")
    assert data["conversation_id"] is None
    assert data["parent_run_id"] is None
    assert data["resumable"] is False
    request_path = tmp_path / "state" / "cliexec" / "runs" / data["run_id"] / "request.json"
    stored_request = json.loads(request_path.read_text(encoding="utf-8"))
    assert stored_request["request"]["prompt"] == "review this"


def test_argv_input_substitutes_prompt_as_one_argument(tmp_path: Path, invoke_cli) -> None:
    config = write_mock_config(
        tmp_path / "config.toml",
        mode="argv",
        input_mode="argv",
    )

    result = invoke_cli(*_run_args(config, tmp_path), input="spaces ; $(are literal)")

    payload = decode_stdout(result)
    assert result.returncode == 0, result.stderr
    assert payload["data"]["final_text"] == f"argv:{_worker_prompt('spaces ; $(are literal)')}"


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
    assert data["partial_text"] == _session_result("keep me")
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


def test_generated_session_continues_exact_linear_conversation(tmp_path: Path, invoke_cli) -> None:
    session_root = tmp_path / "sessions"
    config = write_mock_config(
        tmp_path / "config.toml",
        command_args=["--session-root", str(session_root)],
        session_toml="""
id_strategy = "generated"
new_args = ["--session-id", "{session_id}"]
resume_args = ["--resume", "{session_id}"]
""",
    )

    first = invoke_cli(
        *_run_args(config, tmp_path, "--permission", "workspace_write"),
        input="first",
    )
    first_data = decode_stdout(first)["data"]
    first_id = first_data["run_id"]

    assert first.returncode == 0
    assert first_data["final_text"] == _session_result("first")
    assert first_data["conversation_id"]
    assert first_data["parent_run_id"] is None
    assert first_data["resumable"] is True
    assert "native_session_id" not in first_data

    second = invoke_cli(
        "run",
        "mock",
        "--config",
        str(config),
        "--continue",
        first_id,
        input="second",
    )
    second_data = decode_stdout(second)["data"]

    assert second.returncode == 0
    assert second_data["final_text"] == _session_result("first", "second")
    assert second_data["conversation_id"] == first_data["conversation_id"]
    assert second_data["parent_run_id"] == first_id
    assert second_data["permission"]["requested"] == "read_only"
    assert second_data["resumable"] is True

    first_status = decode_stdout(invoke_cli("status", first_id))["data"]
    assert first_status["resumable"] is False
    assert "continued_by_run_id" not in first_status
    runs_output = invoke_cli("runs").stdout
    assert "native_session_id" not in runs_output
    assert "session_claimed" not in runs_output
    assert "continued_by_run_id" not in runs_output

    branched = invoke_cli(
        "run",
        "mock",
        "--config",
        str(config),
        "--continue",
        first_id,
        input="branch",
    )
    branch_payload = decode_stdout(branched)

    assert branched.returncode == 2
    assert branch_payload["error"]["code"] == "CONVERSATION_CONFLICT"


def test_output_session_strategy_continues_jsonl_worker(tmp_path: Path, invoke_cli) -> None:
    session_root = tmp_path / "sessions"
    config = write_mock_config(
        tmp_path / "config.toml",
        mode="jsonl",
        output_format="jsonl",
        output_extra='match = { type = "result" }\nfield = "result.text"',
        command_args=["--session-root", str(session_root), "--output-session"],
        session_toml="""
id_strategy = "output"
resume_args = ["--resume", "{session_id}"]
id_match = { type = "session" }
id_field = "session_id"
""",
    )

    first = invoke_cli(*_run_args(config, tmp_path), input="first")
    first_data = decode_stdout(first)["data"]
    second = invoke_cli(
        "run",
        "mock",
        "--config",
        str(config),
        "--continue",
        first_data["run_id"],
        input="second",
    )
    second_data = decode_stdout(second)["data"]

    assert first.returncode == 0
    assert second.returncode == 0
    assert second_data["final_text"] == _session_result("first", "second")
    assert second_data["conversation_id"] == first_data["conversation_id"]


def test_missing_output_session_id_is_protocol_failure(tmp_path: Path, invoke_cli) -> None:
    config = write_mock_config(
        tmp_path / "config.toml",
        mode="jsonl",
        output_format="jsonl",
        output_extra='match = { type = "result" }\nfield = "result.text"',
        session_toml="""
id_strategy = "output"
resume_args = ["--resume", "{session_id}"]
id_match = { type = "session" }
id_field = "session_id"
""",
    )

    result = invoke_cli(*_run_args(config, tmp_path), input="prompt")
    data = decode_stdout(result)["data"]

    assert result.returncode == 1
    assert data["state"] == "failed"
    assert data["error"]["code"] == "PROTOCOL_ERROR"
    assert data["partial_text"] == _session_result("prompt")
    assert data["resumable"] is False


def test_adapter_without_session_contract_rejects_continuation(tmp_path: Path, invoke_cli) -> None:
    config = write_mock_config(tmp_path / "config.toml")
    first = invoke_cli(*_run_args(config, tmp_path), input="first")
    first_id = decode_stdout(first)["data"]["run_id"]

    continued = invoke_cli(
        "run",
        "mock",
        "--config",
        str(config),
        "--continue",
        first_id,
        input="second",
    )

    assert continued.returncode == 2
    assert decode_stdout(continued)["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_continued_run_rejects_a_different_cwd_without_consuming_tip(
    tmp_path: Path, invoke_cli
) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    config = write_mock_config(
        tmp_path / "config.toml",
        command_args=["--session-root", str(tmp_path / "sessions")],
        session_toml="""
id_strategy = "generated"
new_args = ["--session-id", "{session_id}"]
resume_args = ["--resume", "{session_id}"]
""",
    )
    first = invoke_cli(*_run_args(config, first_cwd), input="first")
    first_id = decode_stdout(first)["data"]["run_id"]

    wrong_cwd = invoke_cli(
        "run",
        "mock",
        "--config",
        str(config),
        "--continue",
        first_id,
        "--cwd",
        str(second_cwd),
        input="wrong",
    )
    wrong_agent = invoke_cli(
        "run",
        "agy",
        "--config",
        str(config),
        "--continue",
        first_id,
        input="wrong agent",
    )
    retry = invoke_cli(
        "run",
        "mock",
        "--config",
        str(config),
        "--continue",
        first_id,
        input="retry",
    )

    assert wrong_cwd.returncode == 2
    assert decode_stdout(wrong_cwd)["error"]["code"] == "INVALID_REQUEST"
    assert wrong_agent.returncode == 2
    assert decode_stdout(wrong_agent)["error"]["code"] == "INVALID_REQUEST"
    assert retry.returncode == 0
    assert decode_stdout(retry)["data"]["final_text"] == _session_result("first", "retry")


@pytest.mark.parametrize(
    ("mode", "command_args", "extra"),
    [
        ("text", ["--exit-code", "7"], ()),
        ("sleep", ["--sleep-seconds", "30"], ("--timeout", "0.2s")),
    ],
)
def test_failed_or_timed_out_session_can_be_continued(
    tmp_path: Path,
    invoke_cli,
    mode: str,
    command_args: list[str],
    extra: tuple[str, ...],
) -> None:
    session_root = tmp_path / "sessions"
    session_toml = """
id_strategy = "generated"
new_args = ["--session-id", "{session_id}"]
resume_args = ["--resume", "{session_id}"]
"""
    config = write_mock_config(
        tmp_path / "config.toml",
        mode=mode,
        command_args=["--session-root", str(session_root), *command_args],
        session_toml=session_toml,
    )
    first = invoke_cli(*_run_args(config, tmp_path, *extra), input="first")
    first_data = decode_stdout(first)["data"]

    assert first.returncode == 1
    assert first_data["state"] in {"failed", "timed_out"}
    assert first_data["resumable"] is True

    write_mock_config(
        config,
        command_args=["--session-root", str(session_root)],
        session_toml=session_toml,
    )
    continued = invoke_cli(
        "run",
        "mock",
        "--config",
        str(config),
        "--continue",
        first_data["run_id"],
        input="retry",
    )

    assert continued.returncode == 0
    assert decode_stdout(continued)["data"]["final_text"] == _session_result("first", "retry")


def test_cancelled_session_can_be_continued(tmp_path: Path, invoke_cli) -> None:
    session_root = tmp_path / "sessions"
    session_toml = """
id_strategy = "generated"
new_args = ["--session-id", "{session_id}"]
resume_args = ["--resume", "{session_id}"]
"""
    config = write_mock_config(
        tmp_path / "config.toml",
        mode="sleep",
        command_args=[
            "--session-root",
            str(session_root),
            "--sleep-seconds",
            "30",
        ],
        session_toml=session_toml,
    )
    started = invoke_cli(
        "start",
        "mock",
        "--config",
        str(config),
        "--cwd",
        str(tmp_path),
        input="first",
    )
    run_id = decode_stdout(started)["data"]["run_id"]
    cancelled = invoke_cli("cancel", run_id)

    assert cancelled.returncode == 0
    assert decode_stdout(cancelled)["data"]["resumable"] is True

    write_mock_config(
        config,
        command_args=["--session-root", str(session_root)],
        session_toml=session_toml,
    )
    continued = invoke_cli(
        "run",
        "mock",
        "--config",
        str(config),
        "--continue",
        run_id,
        input="retry",
    )

    assert continued.returncode == 0
    assert decode_stdout(continued)["data"]["final_text"] == _session_result("first", "retry")


def test_rejected_continuation_does_not_consume_parent_tip(tmp_path: Path, invoke_cli) -> None:
    session_root = tmp_path / "sessions"
    session_toml = """
id_strategy = "generated"
new_args = ["--session-id", "{session_id}"]
resume_args = ["--resume", "{session_id}"]
"""
    config = write_mock_config(
        tmp_path / "config.toml",
        command_args=["--session-root", str(session_root)],
        max_concurrency=1,
        session_toml=session_toml,
    )
    first = invoke_cli(*_run_args(config, tmp_path), input="first")
    first_id = decode_stdout(first)["data"]["run_id"]

    write_mock_config(
        config,
        mode="sleep",
        command_args=[
            "--session-root",
            str(session_root),
            "--sleep-seconds",
            "30",
        ],
        max_concurrency=1,
        session_toml=session_toml,
    )
    blocker_cwd = tmp_path / "blocker"
    blocker_cwd.mkdir()
    blocker = invoke_cli(
        "start",
        "mock",
        "--config",
        str(config),
        "--cwd",
        str(blocker_cwd),
        input="block",
    )
    blocker_id = decode_stdout(blocker)["data"]["run_id"]
    rejected = invoke_cli(
        "run",
        "mock",
        "--config",
        str(config),
        "--continue",
        first_id,
        input="rejected",
    )

    assert rejected.returncode == 2
    assert decode_stdout(rejected)["error"]["code"] == "CONCURRENCY_LIMIT"
    invoke_cli("cancel", blocker_id)

    write_mock_config(
        config,
        command_args=["--session-root", str(session_root)],
        max_concurrency=1,
        session_toml=session_toml,
    )
    retry = invoke_cli(
        "run",
        "mock",
        "--config",
        str(config),
        "--continue",
        first_id,
        input="retry",
    )

    assert retry.returncode == 0
    assert decode_stdout(retry)["data"]["final_text"] == _session_result("first", "retry")


def test_start_reserves_continuation_tip_while_child_is_active(tmp_path: Path, invoke_cli) -> None:
    session_root = tmp_path / "sessions"
    session_toml = """
id_strategy = "generated"
new_args = ["--session-id", "{session_id}"]
resume_args = ["--resume", "{session_id}"]
"""
    config = write_mock_config(
        tmp_path / "config.toml",
        command_args=["--session-root", str(session_root)],
        session_toml=session_toml,
    )
    first = invoke_cli(*_run_args(config, tmp_path), input="first")
    first_data = decode_stdout(first)["data"]

    write_mock_config(
        config,
        mode="sleep",
        command_args=[
            "--session-root",
            str(session_root),
            "--sleep-seconds",
            "30",
        ],
        session_toml=session_toml,
    )
    active = invoke_cli(
        "start",
        "mock",
        "--config",
        str(config),
        "--continue",
        first_data["run_id"],
        input="second",
    )
    active_data = decode_stdout(active)["data"]

    assert active.returncode == 0
    assert active_data["state"] == "running"
    assert active_data["parent_run_id"] == first_data["run_id"]
    assert active_data["conversation_id"] == first_data["conversation_id"]
    assert active_data["resumable"] is False

    duplicate = invoke_cli(
        "run",
        "mock",
        "--config",
        str(config),
        "--continue",
        first_data["run_id"],
        input="duplicate",
    )

    assert duplicate.returncode == 2
    assert decode_stdout(duplicate)["error"]["code"] == "CONVERSATION_CONFLICT"
    invoke_cli("cancel", active_data["run_id"])


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
