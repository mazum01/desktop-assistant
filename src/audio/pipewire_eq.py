"""
PipeWire system-level equalizer for VERA.

Creates a PipeWire filter-chain EQ sink that intercepts ALL audio —
pianobar, TTS, beeps — so the EQ applies universally.

Strategy
--------
1. Write ``~/.config/pipewire/filter-chain.conf.d/da-eq.conf`` with
   the desired biquad filter graph.
2. Restart the ``filter-chain`` user service (already enabled on the Pi).
3. Poll ``wpctl status`` until the "DA Equalizer" sink appears, then
   call ``wpctl set-default`` to route all audio through it.
4. Migrate any existing PulseAudio sink-inputs (e.g. pianobar) to the
   new DA Equalizer sink via ``pactl move-sink-input`` so in-flight
   streams are immediately EQ'd without a daemon restart.
5. Return success flag so callers can fall back to software EQ.

Callers run this on a daemon thread because step 2+3 take ~1–2 s.

Note: When PipeWire EQ is active, ``AudioOutput.set_eq_preset("flat")``
is called to disable the redundant Python biquad path so audio is not
double-processed.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CONF_DIR  = Path.home() / ".config" / "pipewire" / "filter-chain.conf.d"
_CONF_FILE = _CONF_DIR / "da-eq.conf"

# Flag set True after a successful apply_* call.
_active: bool = False


# ── Named preset definitions ─────────────────────────────────────────────────
# Each preset is a list of (hz, gain_db, q, filter_type) tuples.
# filter_type: "lowshelf" | "peaking" | "highshelf"

PRESET_BANDS: dict[str, list] = {
    "flat": [
        (120.0,    0.0, 1.0, "lowshelf"),
        (400.0,    0.0, 1.0, "peaking"),
        (1600.0,   0.0, 1.0, "peaking"),
        (5000.0,   0.0, 1.0, "peaking"),
        (10000.0,  0.0, 1.0, "highshelf"),
    ],
    "bass_boost": [
        (120.0,    6.0, 1.0, "lowshelf"),
        (400.0,    2.0, 1.0, "peaking"),
        (1600.0,   0.0, 1.0, "peaking"),
        (5000.0,   0.0, 1.0, "peaking"),
        (10000.0,  0.0, 1.0, "highshelf"),
    ],
    "treble_boost": [
        (120.0,    0.0, 1.0, "lowshelf"),
        (400.0,    0.0, 1.0, "peaking"),
        (1600.0,   0.0, 1.0, "peaking"),
        (5000.0,   1.0, 1.0, "peaking"),
        (10000.0,  5.0, 1.0, "highshelf"),
    ],
    "vocal": [
        (120.0,    0.0, 1.0, "lowshelf"),
        (400.0,    0.0, 1.0, "peaking"),
        (1600.0,   3.0, 1.5, "peaking"),
        (3500.0,   4.0, 1.5, "peaking"),
        (10000.0,  0.0, 1.0, "highshelf"),
    ],
    "loudness": [
        (80.0,     6.0, 1.0, "lowshelf"),
        (400.0,    0.0, 1.0, "peaking"),
        (1600.0,   0.0, 1.0, "peaking"),
        (5000.0,   2.0, 1.0, "peaking"),
        (12000.0,  4.0, 1.0, "highshelf"),
    ],
    "warm": [
        (300.0,    3.0, 1.0, "lowshelf"),
        (600.0,    2.0, 1.0, "peaking"),
        (1600.0,   0.0, 1.0, "peaking"),
        (4000.0,  -2.0, 1.0, "peaking"),
        (8000.0,  -3.0, 1.0, "highshelf"),
    ],
}

_LABEL_MAP = {
    "lowshelf":  "bq_lowshelf",
    "peaking":   "bq_peaking",
    "highshelf": "bq_highshelf",
}


# ── Config generation ─────────────────────────────────────────────────────────

def _build_config(bands: list) -> str:
    """Return PipeWire filter-chain config text for the given EQ bands.

    bands: list of (hz, gain_db, q, filter_type) tuples
    """
    nodes_lines = []
    for i, (hz, gain_db, q, ftype) in enumerate(bands, 1):
        label = _LABEL_MAP.get(ftype, "bq_peaking")
        nodes_lines.append(
            f"                    {{ type = builtin  name = eq_band_{i}"
            f"  label = {label}"
            f'  control = {{ "Freq" = {hz:.1f}  "Q" = {q:.2f}  "Gain" = {gain_db:.2f} }} }}'
        )

    links_lines = [
        f'                    {{ output = "eq_band_{i}:Out"  input = "eq_band_{i+1}:In" }}'
        for i in range(1, len(bands))
    ]

    nodes_str = "\n".join(nodes_lines)
    links_str = "\n".join(links_lines)

    return f"""# VERA system equalizer — auto-generated, do not edit by hand.
context.modules = [
    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = "DA Equalizer"
            media.name       = "DA Equalizer"
            filter.graph = {{
                nodes = [
{nodes_str}
                ]
                links = [
{links_str}
                ]
            }}
            audio.channels = 2
            audio.position = [ FL FR ]
            capture.props = {{
                node.name   = "effect_input.da_eq"
                media.class = Audio/Sink
            }}
            playback.props = {{
                node.name   = "effect_output.da_eq"
                node.passive = true
            }}
        }}
    }}
]
"""


# ── PipeWire control helpers ──────────────────────────────────────────────────

def _restart_filter_chain() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "filter-chain.service"],
            timeout=8, capture_output=True,
        )
        return r.returncode == 0
    except Exception as exc:
        log.warning("pipewire_eq: filter-chain restart failed: %s", exc)
        return False


def _get_eq_sink_id() -> Optional[str]:
    """Return the wpctl node ID of the DA Equalizer sink, or None."""
    try:
        r = subprocess.run(
            ["wpctl", "status"], capture_output=True, text=True, timeout=5
        )
        for line in r.stdout.splitlines():
            if "DA Equalizer" in line:
                # Format: "  *  <id>. DA Equalizer  [...]"
                token = line.split(".")[0].strip().lstrip("*").strip()
                if token.isdigit():
                    return token
    except Exception as exc:
        log.warning("pipewire_eq: wpctl status failed: %s", exc)
    return None


def _set_default_sink(sink_id: str) -> bool:
    try:
        r = subprocess.run(
            ["wpctl", "set-default", sink_id],
            timeout=3, capture_output=True,
        )
        return r.returncode == 0
    except Exception as exc:
        log.warning("pipewire_eq: wpctl set-default failed: %s", exc)
        return False


# The static PipeWire node.name for the DA Equalizer capture side.
# This is what PulseAudio-compatible clients see as the sink name.
_DA_EQ_SINK_NAME = "effect_input.da_eq"


def _migrate_sink_inputs() -> None:
    """Move all existing PA sink-inputs to the DA Equalizer sink.

    After filter-chain restarts, in-flight PulseAudio streams (e.g. pianobar)
    land on the hardware fallback sink because their old sink disappeared.
    This call migrates them to the new DA Equalizer immediately so the new EQ
    takes effect without restarting any client process.
    """
    try:
        r = subprocess.run(
            ["pactl", "list", "short", "sink-inputs"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return
        for line in r.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            input_id = parts[0]
            subprocess.run(
                ["pactl", "move-sink-input", input_id, _DA_EQ_SINK_NAME],
                capture_output=True, timeout=3,
            )
        log.debug("pipewire_eq: migrated PA sink-inputs to %s", _DA_EQ_SINK_NAME)
    except Exception as exc:
        log.debug("pipewire_eq: sink-input migration failed (non-fatal): %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────

def is_active() -> bool:
    """True after a successful apply call this session."""
    return _active


def is_configured() -> bool:
    """True if the DA EQ config file exists on disk."""
    return _CONF_FILE.exists()


def apply_preset(preset: str) -> bool:
    """Apply a named EQ preset via PipeWire filter-chain.

    Writes the config, restarts filter-chain, and sets the EQ sink as
    the system default.  Runs up to ~3 s waiting for the sink to appear.
    Returns True on success.
    """
    bands = PRESET_BANDS.get(preset)
    if bands is None:
        log.warning("pipewire_eq: unknown preset %r", preset)
        return False
    return _apply_bands(bands, label=f"preset:{preset}")


def apply_custom_bands(bands: list) -> bool:
    """Apply user-defined EQ bands via PipeWire filter-chain.

    bands: list of dicts {hz, gain_db, q}
    Returns True on success.
    """
    if not bands:
        return apply_preset("flat")
    band_tuples = [
        (float(b["hz"]), float(b["gain_db"]), float(b.get("q", 1.0)), "peaking")
        for b in bands
    ]
    return _apply_bands(band_tuples, label=f"custom:{len(band_tuples)}-band")


def ensure_default() -> None:
    """If DA EQ config exists, make the EQ sink the system default.

    Called at service startup — no restart needed because filter-chain
    already loaded the persisted config on boot.
    """
    global _active
    if not _CONF_FILE.exists():
        return
    sink_id = _get_eq_sink_id()
    if sink_id:
        _set_default_sink(sink_id)
        _active = True
        log.info("pipewire_eq: restored DA Equalizer as default sink (id %s)", sink_id)
    else:
        log.info("pipewire_eq: DA EQ config exists but sink not found — skipping")


# ── Internal ──────────────────────────────────────────────────────────────────

def _apply_bands(bands: list, label: str = "") -> bool:
    global _active
    try:
        _CONF_DIR.mkdir(parents=True, exist_ok=True)
        _CONF_FILE.write_text(_build_config(bands))
        log.info("pipewire_eq: wrote config (%d bands, %s)", len(bands), label)
    except Exception as exc:
        log.warning("pipewire_eq: failed to write config: %s", exc)
        return False

    if not _restart_filter_chain():
        log.warning("pipewire_eq: filter-chain restart failed")
        return False

    # Poll for the sink to appear (up to ~3 s in 300 ms steps).
    sink_id: Optional[str] = None
    for _ in range(10):
        time.sleep(0.3)
        sink_id = _get_eq_sink_id()
        if sink_id:
            break

    if not sink_id:
        log.warning("pipewire_eq: DA Equalizer sink did not appear after restart")
        return False

    ok = _set_default_sink(sink_id)
    if ok:
        _active = True
        log.info("pipewire_eq: applied %s — sink %s set as default", label, sink_id)
        # Move in-flight PA streams (e.g. pianobar) to the new EQ sink so
        # the preset change is heard immediately without restarting clients.
        _migrate_sink_inputs()
    return ok
