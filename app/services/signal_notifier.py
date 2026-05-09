"""Signal messenger notification service via CallMeBot.

Sends trading alerts to a Signal phone number using the CallMeBot free
API. No extra infrastructure required -- notifications are delivered via
a simple HTTPS GET request.

Setup (one-time, on your phone):
    1. Add +34 644 52 74 88 to your Signal contacts.
    2. Send: "I allow callmebot to send me messages"
    3. CallMeBot replies instantly with your personal API key.
    4. Set SIGNAL_PHONE and SIGNAL_API_KEY in your .env.

API reference:
    GET https://signal.callmebot.com/signal/send.php
        ?phone=PHONE&apikey=APIKEY&text=MESSAGE

Messages are plain text (Signal does not render HTML). HTML snippets
from the Telegram formatter are stripped before sending.

Exports:
    SignalNotifier  -- main service class
"""

from __future__ import annotations

import asyncio
import re
from html import unescape
from urllib.parse import quote

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

CALLMEBOT_URL = "https://signal.callmebot.com/signal/send.php"


class SignalNotifier:
    """Sends trading alerts to Signal via the CallMeBot API.

    Mirrors the interface of TelegramNotifier so it can be used as a
    drop-in second notification channel alongside Telegram. All public
    ``notify_*`` methods are fire-and-forget: they never raise exceptions.

    Args:
        phone:   Recipient phone number in E.164 format (e.g. +14155552671).
        api_key: CallMeBot API key obtained during the setup handshake.
    """

    def __init__(self, phone: str, api_key: str) -> None:
        self.phone = phone.strip()
        self.api_key = api_key.strip()
        self._rate_lock = asyncio.Lock()
        self._last_send: float = 0.0

    @property
    def enabled(self) -> bool:
        """Return True when both phone and API key are configured."""
        return bool(self.phone and self.api_key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(text: str) -> str:
        """Convert Telegram HTML snippets to plain text for Signal."""
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<b>(.*?)</b>", r"\1", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<i>(.*?)</i>", r"\1", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _truncate(text: str, max_chars: int = 1000) -> str:
        """Trim message to Signal's practical length limit."""
        text = text.strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "…"

    async def _rate_limit(self) -> None:
        """Stay below CallMeBot's rate limit (1 message/second)."""
        async with self._rate_lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_send
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)
            self._last_send = asyncio.get_event_loop().time()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException)
        ),
    )
    async def _send_message(self, text: str) -> None:
        """Deliver one message to Signal via CallMeBot with retry."""
        await self._rate_limit()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                CALLMEBOT_URL,
                params={
                    "phone": self.phone,
                    "apikey": self.api_key,
                    "text": text,
                },
            )
            response.raise_for_status()

    # ------------------------------------------------------------------
    # Message formatters
    # ------------------------------------------------------------------

    def format_signal(self, signal, strategy_name: str = "Unknown") -> str:
        """Format a trade signal as plain text for Signal."""
        arrow = "▲" if signal.direction == "BUY" else "▼"
        reasoning = (signal.reasoning or "").split("|")[0].strip()
        lines = [
            f"{arrow} {signal.symbol} {signal.direction}",
            "",
            f"Entry:    {signal.entry_price}",
            f"Stop:     {signal.stop_loss}",
            f"TP1:      {signal.take_profit_1}",
            f"TP2:      {signal.take_profit_2}",
            f"R:R:      {signal.risk_reward}",
            f"Conf:     {signal.confidence}%",
            f"Strategy: {strategy_name}",
        ]
        if reasoning:
            lines += ["", reasoning]
        return self._truncate("\n".join(lines))

    def format_outcome(self, signal, outcome) -> str:
        """Format a signal outcome as plain text for Signal."""
        emoji_map = {
            "tp1_hit": "✅",
            "tp2_hit": "✅✅",
            "sl_hit": "❌",
            "expired": "⏰",
        }
        emoji = emoji_map.get(outcome.result, "")
        return self._truncate(
            f"{emoji} {signal.symbol} {signal.direction} — {outcome.result.upper()}\n"
            f"\n"
            f"Entry:    {signal.entry_price}\n"
            f"Exit:     {outcome.exit_price}\n"
            f"P&L:      {outcome.pnl_pips} pips\n"
            f"Duration: {outcome.duration_minutes} min"
        )

    def format_degradation(
        self, strategy_name: str, reason: str, is_recovery: bool = False
    ) -> str:
        """Format a strategy degradation or recovery alert."""
        if is_recovery:
            return self._truncate(
                f"🔄 Strategy Recovered: {strategy_name}\n\n{reason}"
            )
        return self._truncate(
            f"⚠️ Strategy Degraded: {strategy_name}\n\n"
            f"Reason: {reason}\n\n"
            f"Auto-deprioritized. Will recover if metrics improve."
        )

    def format_circuit_breaker(self, reason: str, active: bool) -> str:
        """Format a circuit breaker activation or reset alert."""
        if active:
            return self._truncate(
                f"🛑 CIRCUIT BREAKER ACTIVATED\n\n"
                f"Reason: {reason}\n\n"
                f"Signal generation halted. Auto-resets after 24 hours."
            )
        return self._truncate(
            f"✅ Circuit Breaker Reset\n\nSignal generation resumed. {reason}"
        )

    def format_system_alert(self, title: str, details: str) -> str:
        """Format a system/infrastructure alert."""
        return self._truncate(
            f"⚠️ SYSTEM ALERT: {title}\n\n{self._strip_html(details)}"
        )

    # ------------------------------------------------------------------
    # Fire-and-forget wrappers
    # ------------------------------------------------------------------

    async def notify_signal(self, signal, strategy_name: str = "Unknown") -> None:
        """Send a trade signal alert via Signal. Never raises."""
        if not self.enabled:
            logger.debug("Signal notifier disabled, skipping signal notification")
            return
        try:
            await self._send_message(self.format_signal(signal, strategy_name))
            logger.info(
                "Signal notification sent for signal_id={}", signal.id
            )
        except Exception:
            logger.exception(
                "Signal notification failed for signal_id={}", signal.id
            )

    async def notify_outcome(self, signal, outcome) -> None:
        """Send an outcome alert via Signal. Never raises."""
        if not self.enabled:
            logger.debug("Signal notifier disabled, skipping outcome notification")
            return
        try:
            await self._send_message(self.format_outcome(signal, outcome))
            logger.info(
                "Signal outcome notification sent for signal_id={}", signal.id
            )
        except Exception:
            logger.exception(
                "Signal outcome notification failed for signal_id={}", signal.id
            )

    async def notify_degradation(
        self, strategy_name: str, reason: str, is_recovery: bool = False
    ) -> None:
        """Send a degradation/recovery alert via Signal. Never raises."""
        if not self.enabled:
            logger.debug("Signal notifier disabled, skipping degradation notification")
            return
        try:
            await self._send_message(
                self.format_degradation(strategy_name, reason, is_recovery)
            )
            label = "recovery" if is_recovery else "degradation"
            logger.info(
                "Signal {} notification sent for '{}'", label, strategy_name
            )
        except Exception:
            logger.exception(
                "Signal degradation notification failed for '{}'", strategy_name
            )

    async def notify_circuit_breaker(self, reason: str, active: bool) -> None:
        """Send a circuit breaker alert via Signal. Never raises."""
        if not self.enabled:
            logger.debug("Signal notifier disabled, skipping circuit breaker notification")
            return
        try:
            await self._send_message(self.format_circuit_breaker(reason, active))
            logger.info(
                "Signal circuit breaker notification sent (active={})", active
            )
        except Exception:
            logger.exception("Signal circuit breaker notification failed")

    async def notify_system_alert(self, title: str, details: str) -> None:
        """Send a system alert via Signal. Never raises."""
        if not self.enabled:
            logger.debug("Signal notifier disabled, skipping system alert")
            return
        try:
            await self._send_message(self.format_system_alert(title, details))
            logger.info("Signal system alert sent: '{}'", title)
        except Exception:
            logger.exception("Signal system alert failed: '{}'", title)
