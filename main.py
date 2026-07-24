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

# Tiered polling: fresher snapshots when scratches actually happen
POLL_NEAR = 15        # races within 1h
POLL_MID = 60         # races within 6h
POLL_FAR = 300        # everything further out
NEAR_SECONDS = 3600
MID_SECONDS = 21600

WINDOW_SECONDS = 129600     # 36h lookahead
PURGE_AFTER_SECONDS = 7200  # drop cache 2h after race start
SEEN_TTL_SECONDS = 172800   # forget alerted runners after 48h
HISTORY_LEN = 5             # price readings kept per runner
STATE_FILE = "nr_state.json"

price_cache = {}      # runner_id -> snapshot dict
seen_withdrawn = {}   # runner_id -> epoch alerted
_last_scan = {}       # market_id -> epoch last polled


# ---------- persistence ----------
def load_state():
    if not os.path.exists(STATE_FILE):
        print("No state file — cold start. Cache builds as it polls.")
        return
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        now = time.time()

        for k, v in (d.get("prices") or {}).items():
            try:
                if now - v.get("race_epoch", 0) < PURGE_AFTER_SECONDS:
                    price_cache[int(k)] = v
            except (ValueError, TypeError, AttributeError):
                continue

        raw = d.get("seen")
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    if now - float(v) < SEEN_TTL_SECONDS:
                        seen_withdrawn[int(k)] = float(v)
                except (ValueError, TypeError):
                    continue
        elif isinstance(raw, list):
            for k in raw:
                try:
                    seen_withdrawn[int(k)] = now
                except (ValueError, TypeError):
                    continue

        print(f"Loaded: {len(seen_withdrawn)} alerted, {len(price_cache)} cached.")
    except Exception as e:
        print(f"State load failed: {e}")


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"seen": seen_withdrawn, "prices": price_cache}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"State save failed: {e}")


def purge_stale():
    now = time.time()
    dead = [
        r for r, v in price_cache.items()
        if now - v.get("race_epoch", now) > PURGE_AFTER_SECONDS
    ]
    for r in dead:
        price_cache.pop(r, None)
    old = [r for r, t in seen_withdrawn.items() if now - t > SEEN_TTL_SECONDS]
    for r in old:
        seen_withdrawn.pop(r, None)
    return len(dead), len(old)


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


def is_withdrawn(runner):
    status = str(runner.get("status", "")).lower()
    return runner.get("withdrawn") is True or status in (
        "withdrawn", "scratched", "removed",
    )


def extract_book(runner):
    """Best back (highest), best lay (lowest), mid, total offered size.

    Matchbook mixes back and lay entries in one 'prices' array, so they
    must be separated by side before taking extremes.
    """
    backs, lays, size = [], [], 0.0

    for p in runner.get("prices", []) or []:
        try:
            dec = float(p.get("decimal-odds") or p.get("odds") or p.get("decimal"))
        except (TypeError, ValueError):
            continue
        if dec <= 1.0:
            continue
        try:
            amt = float(p.get("available-amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        size += amt

        side = str(p.get("side", "")).lower()
        if side == "back":
            backs.append(dec)
        elif side == "lay":
            lays.append(dec)

    best_back = max(backs) if backs else None      # best price to back at
    best_lay = min(lays) if lays else None         # best price to lay at

    if best_back and best_lay:
        mid = (best_back + best_lay) / 2
    else:
        mid = best_back or best_lay

    try:
        vol = float(runner.get("volume") or 0)
    except (TypeError, ValueError):
        vol = 0.0

    return best_back, best_lay, mid, size, vol


def fmt(v):
    return f"{v:.2f}" if v else "N/A"


def poll_interval_for(delta_seconds):
    if delta_seconds <= NEAR_SECONDS:
        return POLL_NEAR
    if delta_seconds <= MID_SECONDS:
        return POLL_MID
    return POLL_FAR


# ---------- core ----------
def scan():
    alerts = markets = stored = skipped = 0

    try:
        events = get_json(EVENTS_URL).get("events", []) or []
    except Exception as e:
        print(f"Events fetch error: {e}")
        return 0

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_epoch = time.time()

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
        race_epoch = event_dt.timestamp()
        interval = poll_interval_for(delta)

        for market in event.get("markets", []) or []:
            if "win" not in str(market.get("name", "")).lower():
                continue

            market_id = market.get("id")

            # Tiered polling — skip distant races most cycles
            if now_epoch - _last_scan.get(market_id, 0) < interval:
                skipped += 1
                continue
            _last_scan[market_id] = now_epoch

            url = (
                f"https://api.matchbook.com/edge/rest/events/{event_id}"
                f"/markets/{market_id}/runners"
                "?states=open,suspended&include-withdrawn=true"
                "&include-prices=true&price-depth=3"
            )

            try:
                runners = get_json(url).get("runners", []) or []
            except Exception as e:
                print(f"Market {market_id} error: {e}")
                continue

            markets += 1

            # ---- PASS 1: store live book for open runners ----
            for runner in runners:
                rid = runner.get("id")
                if not rid or is_withdrawn(runner):
                    continue

                back, lay, mid, size, vol = extract_book(runner)
                if not mid:
                    continue

                prev = price_cache.get(rid, {})
                hist = prev.get("history", [])
                hist.append({"t": now_utc.strftime("%H:%M:%S"), "mid": round(mid, 2)})
                hist = hist[-HISTORY_LEN:]

                price_cache[rid] = {
                    "back": back,
                    "lay": lay,
                    "mid": mid,
                    "size": size,
                    "vol": vol,
                    "ts": now_utc.strftime("%d-%b %H:%M:%S"),
                    "epoch": now_epoch,
                    "name": runner.get("name"),
                    "race": event_name,
                    "race_epoch": race_epoch,
                    "history": hist,
                }
                stored += 1

            # ---- PASS 2: withdrawal -> replay stored book, alert once ----
            for runner in runners:
                rid = runner.get("id")
                if not rid or not is_withdrawn(runner):
                    continue
                if rid in seen_withdrawn:
                    continue

                name = runner.get("name", "Unknown Horse")
                c = price_cache.get(rid, {})
                lb, ll, lm, ls, lv = extract_book(runner)

                back = c.get("back") or lb
                lay = c.get("lay") or ll
                mid = c.get("mid") or lm
                size = c.get("size") or ls
                vol = c.get("vol") or lv
                snap = c.get("ts")
                hist = c.get("history", [])

                if snap:
                    age = int((now_epoch - c.get("epoch", now_epoch)) / 60)
                    snap_line = f"🕐 *Captured:* `{snap} UTC` ({age}m before scratch)\n"
                else:
                    snap_line = (
                        "⚠️ _No stored price — withdrawn before monitoring "
                        "began._\n"
                    )

                rf = f"~{(1 / mid) * 100:.1f}%" if mid and mid > 1.0 else "N/A"

                trend = ""
                if len(hist) >= 2:
                    moves = " → ".join(str(h["mid"]) for h in hist)
                    direction = (
                        "shortening" if hist[-1]["mid"] < hist[0]["mid"]
                        else "drifting" if hist[-1]["mid"] > hist[0]["mid"]
                        else "steady"
                    )
                    trend = f"📈 *Move:* `{moves}` ({direction})\n"

                msg = (
                    f"🚨 *NON-RUNNER DETECTED*\n\n"
                    f"🏇 *Horse:* {name}\n"
                    f"📍 *Race:* {event_name}\n"
                    f"📊 *Pre-Scratch Price:* `{fmt(mid)}`\n"
                    f"📘 *Book:* back `{fmt(back)}` / lay `{fmt(lay)}`\n"
                    f"💰 *Offered Size:* `{size:,.0f}`\n"
                    f"{trend}"
                    f"📉 *Est. Reduction Factor:* `{rf}`\n"
                    f"{snap_line}"
                    f"⏰ *Race Time:* {start_str[:16].replace('T', ' ')} UTC"
                )

                print(
                    f"[{now_utc.strftime('%H:%M:%S')}] ALERT: {name} @ "
                    f"{event_name} mid={fmt(mid)} snap={snap or 'none'}"
                )

                if send_telegram(msg):
                    seen_withdrawn[rid] = now_epoch
                    alerts += 1
                    save_state()
                else:
                    print(f"Send failed for {name} — retrying next cycle.")

    pc, ps = purge_stale()
    print(
        f"[{now_utc.strftime('%H:%M:%S')}] markets={markets} skipped={skipped} "
        f"stored={stored} cache={len(price_cache)} alerts={alerts} "
        f"purged={pc}/{ps}"
    )
    return alerts


if __name__ == "__main__":
    load_state()
    print("Monitor started. Tiered polling: 15s near / 60s mid / 300s far.")
    cycle = 0
    while True:
        try:
            scan()
            cycle += 1
            if cycle % 15 == 0:
                save_state()
        except KeyboardInterrupt:
            save_state()
            print("Stopped.")
            break
        except Exception as e:
            print(f"Loop error: {e}")
        time.sleep(POLL_NEAR)
