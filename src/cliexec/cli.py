from __future__ import annotations

import json
import math
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import click

from . import __version__
from .config import AppConfig, default_user_config, load_config
from .doctor import check_agents
from .errors import CLIExecError
from .models import SCHEMA_VERSION, Permission, TaskRequest, TaskState
from .preset_loader import load_builtin_presets
from .service import TaskService, result_exit_code
from .store import RunStore
from .supervisor import supervise
from .util import config_home, parse_duration, permission_mode

F = TypeVar("F", bound=Callable[..., Any])


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _emit(
    data: object,
    *,
    ok: bool = True,
    error: dict[str, object] | None = None,
    output_format: str = "json",
) -> None:
    if output_format == "text":
        if not ok and error:
            click.echo(f"{error['code']}: {error['message']}")
            return
        if isinstance(data, dict):
            if data.get("final_text") is not None:
                click.echo(data["final_text"], nl=not str(data["final_text"]).endswith("\n"))
                return
            if data.get("partial_text") is not None:
                click.echo(data["partial_text"], nl=not str(data["partial_text"]).endswith("\n"))
                return
        click.echo(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default))
        return
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "data": data,
        "error": error,
    }
    click.echo(
        json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    )


def _effective_option(local: object, context: click.Context, name: str, fallback: object) -> object:
    if local is not None:
        return local
    value = (context.find_root().obj or {}).get(name)
    return fallback if value is None else value


def _config_option(function: F) -> F:
    return click.option(
        "--config",
        "config_path",
        type=click.Path(path_type=Path, dir_okay=False),
        help="Explicit TOML config file.",
    )(function)


def _format_option(function: F) -> F:
    return click.option(
        "--format",
        "output_format",
        type=click.Choice(("json", "text")),
        default=None,
        help="Output format (default: json).",
    )(function)


def _common_options(function: F) -> F:
    return _format_option(_config_option(function))


def _task_options(function: F) -> F:
    function = click.option(
        "--continue",
        "continue_run_id",
        metavar="RUN_ID",
        help="Continue the exact session represented by the latest run ID.",
    )(function)
    function = click.option(
        "--image",
        "images",
        multiple=True,
        type=click.Path(path_type=Path, dir_okay=False),
        help="Image input; repeatable.",
    )(function)
    function = click.option(
        "--file",
        "files",
        multiple=True,
        type=click.Path(path_type=Path, dir_okay=False),
        help="File input; repeatable.",
    )(function)
    function = click.option(
        "--prompt-file",
        type=click.Path(path_type=Path, dir_okay=False, exists=True),
        help="Read the prompt from a file instead of stdin.",
    )(function)
    function = click.option("--timeout", help="Task timeout, for example 30s or 45m.")(function)
    function = click.option(
        "--permission",
        type=click.Choice(tuple(mode.value for mode in Permission)),
        default=Permission.READ_ONLY.value,
        show_default=True,
    )(function)
    function = click.option(
        "--cwd",
        type=click.Path(path_type=Path, file_okay=False),
        default=None,
        help="Working directory; defaults to the current or continued run directory.",
    )(function)
    return _common_options(function)


def _resolved_common(
    context: click.Context, config_path: Path | None, output_format: str | None
) -> tuple[Path | None, str]:
    resolved_config = _effective_option(config_path, context, "config_path", None)
    resolved_format = _effective_option(output_format, context, "output_format", "json")
    return resolved_config, str(resolved_format)


def _read_prompt(prompt_file: Path | None) -> str:
    if prompt_file is not None:
        try:
            return prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise CLIExecError("INVALID_REQUEST", f"cannot read prompt file: {exc}") from exc
    if sys.stdin.isatty():
        raise CLIExecError(
            "INVALID_REQUEST", "prompt is required on stdin or through --prompt-file"
        )
    return sys.stdin.read()


def _make_request(
    service: TaskService,
    *,
    agent: str,
    cwd: Path | None,
    permission: str,
    timeout: str | None,
    prompt_file: Path | None,
    files: tuple[Path, ...],
    images: tuple[Path, ...],
    continue_run_id: str | None,
) -> TaskRequest:
    timeout_seconds = (
        service.config.policy.default_timeout if timeout is None else parse_duration(timeout)
    )
    return TaskRequest(
        agent=agent,
        prompt=_read_prompt(prompt_file),
        cwd=cwd,
        permission=Permission(permission),
        timeout_seconds=timeout_seconds,
        files=list(files),
        images=list(images),
        continue_run_id=continue_run_id,
    )


def _emit_task(data: dict[str, Any], output_format: str, *, terminal_exit: bool) -> int:
    if data.get("state") == TaskState.REJECTED.value:
        error = data.get("error") or {
            "code": "TASK_REJECTED",
            "message": "task was rejected",
        }
        _emit(data, ok=False, error=error, output_format=output_format)
        return 2
    _emit(data, output_format=output_format)
    return result_exit_code(data) if terminal_exit else 0


def _state_service(config_path: Path | None) -> TaskService:
    return TaskService(config_path=config_path)


def _agent_summaries(config: AppConfig) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name, agent in sorted(config.agents.items()):
        executable = agent.command[0]
        available = (
            Path(executable).expanduser().exists()
            if os.sep in executable
            else shutil.which(executable) is not None
        )
        result.append(
            {
                "name": name,
                "enabled": agent.enabled,
                "available": available,
                "builtin": agent.builtin,
                "command": executable,
                "capabilities": agent.capabilities(),
            }
        )
    return result


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Explicit TOML config file.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(("json", "text")),
    default="json",
    show_default=True,
)
@click.version_option(version=__version__)
@click.pass_context
def cli(context: click.Context, config_path: Path | None, output_format: str) -> None:
    """Delegate bounded task turns to installed Agent CLIs."""
    context.ensure_object(dict)
    context.obj.update(config_path=config_path, output_format=output_format)


@cli.command("init")
@_format_option
@click.pass_context
def init_command(context: click.Context, output_format: str | None) -> int:
    """Create the user config without overwriting an existing file."""
    _, resolved_format = _resolved_common(context, None, output_format)
    presets = load_builtin_presets()
    installed = sorted(
        name for name, raw in presets.items() if shutil.which(str(raw["command"][0])) is not None
    )
    directory = config_home()
    path = directory / "config.toml"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    permission_mode(directory, 0o700)
    created = False
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(default_user_config(installed))
        permission_mode(path, 0o600)
        created = True
    except FileExistsError:
        pass
    data = {"path": str(path), "created": created, "installed_presets": installed}
    _emit(data, output_format=resolved_format)
    return 0


@cli.command("agents")
@_common_options
@click.pass_context
def agents_command(
    context: click.Context, config_path: Path | None, output_format: str | None
) -> int:
    """List configured agents and declarative capabilities."""
    resolved_config, resolved_format = _resolved_common(context, config_path, output_format)
    data = {"agents": _agent_summaries(load_config(resolved_config))}
    _emit(data, output_format=resolved_format)
    return 0


@cli.command("runs")
@_format_option
@click.pass_context
def runs_command(context: click.Context, output_format: str | None) -> int:
    """List retained task runs, newest first."""
    resolved_config, resolved_format = _resolved_common(context, None, output_format)
    _emit({"runs": _state_service(resolved_config).list_runs()}, output_format=resolved_format)
    return 0


def _execute_task(
    context: click.Context,
    *,
    blocking: bool,
    agent: str,
    cwd: Path | None,
    permission: str,
    timeout: str | None,
    prompt_file: Path | None,
    files: tuple[Path, ...],
    images: tuple[Path, ...],
    continue_run_id: str | None,
    config_path: Path | None,
    output_format: str | None,
) -> int:
    resolved_config, resolved_format = _resolved_common(context, config_path, output_format)
    service = TaskService(config_path=resolved_config)
    request = _make_request(
        service,
        agent=agent,
        cwd=cwd,
        permission=permission,
        timeout=timeout,
        prompt_file=prompt_file,
        files=files,
        images=images,
        continue_run_id=continue_run_id,
    )
    data = service.run_task(request) if blocking else service.start_task(request)
    return _emit_task(
        data, resolved_format, terminal_exit=blocking or TaskState(data["state"]).terminal
    )


@cli.command("run")
@click.argument("agent")
@_task_options
@click.pass_context
def run_command(
    context: click.Context,
    agent: str,
    cwd: Path | None,
    permission: str,
    timeout: str | None,
    prompt_file: Path | None,
    files: tuple[Path, ...],
    images: tuple[Path, ...],
    continue_run_id: str | None,
    config_path: Path | None,
    output_format: str | None,
) -> int:
    """Run a task synchronously and return its final result."""
    return _execute_task(
        context,
        blocking=True,
        agent=agent,
        cwd=cwd,
        permission=permission,
        timeout=timeout,
        prompt_file=prompt_file,
        files=files,
        images=images,
        continue_run_id=continue_run_id,
        config_path=config_path,
        output_format=output_format,
    )


@cli.command("start")
@click.argument("agent")
@_task_options
@click.pass_context
def start_command(
    context: click.Context,
    agent: str,
    cwd: Path | None,
    permission: str,
    timeout: str | None,
    prompt_file: Path | None,
    files: tuple[Path, ...],
    images: tuple[Path, ...],
    continue_run_id: str | None,
    config_path: Path | None,
    output_format: str | None,
) -> int:
    """Start a task under a detached per-task supervisor."""
    return _execute_task(
        context,
        blocking=False,
        agent=agent,
        cwd=cwd,
        permission=permission,
        timeout=timeout,
        prompt_file=prompt_file,
        files=files,
        images=images,
        continue_run_id=continue_run_id,
        config_path=config_path,
        output_format=output_format,
    )


@cli.command("status")
@click.argument("run_id")
@_format_option
@click.pass_context
def status_command(context: click.Context, run_id: str, output_format: str | None) -> int:
    """Return the current state for a run."""
    resolved_config, resolved_format = _resolved_common(context, None, output_format)
    _emit(_state_service(resolved_config).status(run_id), output_format=resolved_format)
    return 0


@cli.command("result")
@click.argument("run_id")
@_format_option
@click.pass_context
def result_command(context: click.Context, run_id: str, output_format: str | None) -> int:
    """Return a terminal run's normalized result."""
    resolved_config, resolved_format = _resolved_common(context, None, output_format)
    data = _state_service(resolved_config).result(run_id)
    return _emit_task(data, resolved_format, terminal_exit=True)


@cli.command("cancel")
@click.argument("run_id")
@_format_option
@click.pass_context
def cancel_command(context: click.Context, run_id: str, output_format: str | None) -> int:
    """Cancel a run and its complete child process group."""
    resolved_config, resolved_format = _resolved_common(context, None, output_format)
    _emit(_state_service(resolved_config).cancel(run_id), output_format=resolved_format)
    return 0


@cli.command("logs")
@click.argument("run_id")
@click.option("--tail", type=click.IntRange(min=1), default=200, show_default=True)
@click.option("--stream", type=click.Choice(("stdout", "stderr", "both")), default="both")
@_format_option
@click.pass_context
def logs_command(
    context: click.Context,
    run_id: str,
    tail: int,
    stream: str,
    output_format: str | None,
) -> int:
    """Read retained stdout and stderr without following them."""
    _, resolved_format = _resolved_common(context, None, output_format)
    paths = RunStore().paths(run_id)

    def read_tail(path: Path) -> str:
        if not path.exists():
            return ""
        return "".join(path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-tail:])

    data: dict[str, Any] = {"run_id": run_id}
    if stream in {"stdout", "both"}:
        data["stdout"] = read_tail(paths["stdout"])
    if stream in {"stderr", "both"}:
        data["stderr"] = read_tail(paths["stderr"])
    _emit(data, output_format=resolved_format)
    return 0


@cli.command("purge")
@click.option("--older-than", help="Remove terminal runs older than this duration.")
@click.option("--all", "purge_all", is_flag=True, help="Remove every terminal run.")
@_common_options
@click.pass_context
def purge_command(
    context: click.Context,
    older_than: str | None,
    purge_all: bool,
    config_path: Path | None,
    output_format: str | None,
) -> int:
    """Remove retained terminal runs; active runs are never removed."""
    resolved_config, resolved_format = _resolved_common(context, config_path, output_format)
    config = load_config(resolved_config)
    retention = config.policy.retention_days
    if older_than is not None:
        retention = max(1, math.ceil(parse_duration(older_than) / 86400))
    removed = RunStore().purge(retention, force=purge_all)
    _emit({"removed": removed, "count": len(removed)}, output_format=resolved_format)
    return 0


@cli.group("config")
def config_group() -> None:
    """Inspect and validate configuration."""


@config_group.command("check")
@_common_options
@click.pass_context
def config_check_command(
    context: click.Context, config_path: Path | None, output_format: str | None
) -> int:
    """Parse every config layer and validate the resolved schema."""
    resolved_config, resolved_format = _resolved_common(context, config_path, output_format)
    config = load_config(resolved_config)
    data = {
        "valid": True,
        "sources": [str(path) for path in config.sources],
        "agents": sorted(config.agents),
        "policy": {
            "max_concurrency": config.policy.max_concurrency,
            "default_timeout": config.policy.default_timeout,
            "max_timeout": config.policy.max_timeout,
            "max_permission": config.policy.max_permission.value,
            "retention_days": config.policy.retention_days,
            "inline_result_bytes": config.policy.inline_result_bytes,
            "max_output_bytes": config.policy.max_output_bytes,
        },
    }
    _emit(data, output_format=resolved_format)
    return 0


@cli.command("doctor")
@click.argument("agent", required=False)
@click.option("--smoke", is_flag=True, help="Run an authenticated read-only smoke task.")
@_common_options
@click.pass_context
def doctor_command(
    context: click.Context,
    agent: str | None,
    smoke: bool,
    config_path: Path | None,
    output_format: str | None,
) -> int:
    """Check executable presence and the preset version/help contract."""
    resolved_config, resolved_format = _resolved_common(context, config_path, output_format)
    config = load_config(resolved_config)
    selected = config.agents
    if agent is not None:
        selected = {agent: config.agent(agent)}
    checks = check_agents(selected)
    smoke_result: dict[str, Any] | None = None
    if smoke:
        if agent is None:
            raise CLIExecError("INVALID_REQUEST", "doctor --smoke requires an agent")
        service = TaskService(config=config)
        request = TaskRequest(
            agent=agent,
            prompt="Reply with exactly: CLIEXEC_SMOKE_OK",
            cwd=Path.cwd(),
            permission=Permission.READ_ONLY,
            timeout_seconds=min(120.0, service.config.policy.max_timeout),
        )
        smoke_result = service.run_task(request)
    ok = all(check["ok"] for check in checks)
    if smoke_result is not None:
        ok = ok and smoke_result.get("state") == TaskState.COMPLETED.value
    data = {"agents": checks, "smoke": smoke_result}
    _emit(
        data,
        ok=ok,
        error=None if ok else {"code": "DOCTOR_FAILED", "message": "one or more checks failed"},
        output_format=resolved_format,
    )
    return 0 if ok else 1


@cli.group("skill")
def skill_group() -> None:
    """Install controller Agent Skill assets."""


@skill_group.command("install")
@click.option("--target", type=click.Choice(("claude", "codex", "all")), default="all")
@click.option("--force", is_flag=True, help="Replace a foreign same-named Skill.")
@_format_option
@click.pass_context
def skill_install_command(
    context: click.Context, target: str, force: bool, output_format: str | None
) -> int:
    """Install the packaged CLIExec Skill for Claude Code and/or Codex."""
    from .skill_install import install_skill

    _, resolved_format = _resolved_common(context, None, output_format)
    data = {"installations": [result.to_dict() for result in install_skill(target, force=force)]}
    _emit(data, output_format=resolved_format)
    return 0


@cli.command("_supervise", hidden=True)
@click.argument("run_id")
@click.option("--state-root", required=True, type=click.Path(path_type=Path, file_okay=False))
def supervise_command(run_id: str, state_root: Path) -> int:
    return supervise(run_id, state_root)


def _requested_output_format() -> str:
    for index, argument in enumerate(sys.argv[1:]):
        if argument == "--format" and index + 2 <= len(sys.argv[1:]):
            return sys.argv[1:][index + 1]
        if argument.startswith("--format="):
            return argument.partition("=")[2]
    return "json"


def main() -> None:
    try:
        result = cli.main(standalone_mode=False)
    except CLIExecError as exc:
        _emit(None, ok=False, error=exc.to_dict(), output_format=_requested_output_format())
        raise SystemExit(exc.exit_code) from None
    except click.ClickException as exc:
        error = {"code": "USAGE_ERROR", "message": exc.format_message()}
        _emit(None, ok=False, error=error, output_format=_requested_output_format())
        raise SystemExit(2) from None
    except click.Abort:
        error = {"code": "ABORTED", "message": "operation aborted"}
        _emit(None, ok=False, error=error, output_format=_requested_output_format())
        raise SystemExit(2) from None
    except Exception as exc:  # pragma: no cover - last-resort stable CLI envelope
        error = {
            "code": "INTERNAL_ERROR",
            "message": f"{type(exc).__name__}: {exc}",
        }
        _emit(None, ok=False, error=error, output_format=_requested_output_format())
        raise SystemExit(2) from None
    raise SystemExit(0 if result is None else int(result))
