import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "tilt_sweep.py"
_SPEC = importlib.util.spec_from_file_location("tilt_sweep_script", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
tilt_sweep = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tilt_sweep)


def test_plan_sweeps_five_times_then_neutral():
    assert tilt_sweep.sweep_plan(80, 100, 5, 90) == [80] + [100, 80] * 5 + [90]


def test_script_is_independent_of_vera_code():
    source = _PATH.read_text()
    assert "src." not in source and "from src" not in source


def test_dry_run_reports_cycles_and_neutral(capsys):
    assert tilt_sweep.main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Cycle 5/5 done" in output
    assert output.strip().endswith("Neutral at 90 degrees")


@pytest.mark.parametrize("argv", [
    ["--low", "60"], ["--high", "120"], ["--low", "100", "--high", "90"],
    ["--cycles", "0"], ["--speed", "0"], ["--channel", "16"],
])
def test_unsafe_arguments_are_rejected(argv):
    with pytest.raises(SystemExit):
        tilt_sweep.parse_args(argv)


def test_reversed_mount_mirrors_shaft_angle():
    class Servo:
        angle = None

    args = tilt_sweep.parse_args(["--dry-run", "--reversed"])
    tilt = tilt_sweep.TiltServo(args, start=90)
    tilt._servo = Servo()
    tilt._write(100)
    assert tilt._servo.angle == 80
