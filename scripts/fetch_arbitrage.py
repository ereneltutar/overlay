#!/usr/bin/env python3
"""
Polymarket Arbitrage Watch
---------------------------
Runs automatically every morning. Pulls open (not yet closed) events from
the Polymarket Gamma API, filters to ones with a deadline inside a given
window, and looks for opportunities with two separate methods:

1) RISKLESS ARBITRAGE (in negRisk groups):
   If an event has N mutually exclusive options that cover every outcome
   (for example, "Who wins this race?"), and you buy the "Yes" side of
   every option at its current best ask price, exactly ONE of them
   resolves, so you're guaranteed a $1 payout.
   If the sum of all those ask prices is under $1, the gap is a
   theoretical riskless profit (in practice, fees, slippage, thin
   liquidity, and execution risk can shrink or erase that gap).

   This script only scans "negRisk" groups. For plain binary Yes/No
   markets, the real ask price for the NO side doesn't come back as a
   separate field from the Gamma API, and the CLOB order book endpoint has
   a known stale-data problem, so binary markets were deliberately left
   out of v1.

2) CALIBRATION DRIFT (statistical, NOT riskless):
   scripts/calibration_scan.py (a separate weekly job) reads the
   docs/price_log.jsonl file this script writes below, matches markets
   that have since closed against their real outcome, and measures
   whether there's a systematic gap (favorite-longshot bias) between
   "market price" and "actual resolved rate," writing the result to
   docs/calibration.json.
   This script reads that table and compares TODAY's open markets against
   it: any market whose price falls in a range with a historically
   significant gap gets flagged as a "calibration_signal." This is NOT a
   guarantee for any single bet. It's a statistical tendency that pulls
   expected value (+EV) in your favor across many repeated positions.

   WHY WE DON'T LOOK BACKWARD: Polymarket's /prices-history endpoint
   returned empty data for 98% of markets in a live test (379 markets,
   372 failures). That's a known limitation of Polymarket's own
   infrastructure, not a fluke (it's also why an independent third-party
   data service exists to sell historical data). So instead of looking
   BACKWARD, we look FORWARD: the log_price_snapshot() function below
   appends the current price of every market roughly
   CALIBRATION_LOG_LEAD_DAYS days from closing to docs/price_log.jsonl
   (append-only). Weeks later, once those markets close,
   calibration_scan.py matches the price we logged ourselves against the
   real outcome, with no need for Polymarket's unreliable historical-data
   endpoint at all.

Output: docs/results.json (the static dashboard reads this file)
        docs/price_log.jsonl (forward-looking calibration archive, append-only)
"""

import datetime
import json
import math
import sys
import time
from pathlib import Path

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"

# --- Tunable parameters -----------------------------------------------
DAYS_AHEAD = 30          # scan events whose deadline is at most this many days out
MIN_EDGE_PCT = 0.5       # don't show "opportunities" below this percent (noise filter)
MIN_LIQUIDITY_USD = 50   # require at least this much liquidity per leg (drop thin books)
PAGE_LIMIT = 500
MAX_PAGES = 60           # safety cap
REQUEST_TIMEOUT = 30

# For the momentum / volume-anomaly scan:
MOMENTUM_MIN_VOLUME_24H = 200     # skip markets with less than this much daily volume (noise)
MOMENTUM_VOLUME_MULTIPLIER = 3.0  # last-24h volume must be at least this many times the trailing weekly daily average
MOMENTUM_MIN_PRICE_MOVE = 0.05    # last-24h (or last-1h) price move must be at least this many points (0.05 = 5 points)
MAX_MOMENTUM_SIGNALS = 15         # max signals shown on the dashboard

# For the calibration-drift (favorite-longshot bias) cross-check:
MIN_CALIBRATION_EDGE_PCT = 2.0    # don't show a calibration gap below this percent (noise)
MIN_CALIBRATION_LIQUIDITY_USD = 100  # drop markets below this liquidity
MAX_CALIBRATION_SIGNALS = 20      # max signals shown on the dashboard

# For forward-looking calibration LOGGING (the archive calibration_scan.py reads):
# WARNING: CALIBRATION_LOG_LEAD_DAYS represents the same concept as LEAD_DAYS in
# scripts/calibration_scan.py (both answer "how many days before closing").
# If you change one, review the other; there's no shared code linking them.
CALIBRATION_LOG_LEAD_DAYS = 7
CALIBRATION_LOG_TOLERANCE_DAYS = 0.6  # slack so a once-daily cron doesn't miss this window
CALIBRATION_LOG_MIN_LIQUIDITY = 50
# ----------------------------------------------------------------------------

# --- THIRD, INDEPENDENT SCAN: "Mispricing" (implied vs fair probability) --------
# HOW THIS DIFFERS FROM find_calibration_signal(): that function only shows a
# signal if it's statistically SIGNIFICANT (the Wilson interval excludes the
# bucket midpoint). This scan ignores the significance requirement and looks
# directly at the point gap (>= MISPRICING_MIN_EDGE_PTS), adds a 24-hour volume
# filter, and combines everything into a single score to produce a daily
# "Top N" list. It's a SEPARATE function (find_mispricing_signal); not a single
# line of find_calibration_signal changed.
#
# WHERE "FAIR PROBABILITY" COMES FROM: the repo currently has no independent
# per-market probability model or forecasting source. So this scan uses the
# same bucket data as CAL (docs/calibration.json -> bucket["resolved_yes_rate"],
# i.e. how often markets in that price range have historically resolved YES) as
# its "fair probability" too; only the threshold/filter/scoring logic differs.
# Sample size can still be small for a given bucket (see the low_sample_warning
# field); this isn't a significance test, just a "read carefully" flag.
MISPRICING_MIN_EDGE_PTS = 15.0          # min point gap between implied and the bucket's historical rate
MISPRICING_MIN_VOLUME_24H = 5000        # skip markets below this 24h volume (tradability)
MISPRICING_HORIZON_DAYS = 30            # markets closing within this many days get the "priority" window
MISPRICING_LONGTERM_MIN_EDGE_PTS = 25   # markets beyond HORIZON_DAYS only qualify above this edge
MISPRICING_LOW_SAMPLE_WARNING_N = 30    # below this bucket sample size, set low_sample_warning=True
MAX_MISPRICING_SIGNALS = 20             # daily Top N shown on the dashboard
# ----------------------------------------------------------------------------

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "results.json"
CALIBRATION_PATH = Path(__file__).resolve().parent.parent / "docs" / "calibration.json"
PRICE_LOG_PATH = Path(__file__).resolve().parent.parent / "docs" / "price_log.jsonl"


def fetch_all_events() -> list:
    """Pages through the Gamma API /events endpoint and pulls every active,
    not-yet-closed event.

    Note: the API can return fewer items per page than the requested "limit"
    (the server may enforce its own cap). So we keep paging until an empty
    page comes back, and advance the offset by the actual count received,
    rather than treating "received < requested limit" as "no more data."

    Also: the Gamma API /events endpoint rejects requests past a certain
    offset (~2100 in a live test) with a 422 (Unprocessable Entity). There's
    an undocumented pagination ceiling. This error USED TO leak out of the
    function and lose every event collected up to that point (main() would
    carry on with an empty list). Now this error only STOPS pagination;
    events successfully collected up to that point are returned intact.
    """
    events = []
    offset = 0
    for _ in range(MAX_PAGES):
        params = {
            "active": "true",
            "closed": "false",
            "limit": PAGE_LIMIT,
            "offset": offset,
        }
        try:
            resp = requests.get(f"{GAMMA_BASE}/events", params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            batch = resp.json()
        except requests.RequestException as exc:
            print(f"Pagination stopped at offset={offset} (API error: {exc}). "
                  f"Using the {len(events)} events collected so far.", file=sys.stderr)
            break
        if not isinstance(batch, list) or not batch:
            break
        events.extend(batch)
        offset += len(batch)
        time.sleep(0.2)  # be polite to the API
    return events


def parse_iso(date_str):
    if not date_str:
        return None
    try:
        return datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def find_opportunity(event: dict, now: datetime.datetime):
    """Returns a dict if the event has a negRisk arbitrage opportunity, else None."""
    markets = event.get("markets") or []
    neg_risk_markets = [
        m for m in markets
        if m.get("negRisk")
        and m.get("acceptingOrders")
        and m.get("bestAsk") is not None
    ]
    if len(neg_risk_markets) < 2:
        return None

    legs = []
    total_ask = 0.0
    for m in neg_risk_markets:
        try:
            ask = float(m["bestAsk"])
        except (TypeError, ValueError):
            return None
        if ask <= 0 or ask >= 1:
            return None
        liquidity = float(m.get("liquidityNum") or 0)
        total_ask += ask
        legs.append({
            "outcome": m.get("groupItemTitle") or m.get("question") or "?",
            "ask": round(ask, 4),
            "liquidity": round(liquidity, 2),
            "market_id": m.get("id"),
        })

    if total_ask <= 0:
        return None

    edge_pct = (1 - total_ask) / total_ask * 100
    if edge_pct < MIN_EDGE_PCT:
        return None

    min_liquidity = min(leg["liquidity"] for leg in legs)
    if min_liquidity < MIN_LIQUIDITY_USD:
        return None

    end_date = parse_iso(event.get("endDate"))
    days_left = (end_date - now).days if end_date else None

    return {
        "event_title": event.get("title") or event.get("ticker") or "Unknown event",
        "slug": event.get("slug"),
        "url": f"https://polymarket.com/event/{event.get('slug')}" if event.get("slug") else None,
        "end_date": event.get("endDate"),
        "days_left": days_left,
        "num_outcomes": len(legs),
        "total_cost": round(total_ask, 4),
        "edge_pct": round(edge_pct, 2),
        "min_outcome_liquidity": round(min_liquidity, 2),
        "legs": sorted(legs, key=lambda x: x["ask"]),
    }


def load_calibration():
    """Reads docs/calibration.json. Returns None if no calibration scan has run
    yet (file doesn't exist); this should never crash the daily script, it just
    leaves calibration signals empty."""
    if not CALIBRATION_PATH.exists():
        return None
    try:
        return json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def find_bin(price: float, bins: list):
    """Finds the calibration bucket a given price falls into."""
    for b in bins:
        lo, hi = b["range"]
        if lo <= price < hi or (hi >= 1.0 and price == 1.0):
            return b
    return None


def find_calibration_signal(market: dict, event: dict, bins: list, now: datetime.datetime):
    """Returns a signal if a market's price falls into a bucket with a
    historically statistically significant calibration gap, else None.

    IMPORTANT: this is NOT riskless. It doesn't guarantee a win on any single
    bet; it's a statistical tendency assumed to pull expected value (+EV) in
    your favor across many repeated, INDEPENDENT positions."""
    try:
        price = float(market.get("lastTradePrice") or market.get("bestAsk") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or price >= 1:
        return None

    liquidity = float(market.get("liquidityNum") or 0)
    if liquidity < MIN_CALIBRATION_LIQUIDITY_USD:
        return None

    bucket = find_bin(price, bins)
    if not bucket or not bucket.get("significant"):
        return None

    bias_pct = bucket["bias_pct"]
    actual_rate = bucket["resolved_yes_rate"]

    if bias_pct > 0:
        side, cost, true_rate = "YES", price, actual_rate
    else:
        side, cost, true_rate = "NO", 1 - price, 1 - actual_rate

    if cost <= 0:
        return None

    edge_pct = (true_rate - cost) / cost * 100
    if edge_pct < MIN_CALIBRATION_EDGE_PCT:
        return None

    end_date = parse_iso(event.get("endDate"))
    days_left = (end_date - now).days if end_date else None

    return {
        "market_id": market.get("id"),
        "market_question": market.get("question") or event.get("title") or "?",
        "slug": event.get("slug"),
        "url": f"https://polymarket.com/event/{event.get('slug')}" if event.get("slug") else None,
        "days_left": days_left,
        "recommended_side": side,
        "current_price": round(price, 4),
        "implied_cost": round(cost, 4),
        "bucket_range": bucket["range"],
        "bucket_sample_size": bucket["sample_size"],
        "bucket_historical_rate": round(actual_rate, 4),
        "edge_pct": round(edge_pct, 2),
        "liquidity": round(liquidity, 2),
    }


def find_mispricing_signal(market: dict, event: dict, bins: list, now: datetime.datetime):
    """Looks at the gap between the implied probability (the market's current
    price) and that price bucket's PAST resolution rate (a proxy for "fair
    probability").

    HOW THIS DIFFERS FROM find_calibration_signal():
      - no statistical-significance requirement (Wilson interval); looks
        directly at the point gap
      - has a 24-hour volume filter (tradability, not liquidity)
      - markets beyond HORIZON_DAYS are only included at large edges
        (>LONGTERM_MIN_EDGE_PTS)
      - produces a comparable 'score' instead of a single signal

    Reads the SAME bucket table (docs/calibration.json) but never calls or
    modifies find_calibration_signal; it's a fully independent function."""
    try:
        implied_prob = float(market.get("lastTradePrice") or market.get("bestAsk") or 0)
    except (TypeError, ValueError):
        return None
    if implied_prob <= 0 or implied_prob >= 1:
        return None

    try:
        volume_24h = float(market.get("volume24hr") or 0)
    except (TypeError, ValueError):
        return None
    if volume_24h < MISPRICING_MIN_VOLUME_24H:
        return None

    end_date = parse_iso(event.get("endDate"))
    days_left = (end_date - now).total_seconds() / 86400 if end_date else None

    bucket = find_bin(implied_prob, bins)
    if not bucket or bucket.get("resolved_yes_rate") is None:
        return None  # no usable historical rate for this bucket yet (n < MIN_SAMPLE_PER_BUCKET)
    fair_prob = bucket["resolved_yes_rate"]

    edge_pts = abs(fair_prob - implied_prob) * 100
    if edge_pts < MISPRICING_MIN_EDGE_PTS:
        return None

    # Don't drop markets past 30 days entirely, just the small-edge ones.
    if days_left is not None and days_left > MISPRICING_HORIZON_DAYS \
            and edge_pts <= MISPRICING_LONGTERM_MIN_EDGE_PTS:
        return None

    side = "YES" if fair_prob > implied_prob else "NO"
    liquidity = float(market.get("liquidityNum") or 0)

    # Floor so the score doesn't blow up when days left is near 0 (or unknown).
    score_days = max(days_left, 0.5) if days_left is not None else 0.5
    score = edge_pts * math.sqrt(volume_24h) / score_days

    return {
        "market_id": market.get("id"),
        "market_question": market.get("question") or event.get("title") or "?",
        "slug": event.get("slug"),
        "url": f"https://polymarket.com/event/{event.get('slug')}" if event.get("slug") else None,
        "days_left": round(days_left, 1) if days_left is not None else None,
        "recommended_side": side,
        "implied_probability": round(implied_prob, 4),
        "fair_probability": round(fair_prob, 4),
        "edge_pct": round(edge_pts, 2),          # same field name as the other tags (for sorting/display)
        "volume_24h": round(volume_24h, 2),
        "liquidity": round(liquidity, 2),
        "bucket_range": bucket["range"],
        "bucket_sample_size": bucket["sample_size"],
        "low_sample_warning": bucket["sample_size"] < MISPRICING_LOW_SAMPLE_WARNING_N,
        "score": round(score, 2),
    }


def log_price_snapshot(events: list, now: datetime.datetime) -> int:
    """Appends the current price of every market roughly
    CALIBRATION_LOG_LEAD_DAYS days from closing to docs/price_log.jsonl
    (append-only, never modifies or deletes existing rows). Weeks later, once
    those markets close, calibration_scan.py matches the price logged here
    against the real outcome. Reads existing market_ids from the file first so
    the same market never gets logged twice. Returns the number of new rows
    successfully appended."""
    existing_ids = set()
    if PRICE_LOG_PATH.exists():
        with PRICE_LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    existing_ids.add(json.loads(line)["market_id"])
                except (json.JSONDecodeError, KeyError):
                    continue

    new_entries = []
    for event in events:
        end_date = parse_iso(event.get("endDate"))
        if not end_date:
            continue
        days_until_close = (end_date - now).total_seconds() / 86400
        lo = CALIBRATION_LOG_LEAD_DAYS - CALIBRATION_LOG_TOLERANCE_DAYS
        hi = CALIBRATION_LOG_LEAD_DAYS + CALIBRATION_LOG_TOLERANCE_DAYS
        if not (lo <= days_until_close <= hi):
            continue

        for market in (event.get("markets") or []):
            market_id = market.get("id")
            if not market_id or market_id in existing_ids:
                continue
            try:
                price = float(market.get("lastTradePrice") or market.get("bestAsk") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or price >= 1:
                continue
            liquidity = float(market.get("liquidityNum") or 0)
            if liquidity < CALIBRATION_LOG_MIN_LIQUIDITY:
                continue

            new_entries.append({
                "market_id": market_id,
                "question": market.get("question") or event.get("title") or "?",
                "slug": event.get("slug"),
                "logged_at": now.isoformat(),
                "end_date": event.get("endDate"),
                "price": round(price, 4),
                "liquidity": round(liquidity, 2),
            })
            existing_ids.add(market_id)

    if new_entries:
        with PRICE_LOG_PATH.open("a", encoding="utf-8") as f:
            for entry in new_entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return len(new_entries)


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now + datetime.timedelta(days=DAYS_AHEAD)

    try:
        events = fetch_all_events()
    except requests.RequestException as exc:
        print(f"Polymarket API error: {exc}", file=sys.stderr)
        events = []

    calibration = load_calibration()
    calibration_bins = calibration["bins"] if calibration else None

    opportunities = []
    calibration_signals = []

    for event in events:
        end_date = parse_iso(event.get("endDate"))
        if not end_date or end_date < now or end_date > cutoff:
            continue

        opp = find_opportunity(event, now)
        if opp:
            opportunities.append(opp)

        if calibration_bins:
            for market in (event.get("markets") or []):
                sig = find_calibration_signal(market, event, calibration_bins, now)
                if sig:
                    calibration_signals.append(sig)

    opportunities.sort(key=lambda o: o["edge_pct"], reverse=True)
    calibration_signals.sort(key=lambda s: s["edge_pct"], reverse=True)
    calibration_signals = calibration_signals[:MAX_CALIBRATION_SIGNALS]

    # --- Mispricing scan: a separate, independent pass (doesn't touch the
    # ARB/CAL loop above). It has its own date window: it doesn't inherit the
    # DAYS_AHEAD cutoff, because markets with a large edge (>25 points) need
    # to be checked even past 30 days out, so find_mispricing_signal() applies
    # its own day logic internally.
    mispricing_signals = []
    if calibration_bins:
        for event in events:
            end_date = parse_iso(event.get("endDate"))
            if not end_date or end_date < now:
                continue  # closed / date unknown event
            for market in (event.get("markets") or []):
                sig = find_mispricing_signal(market, event, calibration_bins, now)
                if sig:
                    mispricing_signals.append(sig)

        mispricing_signals.sort(key=lambda s: s["score"], reverse=True)
        mispricing_signals = mispricing_signals[:MAX_MISPRICING_SIGNALS]

    new_log_entries = log_price_snapshot(events, now)

    output = {
        "generated_at": now.isoformat(),
        "days_ahead_filter": DAYS_AHEAD,
        "min_edge_pct_filter": MIN_EDGE_PCT,
        "scanned_events": len(events),
        "opportunities": opportunities,
        "calibration_signals": calibration_signals,
        "calibration_table_generated_at": calibration["generated_at"] if calibration else None,
        "mispricing_signals": mispricing_signals,
        "mispricing_filters": {
            "min_edge_pts": MISPRICING_MIN_EDGE_PTS,
            "min_volume_24h": MISPRICING_MIN_VOLUME_24H,
            "horizon_days": MISPRICING_HORIZON_DAYS,
            "longterm_min_edge_pts": MISPRICING_LONGTERM_MIN_EDGE_PTS,
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Scanned {len(events)} events, found {len(opportunities)} arbitrage opportunities, "
          f"{len(calibration_signals)} calibration signals -> {OUTPUT_PATH}")
    print(f"Found {len(mispricing_signals)} mispricing signals (within the Top {MAX_MISPRICING_SIGNALS}).")
    print(f"Appended {new_log_entries} new markets to price_log.jsonl "
          f"(forward-looking log for the calibration archive).")
    if not calibration:
        print("Note: docs/calibration.json doesn't exist yet. The 'Weekly Calibration Scan' "
              "workflow needs to run at least once before calibration signals can be produced.", file=sys.stderr)


if __name__ == "__main__":
    main()
