import json
import urllib.request

BASE = "https://gamma-api.polymarket.com/events/keyset"

print("--- page 1: ids and next_cursor ---")
with urllib.request.urlopen(f"{BASE}?active=true&closed=false&limit=5") as r:
    p1 = json.loads(r.read())
print("ids:", [e["id"] for e in p1["events"]])
print("next_cursor:", repr(p1.get("next_cursor")))

cursor = p1.get("next_cursor")
print("--- page 2 using cursor param with real next_cursor value ---")
with urllib.request.urlopen(f"{BASE}?active=true&closed=false&limit=5&cursor={cursor}") as r:
    p2 = json.loads(r.read())
print("ids:", [e["id"] for e in p2.get("events", [])])
print("next_cursor:", repr(p2.get("next_cursor")))
overlap = set(e["id"] for e in p1["events"]) & set(e["id"] for e in p2.get("events", []))
print("overlap between page1 and page2 ids:", overlap)

print("--- walk forward with limit=500 repeatedly, verify no repeats and no ceiling ---")
cursor = None
seen = set()
for i in range(8):
    url = f"{BASE}?active=true&closed=false&limit=500"
    if cursor:
        url += f"&cursor={cursor}"
    with urllib.request.urlopen(url) as r:
        d = json.loads(r.read())
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
