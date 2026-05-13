"""Tests for NewsSkill."""
from unittest.mock import MagicMock, patch

from src.skills.news_skill import NewsSkill, _DEFAULT_FEED


def _bus():
    return MagicMock()


def test_matches_news():
    assert NewsSkill().match("what's in the news") is not None


def test_matches_headline():
    assert NewsSkill().match("top headline") is not None


def test_matches_any_news():
    assert NewsSkill().match("any news today") is not None


def test_matches_going_on():
    assert NewsSkill().match("what's going on in the world") is not None


def test_no_match():
    assert NewsSkill().match("turn on the lights") is None


def test_handle_success():
    skill = NewsSkill()
    headlines = ["Headline one", "Headline two", "Headline three"]
    with patch("src.skills.news_skill._fetch_headlines", return_value=headlines):
        m = skill.match("what's in the news")
        result = skill.handle("what's in the news", m, _bus())
    assert "Headline one" in result


def test_handle_single_headline():
    skill = NewsSkill()
    with patch("src.skills.news_skill._fetch_headlines", return_value=["Breaking news"]):
        m = skill.match("top headline")
        result = skill.handle("top headline", m, _bus())
    assert "Breaking news" in result
    assert "Top headline" in result or "top" in result.lower()


def test_handle_network_failure():
    skill = NewsSkill()
    with patch("src.skills.news_skill._fetch_headlines", return_value=[]):
        m = skill.match("what's in the news")
        result = skill.handle("what's in the news", m, _bus())
    assert "couldn't fetch" in result.lower()


def test_config_schema_fields():
    schema = NewsSkill().config_schema
    names = [f.name for f in schema]
    assert "feed_url" in names
    assert "max_headlines" in names


def test_default_feed_url():
    skill = NewsSkill()
    assert skill.get_config()["feed_url"] == _DEFAULT_FEED


def test_set_feed_url():
    skill = NewsSkill()
    skill.set_config("feed_url", "https://example.com/rss.xml")
    assert skill.get_config()["feed_url"] == "https://example.com/rss.xml"


def test_set_max_headlines():
    skill = NewsSkill()
    skill.set_config("max_headlines", 5)
    assert skill.get_config()["max_headlines"] == 5


def test_set_max_headlines_invalid():
    skill = NewsSkill()
    try:
        skill.set_config("max_headlines", 0)
        assert False, "should raise"
    except ValueError:
        pass
