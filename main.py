import datetime
import json
import time
import urllib.request

TELEGRAM_BOT_TOKEN = "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI"
TELEGRAM_CHAT_ID = "8435489741"

EVENTS_URL = "https://api.matchbook.com/edge/rest/events?sport-ids=24735152712200&per-page=100&states=open,suspended&include-prices=true&price-depth=1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    ),
    "Accept": "application/json",
}

seen_withdrawn_ids = set()
price_cache = {}  # Caches last known odds for each runner ID


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
    print(f"Telegram error: {e}")


def get_json(url):
  req = urllib.request.Request(url, headers=HEADERS)
  with urllib.request.urlopen(req) as response:
    return json.loads(response.read().decode("utf-8"))


def parse_price(runner):
  """Attempts to extract a valid decimal price from runner dictionary."""
  # Direct field
  if runner.get("last-priced-decimal"):
    return runner.get("last-priced-decimal")

  # Search inside prices array
  prices = runner.get("prices", [])
  for p in prices:
    if p.get("decimal"):
      return p.get("decimal")

  return None


def check_non_runners():
  try:
    events_data = get_json(EVENTS_URL)
    events = events_data.get("events", [])
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    for event in events:
      start_str = event.get("start")
      if not start_str:
        continue

      event_dt = datetime.datetime.fromisoformat(
          start_str.replace("Z", "+00:00")
      )

      if 0 <= (event_dt - now_utc).total_seconds() <= 129600:
        event_id = event.get("id")
        event_name = event.get("name", "Unknown Race")

        for market in event.get("markets", []):
          market_name = str(market.get("name", "")).lower()

          if "win" in market_name:
            market_id = market.get("id")

            # 1. Update cache with top-level event market prices
            for r in market.get("runners", []):
              r_id = r.get("id")
              p = parse_price(r)
              if p:
                price_cache[r_id] = p

            # 2. Sub-endpoint query for dedicated non-runner detection
            runners_url = f"https://api.matchbook.com/edge/rest/events/{event_id}/markets/{market_id}/runners?states=open,suspended&include-withdrawn=true&include-prices=true&price-depth=1"

            try:
              runners_data = get_json(runners_url)
              runners = runners_data.get("runners", [])

              for runner in runners:
                runner_id = runner.get("id")

                # Update price cache if a price exists
                live_price = parse_price(runner)
                if live_price:
                  price_cache[runner_id] = live_price

                status = str(runner.get("status", "")).lower()
                is_withdrawn = runner.get("withdrawn") is True or status in [
                    "withdrawn",
                    "scratched",
                    "removed",
                ]

                if is_withdrawn and runner_id not in seen_withdrawn_ids:
                  seen_withdrawn_ids.add(runner_id)

                  runner_name = runner.get("name", "Unknown Horse")

                  # Use cached price if current prices array is flushed
                  odds = price_cache.get(runner_id, live_price or "N/A")

                  msg = (
                      f"🚨 *NON-RUNNER DETECTED*\n\n"
                      f"🏇 *Horse:* {runner_name}\n"
                      f"📍 *Race:* {event_name}\n"
                      f"📊 *Pre-Scratch Odds:* {odds}\n"
                      f"⏰ *Race Time:* {start_str[:16].replace('T', ' ')} UTC"
                  )

                  print(f"[{now_utc.strftime('%H:%M:%S')}] ALERT: {runner_name}")
                  send_telegram(msg)

            except Exception as r_err:
              print(
                  f"Error fetching runners for market {market_id}: {r_err}"
              )

  except Exception as e:
    print(f"Error checking events: {e}")


if __name__ == "__main__":
  print("🚀 Matchbook Non-Runner Service Active with Price Caching...")
  while True:
    check_non_runners()
    time.sleep(10)
