from cliexec.config import load_config
from cliexec.preset_loader import load_builtin_presets

EXPECTED_PRESETS = {"agy", "claude", "codex", "grok", "opencode"}
SESSION_PRESETS = {"claude", "codex", "grok", "opencode"}


def test_loads_all_builtin_presets() -> None:
    presets = load_builtin_presets()

    assert set(presets) == EXPECTED_PRESETS


def test_builtin_presets_are_valid_core_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    config = load_config()

    assert set(config.agents) == EXPECTED_PRESETS
    assert all(agent.builtin for agent in config.agents.values())


def test_presets_follow_the_declarative_adapter_contract() -> None:
    for name, preset in load_builtin_presets().items():
        assert preset["enabled"] is True, name
        assert preset["command"] and all(isinstance(arg, str) for arg in preset["command"]), name
        assert preset["success_exit_codes"] == [0], name
        assert preset["allow_unrestricted"] is False, name

        input_config = preset["input"]
        assert input_config["mode"] in {"stdin", "argv"}, name
        if input_config["mode"] == "argv":
            assert input_config["prompt_arg"].count("{prompt}") == 1, name

        output_config = preset["output"]
        assert output_config["format"] in {"text", "json", "jsonl"}, name
        assert output_config["collect"] in {"first", "last", "concat"}, name

        if name in SESSION_PRESETS:
            session = preset["session"]
            assert session["id_strategy"] in {"generated", "output"}, name
            assert any("{session_id}" in arg for arg in session["resume_args"]), name
        else:
            assert "session" not in preset, name

        assert set(preset["modes"]) == {"read_only", "workspace_write", "unrestricted"}, name
        assert all(isinstance(value["args"], list) for value in preset["modes"].values()), name

        probe = preset["probe"]
        assert probe["version_args"], name
        assert "?P<version>" in probe["version_regex"], name
        assert probe["tested_versions"], name
        assert probe["help_args"], name
        assert probe["help_contains"], name


def test_loader_returns_fresh_values() -> None:
    first = load_builtin_presets()
    first["codex"]["command"].append("changed")

    second = load_builtin_presets()

    assert "changed" not in second["codex"]["command"]


def test_known_headless_contracts_are_encoded() -> None:
    presets = load_builtin_presets()

    assert presets["claude"]["output"]["field"] == "result"
    assert presets["codex"]["output"]["field"] == "item.text"
    assert presets["codex"]["output"]["match"] == {
        "type": "item.completed",
        "item.type": "agent_message",
    }
    assert presets["agy"]["input"]["prompt_arg"] == "--print={prompt}"
    assert presets["opencode"]["output"]["match"] == {"type": "text"}
    assert presets["opencode"]["output"]["field"] == "part.text"
    assert presets["grok"]["input"]["prompt_arg"] == "--single={prompt}"
    assert presets["codex"]["session"]["id_field"] == "thread_id"
    assert presets["opencode"]["session"]["id_field"] == "sessionID"
    assert presets["claude"]["session"]["id_strategy"] == "generated"
    assert presets["grok"]["session"]["id_strategy"] == "generated"
    assert "--ephemeral" not in presets["codex"]["command"]
    assert "--no-session-persistence" not in presets["claude"]["command"]
    assert "CLAUDE_CONFIG_DIR" in presets["claude"]["env"]["pass"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in presets["claude"]["env"]["pass"]
    assert "GROK_HOME" in presets["grok"]["env"]["pass"]
