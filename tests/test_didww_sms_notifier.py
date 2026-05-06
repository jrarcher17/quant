"""Tests for DIDWW SMS notification formatting and payloads."""

from types import SimpleNamespace

import pytest

from app.services.didww_sms_notifier import DidwwSmsNotifier


def _signal(**overrides):
    values = {
        "id": 123,
        "symbol": "XAUUSD",
        "direction": "BUY",
        "entry_price": "4635.50",
        "stop_loss": "4625.00",
        "take_profit_1": "4650.00",
        "take_profit_2": "4675.00",
        "confidence": "75",
        "risk_reward": "2.0",
        "reasoning": "EMA momentum confirmed | metadata",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _outcome(**overrides):
    values = {
        "result": "tp1_hit",
        "exit_price": "4650.00",
        "pnl_pips": "145.0",
        "duration_minutes": 90,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_enabled_requires_credentials_destination_and_sender():
    notifier = DidwwSmsNotifier(
        username="user",
        password="pass",
        source="+1 (212) 555-1234",
        destinations="+1 310 555 9999",
    )

    assert notifier.enabled is True
    assert notifier.source == "12125551234"
    assert notifier.destinations == ["13105559999"]


def test_campaign_id_can_replace_source():
    notifier = DidwwSmsNotifier(
        username="user",
        password="pass",
        source="",
        destinations="13105559999",
        campaign_id="campaign-123",
    )

    assert notifier.enabled is True


def test_payload_uses_single_outbound_message_shape():
    notifier = DidwwSmsNotifier(
        username="user",
        password="pass",
        source="12125551234",
        destinations="13105559999",
    )

    assert notifier._message_payload("Hello") == {
        "data": {
            "type": "outbound_messages",
            "attributes": {
                "destination": "13105559999",
                "content": "Hello",
                "source": "12125551234",
            },
        }
    }


def test_payload_uses_bulk_shape_for_multiple_destinations():
    notifier = DidwwSmsNotifier(
        username="user",
        password="pass",
        source="12125551234",
        destinations="13105559999;14165550000",
    )

    payload = notifier._message_payload("Hello")

    assert payload["data"]["type"] == "bulk_outbound_messages"
    assert payload["data"]["attributes"]["destination"] == [
        "13105559999",
        "14165550000",
    ]


def test_signal_format_is_plain_text_and_strips_metadata():
    notifier = DidwwSmsNotifier("user", "pass", "12125551234", "13105559999")

    text = notifier.format_signal(_signal(), strategy_name="ema_momentum")

    assert "XAUUSD BUY signal" in text
    assert "Entry 4635.50 SL 4625.00" in text
    assert "Strategy ema_momentum" in text
    assert "metadata" not in text


def test_system_alert_strips_html():
    notifier = DidwwSmsNotifier("user", "pass", "12125551234", "13105559999")

    text = notifier.format_system_alert(
        "Scanner Failing",
        "Failure count<br><b>Error:</b> Timeout",
    )

    assert text == "System alert: Scanner Failing\nFailure count\nError: Timeout"


@pytest.mark.asyncio
async def test_notify_signal_never_raises(monkeypatch):
    notifier = DidwwSmsNotifier("user", "pass", "12125551234", "13105559999")

    async def fail_send(_content):
        raise RuntimeError("network down")

    monkeypatch.setattr(notifier, "_send_message", fail_send)

    await notifier.notify_signal(_signal(), strategy_name="ema_momentum")
