"""Tests for FanController (sysfs + lgpio fallback) and FanTach."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.thermal.fan import FanController
from src.thermal.fan_tach import FanTach


# ──────────────────────────────────────────────────────────────────────
# Fan — sysfs backend (mocked via tmp_path)
# ──────────────────────────────────────────────────────────────────────

def _make_fake_pwmchip(root: Path, chip: int = 0, channel: int = 1) -> Path:
    chip_dir = root / f"pwmchip{chip}"
    chip_dir.mkdir()
    (chip_dir / "export").write_text("")
    pwm_dir = chip_dir / f"pwm{channel}"
    pwm_dir.mkdir()
    (pwm_dir / "period").write_text("0\n")
    (pwm_dir / "duty_cycle").write_text("0\n")
    (pwm_dir / "enable").write_text("0\n")
    return chip_dir


def test_fan_uses_sysfs_when_chip_present(monkeypatch, tmp_path):
    fake_class = tmp_path / "pwm"
    fake_class.mkdir()
    chip = _make_fake_pwmchip(fake_class, 0, 1)

    # Redirect Path("/sys/class/pwm/pwmchipN") in fan.py.
    import src.thermal.fan as fan_mod

    real_path = fan_mod.Path

    class _RedirectPath(type(real_path())):
        pass

    def _path_factory(p):
        s = str(p)
        if s.startswith("/sys/class/pwm/"):
            return real_path(str(fake_class) + s[len("/sys/class/pwm"):])
        return real_path(s)

    monkeypatch.setattr(fan_mod, "Path", _path_factory)

    fan = FanController(pwm_chip=0, pwm_channel=1, pwm_period_ns=40_000)
    assert fan.backend == "sysfs"

    fan.set_duty(50.0)
    assert (chip / "pwm1" / "duty_cycle").read_text().strip() == "20000"
    assert (chip / "pwm1" / "period").read_text().strip() == "40000"
    assert (chip / "pwm1" / "enable").read_text().strip() == "1"

    fan.set_duty(0.0)
    assert (chip / "pwm1" / "duty_cycle").read_text().strip() == "0"

    fan.set_duty(150.0)   # clamped
    assert fan.duty == 100.0
    assert (chip / "pwm1" / "duty_cycle").read_text().strip() == "40000"


def test_fan_falls_back_to_lgpio_when_sysfs_missing(monkeypatch, tmp_path):
    empty = tmp_path / "pwm-empty"
    empty.mkdir()

    import src.thermal.fan as fan_mod

    real_path = fan_mod.Path

    def _path_factory(p):
        s = str(p)
        if s.startswith("/sys/class/pwm/"):
            return real_path(str(empty) + s[len("/sys/class/pwm"):])
        return real_path(s)

    monkeypatch.setattr(fan_mod, "Path", _path_factory)

    fan = FanController(pwm_chip=0, pwm_channel=1)
    # Either lgpio kicks in (on Pi) or we drop to sim (in CI).
    assert fan.backend in ("lgpio", "sim")
    fan.set_duty(42.0)
    assert fan.duty == 42.0


# ──────────────────────────────────────────────────────────────────────
# Fan tach — pulse counting
# ──────────────────────────────────────────────────────────────────────

def test_tach_rpm_none_with_no_pulses():
    tach = FanTach(gpio=6, pulses_per_rev=2)
    assert tach.rpm is None
    assert tach.pulses_per_sec == 0.0
    tach.close()


def test_tach_rpm_from_injected_pulses():
    tach = FanTach(gpio=6, pulses_per_rev=2)
    now = time.monotonic()
    # 40 pulses in the last second @ 2 ppr → 1200 RPM
    for i in range(40):
        tach.inject_pulse(now - i * 0.02)
    assert tach.rpm == pytest.approx(1200, abs=10)
    tach.close()


def test_tach_window_drops_old_pulses():
    tach = FanTach(gpio=6, pulses_per_rev=2)
    old = time.monotonic() - 5.0
    for _ in range(10):
        tach.inject_pulse(old)
    assert tach.rpm is None
    tach.close()
