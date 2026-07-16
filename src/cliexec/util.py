from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path

from .errors import CONFIG_ERROR, CLIExecError

_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>s|m|h|d)$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_duration(value: str | int | float) -> float:
    if isinstance(value, (int, float)):
        if value <= 0:
            raise CLIExecError(CONFIG_ERROR, "duration must be positive")
        return float(value)
    match = _DURATION_RE.fullmatch(value.strip())
    if not match:
        raise CLIExecError(CONFIG_ERROR, f"invalid duration: {value!r}")
    amount = float(match.group("value"))
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group("unit")]
    if amount <= 0:
        raise CLIExecError(CONFIG_ERROR, "duration must be positive")
    return amount * factor


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cliexec"


def state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "cliexec"


def cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cliexec"


def permission_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except FileNotFoundError:
        return
