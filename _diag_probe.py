import json
import requests

BASE = "https://gamma-api.polymarket.com"

d = requests.get(f"{BASE}/events/keyset", params={"active": "true", "closed": "false", "limit": 50}, timeout=20).json()
binary_market = None
for ev in d["events"]:
    for m in (ev.get("markets") or []):
        if not m.get("negRisk"):
            binary_market = m
            binary_event = ev
            break
    if binary_market:
        break

if not binary_market:
    print("No binary market found in first 50 events")
else:
    print("event:", binary_event.get("title"))
    print("market keys:", sorted(binary_market.keys()))
    for k in ["outcomes", "outcomePrices", "bestBid", "bestAsk", "clobTokenIds",
              "lastTradePrice", "spread", "acceptingOrders", "negRisk"]:
        print(f"  {k}: {binary_market.get(k)!r}")

    token_ids_raw = binary_market.get("clobTokenIds")
    print()
    print("--- trying CLOB order book endpoint for both tokens ---")
    try:
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
    except Exception as e:
        token_ids = None
        print("could not parse clobTokenIds:", e)
    if token_ids:
        for i, tid in enumerate(token_ids):
            try:
                r = requests.get("https://clob.polymarket.com/book", params={"token_id": tid}, timeout=15)
                print(f"token[{i}]={tid} -> HTTP {r.status_code}")
                print("  body:", r.text[:400])
            except Exception as e:
                print(f"token[{i}]={tid} -> error: {e}")
