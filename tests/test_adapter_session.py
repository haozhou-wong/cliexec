from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cliexec.adapter import build_command, parse_session_id
from cliexec.config import AgentConfig, InputConfig, OutputConfig, SessionConfig
from cliexec.errors import PROTOCOL_ERROR, CLIExecError
from cliexec.models import Permission, TaskRequest


def _agent(session: SessionConfig) -> AgentConfig:
    return AgentConfig(
        name="mock",
        command=(sys.executable, "worker.py"),
        input=InputConfig(mode="stdin", image_args=("--image", "{path}")),
        output=OutputConfig(
            format="jsonl",
            match={"type": "result"},
            field="text",
        ),
        session=session,
        modes={Permission.READ_ONLY: ("--read-only",)},
    )


def _request(cwd: Path) -> TaskRequest:
    return TaskRequest(
        agent="mock",
        prompt="next turn",
        cwd=cwd,
        images=[cwd / "image.png"],
    )


def test_generated_session_args_are_used_for_new_and_resumed_runs(tmp_path: Path) -> None:
    agent = _agent(
        SessionConfig(
            id_strategy="generated",
            new_args=("--session-id", "{session_id}"),
            resume_args=("--resume", "{session_id}"),
        )
    )
    request = _request(tmp_path)

    initial = build_command(agent, request, run_id="run-1", native_session_id="session-1")
    resumed = build_command(
        agent,
        request,
        run_id="run-2",
        native_session_id="session-1",
        resume=True,
    )

    assert initial.argv[-5:] == [
        "--read-only",
        "--session-id",
        "session-1",
        "--image",
        str(tmp_path / "image.png"),
    ]
    assert resumed.argv[-5:] == [
        "--read-only",
        "--resume",
        "session-1",
        "--image",
        str(tmp_path / "image.png"),
    ]


def test_output_session_id_is_extracted_and_deduplicated(tmp_path: Path) -> None:
    agent = _agent(
        SessionConfig(
            id_strategy="output",
            resume_args=("--resume", "{session_id}"),
            id_match={"type": "session"},
            id_field="session.id",
        )
    )
    output = tmp_path / "stdout.jsonl"
    output.write_text(
        "\n".join(
            json.dumps(value)
            for value in (
                {"type": "session", "session": {"id": "session-1"}},
                {"type": "result", "text": "done", "session": {"id": "session-1"}},
                {"type": "session", "session": {"id": "session-1"}},
            )
        ),
        encoding="utf-8",
    )

    assert parse_session_id(output, agent) == "session-1"


def test_conflicting_output_session_ids_are_rejected(tmp_path: Path) -> None:
    agent = _agent(
        SessionConfig(
            id_strategy="output",
            resume_args=("--resume", "{session_id}"),
            id_field="session_id",
        )
    )
    output = tmp_path / "stdout.jsonl"
    output.write_text(
        '{"session_id":"one"}\n{"session_id":"two"}\n',
        encoding="utf-8",
    )

    with pytest.raises(CLIExecError) as raised:
        parse_session_id(output, agent)

    assert raised.value.code == PROTOCOL_ERROR
    assert "conflicting" in raised.value.message
