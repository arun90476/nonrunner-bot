import os
import time
import json
import urllib.request
from datetime import datetime, timezone

# ==========================================
# CONFIGURATION
# ==========================================
# Telegram credentials populated directly
TELEGRAM_BOT_TOKEN = "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI"
TELEGRAM_CHAT_ID = "8435489741"

# Matchbook API parameters
MATCHBOOK_URL = (
    "https://api.matchbook.com/edge/rest/events"
    "?sport-ids=9"
    "&states=open,suspended"
    "&include-prices=true"
    "&include-withdrawn=true"
    "&per-page=100"
)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json'
}

POLL_INTERVAL_SECONDS = 10  # Seconds between checks

# Cache set to keep track of already alerted withdrawn runner IDs
seen_withdrawn_ids = set()


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def send_telegram_alert(message: str):
    """Sends a formatted notification to your Telegram chat/channel."""
    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }).encode('utf-8')

    req = urllib.request.Request(
        telegram_url,
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.getcode() == 200:
                print("✅ Telegram alert sent successfully.")
    except Exception as e:
        print(f"❌ Failed to send Telegram alert: {e}")


def check_non_runners():
    """Polls Matchbook API, checks today's races, and flags new non-runners."""
    global seen_withdrawn_ids

    today_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    req = urllib.request.Request(MATCHBOOK_URL, headers=HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.getcode() != 200:
                print(f"⚠️ Unexpected status code: {response.getcode()}")
                return

            data = json.loads(response.read().decode())
            events = data.get("events", [])

            # Filter events for today (UTC)
            today_events = [e for e in events if e.get("start", "").startswith(today_utc_str)]

            for event in today_events:
                event_name = event.get("name", "Unknown Race")
                event_time = event.get("start", "")[11:16]  # Extracts HH:MM in UTC

                for market in event.get("markets", []):
                    # Only focus on WIN market type if available
                    market_type = market.get("market-type", "")
                    if market_type and market_type != "WIN":
                        continue

                    for runner in market.get("runners", []):
                        runner_id = runner.get("id")
                        runner_status = runner.get("status")

                        if runner_status == "withdrawn":
                            # Process only if we haven't alerted this specific runner yet
                            if runner_id not in seen_withdrawn_ids:
                                seen_withdrawn_ids.add(runner_id)
                                
                                horse_name = runner.get("name", "Unknown Horse")
                                last_price = runner.get("last-priced-decimal")
                                
                                # Fallback to prices array if last-priced-decimal isn't set
                                prices = runner.get("prices", [])
                                if prices and not last_price:
                                    last_price = prices[0].get("decimal-odds")

                                price_display = f"{last_price:.2f}" if last_price else "N/A"

                                print(f"🏇 [NON-RUNNER DETECTED] {horse_name} in {event_name} @ {price_display}")

                                alert_msg = (
                                    f"🚨 *NON-RUNNER ALERT*\n\n"
                                    f"🏇 *Horse:* `{horse_name}`\n"
                                    f"🏆 *Race:* `{event_name}`\n"
                                    f"⏰ *Time:* `{event_time} UTC`\n"
                                    f"📊 *Pre-Scratch Odds:* `{price_display}`"
                                )
                                send_telegram_alert(alert_msg)

    except urllib.error.HTTPError as http_err:
        print(f"❌ HTTP Error encountered: {http_err.code} - {http_err.reason}")
    except urllib.error.URLError as url_err:
        print(f"❌ Network URL Error: {url_err.reason}")
    except Exception as e:
        print(f"❌ Unexpected Error: {e}")


# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
if __name__ == "__main__":
    print("🚀 Matchbook Non-Runner Monitor Service Started!")
    print(f"📅 UTC Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print(f"⏱️ Polling interval: {POLL_INTERVAL_SECONDS} seconds\n")

    while True:
        check_non_runners()
        time.sleep(POLL_INTERVAL_SECONDS)
