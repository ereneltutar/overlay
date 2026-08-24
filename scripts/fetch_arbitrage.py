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
   If the sum of all those ask prices plus taker fees is under $1, the gap
   is a theoretical riskless profit (in practice, slippage, thin liquidity,
   and execution risk can still shrink or erase it).

   Taker fees are modeled precisely per Polymarket's published formula
   (fee = rate * shares * price * (1-price), see estimate_taker_fee()
   below), reading each market's actual feesEnabled/feeSchedule.rate
   fields rather than assuming a flat rate or ignoring fees entirely.
   Makers pay nothing; this only applies to buying at the ask, which is
   exactly what constructing this basket requires.

   ARB only scans "negRisk" groups; plain binary Yes/No markets don't
   have a viable path to a real, tradeable NO-side ask, so they're
   deliberately excluded from ARB specifically. This is a structural
   data-availability gap, not a "not implemented yet" one, confirmed
   directly (not just inherited from an earlier assumption): the Gamma
   API exposes exactly one bestAsk/bestBid pair per binary market, for
   the "Yes" token by convention, no second field for "No." Each
   market's clobTokenIds does give both outcomes' token IDs, so the
   CLOB order book endpoint CAN be queried per token, and it does
   return real order-book data - but across every actively-traded
   binary market checked live, the secondary (No) token's ask side
   was consistently a flat 0.99 wall with large size, a non-tradeable
   placeholder rather than real liquidity, while its BID side tracked
   (1 - Yes ask) almost exactly. There is currently no endpoint that
   returns a real, tradeable No-side ask, so "Yes ask + No ask < $1"
   can't be constructed from real data for a plain binary market.

   CAL and MIS below have never had this restriction: they only need
   ONE reliable price per market (lastTradePrice/bestAsk, which Gamma
   does provide correctly for binary markets), so they already scan
   every market in every event regardless of negRisk, and have since
   this script's first version.

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

import gamma_client
import market_category

GAMMA_BASE = gamma_client.GAMMA_BASE

# --- Tunable parameters -----------------------------------------------
DAYS_AHEAD = 30          # scan events whose deadline is at most this many days out
MIN_EDGE_PCT = 0.5       # don't show "opportunities" below this percent (noise filter)
# Same phantom-quote problem documented below for calibration/mispricing applies
# here: liquidityNum is resting order-book depth, not evidence anyone's actually
# trading. $50 let arbs like a 6-outcome long-tail props market price at 800%+
# edge onto the dashboard on lone, untraded resting asks. Raised to match the
# $500 real-world inflection point established for CAL/MIS (see
# MIN_CALIBRATION_LIQUIDITY_USD below), plus a same-day-volume floor (below).
MIN_LIQUIDITY_USD = 500  # require at least this much liquidity per leg (drop thin books)
ARB_MIN_VOLUME_24H_USD = 5000  # require every leg to have real same-day trading volume
# The liquidity/volume floors above are still a cheap pre-filter (skip obvious
# junk before spending an API call on it), but they're proxies. The real
# question -- "would buying this leg actually cost what bestAsk claims?" -- is
# answered by walking each leg's live CLOB order book (see get_order_book /
# walk_ask_book) and simulating a real fill of this many dollars. This is what
# actually catches a phantom resting quote: a stale $0.009 ask with no real
# size behind it can't fill $100, so it fails here even if it slipped past the
# liquidity/volume floors.
CLOB_BASE = "https://clob.polymarket.com"
ARB_TARGET_FILL_USD = 100  # simulated per-leg buy size used to sanity-check real fillability
PAGE_LIMIT = 100         # /events/keyset caps this at 100 regardless of what's requested
MAX_PAGES = 300          # safety cap (300 x 100 = 30,000 events of headroom)
REQUEST_TIMEOUT = 30

# For the momentum / volume-anomaly scan:
MOMENTUM_MIN_VOLUME_24H = 200     # skip markets with less than this much daily volume (noise)
MOMENTUM_VOLUME_MULTIPLIER = 3.0  # last-24h volume must be at least this many times the trailing weekly daily average
MOMENTUM_MIN_PRICE_MOVE = 0.05    # last-24h (or last-1h) price move must be at least this many points (0.05 = 5 points)
MAX_MOMENTUM_SIGNALS = 15         # max signals shown on the dashboard

# For the calibration-drift (favorite-longshot bias) cross-check:
MIN_CALIBRATION_EDGE_PCT = 2.0    # min gap in percentage points between true_rate and cost (noise floor)
# $100 was the old floor after the first liquidity-contamination fix (see
# CALIBRATION_LOG_MIN_LIQUIDITY below); it wasn't enough. Pooled across every
# resolved sample in price_log.jsonl, markets priced >=85% resolved YES only
# 37.5% of the time at $100-250 liquidity, vs 84.8% at $500-1000 and ~100% above
# $2500 -- the real inflection point is around $500, not $100.
MIN_CALIBRATION_LIQUIDITY_USD = 500  # drop markets below this liquidity
# liquidityNum measures resting order-book depth, which a market can have with
# zero actual trades -- most of Polymarket's long-tail auto-generated props
# (corner counts, exact scores, half-outcomes) never get organically traded, so
# lastTradePrice is null and the price falls back to bestAsk: a lone resting
# quote, not a crowd-informed probability. Tell: 99.8% of sub-$250-liquidity,
# >=85%-priced samples sit on an exact 1-cent tick (0.85, 0.86, ...) vs 33% for
# genuinely liquid (>=$2500) ones -- the signature of a quoted-not-traded price.
# find_mispricing_signal already guards against exactly this with a 24h volume
# requirement (MISPRICING_MIN_VOLUME_24H); find_calibration_signal didn't, which
# is why CAL signals kept re-monopolizing on these phantom-quote markets even
# after the liquidity floor above went from $50 to $100, while MIS (which has
# this filter) stayed diversified throughout. Same bar as mispricing's, which
# already has a clean track record at this threshold.
MIN_CALIBRATION_VOLUME_24H_USD = 5000
MAX_CALIBRATION_SIGNALS = 20      # max signals shown on the dashboard

# For forward-looking calibration LOGGING (the archive calibration_scan.py reads):
# WARNING: CALIBRATION_LOG_LEAD_DAYS represents the same concept as LEAD_DAYS in
# scripts/calibration_scan.py (both answer "how many days before closing").
# If you change one, review the other; there's no shared code linking them.
CALIBRATION_LOG_LEAD_DAYS = 7
CALIBRATION_LOG_TOLERANCE_DAYS = 0.6  # slack so a once-daily cron doesn't miss this window
# Must be >= MIN_CALIBRATION_LIQUIDITY_USD / MIN_CALIBRATION_VOLUME_24H_USD (the
# live signal's own floors). A gap here lets markets too thin/untraded to ever
# qualify as a live signal into the training data that scores every live signal
# in their bucket instead -- see the comments on those two constants above for
# the incidents this caused.
CALIBRATION_LOG_MIN_LIQUIDITY = 500
CALIBRATION_LOG_MIN_VOLUME_24H = 5000
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
MISPRICING_MIN_EDGE_PTS = 5.0           # min point gap between implied and the bucket's historical rate
# Lowered from 15.0 (Aug 24 2026) after 7 straight days of zero mispricing
# signals following the Aug 18 phantom-quote fix (liquidity/volume floors +
# CLOB depth verification). A diagnostic run with this floor dropped to 5.0
# (branch diagnostic/relaxed-arb-mis-thresholds, not merged) surfaced a full
# Top-20 of genuine signals clustered at 5-8 points, all backed by large
# buckets (37-599 samples) and real liquidity/volume ($2.5K-$560K liquidity,
# $5K-$90K 24h volume) -- evidence the 15pt floor was calibrated for a noisier
# market (phantom-quote-inflated apparent edges) that no longer exists post-
# fix, and was screening out every real signal along with the noise. ARB's
# floors (MIN_LIQUIDITY_USD, ARB_MIN_VOLUME_24H_USD above) were NOT changed:
# the same diagnostic run, with those floors also relaxed, still found zero
# arbitrage opportunities -- confirming ARB's zero is real market efficiency,
# not an overly strict filter.
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
    """Pages through the Gamma API /events/keyset endpoint and pulls every
    active, not-yet-closed event.

    This used to hit the plain offset-paginated /events endpoint, which has
    a hard, undocumented ceiling: any request where offset + limit > ~2100
    gets rejected with a 422 (Unprocessable Entity), no matter how many
    events actually exist. That endpoint is also now formally deprecated
    (its responses carry `deprecation: true` and a `sunset` header pointing
    at the cursor-based replacement). Live testing found at least 6,000
    active events on Polymarket, meaning the old code was silently missing
    roughly two-thirds of the market every single day for over a month
    without ever raising an error, since offset-based pagination just stops
    cleanly at its ceiling instead of failing loudly.

    /events/keyset fixes this with cursor pagination instead of a numeric
    offset: each response includes a `next_cursor`, which gets passed back
    as the `after_cursor` query param to fetch the next page (NOT `cursor` -
    that parameter name is silently ignored server-side and just re-returns
    the first page again, a known gotcha: see Polymarket/agents#227). A
    missing/empty `next_cursor` means there are no more pages. The `limit`
    param is capped at 100 by the server regardless of what's requested.
    """
    events = []
    cursor = None
    for _ in range(MAX_PAGES):
        params = {
            "active": "true",
            "closed": "false",
            "limit": PAGE_LIMIT,
        }
        if cursor:
            params["after_cursor"] = cursor
        try:
            resp = requests.get(f"{GAMMA_BASE}/events/keyset", params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            page = resp.json()
        except requests.RequestException as exc:
            print(f"Pagination stopped after cursor={cursor} (API error: {exc}). "
                  f"Using the {len(events)} events collected so far.", file=sys.stderr)
            break
        batch = page.get("events") if isinstance(page, dict) else None
        if not batch:
            break
        events.extend(batch)
        cursor = page.get("next_cursor")
        if not cursor:
            break
        time.sleep(0.2)  # be polite to the API
    return events


def parse_iso(date_str):
    if not date_str:
        return None
    try:
        return datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def leg_token_id(market: dict):
    """Extracts the CLOB token ID for the outcome this market's bestAsk
    refers to. clobTokenIds is Gamma's JSON-encoded [yes_token, no_token]
    pair; index 0 matches the "Yes" convention bestAsk already uses (see
    the module docstring's clobTokenIds note). Returns None if missing or
    unparseable, which the caller treats as "can't verify, don't show"."""
    raw = market.get("clobTokenIds")
    if not raw:
        return None
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None
    return ids[0] if ids else None


def get_order_book(token_id: str):
    """Fetches the live CLOB order book for one outcome token. No auth
    required for read endpoints. Returns None on any failure so callers
    can treat an unreachable/malformed book the same as an unfillable one
    rather than crashing the whole scan."""
    try:
        resp = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def walk_ask_book(asks: list, target_usd: float):
    """Walks the ask side of a real order book (cheapest price first) to
    find the average price actually paid buying $target_usd worth of
    shares, level by level. This is the ground-truth check for the
    liquidityNum/bestAsk phantom-quote problem documented above: a lone
    resting quote shows up here as a book that can't fill target_usd at
    all, which is exactly what should disqualify an "arbitrage." Returns
    None if the book (across all its levels) can't fill target_usd."""
    try:
        levels = sorted(asks, key=lambda lvl: float(lvl["price"]))
    except (TypeError, ValueError, KeyError):
        return None
    remaining = target_usd
    cost = 0.0
    shares = 0.0
    for level in levels:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (TypeError, ValueError, KeyError):
            return None
        if price <= 0:
            continue
        level_value = price * size
        if level_value >= remaining:
            cost += remaining
            shares += remaining / price
            remaining = 0.0
            break
        cost += level_value
        shares += size
        remaining -= level_value
    if remaining > 1e-9 or shares <= 0:
        return None
    return cost / shares


def estimate_taker_fee(market: dict, price: float, shares: float = 1.0) -> float:
    """Estimates the taker fee for buying `shares` at `price`, using
    Polymarket's published formula: fee = rate * shares * price * (1 - price).
    Fees are charged only to the taker (the side crossing the spread, which
    is exactly what buying at "best ask" is) and only when the market has
    them enabled; the fee peaks at a 50c price and goes to zero at the
    extremes (0 or 1). `rate` comes from the market's own feeSchedule, so
    this varies per market/category rather than assuming one global rate.
    Returns 0.0 if the market has fees disabled or no fee schedule at all."""
    if not market.get("feesEnabled"):
        return 0.0
    rate = (market.get("feeSchedule") or {}).get("rate")
    if not rate:
        return 0.0
    return rate * shares * price * (1 - price)


def find_opportunity(event: dict, now: datetime.datetime, fetch_book=get_order_book):
    """Returns a dict if the event has a negRisk arbitrage opportunity, else
    None. `fetch_book` is injectable so tests can simulate order books
    without a real network call; defaults to the live CLOB endpoint."""
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
    total_fee = 0.0
    for m in neg_risk_markets:
        try:
            ask = float(m["bestAsk"])
        except (TypeError, ValueError):
            return None
        if ask <= 0 or ask >= 1:
            return None
        liquidity = float(m.get("liquidityNum") or 0)
        volume_24h = float(m.get("volume24hr") or 0)
        fee = estimate_taker_fee(m, ask)
        total_ask += ask
        total_fee += fee
        legs.append({
            "outcome": m.get("groupItemTitle") or m.get("question") or "?",
            "ask": round(ask, 4),
            "liquidity": round(liquidity, 2),
            "volume_24h": round(volume_24h, 2),
            "market_id": m.get("id"),
            "token_id": leg_token_id(m),
            "fee": round(fee, 4),
        })

    if total_ask <= 0:
        return None

    total_cost = total_ask + total_fee
    edge_pct = (1 - total_cost) / total_cost * 100
    if edge_pct < MIN_EDGE_PCT:
        return None

    min_liquidity = min(leg["liquidity"] for leg in legs)
    if min_liquidity < MIN_LIQUIDITY_USD:
        return None

    # Every leg has to be genuinely tradable, not just resting-quote thin -- an
    # arb is only as real as its worst-traded leg, since that's the one most
    # likely to be a stale/phantom quote that won't actually fill at this ask.
    min_volume_24h = min(leg["volume_24h"] for leg in legs)
    if min_volume_24h < ARB_MIN_VOLUME_24H_USD:
        return None

    # Ground-truth check: walk each leg's real order book to see what buying
    # ARB_TARGET_FILL_USD would actually cost. A stale/phantom resting quote
    # fails here (can't fill the target) even though it cleared the liquidity
    # and volume proxies above. Recompute cost/fee/edge from the real walked
    # prices -- that's what the dashboard should show as "cost," not the raw
    # bestAsk sticker price.
    real_total_ask = 0.0
    real_total_fee = 0.0
    for leg, m in zip(legs, neg_risk_markets):
        token_id = leg["token_id"]
        if not token_id:
            return None
        book = fetch_book(token_id)
        if not book or not book.get("asks"):
            return None
        fill_price = walk_ask_book(book["asks"], ARB_TARGET_FILL_USD)
        if fill_price is None or fill_price <= 0 or fill_price >= 1:
            return None
        leg["sticker_ask"] = leg["ask"]
        leg["ask"] = round(fill_price, 4)
        leg["fee"] = round(estimate_taker_fee(m, fill_price), 4)
        real_total_ask += fill_price
        real_total_fee += leg["fee"]

    real_total_cost = real_total_ask + real_total_fee
    real_edge_pct = (1 - real_total_cost) / real_total_cost * 100
    if real_edge_pct < MIN_EDGE_PCT:
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
        "ask_cost": round(real_total_ask, 4),
        "total_fee": round(real_total_fee, 4),
        "total_cost": round(real_total_cost, 4),
        "edge_pct": round(real_edge_pct, 2),
        "min_outcome_liquidity": round(min_liquidity, 2),
        "min_outcome_volume_24h": round(min_volume_24h, 2),
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


def select_bins_for_market(market: dict, event: dict, bins: list, crypto_bins: list):
    """Routes a live market to the calibration table matching its category
    (see market_category.py for why crypto needs its own table). Pure
    function of the market/event question text plus the two already-loaded
    tables, so it's testable without a network call."""
    question = market.get("question") or event.get("title") or ""
    return crypto_bins if market_category.is_crypto_market(question) else bins


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
    your favor across many repeated, INDEPENDENT positions.

    edge_pct is a percentage-POINT gap (true_rate - cost) * 100, same as
    find_mispricing_signal's edge_pts -- not a ratio over cost. A ratio blows
    up without bound as cost approaches 0, and since calibration_signals gets
    sorted by edge_pct and truncated to MAX_CALIBRATION_SIGNALS, a single
    low-cost bucket with a ratio-inflated "edge" could otherwise monopolize
    every slot in that Top N regardless of how small its real, absolute edge
    was (a real incident: one bucket's edge_pct hit five digits this way and
    crowded out every other candidate for days)."""
    try:
        price = float(market.get("lastTradePrice") or market.get("bestAsk") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or price >= 1:
        return None

    liquidity = float(market.get("liquidityNum") or 0)
    if liquidity < MIN_CALIBRATION_LIQUIDITY_USD:
        return None

    volume_24h = float(market.get("volume24hr") or 0)
    if volume_24h < MIN_CALIBRATION_VOLUME_24H_USD:
        return None

    bucket = find_bin(price, bins)
    if not bucket or not bucket.get("significant"):
        return None

    bias_pct = bucket["bias_pct"]
    actual_rate = bucket["resolved_yes_rate"]

    # Same real per-market taker fee as the ARB scan (rate * price * (1-price),
    # symmetric so it doesn't matter which side ends up being bought), added to
    # the raw market price to get the real cost of entering the position.
    fee = estimate_taker_fee(market, price)
    if bias_pct > 0:
        side, raw_cost, true_rate = "YES", price, actual_rate
    else:
        side, raw_cost, true_rate = "NO", 1 - price, 1 - actual_rate
    cost = raw_cost + fee

    if cost <= 0:
        return None

    edge_pct = (true_rate - cost) * 100
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
        "fee": round(fee, 4),
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

    # Same real per-market taker fee as ARB/CAL (rate * price * (1-price), so
    # it's the same value regardless of which side ends up getting bought),
    # folded into the cost side of the point gap so edge_pts reflects what a
    # bet here would actually net, not just the raw price-vs-history gap.
    side = "YES" if fair_prob > implied_prob else "NO"
    fee = estimate_taker_fee(market, implied_prob)
    raw_cost = implied_prob if side == "YES" else 1 - implied_prob
    cost = raw_cost + fee
    true_prob_for_side = fair_prob if side == "YES" else 1 - fair_prob

    edge_pts = (true_prob_for_side - cost) * 100
    if edge_pts < MISPRICING_MIN_EDGE_PTS:
        return None

    # Don't drop markets past 30 days entirely, just the small-edge ones.
    if days_left is not None and days_left > MISPRICING_HORIZON_DAYS \
            and edge_pts <= MISPRICING_LONGTERM_MIN_EDGE_PTS:
        return None

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
        "implied_cost": round(cost, 4),
        "fee": round(fee, 4),
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
            volume_24h = float(market.get("volume24hr") or 0)
            if volume_24h < CALIBRATION_LOG_MIN_VOLUME_24H:
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
    calibration_crypto_bins = calibration.get("crypto_bins") if calibration else None

    opportunities = []
    calibration_signals = []

    for event in events:
        end_date = parse_iso(event.get("endDate"))
        if not end_date or end_date < now or end_date > cutoff:
            continue

        opp = find_opportunity(event, now)
        if opp:
            opportunities.append(opp)

        if calibration_bins or calibration_crypto_bins:
            for market in (event.get("markets") or []):
                market_bins = select_bins_for_market(market, event, calibration_bins, calibration_crypto_bins)
                if not market_bins:
                    continue
                sig = find_calibration_signal(market, event, market_bins, now)
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
    if calibration_bins or calibration_crypto_bins:
        for event in events:
            end_date = parse_iso(event.get("endDate"))
            if not end_date or end_date < now:
                continue  # closed / date unknown event
            for market in (event.get("markets") or []):
                market_bins = select_bins_for_market(market, event, calibration_bins, calibration_crypto_bins)
                if not market_bins:
                    continue
                sig = find_mispricing_signal(market, event, market_bins, now)
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
