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
