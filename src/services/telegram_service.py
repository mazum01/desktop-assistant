"""TelegramService — forward face-activity audio outputs to Telegram.

Subscribes to ``face.greeted`` bus events published by FaceService and sends
the spoken greeting text as a Telegram message to the configured chat.

Also exposes a generic ``telegram.send`` topic so other services can forward
arbitrary messages in the future.

Topics subscribed
-----------------
face.greeted    ``{face_id, name, text, event_type}``
telegram.send   ``{text: str, [chat_id: str]}``

Topics published
----------------
(none)

Config (config/assistant.yaml — telegram section)
--------------------------------------------------
telegram:
  enabled: true
  bot_token: "<telegram bot token>"
  chat_id: "<numeric chat id>"
  # Emoji prefix per event type (optional)
  emoji_new_face: "👋"
  emoji_returning: "👤"
  emoji_named: "🏷️"
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Optional

from src.core.service import Service

log = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

_DEFAULT_EMOJI: dict[str, str] = {
    "new_face": "👋",
    "returning": "👤",
    "named": "🏷️",
}


def _send_message(token: str, chat_id: str, text: str) -> None:
    """Send *text* to *chat_id* via the Telegram Bot API (blocking)."""
    url = _TELEGRAM_API.format(token=token)
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")


class TelegramService(Service):
    """Send face-greeting text to Telegram."""

    name = "telegram"

    def __init__(
        self,
        bus=None,
        enabled: bool = True,
        bot_token: str = "",
        chat_id: str = "",
        emoji_map: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._enabled = enabled
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._emoji = {**_DEFAULT_EMOJI, **(emoji_map or {})}
        self._unsubs: list = []

    # ── Service lifecycle ─────────────────────────────────────────────

    def on_start(self) -> None:
        if not self._enabled:
            log.info("TelegramService: disabled — not subscribing")
            return
        if not self._bot_token or not self._chat_id:
            log.warning("TelegramService: missing bot_token or chat_id — not subscribing")
            return
        self._unsubs.append(self.bus.subscribe("face.greeted", self._on_face_greeted))
        self._unsubs.append(self.bus.subscribe("telegram.send", self._on_send))
        log.info("TelegramService started (chat_id=%s)", self._chat_id)

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        log.info("TelegramService stopped")

    # ── Bus handlers ─────────────────────────────────────────────────

    def _on_face_greeted(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        text = (payload.get("text") or "").strip()
        event_type = payload.get("event_type", "returning")
        name = payload.get("name", "someone")
        if not text:
            return
        emoji = self._emoji.get(event_type, "👤")
        message = f"{emoji} {text}"
        log.info("TelegramService: face.greeted → %r (%s: %s)", text[:60], event_type, name)
        self._dispatch(message)

    def _on_send(self, _topic, payload) -> None:
        """Generic telegram.send{text, [chat_id]} handler."""
        if not isinstance(payload, dict):
            return
        text = (payload.get("text") or "").strip()
        if not text:
            return
        chat_id = payload.get("chat_id") or self._chat_id
        self._dispatch(text, chat_id=chat_id)

    # ── Helpers ───────────────────────────────────────────────────────

    def _dispatch(self, text: str, chat_id: Optional[str] = None) -> None:
        """Send *text* on a background thread so the bus never blocks."""
        target_chat = chat_id or self._chat_id
        t = threading.Thread(
            target=self._send_safe,
            args=(text, target_chat),
            daemon=True,
        )
        t.start()

    def _send_safe(self, text: str, chat_id: str) -> None:
        try:
            _send_message(self._bot_token, chat_id, text)
            log.debug("TelegramService: sent %r to %s", text[:60], chat_id)
        except urllib.error.URLError as exc:
            log.warning("TelegramService: network error — %s", exc)
        except Exception:
            log.exception("TelegramService: failed to send message")
