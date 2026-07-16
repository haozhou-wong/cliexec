from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import AgentConfig, basic_environment

_VERSION_PART_RE = re.compile(r"\d+")
_CONSTRAINT_RE = re.compile(r"^(<=|>=|<|>|==)\s*([0-9]+(?:\.[0-9]+)*)$")


def _resolve_executable(command: str) -> str | None:
    if os.sep in command:
        path = Path(command).expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    return shutil.which(command)


def _run_probe(executable: str, args: tuple[str, ...]) -> tuple[int | None, str, str | None]:
    if not args:
        return None, "", None
    try:
        completed = subprocess.run(
            [executable, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=basic_environment(()),
        )
    except subprocess.TimeoutExpired:
        return None, "", "probe timed out after 5 seconds"
    except OSError as exc:
        return None, "", f"cannot run probe: {exc}"
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    return completed.returncode, output, None


def _extract_version(output: str, pattern: str | None) -> str | None:
    if pattern:
        match = re.search(pattern, output)
        if not match:
            return None
        if "version" in match.groupdict():
            return match.group("version")
        return match.group(1) if match.groups() else match.group(0)
    match = re.search(r"\d+(?:\.\d+)+", output)
    return match.group(0) if match else None


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in _VERSION_PART_RE.findall(value))


def version_satisfies(version: str, constraints: str) -> bool | None:
    current = _version_tuple(version)
    if not current:
        return None
    for raw_constraint in constraints.split(","):
        match = _CONSTRAINT_RE.fullmatch(raw_constraint.strip())
        if not match:
            return None
        operator, expected_text = match.groups()
        expected = _version_tuple(expected_text)
        width = max(len(current), len(expected))
        left = current + (0,) * (width - len(current))
        right = expected + (0,) * (width - len(expected))
        matches = {
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
            "==": left == right,
        }[operator]
        if not matches:
            return False
    return True


def check_agent(agent: AgentConfig) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    executable = _resolve_executable(agent.command[0])
    result: dict[str, Any] = {
        "name": agent.name,
        "available": executable is not None,
        "executable": executable,
        "version": None,
        "tested_versions": agent.probe.tested_versions,
        "compatible": None,
        "help_compatible": None,
        "errors": errors,
        "warnings": warnings,
    }
    if executable is None:
        errors.append(f"executable not found: {agent.command[0]}")
        result["ok"] = False
        return result

    version_code, version_output, version_error = _run_probe(executable, agent.probe.version_args)
    if version_error:
        errors.append(version_error)
    elif agent.probe.version_args and version_code != 0:
        errors.append(f"version probe exited with code {version_code}")
    else:
        version = _extract_version(version_output, agent.probe.version_regex)
        result["version"] = version
        if agent.probe.version_regex and version is None:
            errors.append("version output did not match version_regex")
        if version and agent.probe.tested_versions:
            compatible = version_satisfies(version, agent.probe.tested_versions)
            result["compatible"] = compatible
            if compatible is False:
                warnings.append(
                    f"version {version} is outside tested range {agent.probe.tested_versions}"
                )
            elif compatible is None:
                warnings.append(
                    f"cannot evaluate tested version range: {agent.probe.tested_versions}"
                )

    help_code, help_output, help_error = _run_probe(executable, agent.probe.help_args)
    if help_error:
        errors.append(help_error)
    elif agent.probe.help_args and help_code != 0:
        errors.append(f"help probe exited with code {help_code}")
    elif agent.probe.help_contains:
        missing = [token for token in agent.probe.help_contains if token not in help_output]
        result["help_compatible"] = not missing
        if missing:
            errors.append(f"help output is missing expected option(s): {', '.join(missing)}")
    else:
        result["help_compatible"] = True

    result["ok"] = not errors
    return result


def check_agents(agents: dict[str, AgentConfig]) -> list[dict[str, Any]]:
    return [check_agent(agent) for _, agent in sorted(agents.items()) if agent.enabled]
