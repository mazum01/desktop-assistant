"""Display unit system — which measurement conventions the UI uses.

A device-wide presentation preference: every browser showing the dashboard
uses the same system. Readings stay in their native units on the bus and in
the API; the web UI converts at render time (``src/web/static/js/core/units.js``
holds the conversion table, mirroring :data:`SYSTEMS` here).

The chosen system is persisted in
``~/.config/desktop-assistant/display_units.json`` so it survives restarts.
The default comes from ``web_dashboard.display_units`` in ``assistant.yaml``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".config" / "desktop-assistant"
STATE_FILE = STATE_DIR / "display_units.json"

# Order is the order shown in Settings.
SYSTEMS: dict[str, dict] = {
    "us": {
        "label": "US Customary",
        "units": {"temperature": "°F", "flow": "gal/min", "volume": "gal",
                  "pressure": "psi", "radon": "pCi/L", "length": "ft"},
    },
    "si": {
        "label": "Metric (SI / MKS)",
        "units": {"temperature": "°C", "flow": "L/min", "volume": "L",
                  "pressure": "kPa", "radon": "Bq/m³", "length": "m"},
    },
    "imperial": {
        "label": "UK Imperial",
        "units": {"temperature": "°C", "flow": "imp gal/min", "volume": "imp gal",
                  "pressure": "psi", "radon": "Bq/m³", "length": "ft"},
    },
    "scientific": {
        "label": "Scientific (SI, kelvin)",
        "units": {"temperature": "K", "flow": "L/min", "volume": "L",
                  "pressure": "kPa", "radon": "Bq/m³", "length": "m"},
    },
}

FALLBACK_SYSTEM = "us"


def normalize(system: Optional[str], default: str = FALLBACK_SYSTEM) -> str:
    """Return *system* if it's a known id, else *default* (or the fallback)."""
    key = str(system or "").strip().lower()
    if key in SYSTEMS:
        return key
    return default if default in SYSTEMS else FALLBACK_SYSTEM


def load(default: str = FALLBACK_SYSTEM, path: Optional[Path] = None) -> str:
    """Return the persisted system id, or *default* when unset/invalid."""
    path = path or STATE_FILE
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data.get("system") in SYSTEMS:
                return data["system"]
    except Exception as exc:
        log.warning("display_units: failed to read %s: %s", path, exc)
    return normalize(default)


def save(system: str, path: Optional[Path] = None) -> str:
    """Persist *system*; raises ``ValueError`` for an unknown id."""
    path = path or STATE_FILE
    if system not in SYSTEMS:
        raise ValueError(f"unknown unit system: {system!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"system": system}))
    tmp.replace(path)
    return system


def describe(system: str) -> dict:
    """API payload: the active system plus every choice, in display order."""
    return {
        "system": system,
        "systems": [{"id": key, "label": spec["label"], "units": dict(spec["units"])}
                    for key, spec in SYSTEMS.items()],
    }
