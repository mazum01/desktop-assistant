"""
Playback spectrum analysis for the mouth display's graphic-EQ visualization.

The existing ``audio.spectrum`` topic is computed from the *microphone*, so it
reflects room noise rather than what VERA is actually playing. Music is played
by pianobar and podcasts by their own player -- both stream directly into
PipeWire, so the host process never sees those samples at all.

This service taps the **playback** signal instead, by capturing the EQ sink's
monitor (``pw-record --property stream.capture.sink=true``). That yields
exactly the post-EQ stereo mix heading to the speaker, regardless of which
process produced it, so one code path covers music, podcasts, and anything
else routed through the filter chain.

Captured audio is windowed, FFT'd, and folded into a small number of
logarithmically-spaced bands (music energy is roughly log-distributed, so
linear bins would waste most of the display on the sparse high end). Bands are
published on the bus for :class:`DisplayService` to forward to the ESP32.

Published topics
    display.spectrum  {"bins": [float 0..1], "ts": float}

The capture subprocess only runs while playback is actually active, so an idle
VERA doesn't hold a permanent monitor stream or burn CPU on FFTs of silence.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from src.core.service import Service

log = logging.getLogger(__name__)

_PW_RECORD = shutil.which("pw-record")


@dataclass
class PlaybackSpectrumConfig:
    enabled: bool = True
    # Sink whose monitor ports are captured. This is the *hardware* output,
    # which every playback path (TTS, pianobar, podcasts) reaches after the
    # filter-chain EQ -- so one capture covers everything the speaker plays,
    # already EQ'd exactly as the listener hears it.
    #
    # Empty means "auto-detect the sink the EQ chain feeds".
    sink_name: str = ""
    sample_rate: int = 44100
    channels: int = 2
    # Number of display bands. Kept small: the 1.47" panel can only show a
    # handful of readable bars, and fewer bands means less BLE traffic.
    bands: int = 12
    # Visualization frame rate. The firmware renders at ~15fps, and BLE
    # throughput is the real constraint, so there's no point going faster.
    fps: float = 12.0
    # Band energy range mapped onto the 0..1 bar height.
    floor_db: float = -72.0
    ceiling_db: float = -24.0
    # Music energy falls off with frequency (~pink spectrum). Without this the
    # upper bars would sit near zero for normal program material.
    tilt_db_per_decade: float = 12.0
    # Asymmetric smoothing: bars jump up quickly to catch transients but fall
    # back slowly, which reads as "musical" rather than jittery.
    attack: float = 0.55
    decay: float = 0.18
    min_hz: float = 50.0
    max_hz: float = 14000.0


class PlaybackSpectrumService(Service):
    """Publishes ``display.spectrum`` frames while playback is active."""

    name = "playback_spectrum"

    def __init__(self, bus, config: Optional[PlaybackSpectrumConfig] = None):
        super().__init__(bus=bus)
        self._cfg = config or PlaybackSpectrumConfig()
        self._proc: Optional[subprocess.Popen] = None
        self._cap_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._active = threading.Event()
        self._levels = np.zeros(max(1, int(self._cfg.bands)), dtype=np.float64)
        self._edges: Optional[np.ndarray] = None

    # ── Lifecycle ───────────────────────────────────────────────────────

    def on_start(self) -> None:
        if not self._cfg.enabled:
            log.info("PlaybackSpectrumService disabled by config")
            return
        if _PW_RECORD is None:
            log.warning("PlaybackSpectrumService: pw-record not found — "
                        "playback spectrum disabled")
            return

        self.bus.subscribe("music.state_changed", self._on_music_state)
        self.bus.subscribe("podcast.playback", self._on_podcast_state)

        self._stop.clear()
        self._cap_thread = threading.Thread(
            target=self._run, name="playback-spectrum", daemon=True
        )
        self._cap_thread.start()

    def on_stop(self) -> None:
        self._stop.set()
        self._active.clear()
        self._kill_proc()
        t = self._cap_thread
        if t is not None:
            t.join(timeout=3.0)
        self._cap_thread = None

    # ── Playback state tracking ─────────────────────────────────────────

    def _on_music_state(self, _topic, payload) -> None:
        state = ""
        if isinstance(payload, dict):
            state = str(payload.get("state", "")).strip().lower()
        self._set_active(state == "playing")

    def _on_podcast_state(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        state = str(payload.get("state", "")).strip().lower()
        playing = payload.get("playing")
        if isinstance(playing, bool):
            self._set_active(playing)
        else:
            self._set_active(state == "playing")

    def _set_active(self, active: bool) -> None:
        """Start/stop capture to match playback state."""
        if active:
            if not self._active.is_set():
                log.info("PlaybackSpectrumService: playback started — "
                         "capturing spectrum")
            self._active.set()
        else:
            if self._active.is_set():
                log.info("PlaybackSpectrumService: playback stopped")
            self._active.clear()
            self._kill_proc()

    # ── Capture ─────────────────────────────────────────────────────────

    def _resolve_sink_name(self) -> Optional[str]:
        """Return the sink node whose monitor ports carry the playback mix.

        Prefers the configured name; otherwise picks the ALSA output the EQ
        filter-chain feeds, so the tap follows the real signal path instead of
        assuming a fixed device name.
        """
        if self._cfg.sink_name:
            return self._cfg.sink_name
        try:
            out = subprocess.run(
                ["pw-link", "-o"], capture_output=True, text=True, timeout=4.0
            ).stdout
        except Exception:
            return None
        for line in out.splitlines():
            name = line.strip()
            if name.startswith("alsa_output.") and ":monitor_FL" in name:
                return name.split(":", 1)[0]
        return None

    def _spawn(self) -> Optional[subprocess.Popen]:
        """Start pw-record and link it to the sink's monitor ports.

        ``pw-record --target <sink>`` does *not* reliably attach to a sink's
        monitor on this system: the session manager routes the capture stream
        to the default *source* instead, which silently yields reSpeaker
        microphone audio (room noise) rather than playback. Verified by
        capturing during a known 440 Hz test tone and seeing unrelated
        low-frequency room content.

        So the stream is created unconnected (``--target 0``) and its input
        ports are linked explicitly to the sink's ``monitor_*`` ports, which is
        unambiguous and confirmed to capture the true playback signal.
        """
        sink = self._resolve_sink_name()
        if not sink:
            log.warning("PlaybackSpectrumService: no playback sink monitor found")
            return None

        node = f"veraeqviz{os.getpid()}"
        cmd = [
            _PW_RECORD,
            "--target", "0",
            "-P", f"{{ node.name={node} stream.capture.sink=true }}",
            "--rate", str(int(self._cfg.sample_rate)),
            "--channels", str(int(self._cfg.channels)),
            "--format", "s16",
            "-",
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except Exception:
            log.exception("PlaybackSpectrumService: failed to spawn pw-record")
            return None

        if not self._link_monitor(sink, node):
            log.warning("PlaybackSpectrumService: could not link %s monitor "
                        "ports — spectrum unavailable", sink)
            try:
                proc.terminate()
            except Exception:
                pass
            return None
        return proc

    def _link_monitor(self, sink: str, node: str) -> bool:
        """Link sink monitor ports to the capture node. Retries until ready.

        The pw-record node doesn't exist immediately after spawn, so linking
        is retried briefly rather than assumed to succeed first try.
        """
        deadline = time.time() + 3.0
        pairs = [("monitor_FL", "input_FL"), ("monitor_FR", "input_FR")]
        linked = False
        while time.time() < deadline and not self._stop.is_set():
            ok = 0
            for src, dst in pairs:
                try:
                    r = subprocess.run(
                        ["pw-link", f"{sink}:{src}", f"{node}:{dst}"],
                        capture_output=True, text=True, timeout=3.0,
                    )
                except Exception:
                    continue
                err = (r.stderr or "").lower()
                # "exists" means a previous attempt already linked this pair.
                if r.returncode == 0 or "exists" in err:
                    ok += 1
            if ok == len(pairs):
                linked = True
                break
            time.sleep(0.2)
        return linked

    def _kill_proc(self) -> None:
        p = self._proc
        self._proc = None
        if p is None:
            return
        try:
            p.terminate()
            p.wait(timeout=2.0)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def _run(self) -> None:
        """Capture loop: read blocks, compute bands, publish frames."""
        chans = max(1, int(self._cfg.channels))
        # One FFT window per published frame.
        frames = max(256, int(self._cfg.sample_rate / max(1.0, self._cfg.fps)))
        nbytes = frames * chans * 2

        while not self._stop.is_set():
            if not self._active.wait(timeout=0.25):
                continue
            if self._proc is None:
                self._proc = self._spawn()
                if self._proc is None:
                    time.sleep(1.0)
                    continue
                self._levels[:] = 0.0

            proc = self._proc
            try:
                raw = proc.stdout.read(nbytes) if proc.stdout else b""
            except Exception:
                raw = b""

            if not raw or len(raw) < nbytes:
                # Stream ended (sink restarted / playback stopped).
                self._kill_proc()
                if self._active.is_set():
                    time.sleep(0.3)
                continue

            try:
                bins = self._compute_bands(raw, chans)
            except Exception:
                log.exception("PlaybackSpectrumService: band computation failed")
                continue

            if bins is not None:
                self.bus.publish(
                    "display.spectrum", {"bins": bins, "ts": time.time()}
                )

        self._kill_proc()

    # ── Analysis ────────────────────────────────────────────────────────

    def _band_edges(self, n_freqs: int, nyquist: float) -> np.ndarray:
        """Log-spaced FFT bin edges, cached per FFT size.

        Musical energy is roughly log-distributed, so linear bands would put
        most of the display's bars in the sparse top octaves and cram all the
        audible action into the first one or two.
        """
        if self._edges is not None and len(self._edges) == int(self._cfg.bands) + 1:
            if self._edges[-1] <= n_freqs:
                return self._edges

        nb = max(1, int(self._cfg.bands))
        lo = max(1.0, float(self._cfg.min_hz))
        hi = min(float(self._cfg.max_hz), max(lo * 2.0, nyquist))
        hz = np.logspace(math.log10(lo), math.log10(hi), nb + 1)
        edges = np.clip((hz / nyquist) * n_freqs, 1, n_freqs).astype(int)
        # Guarantee every band owns at least one bin.
        for i in range(1, len(edges)):
            if edges[i] <= edges[i - 1]:
                edges[i] = min(edges[i - 1] + 1, n_freqs)
        self._edges = edges
        return edges

    def _compute_bands(self, raw: bytes, chans: int) -> Optional[list]:
        pcm = np.frombuffer(raw, dtype=np.int16)
        if pcm.size < chans * 64:
            return None
        if chans > 1:
            usable = (pcm.size // chans) * chans
            mono = pcm[:usable].reshape(-1, chans).mean(axis=1)
        else:
            mono = pcm
        x = mono.astype(np.float64) / 32768.0

        window = np.hanning(x.size)
        wsum = float(np.sum(window))
        if wsum <= 0.0:
            return None
        mag = np.abs(np.fft.rfft(x * window)) * (2.0 / wsum)
        if mag.size <= 4:
            return None
        mag = mag[1:]  # drop DC

        nyq = float(self._cfg.sample_rate) / 2.0
        edges = self._band_edges(mag.size, nyq)

        span = max(1e-6, float(self._cfg.ceiling_db) - float(self._cfg.floor_db))
        tilt = float(self._cfg.tilt_db_per_decade)
        bin_hz = nyq / float(mag.size)
        out = []
        for i in range(len(edges) - 1):
            seg = mag[edges[i]:edges[i + 1]]
            level = float(np.sqrt(np.mean(seg * seg))) if seg.size else 0.0
            db = 20.0 * math.log10(level + 1e-12)
            centre_hz = max(1.0, (edges[i] + edges[i + 1]) * 0.5 * bin_hz)
            db += tilt * math.log10(centre_hz / max(1.0, float(self._cfg.min_hz)))
            norm = (db - float(self._cfg.floor_db)) / span
            out.append(max(0.0, min(1.0, norm)))

        target = np.asarray(out, dtype=np.float64)
        if self._levels.size != target.size:
            self._levels = np.zeros(target.size, dtype=np.float64)
        rising = target > self._levels
        alpha = np.where(rising, float(self._cfg.attack), float(self._cfg.decay))
        self._levels += alpha * (target - self._levels)

        return [round(float(v), 3) for v in self._levels]
