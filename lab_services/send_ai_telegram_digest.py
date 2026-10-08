"""
LAB SERVICES — DISPATCH AI ANALYSIS DIGEST TO TELEGRAM
======================================================
Utility to compile live telemetry snapshot and dispatch
Antigravity's rich AI market intelligence report to Telegram.
"""

import sys
import os
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from dotenv import load_dotenv

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(project_root, ".env"))
sys.path.insert(0, project_root)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from backend.core.telegram_notifier import TelegramNotifier

IST = timezone(timedelta(hours=5, minutes=30))


async def dispatch_telegram_ai_report(html_message: str) -> bool:
    """Dispatches a formatted HTML AI analysis message to Telegram."""
    notifier = TelegramNotifier()
    await notifier.initialize()
    if not notifier.enabled or not notifier.client:
        print("[ERROR] Telegram notifier not enabled or failed to initialize.")
        return False

    try:
        await notifier.enqueue_message(html_message)
        # Give the background worker a moment to deliver
        await asyncio.sleep(2.5)
        print("[SUCCESS] AI Analysis Report dispatched to Telegram!")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram report: {e}")
        return False
    finally:
        await notifier.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # If passed via file or argument
        msg = sys.argv[1]
        asyncio.run(dispatch_telegram_ai_report(msg))
    else:
        print("Usage: python send_ai_telegram_digest.py \"<HTML_MESSAGE>\"")
