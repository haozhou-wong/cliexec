from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

from .errors import CLIExecError

MANAGED_MARKER = ".cliexec-managed.json"
SKILL_CONFLICT = "SKILL_CONFLICT"
INVALID_SKILL_TARGET = "INVALID_SKILL_TARGET"

_TARGET_PATHS = {
    "claude": Path(".claude/skills/cliexec"),
    "codex": Path(".agents/skills/cliexec"),
}
_MARKER_VALUE = {"manager": "cliexec", "schema_version": 1}


@dataclass(frozen=True, slots=True)
class SkillInstallResult:
    target: str
    path: Path
    status: Literal["installed", "updated", "unchanged"]

    def to_dict(self) -> dict[str, str]:
        return {
            "target": self.target,
            "path": str(self.path),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class _InstallPlan:
    target: str
    destination: Path
    status: Literal["installed", "updated", "unchanged"]


def _source_files() -> dict[Path, bytes]:
    root = files("cliexec").joinpath("skills", "cliexec")
    if not root.is_dir():
        root = Path(__file__).resolve().parents[2] / "skills" / "cliexec"
    if not root.is_dir():
        raise CLIExecError("SKILL_RESOURCE_ERROR", "packaged CLIExec Skill is missing")
    collected: dict[Path, bytes] = {}

    def collect(resource: Traversable, relative: Path) -> None:
        for child in resource.iterdir():
            child_relative = relative / child.name
            if child.is_dir():
                collect(child, child_relative)
            elif child.is_file():
                with child.open("rb") as handle:
                    collected[child_relative] = handle.read()

    collect(root, Path())
    if Path("SKILL.md") not in collected:
        raise CLIExecError("SKILL_RESOURCE_ERROR", "packaged CLIExec Skill is missing SKILL.md")
    return collected


def _is_identical(destination: Path, source: dict[Path, bytes]) -> bool:
    if destination.is_symlink() or not destination.is_dir():
        return False
    actual: set[Path] = set()
    try:
        for path in destination.rglob("*"):
            relative = path.relative_to(destination)
            if relative == Path(MANAGED_MARKER):
                continue
            if path.is_symlink():
                return False
            if path.is_file():
                actual.add(relative)
                if relative not in source or path.read_bytes() != source[relative]:
                    return False
    except OSError:
        return False
    return actual == set(source)


def _is_managed(destination: Path) -> bool:
    if destination.is_symlink():
        return False
    marker = destination / MANAGED_MARKER
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        return json.loads(marker.read_text(encoding="utf-8")) == _MARKER_VALUE
    except (OSError, json.JSONDecodeError):
        return False


def _plan_install(
    target: str,
    destination: Path,
    source: dict[Path, bytes],
    *,
    force: bool,
) -> _InstallPlan:
    if not destination.exists() and not destination.is_symlink():
        return _InstallPlan(target, destination, "installed")
    if _is_identical(destination, source):
        return _InstallPlan(target, destination, "unchanged")
    if not force and not _is_managed(destination):
        raise CLIExecError(
            SKILL_CONFLICT,
            f"refusing to overwrite foreign Skill directory: {destination}",
            details={"path": str(destination), "target": target},
        )
    return _InstallPlan(target, destination, "updated")


def _write_tree(directory: Path, source: dict[Path, bytes]) -> None:
    for relative, content in source.items():
        destination = directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(0o644)
    marker = directory / MANAGED_MARKER
    marker.write_text(json.dumps(_MARKER_VALUE, sort_keys=True) + "\n", encoding="utf-8")
    marker.chmod(0o644)


def _replace_directory(destination: Path, source: dict[Path, bytes]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".cliexec-skill-", dir=destination.parent))
    backup: Path | None = None
    try:
        _write_tree(temporary, source)
        if destination.exists() or destination.is_symlink():
            backup = destination.parent / (
                f".{destination.name}.cliexec-backup-{os.getpid()}-{uuid.uuid4().hex}"
            )
            os.replace(destination, backup)
        try:
            os.replace(temporary, destination)
        except OSError:
            if backup is not None and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup is not None:
            if backup.is_symlink() or backup.is_file():
                backup.unlink(missing_ok=True)
            else:
                shutil.rmtree(backup)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def install_skill(
    target: Literal["claude", "codex", "all"] | str,
    *,
    force: bool = False,
    home: Path | None = None,
) -> list[SkillInstallResult]:
    """Install the packaged CLIExec Skill for one or both controller CLIs."""
    if target == "all":
        targets = ("claude", "codex")
    elif target in _TARGET_PATHS:
        targets = (target,)
    else:
        expected = ", ".join((*_TARGET_PATHS, "all"))
        raise CLIExecError(
            INVALID_SKILL_TARGET,
            f"unknown Skill target {target!r}; expected one of: {expected}",
        )

    root = (home or Path.home()).expanduser().resolve()
    source = _source_files()
    plans = [
        _plan_install(name, root / _TARGET_PATHS[name], source, force=force) for name in targets
    ]

    results: list[SkillInstallResult] = []
    for plan in plans:
        if plan.status != "unchanged":
            _replace_directory(plan.destination, source)
        results.append(SkillInstallResult(plan.target, plan.destination, plan.status))
    return results
