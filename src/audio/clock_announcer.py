"""
Clock announcer — spoken time at :00 and :30 each hour.

At the half-hour (:30): announces the time.
At the top of the hour (:00): announces the time, then tells a dad joke.

Designed to run in a background daemon thread. The caller supplies a
`say_fn` callback (e.g. `lambda text: bus.publish("av.say", {"text": text})`)
so this module has no direct dependency on AVService.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dad-joke pool
# ---------------------------------------------------------------------------

_DAD_JOKES: list[str] = [
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

_last_joke_index: int = -1


def _pick_joke() -> str:
    """Return a random joke, avoiding immediate repeats."""
    global _last_joke_index
    choices = [i for i in range(len(_DAD_JOKES)) if i != _last_joke_index]
    idx = random.choice(choices)
    _last_joke_index = idx
    return _DAD_JOKES[idx]


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
