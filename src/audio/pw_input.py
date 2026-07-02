"""
PipeWire-native microphone capture.

The reSpeaker Flex XVF3800 mic array is held exclusively by PipeWire, so the
only PortAudio route to it is the ``pulse`` device — and ``sd.rec()`` on
``pulse`` can block indefinitely at the C level (ALSA-pulse plugin), which
wedges process shutdown.

This module captures from a PipeWire audio source by running ``pw-record`` as
a subprocess that streams raw S16 mono PCM to stdout.  A reader thread fills a
bounded ring buffer; :meth:`PipeWireMicInput.record` pulls fixed-length chunks
from it.  Shutdown is clean and bounded: terminating the subprocess closes the
pipe, the reader thread unblocks, and we join it — no C-level hang.

Exposes the same ``record(seconds) -> np.ndarray`` / ``hardware_ready`` API as
:class:`src.audio.input.AudioInput`, so it is a drop-in replacement.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

_PW_RECORD = shutil.which("pw-record")


@dataclass
class PipeWireMicConfig:
    sample_rate: int = 16000
    channels: int = 1
    # If set, extract just this channel from multi-channel capture and return
    # a mono array. This lets ReSpeaker use PipeWire capture while preserving
    # its processed/raw channel selection behavior.
    select_channel: Optional[int] = None
    # Substring used to pick the PipeWire source by node.name.  Empty → use the
    # default source (which is the reSpeaker on this system).
    source_match: str = "reSpeaker"
    # Seconds of audio to keep buffered before old samples are dropped.
    buffer_seconds: float = 2.0


def _resolve_source_name(match: str) -> Optional[str]:
    """Return the best Audio/Source node.name matching *match*.

    Prefer physical mic inputs (``alsa_input.*``) over monitor sources.  On
    PipeWire systems a sink monitor is also an ``Audio/Source`` and can match
    ``reSpeaker``; selecting that would feed playback/silence instead of mic
    capture.
    """
    if not match:
        return None
    try:
        r = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        candidates: list[str] = []
        for obj in json.loads(r.stdout):
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = obj.get("info", {}).get("props", {})
            if props.get("media.class") != "Audio/Source":
                continue
            name = props.get("node.name", "")
            if match.lower() in name.lower():
                candidates.append(name)
        if not candidates:
            return None
        candidates.sort(
            key=lambda n: (
                not n.startswith("alsa_input."),
                ".monitor" in n,
                n,
            )
        )
        return candidates[0]
    except Exception as exc:
        log.debug("pw_input: source resolve failed: %s", exc)
    return None


class PipeWireMicInput:
    """Microphone capture via a long-lived ``pw-record`` subprocess."""

    def __init__(self, config: Optional[PipeWireMicConfig] = None) -> None:
        self._cfg = config or PipeWireMicConfig()
        self._sim = _PW_RECORD is None
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._running = False
        self._source_name: Optional[str] = None

        if self._sim:
            log.warning("[sim] pw-record not found — PipeWire mic input in sim mode")
            return
        self._start()

    # ── Lifecycle ───────────────────────────────────────────────────────

    def _start(self) -> None:
        self._source_name = _resolve_source_name(self._cfg.source_match)
        cmd = [
            _PW_RECORD,
            "--rate", str(self._cfg.sample_rate),
            "--channels", str(self._cfg.channels),
            "--format", "s16",
        ]
        if self._source_name:
            cmd += ["--target", self._source_name]
        cmd += ["-"]  # stdout
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.warning("pw_input: failed to start pw-record: %s", exc)
            self._sim = True
            return
        self._running = True
        self._reader = threading.Thread(
            target=self._read_loop, name="pw-mic-reader", daemon=True,
        )
        self._reader.start()
        log.info(
            "PipeWireMicInput started — source=%s rate=%d ch=%d",
            self._source_name or "(default)", self._cfg.sample_rate, self._cfg.channels,
        )

    def _read_loop(self) -> None:
        max_bytes = int(self._cfg.buffer_seconds * self._cfg.sample_rate
                        * self._cfg.channels * 2)  # 2 bytes per s16 sample
        stdout = self._proc.stdout if self._proc else None
        if stdout is None:
            return
        while self._running:
            try:
                data = stdout.read(4096)
            except Exception:
                break
            if not data:
                break  # pipe closed (process exited)
            with self._lock:
                self._buf.extend(data)
                if len(self._buf) > max_bytes:
                    # Keep only the most recent max_bytes.
                    del self._buf[:-max_bytes]

    def close(self) -> None:
        """Stop capture and reap the subprocess.  Bounded and idempotent."""
        self._running = False
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._reader is not None:
            self._reader.join(timeout=2)
            self._reader = None

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def hardware_ready(self) -> bool:
        return (not self._sim) and self._proc is not None and self._proc.poll() is None

    @property
    def device_index(self) -> Optional[int]:
        return None

    @property
    def device_info(self) -> Optional[dict]:
        if self._source_name:
            return {"name": self._source_name, "backend": "pipewire"}
        return None

    # ── Capture ─────────────────────────────────────────────────────────

    def record(self, seconds: float) -> np.ndarray:
        """Return *seconds* of mono float32 audio from the ring buffer.

        Waits up to ``seconds + 1`` for enough data; returns silence (zeros)
        on timeout so callers never block indefinitely.
        """
        n_samples = int(seconds * self._cfg.sample_rate)
        need_bytes = n_samples * self._cfg.channels * 2
        if self._sim or not self.hardware_ready:
            return np.zeros(n_samples, dtype=np.float32)

        deadline = time.monotonic() + seconds + 1.0
        while time.monotonic() < deadline:
            with self._lock:
                if len(self._buf) >= need_bytes:
                    raw = bytes(self._buf[:need_bytes])
                    del self._buf[:need_bytes]
                    break
            time.sleep(0.005)
        else:
            # Timeout: return whatever is buffered, zero-padded.
            with self._lock:
                raw = bytes(self._buf[:need_bytes])
                del self._buf[: len(raw)]
            if len(raw) < need_bytes:
                raw = raw + b"\x00" * (need_bytes - len(raw))

        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if self._cfg.channels > 1:
            samples = samples.reshape(-1, self._cfg.channels)
            if self._cfg.select_channel is not None:
                ch = max(0, min(int(self._cfg.select_channel), self._cfg.channels - 1))
                samples = samples[:, ch].copy()
        return samples
