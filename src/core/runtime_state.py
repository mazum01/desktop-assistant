"""Runtime state persistence.

Stores settings that change at runtime (servo toggles, tracking flags) so
they survive daemon restarts. Written atomically to config/runtime_state.yaml.

The runtime state *overlays* assistant.yaml — only the keys present in the
runtime file override the base config; everything else falls through to the
user's config defaults.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).parents[2] / "config" / "runtime_state.yaml"
_lock = threading.Lock()


def load(path: Path = _DEFAULT_PATH) -> dict:
    """Load runtime state from disk; returns empty dict if file is missing or corrupt."""
    try:
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("runtime_state: load failed: %s", exc)
    return {}


def save(state: dict, path: Path = _DEFAULT_PATH) -> None:
    """Atomically write runtime state dict to disk."""
    with _lock:
        try:
            tmp = path.with_suffix(".yaml.tmp")
            tmp.write_text(yaml.dump(state, default_flow_style=False))
            tmp.replace(path)
        except Exception as exc:
            log.warning("runtime_state: save failed: %s", exc)
