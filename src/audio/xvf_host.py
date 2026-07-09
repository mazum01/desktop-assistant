"""XVF3800 host-control wrapper for ReSpeaker Flex devices.

This follows Seeed's official USB vendor-control transport model but exposes a
smaller, UI-friendly subset of safe diagnostics and tunables.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
import sys
import threading
import time
from typing import Any

try:
    import usb.core
    import usb.util
except ImportError:  # pragma: no cover - depends on host environment
    usb = None  # type: ignore[assignment]

try:  # pragma: no cover - Windows-only dependency
    import libusb_package
except ImportError:  # pragma: no cover - optional
    libusb_package = None

CONTROL_SUCCESS = 0
SERVICER_COMMAND_RETRY = 64
DEFAULT_VID = 0x2886


@dataclass(frozen=True)
class XvfParameterSpec:
    name: str
    resid: int
    cmdid: int
    length: int
    access: str
    data_type: str
    description: str
    group: str
    widget: str = "number"


_PARAMETERS: tuple[XvfParameterSpec, ...] = (
    XvfParameterSpec("VERSION", 48, 0, 3, "ro", "uint8", "Firmware version", "device", "version"),
    XvfParameterSpec("BLD_MSG", 48, 1, 50, "ro", "char", "Firmware build message", "device", "text"),
    XvfParameterSpec("BOOT_STATUS", 48, 5, 3, "ro", "char", "Boot source status", "device", "text"),
    XvfParameterSpec("AEC_MIC_ARRAY_TYPE", 33, 73, 1, "ro", "int32", "Microphone array type", "signals"),
    XvfParameterSpec("DOA_VALUE", 20, 18, 2, "ro", "uint16", "Direction of arrival", "signals", "doa"),
    XvfParameterSpec("AEC_AZIMUTH_VALUES", 33, 75, 4, "ro", "radians", "Beam azimuths", "signals"),
    XvfParameterSpec("AEC_SPENERGY_VALUES", 33, 80, 4, "ro", "float", "Speech energy per beam", "signals"),
    XvfParameterSpec("AUDIO_MGR_SELECTED_AZIMUTHS", 35, 11, 2, "ro", "radians", "Selected azimuths", "signals"),
    XvfParameterSpec("I2S_INACTIVE", 35, 24, 1, "ro", "uint8", "I2S path inactive", "signals", "bool"),
    XvfParameterSpec("AUDIO_MGR_MIC_GAIN", 35, 0, 1, "rw", "float", "Mic gain", "tunables"),
    XvfParameterSpec("AUDIO_MGR_REF_GAIN", 35, 1, 1, "rw", "float", "Reference gain", "tunables"),
    XvfParameterSpec("AEC_ASROUTONOFF", 33, 35, 1, "rw", "int32", "ASR output enabled", "tunables", "bool"),
    XvfParameterSpec("AEC_ASROUTGAIN", 33, 36, 1, "rw", "float", "ASR output gain", "tunables"),
    XvfParameterSpec("PP_AGCONOFF", 17, 10, 1, "rw", "int32", "AGC enabled", "tunables", "bool"),
    XvfParameterSpec("PP_AGCMAXGAIN", 17, 11, 1, "rw", "float", "AGC max gain", "tunables"),
    XvfParameterSpec("PP_AGCDESIREDLEVEL", 17, 12, 1, "rw", "float", "AGC desired level", "tunables"),
    XvfParameterSpec("PP_AGCTIME", 17, 14, 1, "rw", "float", "AGC time", "tunables"),
    XvfParameterSpec("PP_AGCFASTTIME", 17, 15, 1, "rw", "float", "AGC fast time", "tunables"),
    XvfParameterSpec("PP_LIMITONOFF", 17, 19, 1, "rw", "int32", "Limiter enabled", "tunables", "bool"),
    XvfParameterSpec("PP_LIMITPLIMIT", 17, 20, 1, "rw", "float", "Limiter power limit", "tunables"),
    XvfParameterSpec("PP_MIN_NS", 17, 21, 1, "rw", "float", "Stationary noise floor", "tunables"),
    XvfParameterSpec("PP_MIN_NN", 17, 22, 1, "rw", "float", "Non-stationary noise floor", "tunables"),
    XvfParameterSpec("PP_ECHOONOFF", 17, 23, 1, "rw", "int32", "Echo suppression", "tunables", "bool"),
)

_SAVE_CONFIGURATION = XvfParameterSpec(
    "SAVE_CONFIGURATION", 48, 9, 1, "wo", "uint8", "Persist current configuration", "actions"
)

_PARAMETER_MAP = {spec.name: spec for spec in _PARAMETERS}


def _usb_find():
    if usb is None:  # pragma: no cover - depends on environment
        raise RuntimeError("pyusb is not installed")
    if sys.platform.startswith("win"):  # pragma: no cover - Windows only
        if libusb_package is None:
            raise RuntimeError("Windows requires libusb-package")
        return libusb_package.find
    return usb.core.find


class XvfHostController:
    """Thin wrapper around the XVF3800 USB control protocol."""

    timeout_ms = 100000

    def __init__(self, device: Any) -> None:
        self._device = device
        self._lock = threading.Lock()

    @classmethod
    def find(cls, vid: int = DEFAULT_VID, pid: int | None = None) -> "XvfHostController | None":
        finder = _usb_find()
        if pid is not None:
            device = finder(idVendor=vid, idProduct=pid)
            return None if device is None else cls(device)
        devices = list(finder(find_all=True, idVendor=vid) or [])
        if not devices:
            return None
        devices.sort(key=lambda dev: getattr(dev, "idProduct", 0))
        return cls(devices[0])

    def close(self) -> None:
        usb.util.dispose_resources(self._device)

    def __enter__(self) -> "XvfHostController":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "vid": f"0x{int(getattr(self._device, 'idVendor', 0)):04x}",
            "pid": f"0x{int(getattr(self._device, 'idProduct', 0)):04x}",
            "vendor_id": f"0x{int(getattr(self._device, 'idVendor', 0)):04x}",
            "product_id": f"0x{int(getattr(self._device, 'idProduct', 0)):04x}",
            "bus": getattr(self._device, "bus", None),
            "address": getattr(self._device, "address", None),
        }

    def _response_length(self, spec: XvfParameterSpec) -> int:
        if spec.data_type in {"uint8", "char"}:
            return spec.length + 1
        if spec.data_type in {"float", "radians", "uint32", "int32"}:
            return spec.length * 4 + 1
        if spec.data_type == "uint16":
            return spec.length * 2 + 1
        raise ValueError(f"Unsupported XVF data type {spec.data_type!r}")

    def _decode(self, spec: XvfParameterSpec, response: Any) -> tuple[Any, ...] | str:
        data = response.tobytes()[1:]
        if spec.data_type == "char":
            return data.rstrip(b"\x00").decode("utf-8", errors="ignore")
        if spec.data_type == "uint8":
            return struct.unpack("<" + "B" * spec.length, data)
        if spec.data_type in {"float", "radians"}:
            return struct.unpack("<" + "f" * spec.length, data)
        if spec.data_type == "uint32":
            return struct.unpack("<" + "I" * spec.length, data)
        if spec.data_type == "int32":
            return struct.unpack("<" + "i" * spec.length, data)
        if spec.data_type == "uint16":
            return struct.unpack("<" + "H" * spec.length, data)
        raise ValueError(f"Unsupported XVF data type {spec.data_type!r}")

    def _encode(self, spec: XvfParameterSpec, values: list[Any]) -> bytes:
        if len(values) != spec.length:
            raise ValueError(f"{spec.name} expects {spec.length} value(s)")
        if spec.data_type in {"float", "radians"}:
            return b"".join(struct.pack("<f", float(v)) for v in values)
        if spec.data_type == "char":
            raise ValueError(f"{spec.name} does not support char writes")
        if spec.data_type == "uint8":
            return b"".join(int(v).to_bytes(1, byteorder="little", signed=False) for v in values)
        if spec.data_type == "uint32":
            return b"".join(struct.pack("<I", int(v)) for v in values)
        if spec.data_type == "int32":
            return b"".join(struct.pack("<i", int(v)) for v in values)
        if spec.data_type == "uint16":
            return b"".join(struct.pack("<H", int(v)) for v in values)
        raise ValueError(f"Unsupported XVF data type {spec.data_type!r}")

    def read(self, command: str) -> tuple[Any, ...] | str:
        spec = _PARAMETER_MAP[command]
        with self._lock:
            response = self._device.ctrl_transfer(
                usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                0,
                0x80 | spec.cmdid,
                spec.resid,
                self._response_length(spec),
                self.timeout_ms,
            )
            attempts = 1
            while response[0] != CONTROL_SUCCESS:
                if response[0] != SERVICER_COMMAND_RETRY:
                    raise RuntimeError(f"XVF read {command} failed with status {response[0]}")
                attempts += 1
                if attempts > 100:
                    raise RuntimeError(f"XVF read {command} exceeded retry limit")
                time.sleep(0.01)
                response = self._device.ctrl_transfer(
                    usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                    0,
                    0x80 | spec.cmdid,
                    spec.resid,
                    self._response_length(spec),
                    self.timeout_ms,
                )
            return self._decode(spec, response)

    def write(self, command: str, values: list[Any]) -> None:
        spec = _PARAMETER_MAP[command]
        if spec.access != "rw":
            raise ValueError(f"{command} is not writable")
        payload = self._encode(spec, values)
        with self._lock:
            self._device.ctrl_transfer(
                usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                0,
                spec.cmdid,
                spec.resid,
                payload,
                self.timeout_ms,
            )

    def save_configuration(self) -> None:
        spec = _SAVE_CONFIGURATION
        with self._lock:
            self._device.ctrl_transfer(
                usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                0,
                spec.cmdid,
                spec.resid,
                bytes([1]),
                self.timeout_ms,
            )

    def _format_value(self, spec: XvfParameterSpec, raw: tuple[Any, ...] | str) -> dict[str, Any]:
        if isinstance(raw, str):
            return {"value": raw, "display": raw}
        values = list(raw)
        if spec.name == "VERSION":
            version = ".".join(str(int(v)) for v in values)
            return {"value": version, "display": version, "values": values}
        if spec.name == "DOA_VALUE":
            angle = int(values[0]) if values else 0
            speech_detected = bool(values[1]) if len(values) > 1 else False
            display = f"{angle} deg ({'speech' if speech_detected else 'no speech'})"
            return {
                "value": {"angle_deg": angle, "speech_detected": speech_detected},
                "display": display,
                "values": values,
            }
        if spec.widget == "bool":
            state = bool(values[0])
            return {"value": state, "display": "on" if state else "off", "values": values}
        if spec.length == 1:
            scalar = values[0]
            if isinstance(scalar, float):
                return {"value": float(scalar), "display": f"{float(scalar):.3f}", "values": values}
            return {"value": int(scalar), "display": str(int(scalar)), "values": values}
        display_values = [f"{float(v):.3f}" if isinstance(v, float) else str(v) for v in values]
        return {"value": values, "display": ", ".join(display_values), "values": values}

    def snapshot(self) -> dict[str, Any]:
        readonly: list[dict[str, Any]] = []
        tunables: list[dict[str, Any]] = []
        for spec in _PARAMETERS:
            formatted = self._format_value(spec, self.read(spec.name))
            item = {
                "name": spec.name,
                "command": spec.name,
                "label": spec.description,
                "group": spec.group,
                "access": spec.access,
                "data_type": spec.data_type,
                "dtype": "bool" if spec.widget == "bool" else ("float" if spec.data_type in {"float", "radians"} else "int"),
                "widget": spec.widget,
                "description": spec.description,
                "min": None,
                "max": None,
                **formatted,
            }
            if spec.access == "ro":
                readonly.append(item)
            else:
                tunables.append(item)
        return {
            "connected": True,
            "usb": self.device_info,
            "device": self.device_info,
            "readonly": readonly,
            "tunables": tunables,
        }
