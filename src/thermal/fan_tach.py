"""
Fan tachometer reader.

The Noctua NF-A6x25 tach line is open-collector and pulses **2 times per
revolution**.  We use a polling thread that samples the GPIO at 0.1 ms intervals
and detects falling edges by state comparison.  This approach works reliably on
the Pi 5 (RP1 chip) where lgpio edge callbacks are not delivered.

    rpm              -> int | None    (None if we have no recent pulses)
    pulses_per_sec   -> float

Wiring:
    Fan tach (yellow)  ->  GPIO6 (pin 31)  with 10 kΩ pull-up to 3.3 V
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from collections import deque
from typing import Deque, Optional

try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False

log = logging.getLogger(__name__)

_DEFAULT_GPIO            = 6
_DEFAULT_PULSES_PER_REV  = 2
_WINDOW_S                = 1.0   # rolling pulse window for RPM
_POLL_INTERVAL_S         = 0.0001  # 0.1 ms → 0.23 ms actual on Pi 5; ~99% detection at 5000 RPM


class FanTach:
    """Pulse-counting tachometer for a 4-pin PWM fan."""

    def __init__(
        self,
        gpio: int = _DEFAULT_GPIO,
        pulses_per_rev: int = _DEFAULT_PULSES_PER_REV,
    ) -> None:
        self._gpio        = gpio
        self._ppr         = max(1, pulses_per_rev)
        self._handle: Optional[int] = None
        self._timestamps: Deque[float] = deque()
        self._lock        = threading.Lock()
        self._stop_event  = threading.Event()
        self._thread: Optional[threading.Thread] = None

        if not _LGPIO_AVAILABLE:
            log.warning("lgpio not available — FanTach running in simulation mode")
            return

        try:
            self._handle = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_input(self._handle, self._gpio, lgpio.SET_PULL_UP)
            self._thread = threading.Thread(
                target=self._poll_loop, name="fan-tach-poll", daemon=True
            )
            self._thread.start()
            atexit.register(self.close)
            log.info("FanTach watching GPIO%d (%d ppr) via poll", self._gpio, self._ppr)
        except Exception:
            log.exception("Failed to initialise FanTach — RPM will be None")
            self._handle = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def rpm(self) -> Optional[int]:
        pps = self.pulses_per_sec
        if pps <= 0:
            return None
        return int(round(pps * 60.0 / self._ppr))

    @property
    def pulses_per_sec(self) -> float:
        now    = time.monotonic()
        cutoff = now - _WINDOW_S
        with self._lock:
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            return len(self._timestamps) / _WINDOW_S

    def inject_pulse(self, t: Optional[float] = None) -> None:
        """Test hook: simulate a tach pulse."""
        with self._lock:
            self._timestamps.append(t if t is not None else time.monotonic())

    def close(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._handle is not None:
            try:
                lgpio.gpio_free(self._handle, self._gpio)
                lgpio.gpiochip_close(self._handle)
            except Exception:
                pass
            self._handle = None

    # ------------------------------------------------------------------
    # Internal — polling thread detects falling edges via state comparison
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        prev = 1  # line starts HIGH (pull-up)
        while not self._stop_event.is_set():
            try:
                level = lgpio.gpio_read(self._handle, self._gpio)
                # Falling edge: HIGH → LOW
                if prev == 1 and level == 0:
                    with self._lock:
                        self._timestamps.append(time.monotonic())
                prev = level
            except Exception as exc:
                log.warning("FanTach poll error (retrying): %s", exc)
                time.sleep(0.1)  # brief pause before retrying
                continue
            time.sleep(_POLL_INTERVAL_S)

