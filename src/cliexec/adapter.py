from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import AgentConfig, basic_environment
from .errors import (
    PERMISSION_DENIED,
    PROTOCOL_ERROR,
    SPAWN_ERROR,
    UNSUPPORTED_CAPABILITY,
    CLIExecError,
)
from .models import Permission, TaskRequest


@dataclass(slots=True)
class SpawnSpec:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    stdin_text: str | None


def _replace(template: str, key: str, value: str) -> str:
    return template.replace("{" + key + "}", value)


def _expand(template: tuple[str, ...], key: str, value: str) -> list[str]:
    return [_replace(item, key, value) for item in template]


def _expand_many(template: tuple[str, ...], paths: Iterable[Path]) -> list[str]:
    result: list[str] = []
    for path in paths:
        result.extend(_expand(template, "path", str(path)))
    return result


def build_command(agent: AgentConfig, request: TaskRequest, *, run_id: str) -> SpawnSpec:
    if not agent.supports(request.permission):
        raise CLIExecError(
            UNSUPPORTED_CAPABILITY,
            f"agent {agent.name} does not support permission {request.permission.value}",
        )
    if request.permission is Permission.UNRESTRICTED and not agent.allow_unrestricted:
        raise CLIExecError(
            PERMISSION_DENIED,
            f"unrestricted mode is not enabled for agent {agent.name}",
        )
    if request.files and not agent.input.file_args:
        raise CLIExecError(UNSUPPORTED_CAPABILITY, f"agent {agent.name} does not support files")
    if request.images and not agent.input.image_args:
        raise CLIExecError(UNSUPPORTED_CAPABILITY, f"agent {agent.name} does not support images")

    argv = list(agent.command)
    argv.extend(agent.modes[request.permission])
    argv.extend(_expand(agent.input.cwd_args, "cwd", str(request.cwd)))
    argv.extend(_expand_many(agent.input.file_args, request.files))
    argv.extend(_expand_many(agent.input.image_args, request.images))
    stdin_text: str | None = request.prompt
    if agent.input.mode == "argv":
        argv.append(_replace(agent.input.prompt_arg, "prompt", request.prompt))
        stdin_text = None

    executable = argv[0]
    if os.sep in executable:
        resolved = Path(executable).expanduser().resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise CLIExecError(SPAWN_ERROR, f"executable is unavailable: {executable}")
        argv[0] = str(resolved)
    else:
        resolved_name = shutil.which(executable, path=basic_environment(agent.env_pass).get("PATH"))
        if not resolved_name:
            raise CLIExecError(SPAWN_ERROR, f"executable is not on PATH: {executable}")
        argv[0] = resolved_name

    env = basic_environment(agent.env_pass)
    env["CLIEXEC_RUN_ID"] = run_id
    env["CLIEXEC_DEPTH"] = "1"
    env.setdefault("NO_COLOR", "1")
    return SpawnSpec(argv=argv, cwd=request.cwd, env=env, stdin_text=stdin_text)


def _lookup(value: object, path: str) -> object:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def _matches(value: object, expected: dict[str, object]) -> bool:
    if not isinstance(value, dict):
        return False
    for path, wanted in expected.items():
        try:
            actual = _lookup(value, path)
        except KeyError:
            return False
        if actual != wanted:
            return False
    return True


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _select_json_values(values: Iterable[object], agent: AgentConfig) -> list[str]:
    selected: list[str] = []
    assert agent.output.field is not None
    for value in values:
        if not _matches(value, agent.output.match):
            continue
        try:
            selected.append(_as_text(_lookup(value, agent.output.field)))
        except KeyError:
            continue
    return [value for value in selected if value]


def _collect(values: list[str], mode: str) -> str:
    if not values:
        raise CLIExecError(PROTOCOL_ERROR, "no final result matched the output contract")
    if mode == "first":
        return values[0]
    if mode == "last":
        return values[-1]
    return "\n".join(values)


def parse_output(path: Path, agent: AgentConfig) -> str:
    try:
        if agent.output.format == "text":
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                raise CLIExecError(PROTOCOL_ERROR, "agent returned an empty result")
            return text
        if agent.output.format == "json":
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            return _collect(_select_json_values([value], agent), agent.output.collect)

        values: list[object] = []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    values.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise CLIExecError(
                        PROTOCOL_ERROR, f"invalid JSONL at line {number}: {exc.msg}"
                    ) from exc
        return _collect(_select_json_values(values, agent), agent.output.collect)
    except CLIExecError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIExecError(PROTOCOL_ERROR, f"cannot parse agent output: {exc}") from exc


def best_effort_partial(path: Path, agent: AgentConfig, limit: int = 64 * 1024) -> str | None:
    try:
        return parse_output(path, agent)
    except CLIExecError:
        pass
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(-limit, os.SEEK_END)
            text = handle.read(limit).decode("utf-8", errors="replace").strip()
        return text or None
    except OSError:
        return None
