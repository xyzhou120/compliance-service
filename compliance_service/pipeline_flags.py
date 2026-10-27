"""Pipeline switches without environment variables.

Read from config/pipeline.local.toml when present, and overridable by CLI flags.
Environment variables keep working as a fallback so existing deployments do not
break, but nothing requires them.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULTS = {"page_receipts": False, "canonical_facts": False}
CONFIG_PATH = Path("config") / "pipeline.local.toml"
ENV_NAMES = {"page_receipts": "COMPLIANCE_PAGE_RECEIPTS",
             "canonical_facts": "COMPLIANCE_CANONICAL_FACTS"}

_state = dict(DEFAULTS)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_file(path=None) -> dict:
    """Read [pipeline] from a TOML file; missing or broken files keep defaults."""
    target = Path(path) if path else CONFIG_PATH
    try:
        with target.open("rb") as stream:
            payload = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = payload.get("pipeline")
    if not isinstance(section, dict):
        return {}
    return {key: _as_bool(section[key]) for key in DEFAULTS if key in section}


def configure(*, path=None, **overrides) -> dict:
    """Merge defaults <- file <- explicit overrides (CLI flags)."""
    merged = dict(DEFAULTS)
    merged.update(load_file(path))
    for key, value in overrides.items():
        if key in DEFAULTS and value is not None:
            merged[key] = _as_bool(value)
    _state.update(merged)
    return dict(_state)


def enabled(name: str, environ=None) -> bool:
    """Current setting; the environment only matters when nothing else was set."""
    if name not in DEFAULTS:
        raise KeyError(name)
    if _state.get(name):
        return True
    source = os.environ if environ is None else environ
    return _as_bool(source.get(ENV_NAMES.get(name, ""), ""))


def snapshot() -> dict:
    return dict(_state)
