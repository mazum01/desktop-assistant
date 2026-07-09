import math

from src.audio.xvf_host import XvfHostController, XvfParameterSpec


def test_format_value_replaces_non_finite_scalar_float_with_none():
    ctl = XvfHostController(device=object())
    spec = XvfParameterSpec("PP_MIN_NS", 17, 21, 1, "rw", "float", "Noise floor", "tunables")

    formatted = ctl._format_value(spec, (math.nan,))

    assert formatted["value"] is None
    assert formatted["values"] == [None]
    assert formatted["display"].lower() == "nan"


def test_format_value_replaces_non_finite_array_entries_with_none():
    ctl = XvfHostController(device=object())
    spec = XvfParameterSpec("AEC_SPENERGY_VALUES", 33, 80, 4, "ro", "float", "Speech energy", "signals")

    formatted = ctl._format_value(spec, (1.0, math.inf, -math.inf, math.nan))

    assert formatted["value"] == [1.0, None, None, None]
    assert formatted["values"] == [1.0, None, None, None]
    assert "inf" in formatted["display"].lower()
