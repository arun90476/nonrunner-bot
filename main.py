import datetime
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

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

POLL_SECONDS = 15
PURGE_AFTER_SECONDS = 7200
SEEN_TTL_SECONDS = 172800
HISTORY_LEN = 5
STATE_FILE = os.environ.get("STATE_FILE", "nr_state.json")

# --- ALERT FILTERS (all must pass for a notification) ---
MAX_ODDS = 5.0          # only alert if pre-scratch mid <= this
REQUIRE_VOLUME = True   # only alert if matched volume > 0
UK_TZ = ZoneInfo("Europe/London")  # today's UK card only (BST-aware)

price_cache = {}
seen_withdrawn = {}
market_roster = {}


def log(msg):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def err(msg, exc=None):
    log(f"[ERR] {msg}")
    if exc is not None:
        print(traceback.format_exc(), flush=True)


def is_today_uk(event_dt_utc):
    """True if the race's start, in UK local time, falls on today's UK date."""
    now_uk = datetime.datetime.now(UK_TZ)
    race_uk = event_dt_utc.astimezone(UK_TZ)
    return race_uk.date() == now_uk.date()


def passes_filters(cached):
    """All alert conditions. Returns (ok, reason_if_not)."""
    if not cached:
        return False, "no cached price"
    mid = cached.get("mid")
    vol = cached.get("vol") or 0
    if not mid or mid <= 1.0:
        return False, "no usable price"
    if mid > MAX_ODDS:
        return False, f"odds {mid:.2f} > {MAX_ODDS}"
    if REQUIRE_VOLUME and vol <= 0:
        return False, "matched volume is 0"
    return True, ""


# ---------- persistence ----------
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
            except Exception:
                continue
        raw = d.get("seen")
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    if now - float(v) < SEEN_TTL_SECONDS:
                        seen_withdrawn[int(k)] = float(v)
                except Exception:
                    continue
        log(f"Loaded {len(seen_withdrawn)} alerted, {len(price_cache)} cached.")
    except Exception as e:
        err(f"State load failed: {e}", e)


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"seen": seen_withdrawn, "prices": price_cache}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        err(f"State save failed: {e}", e)


def purge_stale():
    now = time.time()
    for r in [r for r, v in price_cache.items()
              if now - v.get("race_epoch", now) > PURGE_AFTER_SECONDS]:
        price_cache.pop(r, None)
    for r in [r for r, t in seen_withdrawn.items()
              if now - t > SEEN_TTL_SECONDS]:
        seen_withdrawn.pop(r, None)


# ---------- network ----------
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
                err(f"Telegram HTTP {resp.status}")
        except Exception as e:
            err(f"Telegram error ({attempt + 1}/3): {e}")
            time.sleep(2)
    return False


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- parsing ----------
def is_withdrawn(runner):
    status = str(runner.get("status", "")).lower()
    wd = runner.get("withdrawn")
    if wd is True or (isinstance(wd, str) and wd.lower() == "true"):
        return True
    return status in ("withdrawn", "scratched", "removed", "non-runner", "nonrunner")


def extract_book(runner):
    """Binary exchange: side 'win' = back the horse, side 'lose' = lay it."""
    win_prices, lose_prices, size = [], [], 0.0

    for p in runner.get("prices", []) or []:
        if not isinstance(p, dict):
            continue
        try:
            dec = float(p.get("decimal-odds") or p.get("odds"))
        except (TypeError, ValueError):
            continue
        if not dec or dec <= 1.0:
            continue
        side = str(p.get("side", "")).lower()
        if side in ("win", "back"):
            win_prices.append(dec)
        elif side in ("lose", "lay"):
            lose_prices.append(dec)

    best_back = max(win_prices) if win_prices else None

    best_lay = None
    if lose_prices:
        best_lose = max(lose_prices)
        if best_lose > 1.0:
            best_lay = best_lose / (best_lose - 1.0)

    if best_back and best_lay:
        mid = (best_back + best_lay) / 2
    else:
        mid = best_back or best_lay

    try:
        size = float(runner.get("volume") or 0)
    except (TypeError, ValueError):
        size = 0.0

    return best_back, best_lay, mid, size


def fmt(v):
    return f"{v:.2f}" if v else "N/A"


def build_alert(name, race, race_time, cached, live=None, reason="withdrawn"):
    c = cached or {}
    lb = ll = lm = ls = None
    if live is not None:
        lb, ll, lm, ls = extract_book(live)
    back = c.get("back") or lb
    lay = c.get("lay") or ll
    mid = c.get("mid") or lm
    vol = c.get("vol") or ls or 0
    snap = c.get("ts")
    hist = c.get("history", [])

    if snap:
        age = int((time.time() - c.get("epoch", time.time())) / 60)
        snap_line = f"🕐 *Captured:* `{snap} UTC` ({age}m before scratch)\n"
    else:
        snap_line = "⚠️ _No stored price for this runner._\n"

    rf = f"~{(1 / mid) * 100:.1f}%" if mid and mid > 1.0 else "N/A"

    trend = ""
    if len(hist) >= 2:
        moves = " → ".join(str(h["mid"]) for h in hist)
        direction = ("shortening" if hist[-1]["mid"] < hist[0]["mid"]
                     else "drifting" if hist[-1]["mid"] > hist[0]["mid"] else "steady")
        trend = f"📈 *Move:* `{moves}` ({direction})\n"

    tag = "NON-RUNNER DETECTED" if reason == "withdrawn" else "RUNNER REMOVED FROM MARKET"
    return (
        f"🚨 *{tag}*\n\n"
        f"🏇 *Horse:* {name}\n"
        f"📍 *Race:* {race}\n"
        f"📊 *Pre-Scratch Price:* `{fmt(mid)}`\n"
        f"📘 *Back:* `{fmt(back)}` / *Lay-equiv:* `{fmt(lay)}`\n"
        f"💰 *Matched Volume:* `{vol:,.0f}`\n"
        f"{trend}"
        f"📉 *Est. Reduction Factor:* `{rf}`\n"
        f"{snap_line}"
        f"⏰ *Race Time:* {race_time} UTC"
    )


# ---------- core ----------
def scan(warmup=False):
    alerts = markets = stored = withdrawn_seen = vanished = filtered = 0

    try:
        events = get_json(EVENTS_URL).get("events", []) or []
    except Exception as e:
        err(f"Events fetch failed: {e}", e)
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

        # FILTER 1: today's UK card only, and not already started
        if event_dt <= now_utc or not is_today_uk(event_dt):
            continue

        event_id = event.get("id")
        event_name = event.get("name", "Unknown Race")
        race_epoch = event_dt.timestamp()
        race_time = start_str[:16].replace("T", " ")

        for market in event.get("markets", []) or []:
            if "win" not in str(market.get("name", "")).lower():
                continue

            market_id = market.get("id")
            url = (
                f"https://api.matchbook.com/edge/rest/events/{event_id}"
                f"/markets/{market_id}/runners"
                "?include-withdrawn=true&include-prices=true&price-depth=3"
            )

            try:
                rdata = get_json(url)
            except Exception as e:
                err(f"Market {market_id} fetch failed: {e}")
                continue

            runners = rdata.get("runners", []) or []
            if not runners:
                continue

            markets += 1
            current_ids = set()

            # PASS 1 — cache prices
            for runner in runners:
                rid = runner.get("id")
                if not rid:
                    continue
                current_ids.add(rid)
                if is_withdrawn(runner):
                    continue
                back, lay, mid, vol = extract_book(runner)
                if not mid:
                    continue
                prev = price_cache.get(rid, {})
                hist = prev.get("history", [])
                hist.append({"t": now_utc.strftime("%H:%M"), "mid": round(mid, 2)})
                price_cache[rid] = {
                    "back": back, "lay": lay, "mid": mid, "vol": vol,
                    "ts": now_utc.strftime("%d-%b %H:%M:%S"),
                    "epoch": now_epoch, "name": runner.get("name"),
                    "race": event_name, "race_time": race_time,
                    "race_epoch": race_epoch, "history": hist[-HISTORY_LEN:],
                }
                stored += 1

            # PASS 2 — disappearance
            prev_roster = market_roster.get(market_id)
            if prev_roster and len(runners) >= max(2, len(prev_roster) - 3):
                for gone_id, info in prev_roster.items():
                    if gone_id in current_ids or gone_id in seen_withdrawn:
                        continue
                    vanished += 1
                    if warmup:
                        seen_withdrawn[gone_id] = now_epoch
                        continue
                    cached = price_cache.get(gone_id)
                    ok, why = passes_filters(cached)
                    if not ok:
                        seen_withdrawn[gone_id] = now_epoch
                        filtered += 1
                        log(f"FILTERED (vanished): {info['name']} @ "
                            f"{info['race']} — {why}")
                        continue
                    msg = build_alert(info["name"], info["race"],
                                      info["race_time"], cached, None,
                                      reason="vanished")
                    if send_telegram(msg):
                        seen_withdrawn[gone_id] = now_epoch
                        alerts += 1
                        save_state()

            market_roster[market_id] = {
                r.get("id"): {"name": r.get("name", "Unknown"),
                              "race": event_name, "race_time": race_time}
                for r in runners if r.get("id") and not is_withdrawn(r)
            }

            # PASS 3 — explicit withdrawals
            for runner in runners:
                rid = runner.get("id")
                if not rid or not is_withdrawn(runner):
                    continue
                withdrawn_seen += 1
                if rid in seen_withdrawn:
                    continue
                name = runner.get("name", "Unknown Horse")
                if warmup:
                    seen_withdrawn[rid] = now_epoch
                    continue

                # FILTERS 2 + 3: odds <= MAX_ODDS AND volume > 0
                cached = price_cache.get(rid)
                ok, why = passes_filters(cached)
                if not ok:
                    seen_withdrawn[rid] = now_epoch
                    filtered += 1
                    log(f"FILTERED: {name} @ {event_name} — {why}")
                    continue

                msg = build_alert(name, event_name, race_time, cached, runner)
                log(f"ALERT: {name} @ {event_name} mid={cached['mid']:.2f} "
                    f"vol={cached.get('vol', 0):.0f}")
                if send_telegram(msg):
                    seen_withdrawn[rid] = now_epoch
                    alerts += 1
                    save_state()

    purge_stale()
    log(f"markets={markets} stored={stored} cache={len(price_cache)} "
        f"withdrawn={withdrawn_seen} vanished={vanished} "
        f"filtered={filtered} alerts={alerts}")
    return alerts


if __name__ == "__main__":
    log("=== NON-RUNNER MONITOR STARTING ===")
    log(f"Filters: today's UK card only | odds <= {MAX_ODDS} | "
        f"volume > 0 required: {REQUIRE_VOLUME}")
    load_state()
    log("Warm-up scan...")
    try:
        scan(warmup=True)
    except Exception as e:
        err(f"Warm-up failed: {e}", e)
    save_state()
    log(f"Warm-up done. Cache={len(price_cache)}. Alerting live.")

    cycle = 0
    while True:
        try:
            scan(warmup=False)
            cycle += 1
            if cycle % 20 == 0:
                save_state()
        except KeyboardInterrupt:
            save_state()
            log("Stopped.")
            break
        except Exception as e:
            err(f"Loop error: {e}", e)
        time.sleep(POLL_SECONDS)
