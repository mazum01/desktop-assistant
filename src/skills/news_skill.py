"""Skill: speak top news headlines from an RSS feed.

No API key required — uses any public RSS feed URL.
Default: BBC News.

Voice examples
--------------
  "what's in the news"
  "any news today"
  "top headline"
  "give me the headlines"
"""

from __future__ import annotations

import logging
import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from typing import Any, Optional

from .base import ConfigField, Skill

log = logging.getLogger(__name__)

_DEFAULT_FEED = "https://feeds.bbci.co.uk/news/rss.xml"
_TIMEOUT_S = 5
_MAX_HEADLINES = 3


def _fetch_headlines(feed_url: str, max_n: int) -> list[str]:
    try:
        req = urllib.request.Request(
            feed_url,
            headers={"User-Agent": "DesktopAssistant/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        titles: list[str] = []
        for item in root.iter("item"):
            title = item.findtext("title")
            if title:
                titles.append(title.strip())
            if len(titles) >= max_n:
                break
        return titles
    except Exception as exc:
        log.warning("NewsSkill: fetch failed: %s", exc)
        return []


class NewsSkill(Skill):
    """Speak top headlines from a configurable RSS feed."""

    name = "news"

    def __init__(self) -> None:
        super().__init__()
        self._feed_url: str = _DEFAULT_FEED
        self._max_headlines: int = _MAX_HEADLINES

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"\bwhat.s (?:in (?:the )?)?(?:news|happening)\b"),
            re.compile(r"\bwhat is (?:in (?:the )?)?(?:news|happening)\b"),
            re.compile(r"\b(any news( today| right now)?|news today)\b"),
            re.compile(r"\b(top headline|give me (the )?headlines|latest news)\b"),
            re.compile(r"\bwhat.s going on (in the world|today)\b"),
            re.compile(r"\bwhat is going on (in the world|today)\b"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        headlines = _fetch_headlines(self._feed_url, self._max_headlines)
        if not headlines:
            return "Sorry, I couldn't fetch the news right now."
        if len(headlines) == 1:
            return f"Top headline: {headlines[0]}."
        joined = ". ".join(headlines)
        return f"Here are the top {len(headlines)} headlines: {joined}."

    # ------------------------------------------------------------------
    # Config interface
    # ------------------------------------------------------------------

    @property
    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="feed_url",
                label="RSS feed URL",
                type="str",
                default=_DEFAULT_FEED,
                description="Any public RSS feed. Default: BBC News.",
            ),
            ConfigField(
                name="max_headlines",
                label="Headlines to read",
                type="int",
                default=_MAX_HEADLINES,
                min=1,
                max=10,
                description="How many headlines to speak.",
            ),
        ]

    def get_config(self) -> dict:
        return {"feed_url": self._feed_url, "max_headlines": self._max_headlines}

    def set_config(self, key: str, value: Any) -> None:
        if key == "feed_url":
            self._feed_url = str(value).strip() or _DEFAULT_FEED
        elif key == "max_headlines":
            v = int(value)
            if not 1 <= v <= 10:
                raise ValueError("max_headlines must be 1–10")
            self._max_headlines = v
        else:
            raise ValueError(f"NewsSkill has no field {key!r}")
