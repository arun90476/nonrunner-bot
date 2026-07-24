import datetime
import json
import os
import time
import urllib.request

# --- TELEGRAM CONFIG ---
TELEGRAM_BOT_TOKEN = "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI"
TELEGRAM_CHAT_ID = "8435489741"

EVENTS_URL = (
    "https://api.matchbook.com/edge/rest/events"
    "?sport-ids=24735152712200&per-page=100&states=open,suspended"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

POLL_SECONDS = 20
WINDOW_SECONDS = 129600  # 36 hours
STATE_FILE = "nr_state.json"
DEBUG_DUMP = True  # dump raw payload of first withdrawal to verify field names

seen_withdrawn_ids = set()
price_cache = {}  # runner_id -> {"back", "last", "vol", "ts"}
_dumped = False


# ---------- persistence ----------
def load_state():
    global seen_withdrawn_ids, price_cache
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
            seen_withdrawn_ids = set(d.get("seen", []))
            price_cache = {int(k): v for k, v in d.get("prices", {}).items()}
            print(
                f"State loaded: {len(seen_withdrawn_ids)} seen, "
                f"{len(price_cache)} cached prices."
            )
        except Exception as e:
            print(f"State load failed: {e}")


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(
                {"seen": list(seen_withdrawn_ids), "prices": price_cache}, f
            )
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"State save failed: {e}")


# ---------- helpers ----------
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    ).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return True
                print(f"Telegram status {resp.status}")
        except Exception as e:
            print(f"Telegram error (try {attempt + 1}/3): {e}")
            time.sleep(2)
    return False


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_prices(runner):
    """Returns (best_back, last_matched, matched_volume)."""
    best_back = None
    matched_vol = 0.0

    for p in runner.get("prices", []) or []:
        side = str(p.get("side", "")).lower()
        try:
            dec = float(p["decimal"])
        except (KeyError, ValueError, TypeError):
            continue
        if side in ("back", "lay") and best_back is None:
            best_back = dec
        try:
            matched_vol += float(p.get("available-amount") or 0)
        except (ValueError, TypeError):
            pass

    last_matched = None
    for key in ("last-matched-price", "last-priced-decimal", "withdrawn-price"):
        val = runner.get(key)
        if val is None or isinstance(val, dict):
            continue
        try:
            last_matched = float(val)
            break
        except (ValueError, TypeError):
            continue

    if last_matched is None:
        sp = runner.get("sp")
        if isinstance(sp, dict) and sp.get("decimal"):
            try:
                last_matched = float(sp["decimal"])
            except (ValueError, TypeError):
                pass
        elif sp is not None and not isinstance(sp, dict):
            try:
                last_matched = float(sp)
            except (ValueError, TypeError):
                pass

    try:
        vol = float(runner.get("volume") or runner.get("matched-volume") or 0)
        if vol:
            matched_vol = vol
    except (ValueError, TypeError):
        pass

    return best_back, last_matched, matched_vol


def fmt(val):
    return f"{val:.2f}" if val else "N/A"


# ---------- core ----------
def check_non_runners():
    global _dumped
    new_alerts = 0

    try:
        events = get_json(EVENTS_URL).get("events", []) or []
    except Exception as e:
        print(f"Events fetch error: {e}")
        return 0

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    for event in events:
        start_str = event.get("start")
        if not start_str:
            continue
        try:
            event_dt = datetime.datetime.fromisoformat(
                start_str.replace("Z", "+00:00")
            )
        except ValueError:
            continue

        delta = (event_dt - now_utc).total_seconds()
        if not (0 <= delta <= WINDOW_SECONDS):
            continue

        event_id = event.get("id")
        event_name = event.get("name", "Unknown Race")

        for market in event.get("markets", []) or []:
            if "win" not in str(market.get("name", "")).lower():
                continue

            market_id = market.get("id")
            runners_url = (
                f"https://api.matchbook.com/edge/rest/events/{event_id}"
                f"/markets/{market_id}/runners"
                "?states=open,suspended&include-withdrawn=true"
                "&include-prices=true&price-depth=3"
            )

            try:
                runners = get_json(runners_url).get("runners", []) or []
            except Exception as r_err:
                print(f"Market {market_id} error: {r_err}")
                continue

            # Pass 1 — snapshot live runners so odds survive the withdrawal
            for runner in runners:
                r_id = runner.get("id")
                if not r_id:
                    continue
                if str(runner.get("status", "")).lower() == "open":
                    back, last, vol = extract_prices(runner)
                    if back or last:
                        prev = price_cache.get(r_id, {})
                        price_cache[r_id] = {
                            "back": back or prev.get("back"),
                            "last": last or prev.get("last"),
                            "vol": vol or prev.get("vol", 0),
                            "ts": now_utc.strftime("%H:%M:%S"),
                        }

            # Pass 2 — detect withdrawals
            for runner in runners:
                runner_id = runner.get("id")
                status = str(runner.get("status", "")).lower()
                is_withdrawn = runner.get("withdrawn") is True or status in (
                    "withdrawn",
                    "scratched",
                    "removed",
                )

                if not is_withdrawn or not runner_id:
                    continue
                if runner_id in seen_withdrawn_ids:
                    continue

                if DEBUG_DUMP and not _dumped:
                    print("--- RAW WITHDRAWN RUNNER PAYLOAD ---")
                    print(json.dumps(runner, indent=2)[:3000])
                    print("--- END PAYLOAD ---")
                    _dumped = True

                runner_name = runner.get("name", "Unknown Horse")
                cached = price_cache.get(runner_id, {})
                live_back, live_last, live_vol = extract_prices(runner)

                back = cached.get("back") or live_back
                last = cached.get("last") or live_last
                vol = cached.get("vol") or live_vol
                snap_ts = cached.get("ts", "n/a")

                rf_source = last or back
                if rf_source and rf_source > 1.0:
                    rf_display = f"~{(1 / rf_source) * 100:.1f}%"
                else:
                    rf_display = "N/A"

                msg = (
                    f"🚨 *NON-RUNNER DETECTED*\n\n"
                    f"🏇 *Horse:* {runner_name}\n"
                    f"📍 *Race:* {event_name}\n"
                    f"📊 *Last Matched:* `{fmt(last)}`\n"
                    f"📘 *Last Back Price:* `{fmt(back)}`\n"
                    f"💰 *Matched Volume:* `{vol:,.0f}`\n"
                    f"📉 *Est. Reduction Factor:* `{rf_display}`\n"
                    f"🕐 *Snapshot Taken:* `{snap_ts} UTC`\n"
                    f"⏰ *Race Time:* {start_str[:16].replace('T', ' ')} UTC"
                )

                print(
                    f"[{now_utc.strftime('%H:%M:%S')}] ALERT: {runner_name} @ "
                    f"{event_name} (Last: {fmt(last)}, Back: {fmt(back)})"
                )

                # Mark seen only after a confirmed send -> true one-time alert
                if send_telegram(msg):
                    seen_withdrawn_ids.add(runner_id)
                    new_alerts += 1
                    save_state()
                else:
                    print(f"Send failed for {runner_name} — will retry.")

    return new_alerts


if __name__ == "__main__":
    load_state()
    print(f"Monitor started. Polling every {POLL_SECONDS}s.")
    cycle = 0
    while True:
        try:
            check_non_runners()
            cycle += 1
            if cycle % 15 == 0:
                save_state()
        except KeyboardInterrupt:
            save_state()
            print("Stopped.")
            break
        except Exception as e:
            print(f"Loop error: {e}")
        time.sleep(POLL_SECONDS)
