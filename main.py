import datetime
import json
import os
import sys
import time
import urllib.request

# Force logs to appear immediately on Render
sys.stdout.reconfigure(line_buffering=True)

# --- CONFIG ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8435489741")

EVENTS_URL = (
    "https://api.matchbook.com/edge/rest/events"
    "?sport-ids=24735152712200&per-page=100&states=open,suspended"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

POLL_SECONDS = 20
WINDOW_SECONDS = 43200      # 12h lookahead — tighter = every market polled often
PURGE_AFTER_SECONDS = 7200
SEEN_TTL_SECONDS = 172800
HISTORY_LEN = 5
STATE_FILE = os.environ.get("STATE_FILE", "nr_state.json")

# Suppress alerts for horses withdrawn before we ever saw them priced.
# Set False if you want those N/A alerts anyway.
SUPPRESS_UNPRICED = True

price_cache = {}
seen_withdrawn = {}
_warmed_up = False
_dumped = False


def log(msg):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# ---------- persistence (best-effort; wiped on Render restarts) ----------
def load_state():
    if not os.path.exists(STATE_FILE):
        log("No state file — cold start.")
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
        log(f"Loaded {len(seen_withdrawn)} alerted, {len(price_cache)} cached.")
    except Exception as e:
        log(f"State load failed: {e}")


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"seen": seen_withdrawn, "prices": price_cache}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"State save failed: {e}")


def purge_stale():
    now = time.time()
    for r in [r for r, v in price_cache.items()
              if now - v.get("race_epoch", now) > PURGE_AFTER_SECONDS]:
        price_cache.pop(r, None)
    for r in [r for r, t in seen_withdrawn.items()
              if now - t > SEEN_TTL_SECONDS]:
        seen_withdrawn.pop(r, None)


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
                log(f"Telegram status {resp.status}")
        except Exception as e:
            log(f"Telegram error ({attempt + 1}/3): {e}")
            time.sleep(2)
    return False


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def is_withdrawn(runner):
    status = str(runner.get("status", "")).lower()
    return runner.get("withdrawn") is True or status in (
        "withdrawn", "scratched", "removed",
    )


def extract_book(runner):
    """Best back (highest), best lay (lowest), mid, offered size, volume.

    Matchbook returns back and lay entries mixed in one array, so they
    must be split by side before taking extremes.
    """
    backs, lays, size = [], [], 0.0

    for p in runner.get("prices", []) or []:
        dec = None
        for key in ("decimal-odds", "decimalOdds", "odds", "decimal"):
            v = p.get(key)
            if v is None:
                continue
            try:
                dec = float(v)
                break
            except (TypeError, ValueError):
                continue
        if not dec or dec <= 1.0:
            continue

        try:
            size += float(p.get("available-amount") or p.get("availableAmount") or 0)
        except (TypeError, ValueError):
            pass

        side = str(p.get("side", "")).lower()
        if side == "back":
            backs.append(dec)
        elif side == "lay":
            lays.append(dec)

    best_back = max(backs) if backs else None
    best_lay = min(lays) if lays else None
    mid = (best_back + best_lay) / 2 if (best_back and best_lay) else (best_back or best_lay)

    try:
        vol = float(runner.get("volume") or 0)
    except (TypeError, ValueError):
        vol = 0.0

    return best_back, best_lay, mid, size, vol


def fmt(v):
    return f"{v:.2f}" if v else "N/A"


# ---------- core ----------
def scan(alerting=True):
    global _dumped
    alerts = markets = stored = withdrawn_seen = 0

    try:
        events = get_json(EVENTS_URL).get("events", []) or []
    except Exception as e:
        log(f"Events fetch error: {e}")
        return 0

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_epoch = time.time()

    for event in events:
        start_str = event.get("start")
        if not start_str:
            continue
        try:
            event_dt = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        delta = (event_dt - now_utc).total_seconds()
        if not (0 <= delta <= WINDOW_SECONDS):
            continue

        event_id = event.get("id")
        event_name = event.get("name", "Unknown Race")
        race_epoch = event_dt.timestamp()

        for market in event.get("markets", []) or []:
            if "win" not in str(market.get("name", "")).lower():
                continue

            market_id = market.get("id")
            url = (
                f"https://api.matchbook.com/edge/rest/events/{event_id}"
                f"/markets/{market_id}/runners"
                "?states=open,suspended&include-withdrawn=true"
                "&include-prices=true&price-depth=3"
            )

            try:
                runners = get_json(url).get("runners", []) or []
            except Exception as e:
                log(f"Market {market_id} error: {e}")
                continue

            markets += 1

            # One-time raw dump so field names can be verified from logs
            if not _dumped and runners:
                log("--- RAW RUNNER (first seen) ---")
                print(json.dumps(runners[0], indent=2)[:2000], flush=True)
                log("--- END ---")
                _dumped = True

            # PASS 1 — store live book for open runners
            for runner in runners:
                rid = runner.get("id")
                if not rid or is_withdrawn(runner):
                    continue

                back, lay, mid, size, vol = extract_book(runner)
                if not mid:
                    continue

                prev = price_cache.get(rid, {})
                hist = prev.get("history", [])
                hist.append({"t": now_utc.strftime("%H:%M"), "mid": round(mid, 2)})

                price_cache[rid] = {
                    "back": back, "lay": lay, "mid": mid,
                    "size": size, "vol": vol,
                    "ts": now_utc.strftime("%d-%b %H:%M:%S"),
                    "epoch": now_epoch,
                    "name": runner.get("name"),
                    "race": event_name,
                    "race_epoch": race_epoch,
                    "history": hist[-HISTORY_LEN:],
                }
                stored += 1

            # PASS 2 — withdrawals
            for runner in runners:
                rid = runner.get("id")
                if not rid or not is_withdrawn(runner):
                    continue
                withdrawn_seen += 1

                if rid in seen_withdrawn:
                    continue

                cached = price_cache.get(rid)

                # Warm-up pass: record without alerting, so pre-existing
                # withdrawals don't spam on every restart
                if not alerting:
                    seen_withdrawn[rid] = now_epoch
                    continue

                if not cached and SUPPRESS_UNPRICED:
                    seen_withdrawn[rid] = now_epoch
                    log(f"Skipped (no price): {runner.get('name')} @ {event_name}")
                    continue

                c = cached or {}
                lb, ll, lm, ls, lv = extract_book(runner)
                back = c.get("back") or lb
                lay = c.get("lay") or ll
                mid = c.get("mid") or lm
                size = c.get("size") or ls
                snap = c.get("ts")
                hist = c.get("history", [])
                name = runner.get("name", "Unknown Horse")

                if snap:
                    age = int((now_epoch - c.get("epoch", now_epoch)) / 60)
                    snap_line = f"🕐 *Captured:* `{snap} UTC` ({age}m before scratch)\n"
                else:
                    snap_line = "⚠️ _No stored price._\n"

                rf = f"~{(1 / mid) * 100:.1f}%" if mid and mid > 1.0 else "N/A"

                trend = ""
                if len(hist) >= 2:
                    moves = " → ".join(str(h["mid"]) for h in hist)
                    direction = ("shortening" if hist[-1]["mid"] < hist[0]["mid"]
                                 else "drifting" if hist[-1]["mid"] > hist[0]["mid"]
                                 else "steady")
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

                log(f"ALERT: {name} @ {event_name} mid={fmt(mid)} snap={snap or 'none'}")

                if send_telegram(msg):
                    seen_withdrawn[rid] = now_epoch
                    alerts += 1
                    save_state()

    purge_stale()
    log(f"markets={markets} stored={stored} cache={len(price_cache)} "
        f"withdrawn={withdrawn_seen} alerts={alerts}")
    return alerts


if __name__ == "__main__":
    log("=== NON-RUNNER MONITOR STARTING ===")
    load_state()

    # Warm-up: build cache and mark existing withdrawals as already-seen,
    # so a restart doesn't fire N/A alerts for overnight non-runners.
    log("Warm-up scan (no alerts)...")
    scan(alerting=False)
    save_state()
    log(f"Warm-up done. Cache: {len(price_cache)} runners. Alerting now live.")

    cycle = 0
    while True:
        try:
            scan(alerting=True)
            cycle += 1
            if cycle % 15 == 0:
                save_state()
        except KeyboardInterrupt:
            save_state()
            log("Stopped.")
            break
        except Exception as e:
            log(f"Loop error: {e}")
        time.sleep(POLL_SECONDS)
