"""
Clock announcer — spoken time at :00 and :30 each hour.

At the half-hour (:30): announces the time.
At the top of the hour (:00): announces the time, then tells a dad joke.

Designed to run in a background daemon thread. The caller supplies a
`say_fn` callback (e.g. `lambda text: bus.publish("av.say", {"text": text})`)
so this module has no direct dependency on AVService.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dad-joke pool — seeded locally, refreshed from icanhazdadjoke.com daily
# ---------------------------------------------------------------------------

_JOKE_CACHE_FILE = Path.home() / ".config" / "desktop-assistant" / "dad_jokes.json"
_JOKE_REFRESH_INTERVAL = 24 * 3600  # seconds between API refreshes
_JOKE_FETCH_COUNT = 50              # jokes to fetch per refresh
_JOKE_API_URL = "https://icanhazdadjoke.com/search?limit=30&page={page}"
_JOKE_HEADERS = {"Accept": "application/json", "User-Agent": "Desktop-Assistant/1.0"}

# Fallback hardcoded pool used when the cache is cold and the network is down.
_FALLBACK_JOKES: list[str] = [
    "Why don't scientists trust atoms? Because they make up everything.",
    "I told my wife she was drawing her eyebrows too high. She looked surprised.",
    "What do you call cheese that isn't yours? Nacho cheese.",
    "Why can't you give Elsa a balloon? Because she'll let it go.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Did you hear about the mathematician who's afraid of negative numbers? He'll stop at nothing to avoid them.",
    "Why did the scarecrow win an award? Because he was outstanding in his field.",
    "I would tell you a construction joke, but I'm still working on it.",
    "What do you call a fake noodle? An impasta.",
    "Why do cows wear bells? Because their horns don't work.",
    "I used to hate facial hair, but then it grew on me.",
    "What did the ocean say to the beach? Nothing, it just waved.",
    "Why don't eggs tell jokes? They'd crack each other up.",
    "I only know 25 letters of the alphabet. I don't know y.",
    "What do you call a sleeping dinosaur? A dino-snore.",
    "Why did the bicycle fall over? Because it was two-tired.",
    "I asked my dog what two minus two is. He said nothing.",
    "What do you call a factory that makes okay products? A satisfactory.",
    "Did I tell you the joke about the paper? Never mind — it's tearable.",
    "Why did the golfer bring an extra pair of pants? In case he got a hole in one.",
    "What do you call a bear with no teeth? A gummy bear.",
    "I'm on a seafood diet. I see food and I eat it.",
    "Why can't a nose be twelve inches long? Because then it would be a foot.",
    "What did one wall say to the other? I'll meet you at the corner.",
    "Why did the tomato turn red? Because it saw the salad dressing.",
]

# Active joke pool — replaced on successful refresh. Protected by _joke_lock.
_joke_lock: threading.Lock = threading.Lock()
_DAD_JOKES: list[str] = list(_FALLBACK_JOKES)
_last_joke_index: int = -1


def _load_joke_cache() -> list[str]:
    """Load jokes from disk cache. Returns empty list if cache is missing or invalid."""
    try:
        if _JOKE_CACHE_FILE.exists():
            data = json.loads(_JOKE_CACHE_FILE.read_text())
            jokes = data.get("jokes", [])
            if isinstance(jokes, list) and len(jokes) >= 10:
                return jokes
    except Exception as exc:
        log.debug("joke cache load failed: %s", exc)
    return []


def _save_joke_cache(jokes: list[str]) -> None:
    try:
        _JOKE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _JOKE_CACHE_FILE.write_text(
            json.dumps({"jokes": jokes, "fetched_at": time.time()}, indent=2)
        )
    except Exception as exc:
        log.warning("joke cache save failed: %s", exc)


def _cache_age_seconds() -> float:
    """Return seconds since last successful fetch, or infinity if no cache."""
    try:
        if _JOKE_CACHE_FILE.exists():
            data = json.loads(_JOKE_CACHE_FILE.read_text())
            return time.time() - float(data.get("fetched_at", 0))
    except Exception:
        pass
    return float("inf")


def _fetch_jokes_from_api() -> list[str]:
    """Fetch up to _JOKE_FETCH_COUNT jokes from icanhazdadjoke.com.

    Returns a non-empty list on success or raises on network/parse failure.
    """
    jokes: list[str] = []
    pages = max(1, _JOKE_FETCH_COUNT // 30)
    for page in range(1, pages + 1):
        url = _JOKE_API_URL.format(page=page)
        req = Request(url, headers=_JOKE_HEADERS)
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        for item in body.get("results", []):
            text = item.get("joke", "").strip()
            if text:
                jokes.append(text)
        if not body.get("results"):
            break
    return jokes


def _refresh_joke_pool(force: bool = False) -> None:
    """Background job: refresh jokes from API if the cache is stale."""
    global _DAD_JOKES
    if not force and _cache_age_seconds() < _JOKE_REFRESH_INTERVAL:
        return
    try:
        jokes = _fetch_jokes_from_api()
        if jokes:
            random.shuffle(jokes)
            _save_joke_cache(jokes)
            with _joke_lock:
                _DAD_JOKES = jokes
            log.info("Dad-joke pool refreshed: %d jokes from API", len(jokes))
    except (URLError, OSError) as exc:
        log.info("Dad-joke refresh skipped (network): %s", exc)
    except Exception as exc:
        log.warning("Dad-joke refresh failed: %s", exc)


def _init_joke_pool() -> None:
    """Load cache on startup; trigger background refresh if stale."""
    global _DAD_JOKES
    cached = _load_joke_cache()
    if cached:
        with _joke_lock:
            _DAD_JOKES = cached
        log.info("Dad-joke pool loaded from cache: %d jokes", len(cached))
    # Refresh in background — doesn't block startup
    threading.Thread(
        target=_refresh_joke_pool,
        name="joke-refresh-init",
        daemon=True,
    ).start()


# Seed the pool immediately at import time.
_init_joke_pool()


def _pick_joke() -> str:
    """Return a random joke, avoiding immediate repeats."""
    global _last_joke_index
    with _joke_lock:
        pool = _DAD_JOKES
    choices = [i for i in range(len(pool)) if i != _last_joke_index]
    idx = random.choice(choices)
    _last_joke_index = idx
    return pool[idx]


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------

def _spoken_time(dt: datetime, prefix: str = "It is") -> str:
    """Return a natural spoken time string, e.g. 'It is three thirty PM'."""
    hour_12 = dt.hour % 12 or 12
    minute = dt.minute
    ampm = "AM" if dt.hour < 12 else "PM"

    _ONES = [
        "", "one", "two", "three", "four", "five", "six", "seven", "eight",
        "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
        "sixteen", "seventeen", "eighteen", "nineteen",
    ]
    _TENS = ["", "", "twenty", "thirty", "forty", "fifty"]

    def _num(n: int) -> str:
        if n == 0:
            return "o'clock"
        if 1 <= n <= 9:
            return "oh " + _ONES[n]   # "oh seven" not "seven"
        if n < 20:
            return _ONES[n]
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (" " + _ONES[ones] if ones else "")

    hour_word = _ONES[hour_12]
    min_word = _num(minute)

    if minute == 0:
        return f"{prefix} {hour_word} o'clock {ampm}."
    if minute == 30:
        return f"{prefix} {hour_word} thirty {ampm}."
    return f"{prefix} {hour_word} {min_word} {ampm}."


# ---------------------------------------------------------------------------
# ClockAnnouncer
# ---------------------------------------------------------------------------

class ClockAnnouncer:
    """
    Fires spoken time announcements at :00 and :30 of every hour.

    Args:
        say_fn:   Callable that accepts a text string and speaks it.
        enabled:  Set False to start in silent mode; toggle at runtime via
                  `announcer.enabled = True/False`.
    """

    def __init__(
        self,
        say_fn: Callable[[str], None],
        enabled: bool = True,
        is_quiet_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._say = say_fn
        self.enabled = enabled
        self._is_quiet = is_quiet_fn or (lambda: False)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the background watcher thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="clock-announcer",
            daemon=True,
        )
        self._thread.start()
        # Daily joke-pool refresh thread (completely independent of the clock loop).
        threading.Thread(
            target=self._run_joke_refresh,
            name="joke-refresh",
            daemon=True,
        ).start()
        log.info("ClockAnnouncer started (enabled=%s)", self.enabled)

    def stop(self) -> None:
        """Stop the background thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        log.info("ClockAnnouncer stopped")

    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Sleep until the next :00 or :30, fire announcement, repeat."""
        while not self._stop_event.is_set():
            now = datetime.now()
            next_trigger = _next_half_hour(now)
            wait_s = (next_trigger - now).total_seconds()
            # Break the sleep into 1s chunks so stop() is responsive.
            slept = 0.0
            while slept < wait_s and not self._stop_event.is_set():
                chunk = min(1.0, wait_s - slept)
                time.sleep(chunk)
                slept += chunk

            if self._stop_event.is_set():
                break

            fire_time = datetime.now()
            self._announce(fire_time)

    def _run_joke_refresh(self) -> None:
        """Periodically refresh the joke pool from the API while running."""
        while not self._stop_event.is_set():
            # Check every hour; _refresh_joke_pool gates on _JOKE_REFRESH_INTERVAL.
            slept = 0.0
            while slept < 3600 and not self._stop_event.is_set():
                time.sleep(min(60.0, 3600 - slept))
                slept += 60.0
            if not self._stop_event.is_set():
                _refresh_joke_pool()

    def _announce(self, dt: datetime) -> None:
        if not self.enabled:
            log.debug("ClockAnnouncer: skipping (disabled)")
            return
        if self._is_quiet():
            log.debug("ClockAnnouncer: skipping — quiet hours active")
            return

        time_str = _spoken_time(dt)
        log.info("ClockAnnouncer: %s", time_str)

        if dt.minute == 0:
            joke = _pick_joke()
            text = f"{time_str} {joke}"
        else:
            text = time_str

        try:
            self._say(text)
        except Exception:
            log.exception("ClockAnnouncer: say_fn raised")

    def announce_time_now(self) -> None:
        """Speak the current time immediately (no joke), using 'The time is …'."""
        if self._is_quiet():
            log.debug("ClockAnnouncer: on-demand time suppressed — quiet hours active")
            return
        text = _spoken_time(datetime.now(), prefix="The time is")
        log.info("ClockAnnouncer: on-demand time → %s", text)
        try:
            self._say(text)
        except Exception:
            log.exception("ClockAnnouncer: say_fn raised")

    def tell_joke_now(self) -> None:
        """Speak a random dad joke immediately."""
        if self._is_quiet():
            log.debug("ClockAnnouncer: on-demand joke suppressed — quiet hours active")
            return
        joke = _pick_joke()
        log.info("ClockAnnouncer: on-demand joke → %s", joke)
        try:
            self._say(joke)
        except Exception:
            log.exception("ClockAnnouncer: say_fn raised")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _next_half_hour(dt: datetime) -> datetime:
    """Return the next :00 or :30 tick after *dt*."""
    from datetime import timedelta
    if dt.minute < 30:
        target = dt.replace(minute=30, second=0, microsecond=0)
    else:
        target = (dt.replace(minute=0, second=0, microsecond=0)
                  + timedelta(hours=1))
    # Guard against being exactly on the boundary (within 1s).
    if (target - dt).total_seconds() < 1:
        target = target + timedelta(minutes=30)
    return target
