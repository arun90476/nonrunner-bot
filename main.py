import requests
import time
from datetime import datetime

# ==========================================
# REPLACE THESE TWO VALUES WITH YOUR OWN
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
        print(f"Telegram error: {e}")

def check_uk_non_runners():
    url = "https://api.matchbook.com/edge/rest/events?states=open&include-prices=true"
    
    # Get today's date in YYYY-MM-DD
    today_str = datetime.now().strftime("%Y-%m-%d")

    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return

        data = response.json()
        events = data.get("events", [])

        for event in events:
            start_time_iso = event.get("start", "") # Example: "2026-07-22T14:30:00Z"

            # CONDITION 1: Ignore tomorrow's or future races
            if not start_time_iso.startswith(today_str):
                continue

            markets = event.get("markets", [])
            for market in markets:
                if market.get("market-type") not in ["WIN", "ONE_TWO_THREE"]:
                    continue

                runners = market.get("runners", [])
                for runner in runners:
                    runner_id = runner.get("id")
                    status = runner.get("status")

                    # CONDITION 2: Only check newly withdrawn runners
                    if status == "withdrawn" and runner_id not in alerted_runner_ids:
                        prices = runner.get("prices", [])
                        last_price = runner.get("last-priced-decimal", 99.0)

                        if prices:
                            last_price = prices[0].get("decimal-odds", last_price)

                        # CONDITION 3: Market share >= 30% (Odds <= 3.33)
                        if last_price <= 3.33:
                            alerted_runner_ids.add(runner_id)

                            runner_name = runner.get("name")
                            event_name = event.get("name")
                            race_time = start_time_iso.split("T")[1][:5]
                            
                            message = (
                                f"🚨 *TODAY'S MAJOR NON-RUNNER ALERT* 🚨\n\n"
                                f"🏇 *Horse:* {runner_name}\n"
                                f"📍 *Race:* {event_name} ({race_time} UTC)\n"
                                f"📊 *Price:* {last_price} (≥ 30% Market Share)\n"
                                f"📅 *Date:* {today_str} (Today Only)"
                            )
                            send_telegram(message)
                            print(f"Alert Sent: {runner_name} @ {event_name}")

    except Exception as e:
        print(f"Error fetching data: {e}")

print("Cloud Bot Online: Monitoring UK Non-Runners...")
while True:
    check_uk_non_runners()
    time.sleep(10)  # Runs checks every 10 seconds
