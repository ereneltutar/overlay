import requests

BASE = "https://gamma-api.polymarket.com"
d = requests.get(f"{BASE}/events/keyset", params={"active": "true", "closed": "false", "limit": 5}, timeout=20).json()
for ev in d["events"]:
    for m in (ev.get("markets") or [])[:1]:
        print("event:", ev.get("title"))
        print("  feesEnabled:", m.get("feesEnabled"))
        print("  feeType:", m.get("feeType"))
        print("  feeSchedule:", m.get("feeSchedule"))
        print("  makerBaseFee:", m.get("makerBaseFee"))
        print("  takerBaseFee:", m.get("takerBaseFee"))
        print()
