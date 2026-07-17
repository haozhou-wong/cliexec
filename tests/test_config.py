from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_mock_config

from cliexec.config import basic_environment, load_config
from cliexec.errors import CONFIG_ERROR, CLIExecError
from cliexec.models import Permission


def test_explicit_config_loads_declarative_agent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    config_path = write_mock_config(
        tmp_path / "explicit.toml",
        mode="jsonl",
        output_format="jsonl",
        output_extra='match = { type = "result" }\nfield = "result.text"',
        env_pass=["MOCK_TOKEN"],
        max_concurrency=2,
    )

    config = load_config(config_path)

    agent = config.agent("mock")
    assert config.policy.max_concurrency == 2
    assert agent.input.mode == "stdin"
    assert agent.output.format == "jsonl"
    assert agent.output.match == {"type": "result"}
    assert agent.output.field == "result.text"
    assert agent.env_pass == ("MOCK_TOKEN",)
    assert agent.supports(Permission.READ_ONLY)


def test_unknown_config_field_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    config_path = write_mock_config(tmp_path / "config.toml")
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\nunknown = true\n",
        encoding="utf-8",
    )

    with pytest.raises(CLIExecError) as raised:
        load_config(config_path)

    assert raised.value.code == CONFIG_ERROR
    assert "unknown" in raised.value.message


@pytest.mark.parametrize("field", ["enabled", "allow_unrestricted"])
def test_agent_boolean_fields_reject_strings(tmp_path: Path, monkeypatch, field: str) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    config_path = write_mock_config(tmp_path / "config.toml")
    content = config_path.read_text(encoding="utf-8").replace(
        f"{field} = false" if field == "allow_unrestricted" else f"{field} = true",
        f'{field} = "false"',
    )
    config_path.write_text(content, encoding="utf-8")

    with pytest.raises(CLIExecError) as raised:
        load_config(config_path)

    assert raised.value.code == CONFIG_ERROR
    assert f"agents.mock.{field} must be a boolean" in raised.value.message


def test_project_config_is_not_loaded_implicitly(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_mock_config(project / "config.toml")
    monkeypatch.chdir(project)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-user-config"))

    config = load_config()

    assert "mock" not in config.agents


def test_basic_environment_only_passes_allowlisted_names(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("VISIBLE_TOKEN", "yes")
    monkeypatch.setenv("HIDDEN_TOKEN", "no")
    monkeypatch.setenv("LC_TEST", "locale")

    environment = basic_environment(("VISIBLE_TOKEN",))

    assert environment["PATH"] == "/usr/bin"
    assert environment["VISIBLE_TOKEN"] == "yes"
    assert environment["LC_TEST"] == "locale"
    assert "HIDDEN_TOKEN" not in environment


def test_input_argument_templates_require_path_placeholder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    config_path = write_mock_config(tmp_path / "config.toml")
    content = config_path.read_text(encoding="utf-8").replace(
        '[agents.mock.input]\nmode = "stdin"',
        '[agents.mock.input]\nmode = "stdin"\nfile_args = ["--file"]',
    )
    config_path.write_text(content, encoding="utf-8")

    with pytest.raises(CLIExecError) as raised:
        load_config(config_path)

    assert raised.value.code == CONFIG_ERROR
    assert "{path}" in raised.value.message


def test_generated_session_contract_loads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    config_path = write_mock_config(tmp_path / "config.toml")
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + """
[agents.mock.session]
id_strategy = "generated"
new_args = ["--session-id", "{session_id}"]
resume_args = ["--resume", "{session_id}"]
""",
        encoding="utf-8",
    )

    agent = load_config(config_path).agent("mock")

    assert agent.session is not None
    assert agent.session.id_strategy == "generated"
    assert agent.session.new_args == ("--session-id", "{session_id}")
    assert agent.capabilities()["sessions"] is True


def test_output_session_contract_loads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    config_path = write_mock_config(
        tmp_path / "config.toml",
        mode="jsonl",
        output_format="jsonl",
        output_extra='match = { type = "result" }\nfield = "result.text"',
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + """
[agents.mock.session]
id_strategy = "output"
resume_args = ["--resume", "{session_id}"]
id_match = { type = "session" }
id_field = "session.id"
""",
        encoding="utf-8",
    )

    session = load_config(config_path).agent("mock").session

    assert session is not None
    assert session.id_match == {"type": "session"}
    assert session.id_field == "session.id"


@pytest.mark.parametrize(
    "session_toml",
    [
        'id_strategy = "generated"\nresume_args = ["--resume", "{session_id}"]',
        'id_strategy = "output"\nresume_args = ["--resume", "{session_id}"]',
        'id_strategy = "output"\nresume_args = ["--resume"]\nid_field = "session_id"',
        (
            'id_strategy = "output"\nnew_args = ["--id", "{session_id}"]\n'
            'resume_args = ["--resume", "{session_id}"]\nid_field = "session_id"'
        ),
    ],
)
def test_invalid_session_contract_is_rejected(
    tmp_path: Path, monkeypatch, session_toml: str
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    config_path = write_mock_config(
        tmp_path / "config.toml",
        mode="jsonl",
        output_format="jsonl",
        output_extra='match = { type = "result" }\nfield = "result.text"',
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + f"\n[agents.mock.session]\n{session_toml}\n",
        encoding="utf-8",
    )

    with pytest.raises(CLIExecError) as raised:
        load_config(config_path)

    assert raised.value.code == CONFIG_ERROR
