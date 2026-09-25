import asyncio
import os
import sys
from dotenv import load_dotenv

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

from backend.core.telegram_notifier import TelegramNotifier
from loguru import logger

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

async def main():
    logger.info("Initializing Telegram Alert Test...")
    notifier = TelegramNotifier()
    logger.info(f"Bot Token: {notifier.bot_token[:10]}... (configured: {bool(notifier.bot_token)})")
    logger.info(f"Target Chat / Username: {notifier.raw_chat_id}")
    logger.info(f"Enabled: {notifier.enabled}")

    await notifier.initialize()
    logger.info(f"Bot Username: @{notifier.bot_username}")
    logger.info(f"Resolved Chat ID: {notifier.resolved_chat_id}")

    if not notifier.resolved_chat_id:
        print("\n" + "=" * 60)
        print("ACTION REQUIRED TO ACTIVATE TELEGRAM ALERTS:")
        print(f"1. Open Telegram on your phone or desktop: https://t.me/{notifier.bot_username}")
        print("2. Tap 'START' (or send /start)")
        print("3. Re-run this script to automatically bind your chat ID and send test alerts!")
        print("=" * 60 + "\n")
        await notifier.close()
        return

    # 1. Send Test New Signal
    logger.info("Enqueuing Test NEW_SIGNAL alert...")
    test_signal = {
        "signal_id": "TEST_SIG_001",
        "instrument": "NIFTY",
        "option_type": "CE",
        "strike": 23050.0,
        "option_symbol": "NIFTY23050CE",
        "strategy": "OI_SQUEEZE",
        "direction": "CE",
        "entry_price": 147.50,
        "stop_loss": 128.00,
        "target": 186.50,
        "lot_size": 50,
        "quantity": 100,
        "confidence": 92,
        "risk_amount": 1950.0,
        "details": {
            "squeeze_type": "CALL_COVERING_EARLY_IGNITION"
        }
    }
    await notifier.notify_new_signal(test_signal)

    # 2. Send Test Radar Pre-Alert
    logger.info("Enqueuing Test RADAR_PRE_ALERT...")
    test_radar = {
        "alert_type": "CONFLUENCE_PRE_ENTRY",
        "instrument": "NIFTY",
        "direction": "CE",
        "title": "Dual Gamma Wall Breakdown + Institutional Flow Surge",
        "message": "Heavy 23000 Call short covering detected with positive CVD (+142k contracts). Prepare for rapid gamma expansion.",
        "details": {
            "entry_strike": 23050.0,
            "recommended_sl": 23010.0,
            "target_1": 23090.0,
            "target_2": 23140.0
        }
    }
    await notifier.notify_radar_alert(test_radar)

    # 3. Send Test Resolution Alert
    logger.info("Enqueuing Test SIGNAL_RESOLVED...")
    test_resolved = {
        "signal_id": "TEST_SIG_001",
        "instrument": "NIFTY",
        "option_symbol": "NIFTY23050CE",
        "strategy": "OI_SQUEEZE",
        "entry_price": 147.50,
        "exit_price": 186.50,
        "theoretical_pnl": 3900.0,
        "status": "TARGET_HIT",
        "elapsed_sec": 195
    }
    await notifier.notify_signal_resolved(test_resolved)

    # Wait for queue to flush
    logger.info("Waiting for dispatcher worker to flush queue...")
    await asyncio.sleep(4.0)

    await notifier.close()
    logger.success("Test script complete!")

if __name__ == "__main__":
    asyncio.run(main())
