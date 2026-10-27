"""Tests for the display unit-system preference."""
import json

import pytest

from src.core import display_units


def test_systems_cover_every_quantity_with_labels():
    quantities = set(display_units.SYSTEMS["us"]["units"])
    for key, spec in display_units.SYSTEMS.items():
        assert spec["label"]
        assert set(spec["units"]) == quantities, key
    assert display_units.SYSTEMS["us"]["units"]["temperature"] == "°F"
    assert display_units.SYSTEMS["si"]["units"]["temperature"] == "°C"
    assert display_units.SYSTEMS["scientific"]["units"]["temperature"] == "K"


def test_normalize_falls_back_for_unknown_ids():
    assert display_units.normalize("SI") == "si"
    assert display_units.normalize("bogus") == display_units.FALLBACK_SYSTEM
    assert display_units.normalize("bogus", default="imperial") == "imperial"
    assert display_units.normalize(None, default="also-bogus") == display_units.FALLBACK_SYSTEM


def test_load_returns_default_when_unset_or_corrupt(tmp_path):
    path = tmp_path / "units.json"
    assert display_units.load("si", path=path) == "si"
    path.write_text("{not json")
    assert display_units.load("si", path=path) == "si"
    path.write_text(json.dumps({"system": "martian"}))
    assert display_units.load("imperial", path=path) == "imperial"


def test_save_round_trips_and_rejects_unknown(tmp_path):
    path = tmp_path / "nested" / "units.json"
    assert display_units.save("scientific", path=path) == "scientific"
    assert display_units.load("us", path=path) == "scientific"
    with pytest.raises(ValueError):
        display_units.save("martian", path=path)
    assert display_units.load("us", path=path) == "scientific"


def test_describe_lists_systems_in_display_order():
    payload = display_units.describe("si")
    assert payload["system"] == "si"
    assert [s["id"] for s in payload["systems"]] == list(display_units.SYSTEMS)
    assert payload["systems"][1]["units"]["flow"] == "L/min"
