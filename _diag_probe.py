import requests

BASE = "https://gamma-api.polymarket.com/events/keyset"

print("--- page 1: ids and next_cursor ---")
p1 = requests.get(BASE, params={"active": "true", "closed": "false", "limit": 5}, timeout=20).json()
print("ids:", [e["id"] for e in p1["events"]])
print("next_cursor:", repr(p1.get("next_cursor")))

cursor = p1.get("next_cursor")
print("--- page 2 using after_cursor param with real next_cursor value ---")
p2 = requests.get(BASE, params={"active": "true", "closed": "false", "limit": 5, "after_cursor": cursor}, timeout=20).json()
print("ids:", [e["id"] for e in p2.get("events", [])])
print("next_cursor:", repr(p2.get("next_cursor")))
overlap = set(e["id"] for e in p1["events"]) & set(e["id"] for e in p2.get("events", []))
print("overlap between page1 and page2 ids (should be empty):", overlap)

print("--- walk forward with limit=100 (the real max) repeatedly, count the true total ---")
cursor = None
seen = set()
for i in range(60):
    params = {"active": "true", "closed": "false", "limit": 100}
    if cursor:
        params["after_cursor"] = cursor
    d = requests.get(BASE, params=params, timeout=20).json()
    events = d.get("events", [])
    new_ids = [e["id"] for e in events]
    dupes = sum(1 for i2 in new_ids if i2 in seen)
    seen.update(new_ids)
    cursor = d.get("next_cursor")
    print(f"page {i}: got {len(events)} events, dupes-vs-seen={dupes}, cumulative-unique={len(seen)}, next_cursor={cursor!r}")
    if not events or not cursor:
        print("no more pages / no cursor, stopping")
        break

print("FINAL total unique events collected:", len(seen))
