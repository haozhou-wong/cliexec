from __future__ import annotations

from pathlib import Path

import pytest

from cliexec.config import AgentConfig, ProbeConfig
from cliexec.doctor import check_agent, version_satisfies


@pytest.fixture
def probe_executable(tmp_path: Path) -> Path:
    executable = tmp_path / "probe-agent"
    executable.write_text(
        """\
#!/bin/sh
case "$1" in
  --version)
    echo "probe-agent 1.2.3"
    ;;
  --help)
    echo "usage: probe-agent --json --sandbox"
    ;;
  *)
    exit 64
    ;;
esac
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _agent(
    executable: Path,
    *,
    tested_versions: str = ">=1.2.3,<2.0.0",
    help_contains: tuple[str, ...] = ("--json", "--sandbox"),
) -> AgentConfig:
    return AgentConfig(
        name="probe",
        command=(str(executable),),
        probe=ProbeConfig(
            version_args=("--version",),
            version_regex=r"probe-agent (?P<version>\d+\.\d+\.\d+)",
            tested_versions=tested_versions,
            help_args=("--help",),
            help_contains=help_contains,
        ),
    )


def test_check_agent_accepts_matching_version_and_help(probe_executable: Path) -> None:
    result = check_agent(_agent(probe_executable))

    assert result["ok"] is True
    assert result["available"] is True
    assert result["version"] == "1.2.3"
    assert result["compatible"] is True
    assert result["help_compatible"] is True
    assert result["errors"] == []
    assert result["warnings"] == []


def test_out_of_range_version_is_only_a_warning(probe_executable: Path) -> None:
    result = check_agent(_agent(probe_executable, tested_versions=">=2.0.0,<3.0.0"))

    assert result["ok"] is True
    assert result["compatible"] is False
    assert result["errors"] == []
    assert result["warnings"] == ["version 1.2.3 is outside tested range >=2.0.0,<3.0.0"]


def test_missing_expected_help_flag_fails(probe_executable: Path) -> None:
    result = check_agent(_agent(probe_executable, help_contains=("--json", "--missing")))

    assert result["ok"] is False
    assert result["help_compatible"] is False
    assert result["errors"] == ["help output is missing expected option(s): --missing"]


def test_missing_executable_fails(tmp_path: Path) -> None:
    executable = tmp_path / "does-not-exist"

    result = check_agent(_agent(executable))

    assert result["ok"] is False
    assert result["available"] is False
    assert result["executable"] is None
    assert result["errors"] == [f"executable not found: {executable}"]


@pytest.mark.parametrize(
    ("version", "constraints", "expected"),
    [
        ("1.2.3", ">=1.2.3,<2.0.0", True),
        ("1.2.2", ">=1.2.3,<2.0.0", False),
        ("2.0.0", ">=1.2.3,<2.0.0", False),
        ("1.2", "==1.2.0", True),
        ("1.2.4", ">1.2.3,<=1.2.4", True),
        ("1.2.3", "~=1.2", None),
        ("release", ">=1.0", None),
    ],
)
def test_version_satisfies_boundaries(
    version: str, constraints: str, expected: bool | None
) -> None:
    assert version_satisfies(version, constraints) is expected
