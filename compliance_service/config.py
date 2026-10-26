"""Local model configuration.

Configuration is deliberately opt-in.  The service never contacts a model merely
because this module is imported; callers must enable a provider explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path
import tomllib


DEFAULT_MODEL_CONFIG = Path("config") / "models.local.toml"


def _section(raw, name):
    value = raw.get(name, {}) if isinstance(raw, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def load_model_settings(path=None):
    """Load OCR/vision settings without resolving or exposing secret values.

    ``path`` wins over ``COMPLIANCE_MODEL_CONFIG``; absent files are equivalent
    to disabled providers. Local ``api_key`` values are supported, so callers
    must never log or persist the returned mapping as evidence metadata.
    """
    configured = path or os.environ.get("COMPLIANCE_MODEL_CONFIG") or DEFAULT_MODEL_CONFIG
    file_path = Path(configured)
    raw = {}
    if file_path.is_file():
        try:
            with file_path.open("rb") as stream:
                loaded = tomllib.load(stream)
            raw = loaded if isinstance(loaded, dict) else {}
        except (OSError, tomllib.TOMLDecodeError):
            raw = {}
    result = {"path": str(file_path), "ocr": _section(raw, "ocr"), "vision": _section(raw, "vision")}
    for name, defaults in {
        "ocr": {"enabled": False, "provider": "multipart-http", "url": "", "base_url": "", "api_key_env": "", "timeout_seconds": 30},
        "vision": {"enabled": False, "provider": "openai-compatible", "base_url": "", "model": "", "api_key_env": "", "api_key": "", "timeout_seconds": 60},
    }.items():
        result[name] = {**defaults, **result[name]}
        result[name]["enabled"] = result[name].get("enabled") is True
    return result

