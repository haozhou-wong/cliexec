from __future__ import annotations

import json
from pathlib import Path


def test_success_stdout_is_one_versioned_json_envelope(invoke_cli) -> None:
    result = invoke_cli("runs")

    assert result.returncode == 0
    assert result.stderr == ""
    envelope = json.loads(result.stdout)
    assert envelope.keys() == {"schema_version", "ok", "data", "error"}
    assert envelope["schema_version"] == 1
    assert envelope["ok"] is True
    assert envelope["error"] is None
    assert isinstance(envelope["data"]["runs"], list)


def test_runtime_error_stdout_is_one_json_error_envelope(tmp_path: Path, invoke_cli) -> None:
    missing = tmp_path / "missing.toml"

    result = invoke_cli("config", "check", "--config", str(missing))

    assert result.returncode == 2
    assert result.stderr == ""
    envelope = json.loads(result.stdout)
    assert envelope == {
        "schema_version": 1,
        "ok": False,
        "data": None,
        "error": {
            "code": "CONFIG_ERROR",
            "message": f"explicit config does not exist: {missing}",
        },
    }


def test_click_usage_error_uses_same_json_contract(invoke_cli) -> None:
    result = invoke_cli("not-a-command")

    assert result.returncode == 2
    envelope = json.loads(result.stdout)
    assert envelope["schema_version"] == 1
    assert envelope["ok"] is False
    assert envelope["data"] is None
    assert envelope["error"]["code"] == "USAGE_ERROR"
