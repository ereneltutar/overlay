import json
import requests

BASE = "https://gamma-api.polymarket.com/events/keyset"
d = requests.get(BASE, params={"active": "true", "closed": "false", "limit": 3}, timeout=20).json()
ev = d["events"][0]
print("event top-level keys:", sorted(ev.keys()))
print("has endDate:", "endDate" in ev, "value:", ev.get("endDate"))
print("has markets:", "markets" in ev, "count:", len(ev.get("markets") or []))
if ev.get("markets"):
    m = ev["markets"][0]
    print("market[0] keys:", sorted(m.keys()))
    for k in ["negRisk", "acceptingOrders", "bestAsk", "liquidityNum", "id", "groupItemTitle", "question", "lastTradePrice", "volume24hr"]:
        print(f"  has {k}:", k in m)

# also specifically hunt for a negRisk multi-outcome event to confirm shape end to end
found = None
cursor = None
for _ in range(15):
    params = {"active": "true", "closed": "false", "limit": 100}
    if cursor:
        params["after_cursor"] = cursor
    page = requests.get(BASE, params=params, timeout=20).json()
    for e in page.get("events", []):
        neg = [m for m in (e.get("markets") or []) if m.get("negRisk")]
        if len(neg) >= 2:
            found = e
            break
    if found:
        break
    cursor = page.get("next_cursor")
    if not cursor:
        break

print("--- negRisk multi-outcome event found ---")
if found:
    print("event id:", found.get("id"), "title:", found.get("title"))
    print("num negRisk legs:", len([m for m in found["markets"] if m.get("negRisk")]))
    for m in found["markets"]:
        if m.get("negRisk"):
            print("  leg:", m.get("groupItemTitle"), "bestAsk:", m.get("bestAsk"), "acceptingOrders:", m.get("acceptingOrders"), "liquidityNum:", m.get("liquidityNum"), "id:", m.get("id"))
else:
    print("none found in first 1500 events scanned")
