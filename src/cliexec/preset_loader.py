from __future__ import annotations

import tomllib
from importlib.resources import files
from typing import Any


def load_builtin_presets() -> dict[str, dict[str, Any]]:
    """Load the packaged, data-driven agent presets keyed by preset name."""
    preset_dir = files("cliexec").joinpath("presets")
    loaded: dict[str, dict[str, Any]] = {}

    for resource in sorted(preset_dir.iterdir(), key=lambda item: item.name):
        if not resource.name.endswith(".toml"):
            continue
        name = resource.name.removesuffix(".toml")
        with resource.open("rb") as handle:
            value = tomllib.load(handle)
        if not isinstance(value, dict):
            raise ValueError(f"built-in preset {name!r} must contain a TOML table")
        loaded[name] = value

    return loaded
