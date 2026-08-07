import json
import requests

BASE = "https://gamma-api.polymarket.com"

found = []
cursor = None
for _ in range(20):
    params = {"active": "true", "closed": "false", "limit": 100}
    if cursor:
        params["after_cursor"] = cursor
    page = requests.get(f"{BASE}/events/keyset", params=params, timeout=20).json()
    for ev in page.get("events", []):
        for m in (ev.get("markets") or []):
            if not m.get("negRisk") and m.get("acceptingOrders") and m.get("bestAsk") is not None:
                found.append((ev, m))
    cursor = page.get("next_cursor")
    if len(found) >= 5 or not cursor:
        break

print(f"Found {len(found)} active, accepting-orders binary markets to test.\n")

for ev, m in found[:5]:
    print("=" * 60)
    print("event:", ev.get("title"))
    print("outcomes:", m.get("outcomes"))
    print("outcomePrices:", m.get("outcomePrices"))
    print("bestBid:", m.get("bestBid"), "bestAsk:", m.get("bestAsk"), "spread:", m.get("spread"))
    print("volume24hr:", m.get("volume24hr"), "liquidityNum:", m.get("liquidityNum"))

    raw_ids = m.get("clobTokenIds")
    try:
        token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
    except Exception as e:
        token_ids = None
        print("could not parse clobTokenIds:", e)

    if token_ids:
        for i, tid in enumerate(token_ids):
            label = m.get("outcomes")
            try:
                r = requests.get("https://clob.polymarket.com/book", params={"token_id": tid}, timeout=15)
                print(f"  CLOB book token[{i}] -> HTTP {r.status_code}")
                if r.status_code == 200:
                    data = r.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    print(f"    bids: {len(bids)} levels, top={bids[-1] if bids else None}")
                    print(f"    asks: {len(asks)} levels, top={asks[0] if asks else None}")
                else:
                    print("    body:", r.text[:200])
            except Exception as e:
                print(f"  CLOB book token[{i}] -> error: {e}")
    print()
