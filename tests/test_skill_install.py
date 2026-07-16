from __future__ import annotations

import json
from pathlib import Path

import pytest

from cliexec.errors import CLIExecError
from cliexec.skill_install import MANAGED_MARKER, SKILL_CONFLICT, install_skill


def test_cli_installs_packaged_skill_for_both_controllers(invoke_cli) -> None:
    first = invoke_cli("skill", "install", "--target", "all")

    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    assert [item["status"] for item in first_payload["data"]["installations"]] == [
        "installed",
        "installed",
    ]

    second = invoke_cli("skill", "install", "--target", "all")

    assert second.returncode == 0, second.stderr
    second_payload = json.loads(second.stdout)
    assert [item["status"] for item in second_payload["data"]["installations"]] == [
        "unchanged",
        "unchanged",
    ]


def test_install_all_is_idempotent(tmp_path: Path) -> None:
    first = install_skill("all", home=tmp_path)

    assert [result.target for result in first] == ["claude", "codex"]
    assert [result.status for result in first] == ["installed", "installed"]
    destinations = [
        tmp_path / ".claude/skills/cliexec",
        tmp_path / ".agents/skills/cliexec",
    ]
    for destination in destinations:
        assert (destination / "SKILL.md").is_file()
        assert (destination / "agents/openai.yaml").is_file()
        assert (destination / MANAGED_MARKER).is_file()

    before = [(destination / "SKILL.md").stat().st_mtime_ns for destination in destinations]
    second = install_skill("all", home=tmp_path)

    assert [result.status for result in second] == ["unchanged", "unchanged"]
    assert [(destination / "SKILL.md").stat().st_mtime_ns for destination in destinations] == before


def test_managed_skill_is_updated(tmp_path: Path) -> None:
    destination = tmp_path / ".claude/skills/cliexec"
    install_skill("claude", home=tmp_path)
    (destination / "SKILL.md").write_text("changed by user\n", encoding="utf-8")

    result = install_skill("claude", home=tmp_path)

    assert result[0].status == "updated"
    assert "name: cliexec" in (destination / "SKILL.md").read_text(encoding="utf-8")


def test_foreign_skill_requires_force(tmp_path: Path) -> None:
    destination = tmp_path / ".agents/skills/cliexec"
    destination.mkdir(parents=True)
    foreign = destination / "SKILL.md"
    foreign.write_text("foreign skill\n", encoding="utf-8")

    with pytest.raises(CLIExecError) as raised:
        install_skill("codex", home=tmp_path)

    assert raised.value.code == SKILL_CONFLICT
    assert foreign.read_text(encoding="utf-8") == "foreign skill\n"

    result = install_skill("codex", home=tmp_path, force=True)

    assert result[0].status == "updated"
    assert "name: cliexec" in foreign.read_text(encoding="utf-8")
    assert (destination / MANAGED_MARKER).is_file()


def test_all_preflights_conflicts_before_installing(tmp_path: Path) -> None:
    foreign = tmp_path / ".agents/skills/cliexec"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("foreign skill\n", encoding="utf-8")

    with pytest.raises(CLIExecError, match="foreign Skill directory"):
        install_skill("all", home=tmp_path)

    assert not (tmp_path / ".claude/skills/cliexec").exists()
