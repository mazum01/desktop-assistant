"""
Audio/visual sync for the mouth display's spectrum visualization.

The spectrum bars on the ESP32 mouth display necessarily trail the audio they
describe: the FFT window has to fill, the frame crosses BLE, and the firmware
renders it. The speaker, meanwhile, plays the same audio only after PipeWire's
output buffering. ``DisplayService`` measures the display side per frame
(``display.spectrum_latency``); this module estimates the speaker side from
PipeWire's reported latencies, and sets the EQ filter-chain's ``sync_delay``
stage so the sound reaches the listener at the same moment the bars do (or a
configurable ``lead_ms`` after them).

All timings are relative to the spectrum tap -- the EQ sink's monitor, which
is *before* the delay stage -- so changing the delay never feeds back into the
measurement.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class AvSyncConfig:
    enabled: bool = True
    # How far the bars should run *ahead* of the sound. 0 = exactly in sync.
    lead_ms: float = 0.0
    # Average wait between the firmware accepting a frame (when it acks) and
    # the bars being drawn: its EQ renderer redraws every 40 ms.
    render_ms: float = 20.0
    # Share of the measured write->ack round trip spent getting the frame *to*
    # the display (the rest is the ack's trip back).
    ack_outbound_fraction: float = 0.5
    # Fixed trim added to PipeWire's reported output latency, for DAC/USB
    # pipeline delay PipeWire can't see.
    extra_output_ms: float = 0.0
    max_delay_ms: float = 400.0
    # Ignore changes smaller than this: every change makes the delay line jump,
    # which is a tiny audible skip.
    min_change_ms: float = 10.0
    # Need at least this many measured frames before trusting a report.
    min_samples: int = 30


def estimate_display_lag_ms(report: dict, cfg: AvSyncConfig) -> Optional[float]:
    """Audio-window centre (at the tap) -> bars visibly updated, in ms."""
    try:
        to_send = float(report["audio_to_send_ms"])
        write = float(report["ble_write_ms"])
        to_ack = float(report["write_to_ack_ms"])
    except (KeyError, TypeError, ValueError):
        return None
    return to_send + write + to_ack * float(cfg.ack_outbound_fraction) + float(cfg.render_ms)


def compute_delay_ms(report: dict, output_latency_ms: float, cfg: AvSyncConfig) -> Optional[float]:
    """Audio delay that lands the sound ``lead_ms`` after the bars."""
    lag = estimate_display_lag_ms(report, cfg)
    if lag is None:
        return None
    delay = lag + float(cfg.lead_ms) - float(output_latency_ms) - float(cfg.extra_output_ms)
    return max(0.0, min(delay, float(cfg.max_delay_ms)))


def _parse_pw_top_driver(text: str, node_name: str) -> Optional[tuple[int, int]]:
    """Return (quantum, rate) for *node_name*'s driver row in ``pw-top -b``."""
    found = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "R" and parts[-1] == node_name:
            try:
                q, r = int(parts[2]), int(parts[3])
            except ValueError:
                continue
            if q > 0 and r > 0:
                found = (q, r)
    return found


def estimate_output_latency_ms(sink_name: str) -> Optional[float]:
    """Tap -> speaker latency for the hardware sink, from PipeWire's own data.

    One graph cycle (the live driver quantum) plus the sink's reported port
    latency (ALSA period/headroom buffering).
    """
    try:
        top = subprocess.run(["pw-top", "-b", "-n", "3"], capture_output=True,
                             text=True, timeout=8).stdout
    except Exception:
        return None
    qr = _parse_pw_top_driver(top, sink_name)
    if qr is None:
        return None
    quantum, rate = qr
    # Default: the device buffers one graph cycle.
    min_quantum, min_rate, min_ns = 1.0, 0.0, 0.0
    try:
        dump = json.loads(subprocess.run(["pw-dump"], capture_output=True,
                                         text=True, timeout=8).stdout)
        for obj in dump:
            info = obj.get("info") or {}
            if (info.get("props") or {}).get("node.name") != sink_name:
                continue
            for lat in (info.get("params") or {}).get("Latency") or []:
                if lat.get("direction") == "Input":
                    min_quantum = float(lat.get("minQuantum", 1.0))
                    min_rate = float(lat.get("minRate", 0))
                    min_ns = float(lat.get("minNs", 0))
            break
    except Exception:
        pass
    latency_s = min_quantum * quantum / rate + min_rate / rate + min_ns / 1e9
    return latency_s * 1000.0


class AvSyncCalibrator:
    """Turns display latency reports into a live audio sync delay.

    Reports arrive on the BLE notification thread, so the slow PipeWire
    queries and ``pw-cli`` call run on a private worker thread.
    """

    def __init__(
        self,
        cfg: AvSyncConfig,
        sink_name_fn: Callable[[], Optional[str]],
        set_delay_fn: Optional[Callable[[float], bool]] = None,
        output_latency_fn: Callable[[str], Optional[float]] = estimate_output_latency_ms,
    ) -> None:
        if set_delay_fn is None:
            from src.audio import pipewire_eq
            set_delay_fn = pipewire_eq.set_sync_delay
        self._cfg = cfg
        self._sink_name_fn = sink_name_fn
        self._set_delay_fn = set_delay_fn
        self._output_latency_fn = output_latency_fn
        self._lock = threading.Lock()
        self._pending: Optional[dict] = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._applied_ms: Optional[float] = None
        self._output_ms: Optional[float] = None
        self._output_at = 0.0

    @property
    def applied_ms(self) -> Optional[float]:
        return self._applied_ms

    def start(self) -> None:
        if not self._cfg.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="av-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3.0)
        self._thread = None

    def invalidate(self) -> None:
        """Force the next report to re-apply (e.g. the filter-chain restarted)."""
        self._applied_ms = None
        self._output_ms = None

    def on_latency_report(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        if int(payload.get("samples", 0) or 0) < int(self._cfg.min_samples):
            return
        with self._lock:
            self._pending = dict(payload)
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            with self._lock:
                report, self._pending = self._pending, None
            if report is not None and not self._stop.is_set():
                try:
                    self.process(report)
                except Exception:
                    log.exception("AvSync: calibration failed")

    def process(self, report: dict) -> Optional[float]:
        """Compute and (if changed enough) apply the delay. Returns applied ms."""
        sink = self._sink_name_fn()
        now = time.monotonic()
        if sink and (self._output_ms is None or now - self._output_at > 300.0):
            measured = self._output_latency_fn(sink)
            if measured is not None:
                self._output_ms = measured
                self._output_at = now
        if self._output_ms is None:
            return None
        delay = compute_delay_ms(report, self._output_ms, self._cfg)
        if delay is None:
            return None
        if (self._applied_ms is not None
                and abs(delay - self._applied_ms) < float(self._cfg.min_change_ms)):
            # Re-assert the current value (a no-op for the delay line) so a
            # filter-chain rebuild, which resets the delay to 0, self-heals.
            self._set_delay_fn(self._applied_ms / 1000.0)
            return self._applied_ms
        if self._set_delay_fn(delay / 1000.0):
            log.info("AvSync: audio delay %.0f ms (display lag %.0f ms, "
                     "output latency %.0f ms, lead %.0f ms)", delay,
                     estimate_display_lag_ms(report, self._cfg) or 0.0,
                     self._output_ms, self._cfg.lead_ms)
            self._applied_ms = delay
        return self._applied_ms
