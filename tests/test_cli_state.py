from pathlib import Path

from conftest import write_mock_config

from cliexec.cli import _state_service
from cliexec.store import RunStore


def test_state_service_uses_configured_retention(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config_path = write_mock_config(tmp_path / "config.toml")
    content = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        content.replace("retention_days = 30", "retention_days = 7"),
        encoding="utf-8",
    )
    retention_days: list[int] = []
    monkeypatch.setattr(
        RunStore,
        "maybe_purge",
        lambda self, value: retention_days.append(value),
    )

    _state_service(config_path)

    assert retention_days == [7]
