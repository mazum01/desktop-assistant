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
    """Return the PipeWire node ID of the DA Equalizer sink, or None.

    First tries wpctl status (fast).  Falls back to pw-dump if the
    filter-chain node doesn't appear in the wpctl Filters section
    (can happen when WirePlumber hasn't re-indexed the node yet, or
    when the node was registered before the session default changed).
    """
    # Fast path: wpctl status
    try:
        r = subprocess.run(
            ["wpctl", "status"], capture_output=True, text=True, timeout=5
        )
        for line in r.stdout.splitlines():
            if "DA Equalizer" in line or "effect_input.da_eq" in line:
                token = line.split(".")[0].strip().lstrip("*").strip()
                if token.isdigit():
                    return token
    except Exception as exc:
        log.warning("pipewire_eq: wpctl status failed: %s", exc)

    # Fallback: pw-dump
    import json as _json
    try:
        r = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5
        )
        for obj in _json.loads(r.stdout):
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = obj.get("info", {}).get("props", {})
            if props.get("node.name") == "effect_input.da_eq":
                return str(obj["id"])
    except Exception as exc:
        log.debug("pipewire_eq: pw-dump fallback failed: %s", exc)
    return None


def _set_sink_volume(sink_id: str, volume: float) -> None:
    """Set the PipeWire volume of a sink node (0.0–1.5)."""
    try:
        subprocess.run(
            ["wpctl", "set-volume", sink_id, f"{volume:.2f}"],
            capture_output=True, timeout=3,
        )
    except Exception as exc:
        log.debug("pipewire_eq: set-volume failed: %s", exc)


def _pin_hardware_sink_volume(volume: float = 1.0) -> None:
    """Pin the reSpeaker hardware sink (downstream of the EQ) to *volume*.

    The DA EQ output feeds the reSpeaker ALSA sink node, which has its own
    PipeWire volume.  If that drifts below unity (seen at 0.32 after desktop
    panel interaction) it attenuates everything after the EQ.  Find it by
    node name via pw-dump and pin it.
    """
    import json as _json
    try:
        r = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return
        for obj in _json.loads(r.stdout):
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = obj.get("info", {}).get("props", {})
            name = props.get("node.name", "")
            if name.startswith("alsa_output.") and "reSpeaker" in name:
                _set_sink_volume(str(obj["id"]), volume)
                log.info("pipewire_eq: pinned reSpeaker hw sink %s to %.2f", obj["id"], volume)
                return
    except Exception as exc:
        log.debug("pipewire_eq: pin hw sink failed: %s", exc)


def _set_default_sink(sink_id: str) -> bool:
    """Set DA Equalizer as the default PipeWire sink by numeric ID.

    Also persists the choice by node name so it survives PipeWire
    session resets and manual changes in desktop audio panels.
    """
    try:
        r = subprocess.run(
            ["wpctl", "set-default", sink_id],
            timeout=3, capture_output=True,
        )
        ok = r.returncode == 0
    except Exception as exc:
        log.warning("pipewire_eq: wpctl set-default failed: %s", exc)
        return False
    # Persist by name via pw-metadata so the choice survives
    # user changes in desktop sound settings panels.
    try:
        subprocess.run(
            ["pw-metadata", "0", "default.audio.sink",
             '{"name": "effect_input.da_eq"}'],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass  # non-fatal
    return ok


def _get_audio_stream_node_ids() -> list:
    """Return PipeWire node IDs for all active audio output streams.

    Parses pw-dump JSON to find Stream/Output/Audio nodes, excluding
    the DA EQ's own output stream (effect_output.da_eq).
    """
    import json as _json
    try:
        r = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            return []
        ids = []
        for obj in _json.loads(r.stdout):
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = obj.get("info", {}).get("props", {})
            if props.get("media.class") != "Stream/Output/Audio":
                continue
            if props.get("node.name", "") == "effect_output.da_eq":
                continue
            ids.append(str(obj["id"]))
        return ids
    except Exception as exc:
        log.debug("pipewire_eq: pw-dump parse failed: %s", exc)
        return []


def _migrate_streams(sink_id: str) -> None:
    """Move all active audio output streams to the DA Equalizer sink.

    After filter-chain restarts, in-flight streams (e.g. pianobar) land on
    the hardware fallback sink because their old sink disappeared.
    Uses ``pw-metadata <stream_id> target.node <sink_id>`` — the correct
    PipeWire-native way to move a stream without restarting the client.
    """
    stream_ids = _get_audio_stream_node_ids()
    for stream_id in stream_ids:
        try:
            subprocess.run(
                ["pw-metadata", stream_id, "target.node", sink_id],
                capture_output=True, timeout=3,
            )
        except Exception as exc:
            log.debug("pipewire_eq: failed to move stream %s: %s", stream_id, exc)
    if stream_ids:
        log.info("pipewire_eq: migrated %d stream(s) to EQ sink %s", len(stream_ids), sink_id)


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
        _set_sink_volume(sink_id, 1.0)
        _pin_hardware_sink_volume(1.0)
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
        _set_sink_volume(sink_id, 1.0)
        _pin_hardware_sink_volume(1.0)
        log.info("pipewire_eq: applied %s — sink %s set as default", label, sink_id)
        # Move in-flight streams (e.g. pianobar) to the new EQ sink so the
        # preset change is heard immediately without restarting any client.
        _migrate_streams(sink_id)
    return ok
