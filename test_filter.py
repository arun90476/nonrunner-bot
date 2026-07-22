from datetime import datetime, timezone

# Your alert tracking set
alerted_runner_ids = set()

def test_runner_filter(event, runner):
    """Mirror of your main.py logic to test filter outcomes"""
    today_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    # 1. DATE CHECK
    start_time_iso = event.get("start", "")
    if not start_time_iso.startswith(today_utc_str):
        return False, "REJECTED: Race is not today"

    # 2. STATUS CHECK
    runner_id = runner.get("id")
    status = runner.get("status")
    if status != "withdrawn" or runner_id in alerted_runner_ids:
        return False, "REJECTED: Not withdrawn or already alerted"

    # PRICE DETERMINATION
    prices = runner.get("prices", [])
    last_price = runner.get("last-priced-decimal")
    if prices and len(prices) > 0:
        last_price = prices[0].get("decimal-odds", last_price)

    if last_price is None:
        return False, "REJECTED: Price missing"

    last_price = float(last_price)

    # 3. VALUE CHECK (Odds <= 3.33 / Market Share >= 30%)
    if last_price <= 3.33:
        alerted_runner_ids.add(runner_id)
        return True, f"ALERT TRIGGERED! (Horse: {runner.get('name')}, Price: {last_price})"
    else:
        return False, f"REJECTED: Market share < 30% (Price: {last_price} > 3.33)"


# ==========================================
# TEST CASES
# ==========================================
today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

test_cases = [
    {
        "case": "Valid Non-Runner (Today, Withdrawn, Odds 2.50)",
        "event": {"start": f"{today_date}T14:30:00Z"},
        "runner": {"id": 101, "status": "withdrawn", "last-priced-decimal": 2.50, "name": "Short Favorite"}
    },
    {
        "case": "Longshot Non-Runner (Today, Withdrawn, Odds 8.00)",
        "event": {"start": f"{today_date}T14:30:00Z"},
        "runner": {"id": 102, "status": "withdrawn", "last-priced-decimal": 8.00, "name": "Outsider"}
    },
    {
        "case": "Future Race Non-Runner (Tomorrow, Withdrawn, Odds 2.10)",
        "event": {"start": "2029-12-31T14:30:00Z"},
        "runner": {"id": 103, "status": "withdrawn", "last-priced-decimal": 2.10, "name": "Antepost Favorite"}
    },
    {
        "case": "Active Runner (Today, Open, Odds 1.90)",
        "event": {"start": f"{today_date}T14:30:00Z"},
        "runner": {"id": 104, "status": "open", "last-priced-decimal": 1.90, "name": "Running Horse"}
    }
]

print("--- RUNNING LOGIC TEST ---")
for t in test_cases:
    passed, reason = test_runner_filter(t["event"], t["runner"])
    status_icon = "✅ PASS" if passed else "🚫 FILTERED"
    print(f"[{status_icon}] {t['case']}\n   ↳ {reason}\n")
