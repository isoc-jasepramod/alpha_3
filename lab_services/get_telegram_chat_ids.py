import os
import sys
import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
    if not token:
        print("❌ Error: TELEGRAM_BOT_TOKEN not found in .env")
        return

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        res = httpx.get(url, timeout=10.0)
        data = res.json()
        if not data.get("ok"):
            print(f"❌ Telegram API Error: {data}")
            return

        updates = data.get("result", [])
        if not updates:
            print("ℹ️ No recent messages found. Ask your friend to open https://t.me/alpha_3_trade_bot and tap 'START'.")
            return

        seen_chats = {}
        for upd in updates:
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat", {})
            cid = chat.get("id")
            if not cid:
                continue
            username = chat.get("username", "No Username")
            first_name = chat.get("first_name", "")
            last_name = chat.get("last_name", "")
            full_name = f"{first_name} {last_name}".strip() or "Unknown"
            ctype = chat.get("type", "private")
            seen_chats[cid] = {
                "id": cid,
                "username": f"@{username}" if username != "No Username" else "N/A",
                "name": full_name,
                "type": ctype
            }

        print("\n" + "=" * 65)
        print("📱 RECENT USERS / CHATS CONNECTED TO @alpha_3_trade_bot:")
        print("=" * 65)
        for cid, info in seen_chats.items():
            print(f"• Name: {info['name']}")
            print(f"  Username: {info['username']}")
            print(f"  Chat ID:  {info['id']}")
            print(f"  Type:     {info['type']}")
            print("-" * 65)

        print("\n👉 To send alerts to anyone above, add their Chat ID to .env:")
        ids_str = ", ".join(str(c) for c in seen_chats.keys())
        print(f'TELEGRAM_CHAT_ID="{ids_str}"\n')

    except Exception as e:
        print(f"❌ Failed to fetch updates: {e}")

if __name__ == "__main__":
    main()
