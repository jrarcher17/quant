"""Unit tests for SignalNotifier (CallMeBot Signal API).

Covers:
  - enabled property (both configured / partially missing / empty)
  - _strip_html helper
  - format_signal / format_outcome / format_degradation /
    format_circuit_breaker / format_system_alert
  - _send_message calls CallMeBot with correct params
  - notify_signal / notify_outcome / notify_degradation /
    notify_circuit_breaker / notify_system_alert (enabled path)
  - All notify_* methods are no-ops when disabled
  - All notify_* methods swallow exceptions (never raise)
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.signal_notifier import CALLMEBOT_URL, SignalNotifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_notifier(phone: str = "+14155552671", api_key: str = "abc123") -> SignalNotifier:
    return SignalNotifier(phone=phone, api_key=api_key)


def make_signal(**overrides):
    sig = MagicMock()
    sig.id = 42
    sig.symbol = "XAUUSD"
    sig.direction = "BUY"
    sig.entry_price = Decimal("2650.00")
    sig.stop_loss = Decimal("2640.00")
    sig.take_profit_1 = Decimal("2670.00")
    sig.take_profit_2 = Decimal("2690.00")
    sig.risk_reward = Decimal("2.00")
    sig.confidence = Decimal("75.00")
    sig.reasoning = "Liquidity sweep confirmed | H4 confluence"
    for k, v in overrides.items():
        setattr(sig, k, v)
    return sig


def make_outcome(**overrides):
    out = MagicMock()
    out.result = "tp1_hit"
    out.exit_price = Decimal("2670.00")
    out.pnl_pips = Decimal("200.00")
    out.duration_minutes = 90
    for k, v in overrides.items():
        setattr(out, k, v)
    return out


# ---------------------------------------------------------------------------
# enabled property
# ---------------------------------------------------------------------------


def test_enabled_when_both_configured():
    assert make_notifier().enabled is True


def test_disabled_when_phone_missing():
    assert SignalNotifier(phone="", api_key="key123").enabled is False


def test_disabled_when_api_key_missing():
    assert SignalNotifier(phone="+14155552671", api_key="").enabled is False


def test_disabled_when_both_missing():
    assert SignalNotifier(phone="", api_key="").enabled is False


# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------


def test_strip_html_removes_bold_tags():
    result = SignalNotifier._strip_html("<b>Entry:</b> 2650.00")
    assert result == "Entry: 2650.00"


def test_strip_html_converts_br_to_newline():
    result = SignalNotifier._strip_html("line1<br/>line2")
    assert "line1\nline2" == result


def test_strip_html_decodes_entities():
    result = SignalNotifier._strip_html("P&amp;L: 100")
    assert "P&L: 100" in result


# ---------------------------------------------------------------------------
# format_signal
# ---------------------------------------------------------------------------


def test_format_signal_buy_contains_arrow_and_fields():
    n = make_notifier()
    sig = make_signal()
    text = n.format_signal(sig, strategy_name="liquidity_sweep")
    assert "▲" in text
    assert "XAUUSD BUY" in text
    assert "2650.00" in text
    assert "2640.00" in text
    assert "2670.00" in text
    assert "liquidity_sweep" in text


def test_format_signal_sell_contains_down_arrow():
    n = make_notifier()
    sig = make_signal(direction="SELL")
    assert "▼" in n.format_signal(sig)


def test_format_signal_strips_reasoning_after_pipe():
    n = make_notifier()
    sig = make_signal(reasoning="Liquidity sweep confirmed | H4 confluence | extra")
    text = n.format_signal(sig)
    assert "Liquidity sweep confirmed" in text
    assert "H4 confluence" not in text


def test_format_signal_no_reasoning_still_works():
    n = make_notifier()
    sig = make_signal(reasoning="")
    text = n.format_signal(sig)
    assert "XAUUSD" in text


# ---------------------------------------------------------------------------
# format_outcome
# ---------------------------------------------------------------------------


def test_format_outcome_tp1_has_checkmark():
    n = make_notifier()
    text = n.format_outcome(make_signal(), make_outcome(result="tp1_hit"))
    assert "✅" in text
    assert "TP1_HIT" in text


def test_format_outcome_sl_has_cross():
    n = make_notifier()
    text = n.format_outcome(make_signal(), make_outcome(result="sl_hit"))
    assert "❌" in text
    assert "SL_HIT" in text


def test_format_outcome_contains_pnl_and_duration():
    n = make_notifier()
    text = n.format_outcome(make_signal(), make_outcome())
    assert "200" in text
    assert "90" in text


# ---------------------------------------------------------------------------
# format_degradation
# ---------------------------------------------------------------------------


def test_format_degradation_degraded():
    n = make_notifier()
    text = n.format_degradation("ema_momentum", "win rate dropped")
    assert "⚠️" in text
    assert "Degraded" in text
    assert "ema_momentum" in text
    assert "win rate dropped" in text


def test_format_degradation_recovery():
    n = make_notifier()
    text = n.format_degradation("ema_momentum", "metrics improved", is_recovery=True)
    assert "🔄" in text
    assert "Recovered" in text


# ---------------------------------------------------------------------------
# format_circuit_breaker
# ---------------------------------------------------------------------------


def test_format_circuit_breaker_activated():
    n = make_notifier()
    text = n.format_circuit_breaker("5 consecutive losses", active=True)
    assert "🛑" in text
    assert "ACTIVATED" in text


def test_format_circuit_breaker_reset():
    n = make_notifier()
    text = n.format_circuit_breaker("losses cleared", active=False)
    assert "✅" in text
    assert "Reset" in text


# ---------------------------------------------------------------------------
# format_system_alert
# ---------------------------------------------------------------------------


def test_format_system_alert_strips_html():
    n = make_notifier()
    text = n.format_system_alert(
        "Candle Refresh Failing",
        "<b>Error:</b> timeout after 3 retries",
    )
    assert "Candle Refresh Failing" in text
    assert "<b>" not in text
    assert "timeout after 3 retries" in text


# ---------------------------------------------------------------------------
# _send_message posts to CallMeBot with correct params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_calls_correct_url():
    n = make_notifier(phone="+14155552671", api_key="testkey")
    captured = {}

    async def mock_get(url, params=None, **kwargs):
        captured["url"] = url
        captured["params"] = params
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=mock_get)
        mock_client_cls.return_value = mock_client

        await n._send_message("Hello Signal")

    assert captured["url"] == CALLMEBOT_URL
    assert captured["params"]["phone"] == "+14155552671"
    assert captured["params"]["apikey"] == "testkey"
    assert captured["params"]["text"] == "Hello Signal"


# ---------------------------------------------------------------------------
# notify_* - enabled path sends message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_signal_sends_when_enabled():
    n = make_notifier()
    n._send_message = AsyncMock()
    await n.notify_signal(make_signal(), strategy_name="liquidity_sweep")
    n._send_message.assert_awaited_once()
    sent_text = n._send_message.call_args[0][0]
    assert "XAUUSD" in sent_text


@pytest.mark.asyncio
async def test_notify_outcome_sends_when_enabled():
    n = make_notifier()
    n._send_message = AsyncMock()
    await n.notify_outcome(make_signal(), make_outcome())
    n._send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_degradation_sends_when_enabled():
    n = make_notifier()
    n._send_message = AsyncMock()
    await n.notify_degradation("ema_momentum", "win rate fell")
    n._send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_circuit_breaker_sends_when_enabled():
    n = make_notifier()
    n._send_message = AsyncMock()
    await n.notify_circuit_breaker("5 losses", active=True)
    n._send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_system_alert_sends_when_enabled():
    n = make_notifier()
    n._send_message = AsyncMock()
    await n.notify_system_alert("DB Down", "connection refused")
    n._send_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# notify_* - disabled path is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_signal_noop_when_disabled():
    n = SignalNotifier(phone="", api_key="")
    n._send_message = AsyncMock()
    await n.notify_signal(make_signal())
    n._send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_outcome_noop_when_disabled():
    n = SignalNotifier(phone="", api_key="")
    n._send_message = AsyncMock()
    await n.notify_outcome(make_signal(), make_outcome())
    n._send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# notify_* - exceptions are swallowed (never raise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_signal_swallows_exception():
    n = make_notifier()
    n._send_message = AsyncMock(side_effect=RuntimeError("network failure"))
    # Should not raise
    await n.notify_signal(make_signal())


@pytest.mark.asyncio
async def test_notify_outcome_swallows_exception():
    n = make_notifier()
    n._send_message = AsyncMock(side_effect=RuntimeError("timeout"))
    await n.notify_outcome(make_signal(), make_outcome())


@pytest.mark.asyncio
async def test_notify_system_alert_swallows_exception():
    n = make_notifier()
    n._send_message = AsyncMock(side_effect=RuntimeError("oops"))
    await n.notify_system_alert("Test", "details")
