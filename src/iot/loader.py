"""IoT plugin loader — discovers, instantiates, and persists IoT device plugins."""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import pkgutil
from pathlib import Path
from typing import TYPE_CHECKING

from src.iot.base import IoTDevice

if TYPE_CHECKING:
    from src.iot.registry import IoTRegistry

log = logging.getLogger(__name__)

_PERSIST_PATH = Path(__file__).parents[2] / "config" / "iot_devices.json"


# ── Type discovery ────────────────────────────────────────────────────────────


def discover_types() -> dict[str, type[IoTDevice]]:
    """Scan ``src.iot.devices`` and return a mapping of ``device_id → class``.

    Only classes that:
    - Are concrete (not abstract) subclasses of ``IoTDevice``, AND
    - Have a non-empty ``device_id`` class attribute

    …are included.
    """
    import src.iot.devices as _devices_pkg  # imported lazily so tests can monkeypatch

    types: dict[str, type[IoTDevice]] = {}
    for _, modname, _ in pkgutil.iter_modules(_devices_pkg.__path__):
        full_name = f"src.iot.devices.{modname}"
        try:
            mod = importlib.import_module(full_name)
        except Exception:
            log.exception("IoT loader: failed to import %r", full_name)
            continue
        for _name, cls in inspect.getmembers(mod, inspect.isclass):
            if (
                cls is not IoTDevice
                and issubclass(cls, IoTDevice)
                and cls.device_id
                and not inspect.isabstract(cls)
            ):
                if cls.device_id in types:
                    log.warning(
                        "IoT loader: duplicate device_id %r in %r (already registered from %r) — skipping",
                        cls.device_id, full_name, types[cls.device_id].__module__,
                    )
                else:
                    types[cls.device_id] = cls
    return types


def get_type_list() -> list[dict]:
    """Return a summary list of available plugin types."""
    return [
        {
            "type_id":     cls.device_id,
            "device_name": cls.device_name,
            "device_icon": cls.device_icon,
        }
        for cls in discover_types().values()
    ]


# ── Device creation ───────────────────────────────────────────────────────────


def create_device(type_id: str, cfg: dict | None = None, bus=None) -> IoTDevice:
    """Instantiate a device by its ``type_id``.

    Raises ``ValueError`` if the type is not found in ``src.iot.devices``.
    """
    types = discover_types()
    cls = types.get(type_id)
    if cls is None:
        available = list(types.keys()) or ["(none)"]
        raise ValueError(
            f"Unknown IoT device type {type_id!r}. "
            f"Available types: {', '.join(available)}"
        )
    return cls(bus=bus, cfg=cfg or {})


# ── Persistence ───────────────────────────────────────────────────────────────


def load_persisted(registry: "IoTRegistry", bus=None, path: Path | None = None) -> int:
    """Load and register all IoT devices from the persistence file.

    Devices that fail to load are logged and skipped.  Returns the number of
    devices successfully loaded.
    """
    persist_path = path or _PERSIST_PATH
    if not persist_path.exists():
        return 0
    try:
        entries = json.loads(persist_path.read_text())
    except Exception:
        log.exception("IoT loader: failed to read %s", persist_path)
        return 0

    if not isinstance(entries, list):
        log.warning("IoT loader: %s is not a JSON array — skipping", persist_path)
        return 0

    loaded = 0
    for entry in entries:
        type_id = entry.get("type_id", "")
        cfg     = entry.get("config", {})
        if not type_id:
            log.warning("IoT loader: persisted entry missing type_id: %r", entry)
            continue
        try:
            dev = create_device(type_id, cfg, bus=bus)
            registry.register(dev)
            dev.start()
            loaded += 1
            log.info("IoT loader: loaded persisted device %r", type_id)
        except Exception:
            log.exception("IoT loader: failed to load persisted device %r", type_id)

    return loaded


def save_persisted(registry: "IoTRegistry", path: Path | None = None) -> None:
    """Save the current registry to the persistence file.

    Only devices that were loaded via ``create_device()`` (i.e., have a
    discoverable type_id matching ``discover_types()``) are saved.  Devices
    with type_ids that are NOT in ``discover_types()`` (e.g., built-in
    services like radon/drop) are excluded.
    """
    persist_path = path or _PERSIST_PATH
    known_types  = set(discover_types().keys())

    entries = []
    for dev in registry.all():
        if dev.device_id not in known_types:
            continue  # not a discoverable plugin — skip (e.g. hardwired devices)
        entries.append({"type_id": dev.device_id, "config": dict(dev._cfg)})

    try:
        persist_path.parent.mkdir(parents=True, exist_ok=True)
        persist_path.write_text(json.dumps(entries, indent=2))
        log.debug("IoT loader: saved %d device(s) to %s", len(entries), persist_path)
    except Exception:
        log.exception("IoT loader: failed to write %s", persist_path)
