import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from backend.core.telegram_notifier import TelegramNotifier

@pytest.fixture
def sample_signal():
    return {
        "signal_id": "SIG_TEST_99",
        "instrument": "NIFTY",
        "option_type": "CE",
        "strike": 23050.0,
        "option_symbol": "NIFTY23050CE",
        "strategy": "OI_SQUEEZE",
        "direction": "CE",
        "entry_price": 150.0,
        "stop_loss": 130.0,
        "target": 190.0,
        "lot_size": 50,
        "quantity": 100,
        "confidence": 95,
        "risk_amount": 2000.0,
        "details": {
            "squeeze_type": "CALL_COVERING_EARLY_IGNITION"
        }
    }

@pytest.fixture
def sample_radar():
    return {
        "alert_type": "CONFLUENCE_PRE_ENTRY",
        "instrument": "NIFTY",
        "direction": "CE",
        "title": "Dual Gamma Wall Breakdown",
        "message": "High institutional buying flow detected.",
        "details": {
            "entry_strike": 23050.0,
            "recommended_sl": 23010.0,
            "target_1": 23100.0,
            "target_2": 23150.0
        }
    }

@pytest.fixture
def sample_resolution():
    return {
        "signal_id": "SIG_TEST_99",
        "instrument": "NIFTY",
        "option_symbol": "NIFTY23050CE",
        "strategy": "OI_SQUEEZE",
        "entry_price": 150.0,
        "exit_price": 190.0,
        "theoretical_pnl": 4000.0,
        "status": "TARGET_HIT",
        "elapsed_sec": 120
    }

def test_format_signal_html(sample_signal):
    notifier = TelegramNotifier(bot_token="test_token", chat_id="12345", enabled=True)
    html = notifier.format_signal_html(sample_signal)
    assert "ALPHA 3.0 — NEW SIGNAL" in html
    assert "NIFTY" in html
    assert "23050 CE" in html
    assert "₹150.00" in html
    assert "₹130.00" in html
    assert "₹190.00" in html
    assert "95%" in html
    assert "1:2.0" in html

def test_format_radar_html(sample_radar):
    notifier = TelegramNotifier(bot_token="test_token", chat_id="12345", enabled=True)
    html = notifier.format_radar_html(sample_radar)
    assert "ELEVATED RADAR: CONFLUENCE SETUP" in html
    assert "Dual Gamma Wall Breakdown" in html
    assert "23050 CE" in html
    assert "23010.0" in html

def test_format_resolution_html(sample_resolution):
    notifier = TelegramNotifier(bot_token="test_token", chat_id="12345", enabled=True)
    html = notifier.format_resolution_html(sample_resolution)
    assert "TARGET HIT" in html
    assert "+₹4,000.00" in html
    assert "+40.00 pts" in html
    assert "2m 0s" in html

@pytest.mark.asyncio
async def test_queue_and_mock_dispatch(sample_signal):
    notifier = TelegramNotifier(bot_token="1234:ABC", chat_id="999888777", enabled=True)
    
    mock_client = AsyncMock()
    mock_get_me = MagicMock()
    mock_get_me.json.return_value = {"ok": True, "result": {"username": "test_bot"}}
    
    mock_send = MagicMock()
    mock_send.json.return_value = {"ok": True, "result": {"message_id": 101}}

    mock_client.get.return_value = mock_get_me
    mock_client.post.return_value = mock_send

    with patch("httpx.AsyncClient", return_value=mock_client):
        await notifier.initialize()
        assert notifier.bot_username == "test_bot"
        assert notifier.resolved_chat_id == "999888777"

        # Queue a signal
        await notifier.notify_new_signal(sample_signal)
        # Give dispatcher loop time to process
        await asyncio.sleep(0.1)

        assert mock_client.post.called
        call_args = mock_client.post.call_args
        assert "sendMessage" in call_args[0][0]
        json_body = call_args[1]["json"]
        assert json_body["chat_id"] == "999888777"
        assert "ALPHA 3.0 — NEW SIGNAL" in json_body["text"]

        await notifier.close()

@pytest.mark.asyncio
async def test_multi_recipient_dispatch(sample_signal):
    # Comma-separated chat IDs (two recipients)
    notifier = TelegramNotifier(bot_token="1234:ABC", chat_id="11111, 22222", enabled=True)
    
    mock_client = AsyncMock()
    mock_get_me = MagicMock()
    mock_get_me.json.return_value = {"ok": True, "result": {"username": "test_bot"}}
    mock_send = MagicMock()
    mock_send.json.return_value = {"ok": True, "result": {"message_id": 101}}

    mock_client.get.return_value = mock_get_me
    mock_client.post.return_value = mock_send

    with patch("httpx.AsyncClient", return_value=mock_client):
        await notifier.initialize()
        assert "11111" in notifier.resolved_chat_ids
        assert "22222" in notifier.resolved_chat_ids

        await notifier.notify_new_signal(sample_signal)
        await asyncio.sleep(0.1)

        # Should be called once for each recipient (2 times)
        assert mock_client.post.call_count == 2
        calls = mock_client.post.call_args_list
        posted_chats = [c[1]["json"]["chat_id"] for c in calls]
        assert "11111" in posted_chats
        assert "22222" in posted_chats

        await notifier.close()

