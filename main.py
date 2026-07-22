import sys
import requests
import time
from datetime import datetime, timezone

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

def run_simulation_test():
    print("🧪 RUNNING SIMULATION TEST...", flush=True)
    today_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # MOCK API DATA mimicking Matchbook's exact structure
    mock_events = [
        {
            "name": "15:30 Ascot - Queen Elizabeth II Stakes",
            "start": f"{today_utc_str}T15:30:00Z",
            "markets": [
                {
                    "market-type": "WIN",
                    "runners": [
                        {
                            "id": 9991,
                            "name": "Mock Outsider (Should Block)",
                            "status": "withdrawn",
                            "last-priced-decimal": 6.00
                        },
                        {
                            "id": 9992,
                            "name": "Kyprios (Should Alert)",
                            "status": "withdrawn",
                            "last-priced-decimal": 2.20
                        }
                    ]
                }
            ]
        }
    ]

    for event in mock_events:
        start_time_iso = event.get("start", "")
        markets = event.get("markets", [])
        
        for market in markets:
            runners = market.get("runners", [])
            for runner in runners:
                runner_id = runner.get("id")
                status = runner.get("status")

                if status == "withdrawn" and runner_id not in alerted_runner_ids:
                    last_price = float(runner.get("last-priced-decimal"))
                    runner_name = runner.get("name")
                    event_name = event.get("name")

                    if last_price <= 3.33:
                        print(f"[PASSED FILTER] Withdrawn: {runner_name} | Price: {last_price} <= 3.33 | Sending Telegram...", flush=True)
                        alerted_runner_ids.add(runner_id)
                        race_time = start_time_iso.split("T")[1][:5]
                        
                        message = (
                            f"🚨 *TODAY'S MAJOR NON-RUNNER ALERT* 🚨\n\n"
                            f"🏇 *Horse:* {runner_name}\n"
                            f"📍 *Race:* {event_name} ({race_time} UTC)\n"
                            f"📊 *Last Price:* {last_price} (≥ 30% Market Share)\n"
                            f"📅 *Date:* {today_utc_str}"
                        )
                        send_telegram(message)
                        print(f"✅ Telegram Alert Sent for {runner_name}!", flush=True)
                    else:
                        print(f"[BLOCKED BY FILTER] Withdrawn: {runner_name} | Price: {last_price} > 3.33", flush=True)

# Run test once on deploy
run_simulation_test()
