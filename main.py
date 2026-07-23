import datetime
import json
import time
import urllib.request

# --- TELEGRAM CONFIG ---
TELEGRAM_BOT_TOKEN = "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI"
TELEGRAM_CHAT_ID = "8435489741"

# --- MATCHBOOK API ENDPOINTS ---
EVENTS_URL = "https://api.matchbook.com/edge/rest/events?sport-ids=24735152712200&per-page=100&states=open,suspended"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    ),
    "Accept": "application/json",
}

seen_withdrawn_ids = set()
price_cache = {}  # Dynamic memory cache: runner_id -> decimal_price


def send_telegram(message):
  """Sends markdown formatted alert to Telegram."""
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
    with urllib.request.urlopen(req) as resp:
      if resp.status != 200:
        print(f"Telegram returned status {resp.status}")
  except Exception as e:
    print(f"Telegram API error: {e}")


def get_json(url):
  req = urllib.request.Request(url, headers=HEADERS)
  with urllib.request.urlopen(req) as response:
    return json.loads(response.read().decode("utf-8"))


def extract_runner_price(runner):
  """Exhaustive check across active orderbook AND historical API fields."""
  if not runner:
    return None

  # 1. Live back/lay prices array
  prices = runner.get("prices", [])
  if prices:
    for p in prices:
      if p.get("decimal"):
        return float(p["decimal"])

  # 2. Matchbook Direct Keys for Scratched/Withdrawn Runners
  direct_keys = [
      "last-priced-decimal",
      "withdrawn-price",
      "last-matched-price",
      "sp",
      "starting-price",
  ]
  for key in direct_keys:
    val = runner.get(key)
    if val is not None:
      try:
        return float(val)
      except (ValueError, TypeError):
        continue

  # 3. Check nested 'sp' dictionary if present
  sp_data = runner.get("sp")
  if isinstance(sp_data, dict):
    if sp_data.get("decimal"):
      return float(sp_data["decimal"])

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

      # 36-Hour Window
      if 0 <= (event_dt - now_utc).total_seconds() <= 129600:
        event_id = event.get("id")
        event_name = event.get("name", "Unknown Race")

        for market in event.get("markets", []):
          market_name = str(market.get("name", "")).lower()

          if "win" in market_name:
            market_id = market.get("id")
            runners_url = f"https://api.matchbook.com/edge/rest/events/{event_id}/markets/{market_id}/runners?states=open,suspended&include-withdrawn=true&include-prices=true&price-depth=3"

            try:
              runners_data = get_json(runners_url)
              runners = runners_data.get("runners", [])

              # Pass 1: Continuously update cache for active horses
              for runner in runners:
                r_id = runner.get("id")
                status = str(runner.get("status", "")).lower()
                if status == "open":
                  price = extract_runner_price(runner)
                  if price and r_id:
                    price_cache[r_id] = price

              # Pass 2: Process non-runners
              for runner in runners:
                runner_id = runner.get("id")
                status = str(runner.get("status", "")).lower()
                is_withdrawn = runner.get("withdrawn") is True or status in [
                    "withdrawn",
                    "scratched",
                    "removed",
                ]

                if is_withdrawn and runner_id not in seen_withdrawn_ids:
                  seen_withdrawn_ids.add(runner_id)

                  runner_name = runner.get("name", "Unknown Horse")

                  # Fallback Priority: Memory Cache -> Direct Payload Extraction
                  odds = price_cache.get(runner_id) or extract_runner_price(
                      runner
                  )

                  if odds and odds > 1.0:
                    odds_display = f"{odds:.2f}"
                    est_rf = (1 / odds) * 100
                    rf_display = f"~{est_rf:.1f}%"
                  else:
                    odds_display = "N/A"
                    rf_display = "N/A"

                  msg = (
                      f"🚨 *NON-RUNNER DETECTED*\n\n"
                      f"🏇 *Horse:* {runner_name}\n"
                      f"📍 *Race:* {event_name}\n"
                      f"📊 *Pre-Scratch Odds:* `{odds_display}`\n"
                      f"📉 *Est. Reduction Factor:* `{rf_display}`\n"
                      f"⏰ *Race Time:* {start_str[:16].replace('T', ' ')} UTC"
                  )

                  print(
                      f"[{now_utc.strftime('%H:%M:%S')}] ALERT: {runner_name} @"
                      f" {event_name} (Odds: {odds_display})"
                  )
                  send_telegram(msg)

            except Exception as r_err:
              print(f"Error checking market {market_id}: {r_err}")

  except Exception as e:
    print(f"Execution error: {e}")


if __name__ == "__main__":
  check_non_runners()
