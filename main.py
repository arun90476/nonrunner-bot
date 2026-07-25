import json
import urllib.request

TELEGRAM_BOT_TOKEN = "8949652801:AAFPYHnRXHERi4P28UFJKhqPaVd9RnuVeqI"
TELEGRAM_CHAT_ID = "8435489741"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


def tg(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk_start in range(0, len(text), 3500):
        chunk = text[chunk_start:chunk_start + 3500]
        data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": chunk}).encode()
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            print(f"TG fail: {e}", flush=True)


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def main():
    ev = get("https://api.matchbook.com/edge/rest/events"
             "?sport-ids=24735152712200&per-page=20&states=open,suspended")
    events = ev.get("events", [])
    tg(f"DIAG: {len(events)} events returned. Top keys: {list(ev.keys())}")

    # find a market
    for event in events:
        for market in event.get("markets", []) or []:
            if "win" not in str(market.get("name", "")).lower():
                continue
            eid, mid = event["id"], market["id"]
            url = (f"https://api.matchbook.com/edge/rest/events/{eid}"
                   f"/markets/{mid}/runners"
                   "?include-withdrawn=true&include-prices=true&price-depth=3")
            data = get(url)
            runners = data.get("runners", [])
            tg(f"MARKET '{market.get('name')}' | response keys: {list(data.keys())} "
               f"| {len(runners)} runners")
            if not runners:
                continue

            r0 = runners[0]
            tg(f"RUNNER KEYS: {list(r0.keys())}")
            tg(f"status={r0.get('status')!r} withdrawn={r0.get('withdrawn')!r} "
               f"prices_type={type(r0.get('prices')).__name__}")
            tg("FULL RUNNER 0:\n" + json.dumps(r0, indent=1))

            # also dump one the code THINKS is withdrawn
            for rr in runners:
                st = str(rr.get("status", "")).lower()
                if rr.get("withdrawn") is True or st in ("withdrawn", "scratched", "removed"):
                    tg("A 'WITHDRAWN' RUNNER:\n" + json.dumps(rr, indent=1))
                    break
            return

    tg("DIAG: no win market with runners found")


if __name__ == "__main__":
    main()
    print("Done — check Telegram.", flush=True)
