"""
Radon monitoring service — EcoSense EcoQube cloud integration.

Polls the EcoSense cloud API every ``poll_interval_s`` seconds, caches
the latest radon reading in memory, and publishes it on the message bus.
Speaks an audible warning when the basement radon level crosses the EPA
action threshold (4.0 pCi/L).

Credentials are read at startup from environment variables injected via
``/etc/desktop-assistant/secrets.env``:

    ECOSENSE_USERNAME   — your EcoSense account e-mail
    ECOSENSE_PASSWORD   — your EcoSense account password

If either variable is absent the service starts in *degraded mode*: no
polling, but ``/api/radon`` still returns ``{"available": false}``.

Topics published:
    radon.reading   — dict with radon_bqm3, radon_pcil, alert,
                      device_name, serial_number, last_updated, error
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── EcoSense cloud constants ─────────────────────────────────────────────────
_USER_POOL_ID     = "us-west-2_vB73oNa7f"
_CLIENT_ID        = "1dk9ul54cdo42lt6e9u1oa9g1d"
_USER_POOL_REGION = "us-west-2"
_API_URL          = "https://api.cloud.ecosense.io/api/v1/device"

# Conversion: API returns Bq/m³; divide by 37 to get pCi/L
_BQM3_TO_PCIL: float = 37.0

# EPA recommended action thresholds (pCi/L)
_ALERT_CONSIDER: float = 2.7   # Green → Orange
_ALERT_RECOMMEND: float = 4.0  # Orange → Red


class RadonService:
    """
    Background service for EcoSense EcoQube radon monitor.

    Polls EcoSense cloud API on a configurable interval, caches the
    latest reading, publishes on the bus, and optionally speaks a TTS
    warning when the level exceeds the EPA action threshold.
    """

    name = "radon"

    def __init__(self, bus=None, cfg: Optional[dict] = None) -> None:
        self.bus = bus
        self._cfg: dict = cfg or {}
        self._poll_interval: float = float(self._cfg.get("poll_interval_s", 300))
        self._alert_on_red: bool = bool(self._cfg.get("alert_on_red", True))
        self._red_alert_cooldown: float = float(
            self._cfg.get("red_alert_cooldown_s", 3600)
        )
        # Telegram alert when radon ≥ this threshold (pCi/L); 0 disables
        self._telegram_alert_pcil: float = float(
            self._cfg.get("telegram_alert_pcil", 1.5)
        )
        self._telegram_cooldown: float = float(
            self._cfg.get("telegram_alert_cooldown_s", 7200)
        )
        self._latest: Optional[dict] = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_red_alert: float = float("-inf")
        self._last_telegram_alert: float = float("-inf")
        self._degraded = False  # True when credentials are missing

    # ── Service lifecycle ─────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        username = os.environ.get("ECOSENSE_USERNAME", "").strip()
        password = os.environ.get("ECOSENSE_PASSWORD", "").strip()

        self._running = True

        if not username or not password:
            log.warning(
                "RadonService: ECOSENSE_USERNAME / ECOSENSE_PASSWORD not set. "
                "Add credentials to /etc/desktop-assistant/secrets.env. "
                "Service running in degraded mode — no cloud polling."
            )
            self._degraded = True
            return

        self._degraded = False
        self._thread = threading.Thread(
            target=self._poll_loop,
            args=(username, password),
            daemon=True,
            name="radon-poll",
        )
        self._thread.start()
        log.info("RadonService started — polling EcoSense every %.0f s", self._poll_interval)

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        log.info("RadonService stopped")

    # ── Public API ────────────────────────────────────────────────────────

    def get_reading(self) -> Optional[dict]:
        """Return the latest cached radon reading, or None if none available."""
        with self._lock:
            return dict(self._latest) if self._latest else None

    @property
    def degraded(self) -> bool:
        """True if credentials were missing at startup."""
        return self._degraded

    # ── Internal ──────────────────────────────────────────────────────────

    def _poll_loop(self, username: str, password: str) -> None:
        """Background polling thread."""
        cognito = self._authenticate(username, password)
        if cognito is None:
            log.error("RadonService: authentication failed; polling disabled")
            return

        while self._running:
            try:
                reading = self._fetch(cognito, username, password)
                if reading:
                    with self._lock:
                        self._latest = reading
                    if self.bus:
                        self.bus.publish("radon.reading", reading)
                    self._maybe_alert_red(reading)
                    self._maybe_alert_telegram(reading)
            except Exception as exc:
                log.warning("RadonService poll error: %s", exc)
                with self._lock:
                    if self._latest:
                        self._latest = dict(self._latest)
                        self._latest["error"] = str(exc)

            # Sleep in 1-second increments so stop() is responsive
            deadline = time.monotonic() + self._poll_interval
            while self._running and time.monotonic() < deadline:
                time.sleep(1.0)

    def _authenticate(self, username: str, password: str):
        """Attempt Cognito authentication with exponential backoff. Returns Cognito or None."""
        for attempt in range(3):
            try:
                cognito = self._make_cognito(username, password)
                cognito.authenticate(password=password)
                log.info("RadonService: EcoSense Cognito auth OK")
                return cognito
            except Exception as exc:
                wait = 10 * (attempt + 1)
                log.warning(
                    "RadonService: auth attempt %d/3 failed (%s); retry in %ds",
                    attempt + 1, exc, wait,
                )
                time.sleep(wait)
        return None

    def _make_cognito(self, username: str, password: str):
        """Build a pycognito Cognito object (lazy import)."""
        try:
            from pycognito import Cognito  # noqa: PLC0415
            from botocore import UNSIGNED  # noqa: PLC0415
            from botocore.client import Config  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "pycognito or botocore is not installed. "
                "Run: pip install pycognito botocore"
            ) from exc
        return Cognito(
            _USER_POOL_ID,
            _CLIENT_ID,
            user_pool_region=_USER_POOL_REGION,
            username=username,
            boto3_client_kwargs={"config": Config(signature_version=UNSIGNED)},
        )

    def _fetch(self, cognito, username: str, password: str) -> Optional[dict]:
        """Call EcoSense API and return a reading dict."""
        import requests  # available via pycognito deps

        headers = {"Authorization": f"Bearer {cognito.id_token}"}
        params = {"email": username}

        resp = requests.get(_API_URL, headers=headers, params=params, timeout=15)

        if resp.status_code == 401:
            # Token expired — re-authenticate and retry
            log.debug("RadonService: 401 received, re-authenticating")
            cognito.authenticate(password=password)
            headers = {"Authorization": f"Bearer {cognito.id_token}"}
            resp = requests.get(_API_URL, headers=headers, params=params, timeout=15)

        resp.raise_for_status()
        devices: list = resp.json()

        if not devices:
            log.warning("RadonService: EcoSense API returned no devices")
            return None

        # Use the first device (the basement EcoQube)
        device = devices[0]
        return self._parse_device(device)

    def _parse_device(self, device: dict) -> dict:
        """Convert a raw EcoSense device dict into a normalised reading dict."""
        radon_raw = device.get("radon_level")
        now_iso = datetime.now(timezone.utc).isoformat()

        # Treat missing, empty, or zero value as "device initialising"
        try:
            radon_bqm3 = float(radon_raw) if radon_raw is not None else 0.0
        except (ValueError, TypeError):
            radon_bqm3 = 0.0

        if not radon_bqm3:
            return {
                "radon_bqm3": None,
                "radon_pcil": None,
                "alert": "Unknown",
                "device_name": device.get("device_name", "EcoQube"),
                "serial_number": device.get("serial_number", ""),
                "last_updated": now_iso,
                "error": "device initialising or offline",
            }

        radon_pcil = round(radon_bqm3 / _BQM3_TO_PCIL, 2)
        return {
            "radon_bqm3": round(radon_bqm3, 1),
            "radon_pcil": radon_pcil,
            "alert": _compute_alert(radon_pcil),
            "device_name": device.get("device_name", "EcoQube"),
            "serial_number": device.get("serial_number", ""),
            "last_updated": now_iso,
            "error": None,
        }

    def _maybe_alert_red(self, reading: dict) -> None:
        """Speak a TTS warning if alert is Red and cooldown has elapsed."""
        if not self._alert_on_red or reading.get("alert") != "Red":
            return
        now = time.monotonic()
        if now - self._last_red_alert < self._red_alert_cooldown:
            return
        self._last_red_alert = now
        pcil = reading.get("radon_pcil", "unknown")
        if self.bus:
            self.bus.publish(
                "av.say",
                {
                    "text": (
                        f"Warning: basement radon level is {pcil} picocuries per liter. "
                        "This exceeds the EPA action threshold of 4 picocuries per liter. "
                        "Please consider radon mitigation."
                    )
                },
            )
            log.warning(
                "RadonService: RED alert — %.2f pCi/L (%.1f Bq/m³)",
                pcil,
                reading.get("radon_bqm3", 0),
            )

    def _maybe_alert_telegram(self, reading: dict) -> None:
        """Send a Telegram message when radon exceeds the configured threshold.

        This replaces the OpenClaw hourly LLM-based radon check — VERA publishes
        directly to ``telegram.send`` so no Claude API call is needed.
        """
        if not self.bus or self._telegram_alert_pcil <= 0:
            return
        pcil = reading.get("radon_pcil")
        if pcil is None or pcil < self._telegram_alert_pcil:
            return
        now = time.monotonic()
        if now - self._last_telegram_alert < self._telegram_cooldown:
            return
        self._last_telegram_alert = now
        alert_color = reading.get("alert", "Unknown")
        msg = (
            f"⚠️ Radon alert: {pcil:.2f} pCi/L ({alert_color}) — "
            f"above your {self._telegram_alert_pcil:.1f} pCi/L threshold."
        )
        self.bus.publish("telegram.send", {"text": msg})
        log.warning("RadonService: Telegram alert sent — %.2f pCi/L (%s)", pcil, alert_color)


def _compute_alert(radon_pcil: float) -> str:
    """Return EPA-based alert colour for a radon level in pCi/L."""
    if radon_pcil < _ALERT_CONSIDER:
        return "Green"
    if radon_pcil < _ALERT_RECOMMEND:
        return "Orange"
    return "Red"
