"""DIDWW SMS notification service for urgent trading alerts.

Uses DIDWW HTTP OUT SMS trunks:
POST https://sms-out.didww.com/outbound_messages
Content-Type: application/vnd.api+json
HTTP Basic auth with the trunk username/password.
"""

from __future__ import annotations

import asyncio
import re
from html import unescape

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class DidwwSmsNotifier:
    """Sends concise SMS alerts through a DIDWW HTTP OUT trunk.

    Notification methods never raise to callers. This mirrors TelegramNotifier
    so delivery failures cannot interrupt scanning or outcome tracking jobs.
    """

    CONTENT_TYPE = "application/vnd.api+json"

    def __init__(
        self,
        username: str,
        password: str,
        source: str,
        destinations: str,
        campaign_id: str = "",
        endpoint: str = "https://sms-out.didww.com",
    ) -> None:
        self.username = username
        self.password = password
        self.source = self._normalize_phone(source)
        self.destinations = [
            self._normalize_phone(number)
            for number in re.split(r"[,;\s]+", destinations.strip())
            if number.strip()
        ]
        self.campaign_id = campaign_id
        self.endpoint = endpoint.rstrip("/")
        self._rate_lock = asyncio.Lock()
        self._last_send: float = 0.0

    @property
    def enabled(self) -> bool:
        """Return True when credentials and at least one destination exist."""
        has_sender = bool(self.source or self.campaign_id)
        return bool(self.username and self.password and self.destinations and has_sender)

    @staticmethod
    def _normalize_phone(value: str) -> str:
        """Normalize E.164-ish input for DIDWW, stripping separators and +."""
        return re.sub(r"\D", "", value or "")

    @staticmethod
    def _plain_text(text: str) -> str:
        """Convert simple Telegram HTML snippets to SMS-friendly text."""
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _truncate(text: str, max_chars: int = 480) -> str:
        """Keep alerts reasonably short to limit SMS fragments."""
        text = text.strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "..."

    async def _rate_limit(self) -> None:
        """Stay well below DIDWW's documented 10 RPS trunk limit."""
        async with self._rate_lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_send
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)
            self._last_send = asyncio.get_event_loop().time()

    def _message_payload(self, content: str) -> dict:
        """Build JSON:API payload for single or bulk outbound SMS."""
        is_bulk = len(self.destinations) > 1
        message_type = "bulk_outbound_messages" if is_bulk else "outbound_messages"
        attributes: dict[str, str | list[str]] = {
            "destination": self.destinations if is_bulk else self.destinations[0],
            "content": content,
        }
        if self.source:
            attributes["source"] = self.source
        if self.campaign_id:
            attributes["campaign_id"] = self.campaign_id
        return {
            "data": {
                "type": message_type,
                "attributes": attributes,
            }
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException)
        ),
    )
    async def _send_message(self, content: str) -> dict:
        """POST one SMS body to DIDWW with retry and rate limiting."""
        await self._rate_limit()
        path = (
            "/bulk_outbound_messages"
            if len(self.destinations) > 1
            else "/outbound_messages"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self.endpoint}{path}",
                headers={"Content-Type": self.CONTENT_TYPE},
                auth=(self.username, self.password),
                json=self._message_payload(content),
            )
            response.raise_for_status()
            return response.json()

    def format_signal(self, signal, strategy_name: str = "Unknown") -> str:
        """Format a concise trading signal SMS."""
        reasoning = (signal.reasoning or "").split("|")[0].strip()
        text = (
            f"{signal.symbol} {signal.direction} signal\n"
            f"Entry {signal.entry_price} SL {signal.stop_loss}\n"
            f"TP1 {signal.take_profit_1} TP2 {signal.take_profit_2}\n"
            f"Conf {signal.confidence}% R:R {signal.risk_reward}\n"
            f"Strategy {strategy_name}"
        )
        if reasoning:
            text = f"{text}\n{reasoning}"
        return self._truncate(text)

    def format_outcome(self, signal, outcome) -> str:
        """Format a concise outcome SMS."""
        return self._truncate(
            f"{signal.symbol} {signal.direction} {outcome.result.upper()}\n"
            f"Entry {signal.entry_price} Exit {outcome.exit_price}\n"
            f"P&L {outcome.pnl_pips} pips, {outcome.duration_minutes} min"
        )

    def format_degradation(
        self, strategy_name: str, reason: str, is_recovery: bool = False
    ) -> str:
        """Format a strategy degradation/recovery SMS."""
        label = "recovered" if is_recovery else "degraded"
        return self._truncate(f"Strategy {label}: {strategy_name}\n{reason}")

    def format_circuit_breaker(self, reason: str, active: bool) -> str:
        """Format circuit breaker SMS."""
        label = "ACTIVATED" if active else "RESET"
        return self._truncate(f"Circuit breaker {label}\n{reason}")

    def format_system_alert(self, title: str, details: str) -> str:
        """Format system alert SMS."""
        return self._truncate(f"System alert: {title}\n{self._plain_text(details)}")

    async def notify_signal(self, signal, strategy_name: str = "Unknown") -> None:
        """Send a signal alert by SMS. Never raises."""
        if not self.enabled:
            logger.debug("DIDWW SMS disabled, skipping signal notification")
            return
        try:
            await self._send_message(self.format_signal(signal, strategy_name))
            logger.info(
                "DIDWW SMS signal notification sent for signal_id={}",
                signal.id,
            )
        except Exception:
            logger.exception(
                "DIDWW SMS signal notification failed for signal_id={}",
                signal.id,
            )

    async def notify_outcome(self, signal, outcome) -> None:
        """Send an outcome alert by SMS. Never raises."""
        if not self.enabled:
            logger.debug("DIDWW SMS disabled, skipping outcome notification")
            return
        try:
            await self._send_message(self.format_outcome(signal, outcome))
            logger.info("DIDWW SMS outcome notification sent for signal_id={}", signal.id)
        except Exception:
            logger.exception(
                "DIDWW SMS outcome notification failed for signal_id={}",
                signal.id,
            )

    async def notify_degradation(
        self, strategy_name: str, reason: str, is_recovery: bool = False
    ) -> None:
        """Send a degradation/recovery alert by SMS. Never raises."""
        if not self.enabled:
            logger.debug("DIDWW SMS disabled, skipping degradation notification")
            return
        try:
            await self._send_message(
                self.format_degradation(strategy_name, reason, is_recovery)
            )
            logger.info(
                "DIDWW SMS degradation notification sent for '{}'",
                strategy_name,
            )
        except Exception:
            logger.exception(
                "DIDWW SMS degradation notification failed for '{}'",
                strategy_name,
            )

    async def notify_circuit_breaker(self, reason: str, active: bool) -> None:
        """Send a circuit breaker alert by SMS. Never raises."""
        if not self.enabled:
            logger.debug("DIDWW SMS disabled, skipping circuit breaker notification")
            return
        try:
            await self._send_message(self.format_circuit_breaker(reason, active))
            logger.info("DIDWW SMS circuit breaker notification sent (active={})", active)
        except Exception:
            logger.exception("DIDWW SMS circuit breaker notification failed")

    async def notify_system_alert(self, title: str, details: str) -> None:
        """Send a system alert by SMS. Never raises."""
        if not self.enabled:
            logger.debug("DIDWW SMS disabled, skipping system alert")
            return
        try:
            await self._send_message(self.format_system_alert(title, details))
            logger.info("DIDWW SMS system alert sent: '{}'", title)
        except Exception:
            logger.exception("DIDWW SMS system alert failed: '{}'", title)
