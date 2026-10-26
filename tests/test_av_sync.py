"""Tests for src.audio.av_sync — spectrum display / speaker A/V sync."""

from src.audio.av_sync import (
    AvSyncCalibrator,
    AvSyncConfig,
    _parse_pw_top_driver,
    compute_delay_ms,
    estimate_display_lag_ms,
)

REPORT = {
    "samples": 120,
    "audio_to_send_ms": 47.0,
    "ble_write_ms": 3.0,
    "write_to_ack_ms": 88.0,
    "audio_to_display_ms": 138.0,
}


def test_display_lag_counts_outbound_half_of_ack_and_render():
    cfg = AvSyncConfig(render_ms=20.0, ack_outbound_fraction=0.5)
    assert estimate_display_lag_ms(REPORT, cfg) == 47.0 + 3.0 + 44.0 + 20.0


def test_delay_is_display_lag_minus_output_latency_plus_lead():
    cfg = AvSyncConfig(render_ms=20.0, lead_ms=0.0)
    assert compute_delay_ms(REPORT, 58.7, cfg) == 114.0 - 58.7
    cfg.lead_ms = 30.0
    assert compute_delay_ms(REPORT, 58.7, cfg) == 114.0 - 58.7 + 30.0


def test_delay_clamped_to_range():
    assert compute_delay_ms(REPORT, 500.0, AvSyncConfig()) == 0.0
    assert compute_delay_ms(REPORT, 0.0, AvSyncConfig(max_delay_ms=50.0)) == 50.0


def test_incomplete_report_gives_no_delay():
    assert compute_delay_ms({"samples": 5}, 50.0, AvSyncConfig()) is None


def test_parse_pw_top_driver_skips_idle_snapshot():
    text = (
        "R   57      0      0   0.0us   0.0us  ???   ???     0    S16LE 2 16000 sinkA\n"
        "R   57   2048  48000   4.3ms 110.3us  0.10  0.00    0    S16LE 2 16000 sinkA\n"
        "R  151   8192  44100  88.5us 416.0us  0.00  0.01    0    S16LE 2 44100  + other\n"
    )
    assert _parse_pw_top_driver(text, "sinkA") == (2048, 48000)
    assert _parse_pw_top_driver(text, "missing") is None


def _calibrator(**cfg):
    applied = []
    cal = AvSyncCalibrator(
        AvSyncConfig(**cfg),
        sink_name_fn=lambda: "sinkA",
        set_delay_fn=lambda s: applied.append(round(s * 1000.0, 1)) or True,
        output_latency_fn=lambda _sink: 58.0,
    )
    return cal, applied


def test_calibrator_applies_and_reasserts_small_changes():
    cal, applied = _calibrator()
    assert cal.process(REPORT) == 56.0
    # A 4 ms wobble re-asserts the current value rather than jumping the
    # delay line (which would be an audible skip).
    wobble = dict(REPORT, write_to_ack_ms=96.0)
    assert cal.process(wobble) == 56.0
    assert applied == [56.0, 56.0]
    moved = dict(REPORT, write_to_ack_ms=148.0)
    assert cal.process(moved) == 86.0
    assert applied[-1] == 86.0


def test_calibrator_ignores_thin_reports():
    cal, _ = _calibrator(min_samples=30)
    cal.on_latency_report("display.spectrum_latency", dict(REPORT, samples=3))
    assert cal._pending is None
    cal.on_latency_report("display.spectrum_latency", REPORT)
    assert cal._pending == REPORT


def test_calibrator_waits_for_output_latency():
    cal = AvSyncCalibrator(AvSyncConfig(), sink_name_fn=lambda: None,
                           set_delay_fn=lambda _s: True,
                           output_latency_fn=lambda _s: 50.0)
    assert cal.process(REPORT) is None
