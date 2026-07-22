import sys
import requests
import time
from datetime import datetime, timezone

# Force Python to instantly output print statements to Render logs
sys.stdout.reconfigure(line_buffering=True)

# ==========================================
# TELEGRAM CREDENTIALS
TELEGRAM_BOT_TOKEN = "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI"
TELEGRAM_CHAT_ID = "8435489741"
# ==========================================

alerted_runner_ids = set()

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

def check_uk_non_runners():
    # Matchbook API - Category ID 247352524380009 limits request strictly to Horse Racing
    url = "https://api.matchbook.com/edge/rest/events?category-ids=247352524380009&states=open&include-prices=true"
    
    # Strictly check UTC date to align with Matchbook ISO timestamps
    today_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code != 200:
            print(f"API HTTP Error: {response.status_code}", flush=True)
            return

        data = response.json()
        events = data.get("events", [])

        for event in events:
            start_time_iso = event.get("start", "") # e.g. "2026-07-22T14:30:00Z"

            # 1. DATE CHECK: Ignore future / ante-post dates
            if not start_time_iso.startswith(today_utc_str):
                continue

            markets = event.get("markets", [])
            for market in markets:
                # Target WIN and PLACE markets
                if market.get("market-type") not in ["WIN", "ONE_TWO_THREE"]:
                    continue

                runners = market.get("runners", [])
                for runner in runners:
                    runner_id = runner.get("id")
                    status = runner.get("status")

                    # 2. STATUS CHECK: Newly withdrawn runners only
                    if status == "withdrawn" and runner_id not in alerted_runner_ids:
                        
                        # Grab price from active prices OR fallback to last known decimal price
                        prices = runner.get("prices", [])
                        last_price = runner.get("last-priced-decimal")

                        if prices and len(prices) > 0:
                            last_price = prices[0].get("decimal-odds", last_price)

                        # If price is missing/null, skip safely
                        if last_price is None:
                            continue

                        last_price = float(last_price)
                        runner_name = runner.get("name", "Unknown Horse")
                        event_name = event.get("name", "UK Race")

                        # =======================================================
                        # LIVE DIAGNOSTIC LOG (Visible in Render Dashboard)
                        # =======================================================
                        if last_price <= 3.33:
                            print(f"[PASSED FILTER] Withdrawn: {runner_name} | Price: {last_price} <= 3.33 | Triggering Alert...", flush=True)
                        else:
                            print(f"[BLOCKED BY FILTER] Withdrawn: {runner_name} | Price: {last_price} > 3.33 (Market Share < 30%)", flush=True)

                        # 3. VALUE CHECK: Odds <= 3.33 (>= 30% Market Share)
                        if last_price <= 3.33:
                            alerted_runner_ids.add(runner_id)

                            race_time = start_time_iso.split("T")[1][:5] if "T" in start_time_iso else "N/A"
                            
                            message = (
                                f"🚨 *TODAY'S MAJOR NON-RUNNER ALERT* 🚨\n\n"
                                f"🏇 *Horse:* {runner_name}\n"
                                f"📍 *Race:* {event_name} ({race_time} UTC)\n"
                                f"📊 *Last Price:* {last_price} (≥ 30% Market Share)\n"
                                f"📅 *Date:* {today_utc_str}"
                            )
                            send_telegram(message)
                            print(f"✅ Telegram Alert Sent: {runner_name} @ {event_name}", flush=True)

    except Exception as e:
        print(f"Error fetching data: {e}", flush=True)

# Startup Notification
print("Cloud Bot Online: Monitoring UK Non-Runners...", flush=True)
send_telegram("🚀 *Bot Online:* Connected to Matchbook API! Live filter active.")

while True:
    check_uk_non_runners()
    time.sleep(10)  # Polling interval: 10 seconds
