"""Tests for WeatherSkill."""
from unittest.mock import MagicMock, patch

from src.skills.weather_skill import WeatherSkill


def _bus():
    return MagicMock()


_SAMPLE_RESPONSE = {
    "current_condition": [{
        "temp_C": "20",
        "FeelsLikeC": "18",
        "weatherDesc": [{"value": "Partly cloudy"}],
        "humidity": "65",
    }],
    "nearest_area": [{"areaName": [{"value": "Springfield"}]}],
}


def test_matches_weather():
    assert WeatherSkill().match("what's the weather") is not None


def test_matches_forecast():
    assert WeatherSkill().match("what is the forecast") is not None


def test_matches_will_it_rain():
    assert WeatherSkill().match("will it rain today") is not None


def test_matches_how_hot():
    assert WeatherSkill().match("how hot is it outside") is not None


def test_no_match():
    assert WeatherSkill().match("play some music") is None


def test_handle_success():
    skill = WeatherSkill()
    with patch("src.skills.weather_skill._fetch_weather", return_value=_SAMPLE_RESPONSE):
        m = skill.match("what's the weather")
        result = skill.handle("what's the weather", m, _bus())
    assert isinstance(result, str)
    assert "Partly cloudy" in result


def test_handle_network_failure():
    skill = WeatherSkill()
    with patch("src.skills.weather_skill._fetch_weather", return_value=None):
        m = skill.match("what's the weather")
        result = skill.handle("what's the weather", m, _bus())
    assert "couldn't reach" in result.lower()


def test_config_schema_fields():
    schema = WeatherSkill().config_schema
    names = [f.name for f in schema]
    assert "location" in names
    assert "units" in names


def test_set_config_location():
    skill = WeatherSkill()
    skill.set_config("location", "New York")
    assert skill.get_config()["location"] == "New York"


def test_set_config_units_metric():
    skill = WeatherSkill()
    skill.set_config("units", "metric")
    assert skill.get_config()["units"] == "metric"


def test_set_config_units_invalid():
    skill = WeatherSkill()
    try:
        skill.set_config("units", "kelvin")
        assert False, "should raise"
    except ValueError:
        pass


def test_imperial_temp_format():
    skill = WeatherSkill()
    skill.set_config("units", "imperial")
    with patch("src.skills.weather_skill._fetch_weather", return_value=_SAMPLE_RESPONSE):
        m = skill.match("what's the weather")
        result = skill.handle("what's the weather", m, _bus())
    assert "°F" in result


def test_metric_temp_format():
    skill = WeatherSkill()
    skill.set_config("units", "metric")
    with patch("src.skills.weather_skill._fetch_weather", return_value=_SAMPLE_RESPONSE):
        m = skill.match("what's the weather")
        result = skill.handle("what's the weather", m, _bus())
    assert "°C" in result
