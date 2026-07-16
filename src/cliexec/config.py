from __future__ import annotations

import copy
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import AGENT_NOT_FOUND, CONFIG_ERROR, CLIExecError
from .models import SCHEMA_VERSION, Permission
from .util import config_home, parse_duration

_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(slots=True)
class PolicyConfig:
    max_concurrency: int = 4
    default_timeout: float = 1800.0
    max_timeout: float = 7200.0
    max_permission: Permission = Permission.WORKSPACE_WRITE
    retention_days: int = 30
    inline_result_bytes: int = 256 * 1024
    max_output_bytes: int = 64 * 1024 * 1024


@dataclass(slots=True)
class InputConfig:
    mode: str = "stdin"
    prompt_arg: str = "{prompt}"
    file_args: tuple[str, ...] = ()
    image_args: tuple[str, ...] = ()
    cwd_args: tuple[str, ...] = ()


@dataclass(slots=True)
class OutputConfig:
    format: str = "text"
    match: dict[str, object] = field(default_factory=dict)
    field: str | None = None
    collect: str = "last"


@dataclass(slots=True)
class ProbeConfig:
    version_args: tuple[str, ...] = ("--version",)
    version_regex: str | None = None
    tested_versions: str | None = None
    help_args: tuple[str, ...] = ("--help",)
    help_contains: tuple[str, ...] = ()


@dataclass(slots=True)
class AgentConfig:
    name: str
    command: tuple[str, ...]
    enabled: bool = True
    success_exit_codes: tuple[int, ...] = (0,)
    allow_unrestricted: bool = False
    input: InputConfig = field(default_factory=InputConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    modes: dict[Permission, tuple[str, ...]] = field(default_factory=dict)
    env_pass: tuple[str, ...] = ()
    probe: ProbeConfig = field(default_factory=ProbeConfig)
    builtin: bool = False

    def supports(self, permission: Permission) -> bool:
        return permission in self.modes

    def capabilities(self) -> dict[str, object]:
        return {
            "permissions": [mode.value for mode in self.modes],
            "files": bool(self.input.file_args),
            "images": bool(self.input.image_args),
            "input_mode": self.input.mode,
            "output_format": self.output.format,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "enabled": self.enabled,
            "success_exit_codes": list(self.success_exit_codes),
            "allow_unrestricted": self.allow_unrestricted,
            "input": {
                "mode": self.input.mode,
                "prompt_arg": self.input.prompt_arg,
                "file_args": list(self.input.file_args),
                "image_args": list(self.input.image_args),
                "cwd_args": list(self.input.cwd_args),
            },
            "output": {
                "format": self.output.format,
                "match": self.output.match,
                "field": self.output.field,
                "collect": self.output.collect,
            },
            "modes": {mode.value: {"args": list(args)} for mode, args in self.modes.items()},
            "env": {"pass": list(self.env_pass)},
            "probe": {
                "version_args": list(self.probe.version_args),
                "version_regex": self.probe.version_regex,
                "tested_versions": self.probe.tested_versions,
                "help_args": list(self.probe.help_args),
                "help_contains": list(self.probe.help_contains),
            },
            "builtin": self.builtin,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentConfig:
        raw = copy.deepcopy(value)
        name = str(raw.pop("name"))
        return _parse_agent(name, raw)


@dataclass(slots=True)
class AppConfig:
    policy: PolicyConfig
    agents: dict[str, AgentConfig]
    raw: dict[str, Any]
    sources: tuple[Path, ...] = ()

    def agent(self, name: str) -> AgentConfig:
        try:
            agent = self.agents[name]
        except KeyError as exc:
            raise CLIExecError(AGENT_NOT_FOUND, f"unknown agent: {name}") from exc
        if not agent.enabled:
            raise CLIExecError(AGENT_NOT_FOUND, f"agent is disabled: {name}")
        return agent


def _unknown_keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise CLIExecError(CONFIG_ERROR, f"unknown {context} field(s): {', '.join(unknown)}")


def _string_tuple(value: object, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CLIExecError(CONFIG_ERROR, f"{context} must be an array of strings")
    return tuple(value)


def _boolean(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise CLIExecError(CONFIG_ERROR, f"{context} must be a boolean")
    return value


def _parse_policy(value: dict[str, Any]) -> PolicyConfig:
    _unknown_keys(
        value,
        {
            "max_concurrency",
            "default_timeout",
            "max_timeout",
            "max_permission",
            "retention_days",
            "inline_result_bytes",
            "max_output_bytes",
        },
        "policy",
    )
    policy = PolicyConfig(
        max_concurrency=int(value.get("max_concurrency", 4)),
        default_timeout=parse_duration(value.get("default_timeout", "30m")),
        max_timeout=parse_duration(value.get("max_timeout", "2h")),
        max_permission=Permission(value.get("max_permission", "workspace_write")),
        retention_days=int(value.get("retention_days", 30)),
        inline_result_bytes=int(value.get("inline_result_bytes", 256 * 1024)),
        max_output_bytes=int(value.get("max_output_bytes", 64 * 1024 * 1024)),
    )
    if policy.max_concurrency < 1:
        raise CLIExecError(CONFIG_ERROR, "policy.max_concurrency must be at least 1")
    if policy.default_timeout > policy.max_timeout:
        raise CLIExecError(CONFIG_ERROR, "default_timeout cannot exceed max_timeout")
    if policy.retention_days < 1:
        raise CLIExecError(CONFIG_ERROR, "retention_days must be at least 1")
    if policy.inline_result_bytes < 1024:
        raise CLIExecError(CONFIG_ERROR, "inline_result_bytes must be at least 1024")
    if policy.max_output_bytes < policy.inline_result_bytes:
        raise CLIExecError(
            CONFIG_ERROR, "max_output_bytes cannot be smaller than inline_result_bytes"
        )
    return policy


def _parse_agent(name: str, value: dict[str, Any]) -> AgentConfig:
    if not _AGENT_NAME_RE.fullmatch(name):
        raise CLIExecError(CONFIG_ERROR, f"invalid agent name: {name!r}")
    _unknown_keys(
        value,
        {
            "preset",
            "enabled",
            "command",
            "success_exit_codes",
            "allow_unrestricted",
            "input",
            "output",
            "modes",
            "env",
            "probe",
            "builtin",
        },
        f"agents.{name}",
    )
    command = _string_tuple(value.get("command"), f"agents.{name}.command")
    if not command:
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.command cannot be empty")

    input_raw = value.get("input", {})
    if not isinstance(input_raw, dict):
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.input must be a table")
    _unknown_keys(
        input_raw,
        {"mode", "prompt_arg", "file_args", "image_args", "cwd_args"},
        f"agents.{name}.input",
    )
    input_config = InputConfig(
        mode=str(input_raw.get("mode", "stdin")),
        prompt_arg=str(input_raw.get("prompt_arg", "{prompt}")),
        file_args=_string_tuple(input_raw.get("file_args"), f"agents.{name}.input.file_args"),
        image_args=_string_tuple(input_raw.get("image_args"), f"agents.{name}.input.image_args"),
        cwd_args=_string_tuple(input_raw.get("cwd_args"), f"agents.{name}.input.cwd_args"),
    )
    if input_config.mode not in {"stdin", "argv"}:
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.input.mode must be stdin or argv")
    if input_config.mode == "argv" and "{prompt}" not in input_config.prompt_arg:
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.input.prompt_arg must contain {{prompt}}")
    for field_name, templates, placeholder in (
        ("file_args", input_config.file_args, "{path}"),
        ("image_args", input_config.image_args, "{path}"),
        ("cwd_args", input_config.cwd_args, "{cwd}"),
    ):
        if templates and not any(placeholder in item for item in templates):
            raise CLIExecError(
                CONFIG_ERROR,
                f"agents.{name}.input.{field_name} must contain {placeholder}",
            )

    output_raw = value.get("output", {})
    if not isinstance(output_raw, dict):
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.output must be a table")
    _unknown_keys(output_raw, {"format", "match", "field", "collect"}, f"agents.{name}.output")
    match = output_raw.get("match", {})
    if not isinstance(match, dict):
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.output.match must be a table")
    output_config = OutputConfig(
        format=str(output_raw.get("format", "text")),
        match=copy.deepcopy(match),
        field=str(output_raw["field"]) if output_raw.get("field") is not None else None,
        collect=str(output_raw.get("collect", "last")),
    )
    if output_config.format not in {"text", "json", "jsonl"}:
        raise CLIExecError(
            CONFIG_ERROR, f"agents.{name}.output.format must be text, json, or jsonl"
        )
    if output_config.collect not in {"first", "last", "concat"}:
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.output.collect is invalid")
    if output_config.format != "text" and not output_config.field:
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.output.field is required for JSON output")

    modes_raw = value.get("modes", {})
    if not isinstance(modes_raw, dict):
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.modes must be a table")
    modes: dict[Permission, tuple[str, ...]] = {}
    for raw_mode, raw_mode_value in modes_raw.items():
        try:
            mode = Permission(raw_mode)
        except ValueError as exc:
            raise CLIExecError(CONFIG_ERROR, f"unknown permission mode: {raw_mode}") from exc
        if not isinstance(raw_mode_value, dict):
            raise CLIExecError(CONFIG_ERROR, f"agents.{name}.modes.{raw_mode} must be a table")
        _unknown_keys(raw_mode_value, {"args"}, f"agents.{name}.modes.{raw_mode}")
        modes[mode] = _string_tuple(
            raw_mode_value.get("args", []), f"agents.{name}.modes.{raw_mode}.args"
        )

    env_raw = value.get("env", {})
    if not isinstance(env_raw, dict):
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.env must be a table")
    _unknown_keys(env_raw, {"pass"}, f"agents.{name}.env")

    probe_raw = value.get("probe", {})
    if not isinstance(probe_raw, dict):
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.probe must be a table")
    _unknown_keys(
        probe_raw,
        {"version_args", "version_regex", "tested_versions", "help_args", "help_contains"},
        f"agents.{name}.probe",
    )
    probe = ProbeConfig(
        version_args=_string_tuple(
            probe_raw.get("version_args", ["--version"]), f"agents.{name}.probe.version_args"
        ),
        version_regex=(
            str(probe_raw["version_regex"]) if probe_raw.get("version_regex") is not None else None
        ),
        tested_versions=(
            str(probe_raw["tested_versions"])
            if probe_raw.get("tested_versions") is not None
            else None
        ),
        help_args=_string_tuple(
            probe_raw.get("help_args", ["--help"]), f"agents.{name}.probe.help_args"
        ),
        help_contains=_string_tuple(
            probe_raw.get("help_contains", []), f"agents.{name}.probe.help_contains"
        ),
    )

    codes = value.get("success_exit_codes", [0])
    if not isinstance(codes, list) or not all(isinstance(code, int) for code in codes):
        raise CLIExecError(CONFIG_ERROR, f"agents.{name}.success_exit_codes must be integers")
    return AgentConfig(
        name=name,
        command=command,
        enabled=_boolean(value.get("enabled", True), f"agents.{name}.enabled"),
        success_exit_codes=tuple(codes),
        allow_unrestricted=_boolean(
            value.get("allow_unrestricted", False), f"agents.{name}.allow_unrestricted"
        ),
        input=input_config,
        output=output_config,
        modes=modes,
        env_pass=_string_tuple(env_raw.get("pass", []), f"agents.{name}.env.pass"),
        probe=probe,
        builtin=_boolean(value.get("builtin", False), f"agents.{name}.builtin"),
    )


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CLIExecError(CONFIG_ERROR, f"cannot read config {path}: {exc}") from exc
    if value.get("version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise CLIExecError(CONFIG_ERROR, f"unsupported config version in {path}")
    _unknown_keys(value, {"version", "policy", "agents"}, str(path))
    return value


def _apply_layer(
    resolved: dict[str, Any], layer: dict[str, Any], builtins: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    result = copy.deepcopy(resolved)
    if "policy" in layer:
        policy = layer["policy"]
        if not isinstance(policy, dict):
            raise CLIExecError(CONFIG_ERROR, "policy must be a table")
        result["policy"] = deep_merge(result.get("policy", {}), policy)
    agents = layer.get("agents", {})
    if not isinstance(agents, dict):
        raise CLIExecError(CONFIG_ERROR, "agents must be a table")
    result_agents = result.setdefault("agents", {})
    for name, raw in agents.items():
        if not isinstance(raw, dict):
            raise CLIExecError(CONFIG_ERROR, f"agents.{name} must be a table")
        preset = raw.get("preset")
        if preset is not None:
            if not isinstance(preset, str) or preset not in builtins:
                raise CLIExecError(CONFIG_ERROR, f"unknown preset for agents.{name}: {preset!r}")
            base = builtins[preset]
        else:
            base = result_agents.get(name, {})
        result_agents[name] = deep_merge(base, raw)
    return result


def load_config(explicit_path: Path | None = None) -> AppConfig:
    try:
        from .preset_loader import load_builtin_presets

        builtins = load_builtin_presets()
    except ImportError:
        builtins = {}
    builtins = {name: deep_merge(raw, {"builtin": True}) for name, raw in builtins.items()}
    resolved: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "policy": {},
        "agents": copy.deepcopy(builtins),
    }
    sources: list[Path] = []
    user_path = config_home() / "config.toml"
    for path in (user_path, explicit_path):
        if path is None or not path.exists():
            if path is explicit_path and path is not None:
                raise CLIExecError(CONFIG_ERROR, f"explicit config does not exist: {path}")
            continue
        layer = _read_toml(path)
        resolved = _apply_layer(resolved, layer, builtins)
        sources.append(path)
    policy_raw = resolved.get("policy", {})
    if not isinstance(policy_raw, dict):
        raise CLIExecError(CONFIG_ERROR, "policy must be a table")
    policy = _parse_policy(policy_raw)
    agents: dict[str, AgentConfig] = {}
    for name, raw in resolved.get("agents", {}).items():
        if not isinstance(raw, dict):
            raise CLIExecError(CONFIG_ERROR, f"agents.{name} must be a table")
        agents[name] = _parse_agent(name, raw)
    return AppConfig(policy=policy, agents=agents, raw=resolved, sources=tuple(sources))


def _quote_toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _quote_toml(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    if value is None:
        return _quote_toml("")
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def toml_dumps(value: dict[str, Any]) -> str:
    lines: list[str] = []

    def emit_table(prefix: tuple[str, ...], table: dict[str, Any]) -> None:
        scalars = {key: item for key, item in table.items() if not isinstance(item, dict)}
        children = {key: item for key, item in table.items() if isinstance(item, dict)}
        if prefix:
            lines.append("[" + ".".join(prefix) + "]")
        for key, item in scalars.items():
            lines.append(f"{key} = {_toml_scalar(item)}")
        if prefix or scalars:
            lines.append("")
        for key, child in children.items():
            emit_table((*prefix, key), child)

    emit_table((), value)
    return "\n".join(lines).rstrip() + "\n"


def default_user_config(installed_presets: list[str]) -> str:
    agents = {name: {"preset": name, "enabled": True} for name in installed_presets}
    value: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "policy": {
            "max_concurrency": 4,
            "default_timeout": "30m",
            "max_timeout": "2h",
            "max_permission": "workspace_write",
            "retention_days": 30,
            "inline_result_bytes": 262144,
            "max_output_bytes": 67108864,
        },
        "agents": agents,
    }
    return toml_dumps(value)


def basic_environment(extra_names: tuple[str, ...]) -> dict[str, str]:
    exact = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TERM",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
    allowed = exact | set(extra_names)
    return {
        key: value for key, value in os.environ.items() if key in allowed or key.startswith("LC_")
    }
