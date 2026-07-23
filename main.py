import datetime
import json
import time
import urllib.request

# Configuration
TELEGRAM_BOT_TOKEN = "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI"
TELEGRAM_CHAT_ID = "8435489741"
MATCHBOOK_URL = "https://api.matchbook.com/edge/rest/events?sport-ids=9&include-withdrawn=true&per-page=100&states=open,suspended"

seen_withdrawn_ids = set()


def send_telegram(message):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = json.dumps({
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message,
      "parse_mode": "Markdown",
  }).encode("utf-8")
  req = urllib.request.Request(
      url, data=payload, headers={"Content-Type": "application/json"}
  )
  try:
    urllib.request.urlopen(req)
  except Exception as e:
    print(f"Failed to send Telegram msg: {e}")


def check_non_runners():
  req = urllib.request.Request(
      MATCHBOOK_URL,
      headers={
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
          "Accept": "application/json",
      },
  )

  try:
    with urllib.request.urlopen(req) as response:
      data = json.loads(response.read().decode("utf-8"))
      events = data.get("events", [])

      today_utc = datetime.datetime.now(datetime.timezone.utc).date()

      for event in events:
        start_time_str = event.get("start")
        if not start_time_str:
          continue

        # Safely parse event date in UTC
        event_date = datetime.datetime.fromisoformat(
            start_time_str.replace("Z", "+00:00")
        ).date()

        # Check today's races
        if event_date == today_utc:
          event_name = event.get("name", "Unknown Event")

          for market in event.get("markets", []):
            # Focus on Win markets
            if "win" in market.get("name", "").lower():
              for runner in market.get("runners", []):

                # Detect Non-Runner state across different Matchbook flags
                is_withdrawn = (
                    runner.get("status") == "withdrawn"
                    or runner.get("withdrawn") is True
                )
                runner_id = runner.get("id")

                if is_withdrawn and runner_id not in seen_withdrawn_ids:
                  seen_withdrawn_ids.add(runner_id)

                  runner_name = runner.get("name")
                  odds = runner.get("last-priced-decimal", "N/A")

                  msg = (
                      f"🚨 *NON-RUNNER ALERT*\n\n"
                      f"🏇 *Horse:* {runner_name}\n"
                      f"📍 *Race:* {event_name}\n"
                      f"📊 *Pre-Scratch Odds:* {odds}"
                  )
                  print(f"Match found: {runner_name} in {event_name}")
                  send_telegram(msg)

  except Exception as e:
    print(f"Error fetching data: {e}")


if __name__ == "__main__":
  print("🚀 Matchbook Non-Runner Monitor Started...")
  while True:
    check_non_runners()
    time.sleep(10)
