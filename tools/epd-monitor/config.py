"""Configuration loader for EPD Monitor.

Reads a TOML file, validates required fields, and returns typed dicts
suitable for passing to provider factories.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            raise ImportError(
                "Python < 3.11 requires 'tomli': pip install tomli"
            ) from None


# Defaults
_DEFAULTS: dict[str, Any] = {
    "device_name": "NRF_EPD",
    "device_address": "",
    "refresh_interval": 1800,
    "scan_timeout": 15,
    "log_level": "INFO",
    "title": "SUB MONITOR",
    "font_path": "",
    "fast_write": False,
}


def defaults() -> dict[str, Any]:
    """Settings to use when there is no config file (BLE-only commands)."""
    return dict(_DEFAULTS)


def load(path: str | Path, *, require_providers: bool = True) -> dict[str, Any]:
    """Load and validate config from *path*.

    Returns a dict with top-level settings and a ``providers`` list.
    ``require_providers`` is off for the commands that only talk to the device
    and never fetch data - scanning, describing, patterns, fault injection.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")

    with p.open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)

    cfg: dict[str, Any] = {**_DEFAULTS, **{k: v for k, v in raw.items() if k != "providers"}}

    providers: list[dict[str, Any]] = raw.get("providers", [])
    if not providers:
        if require_providers:
            raise ValueError("Config must define at least one [[providers]] entry")
        return cfg

    for i, prov in enumerate(providers):
        if "type" not in prov:
            raise ValueError(f"providers[{i}] is missing 'type'")

    cfg["providers"] = providers
    return cfg
