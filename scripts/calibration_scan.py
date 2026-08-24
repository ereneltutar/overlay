#!/usr/bin/env python3
"""
Calibration Drift Scanner — Favorite-Longshot Bias Detector (v2: forward-looking)
--------------------------------------------------------------------------------
Measures whether there's a systematic gap (favorite-longshot bias) between
"the probability the market prices" and "the rate that actually resolved."

WHY WE DON'T LOOK BACKWARD (why v1 became v2):
  v1 queried Polymarket's CLOB /prices-history endpoint for the past, asking
  "what was the price N days before closing?" In a live test (379 qualifying
  markets, 372 failures) that endpoint returned EMPTY data for 98% of
  markets. That's not a random glitch: Polymarket's own general API only
  reliably serves live state, which is also why an independent third-party
  company ("PolymarketData.co") sells a paid historical-data archive
  specifically to fill that gap.

  So v2 doesn't look BACKWARD, it looks FORWARD: scripts/fetch_arbitrage.py
  (the main script that runs every morning) appends the current price of
  every market roughly 7 days from closing to docs/price_log.jsonl
  (append-only). This script reads that log, checks the Gamma API (reliable,
  no known issues) for whether those markets have NOW closed, matches the
  closed ones against their real outcome, and applies the same
  bucketing/Wilson-interval statistics.

  THE COST: this method doesn't give instant results. Realistically it takes
  weeks or months to get the first meaningful signals (30+ samples per
  bucket), since it needs markets logged at the right time to accumulate and
  then actually close. But it's the ONE solid method that doesn't depend at
  all on Polymarket's unreliable historical-data infrastructure.

Input: docs/price_log.jsonl (written by fetch_arbitrage.py)
Output: docs/calibration.json (SAME shape as v1; fetch_arbitrage.py's
        find_calibration_signal() function has no idea this changed, no
        code changes were needed on that side)
        docs/resolution_cache.json: every market_id already confirmed
        CLOSED, so it's never re-checked via the API again. Without this,
        a fixed "check the oldest N" cap re-verifies already-resolved
        markets forever, and once the log passes N entries, anything
        past that point never gets checked at all since the oldest N
        never change.
"""

import datetime
import json
import math
import sys
import time
from pathlib import Path

import gamma_client
import market_category

# --- Tunable parameters -----------------------------------------------
BIN_WIDTH = 0.05              # bucket width through the middle of the range
# The top and bottom BIN_WIDTH-wide buckets ([0,.05) and [.95,1.0]) used to
# be a single bucket each, which blends genuinely-uncertain markets (priced
# e.g. 95%) with effectively-decided ones (priced 99.9%) into one averaged
# "historical win rate" -- see find_calibration_signal()'s docstring in
# fetch_arbitrage.py. That let a near-certain market get judged against the
# whole tail's average and produce a wildly overconfident signal (a real
# case: a market priced at a 0.1% NO cost got assigned a bucket-average 26%
# NO win rate instead of the near-0% its own price implied, and Kelly staked
# real money on it as if 26% were correct). TAIL_BIN_WIDTH splits just the
# outer TAIL_ZONE_WIDTH of the range into finer buckets so a market that
# extreme is compared against genuinely similar historical markets instead.
TAIL_ZONE_WIDTH = 0.05        # how much of each end (0..this, (1-this)..1) gets finer bins
TAIL_BIN_WIDTH = 0.01         # width of those finer tail bins
MIN_SAMPLE_PER_BUCKET = 30    # buckets with fewer samples than this have no statistical confidence
# Must match fetch_arbitrage.py's MIN_CALIBRATION_LIQUIDITY_USD (the live signal's own
# floor). A market too illiquid to ever qualify as a live signal shouldn't get a vote in
# the historical rate that judges every live signal in its bucket. $100 (this constant's
# prior value) turned out to still be far too low: pooled across price_log.jsonl, markets
# priced >=85% resolved YES only 37.5% of the time at $100-250 liquidity vs 84.8% at
# $500-1000 and ~100% above $2500 -- the real inflection point is around $500. The root
# cause is that most of Polymarket's long-tail auto-generated markets (corner counts,
# exact scores, half-outcomes) never get organically traded, so their price falls back to
# a resting bestAsk quote instead of a real trade -- see MIN_CALIBRATION_VOLUME_24H_USD in
# fetch_arbitrage.py for the matching volume-based fix applied going forward. That fix only
# affects NEWLY logged rows (price_log.jsonl never recorded volume, so the existing backlog
# can't be filtered by it retroactively); this liquidity floor is the defense-in-depth that
# also cleans up the backlog already in the log.
MIN_SAMPLE_LIQUIDITY_USD = 500
MAX_LOG_ENTRIES_TO_CHECK = 2500  # runtime / rate-limit safety cap (Gamma API ~60 requests/min)
SLEEP_BETWEEN_CALLS = 1.15    # ~52 requests/min, a safe margin under the Gamma API's ~60/min limit
# ----------------------------------------------------------------------------

PRICE_LOG_PATH = Path(__file__).resolve().parent.parent / "docs" / "price_log.jsonl"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "calibration.json"
CACHE_PATH = Path(__file__).resolve().parent.parent / "docs" / "resolution_cache.json"


def load_price_log() -> list:
    """Reads docs/price_log.jsonl. Returns an empty list if the file doesn't
    exist yet (nothing has been logged)."""
    entries = []
    if not PRICE_LOG_PATH.exists():
        return entries
    with PRICE_LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def load_resolution_cache() -> dict:
    """Reads docs/resolution_cache.json: {market_id: {outcome_yes, checked_at}}
    for every market already confirmed CLOSED in a previous run (outcome_yes
    is True/False for a clean resolution, or None for closed-but-ambiguous;
    either way it's a final state that never needs re-checking). Markets
    still open are deliberately never cached, since their state can change."""
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_resolution_cache(cache: dict):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def filter_by_liquidity(log_entries: list, min_liquidity: float) -> list:
    """Drops logged markets below min_liquidity -- too thin to trust their
    price as a real probability (see MIN_SAMPLE_LIQUIDITY_USD). Pure function
    so the filter is testable without a network call or the full price log."""
    return [e for e in log_entries if float(e.get("liquidity") or 0) >= min_liquidity]


def partition_log_entries(log_entries: list, cache: dict, max_to_check: int):
    """Splits log entries (assumed oldest-first) into three groups so the
    per-run API budget is spent only on markets whose resolution isn't
    already known:
      - cached: market_id has a final resolution already, no API call needed
      - to_check: not cached, the oldest max_to_check of them, hit the API
        this run
      - deferred: not cached, beyond this run's budget, picked up by a
        future run once older entries resolve and free up room

    Without this split, a fixed "check the oldest N" cap re-verifies markets
    that already resolved weeks ago forever, and once the log has more than
    N entries, anything past position N never gets checked at all since the
    oldest N never change. Pure function so the allocation logic is testable
    without a network call."""
    cached, to_check, deferred = [], [], []
    for entry in log_entries:
        market_id = entry.get("market_id")
        if market_id in cache:
            cached.append(entry)
        elif len(to_check) < max_to_check:
            to_check.append(entry)
        else:
            deferred.append(entry)
    return cached, to_check, deferred


def wilson_interval(k: int, n: int, z: float = 1.96):
    """95% Wilson score confidence interval (more reliable than a normal
    approximation for small samples)."""
    if n == 0:
        return (0.0, 0.0)
    p_hat = k / n
    denom = 1 + (z ** 2) / n
    center = (p_hat + (z ** 2) / (2 * n)) / denom
    margin = (z / denom) * math.sqrt((p_hat * (1 - p_hat) / n) + (z ** 2) / (4 * n ** 2))
    return (max(0.0, center - margin), min(1.0, center + margin))


def bucket_edges() -> list:
    """(lo, hi) edges for compute_bins: BIN_WIDTH through the middle of the
    range, TAIL_BIN_WIDTH within TAIL_ZONE_WIDTH of 0 and 1. Computed in
    integer cents to avoid float drift from repeated addition."""
    def frange(lo_cents, hi_cents, step_cents):
        return [(c / 100, (c + step_cents) / 100) for c in range(lo_cents, hi_cents, step_cents)]

    tail_cents = round(TAIL_ZONE_WIDTH * 100)
    tail_step = round(TAIL_BIN_WIDTH * 100)
    mid_step = round(BIN_WIDTH * 100)

    return (
        frange(0, tail_cents, tail_step) +
        frange(tail_cents, 100 - tail_cents, mid_step) +
        frange(100 - tail_cents, 100, tail_step)
    )


def build_sample(entry: dict, outcome_yes: bool) -> dict:
    """Builds one resolved-market sample from a price_log.jsonl entry, tagged
    with its category so compute_bins() can be run separately per category
    (see split_samples_by_category and market_category.is_crypto_market)."""
    return {
        "reference_price": entry["price"],
        "resolved_yes": outcome_yes,
        "is_crypto": market_category.is_crypto_market(entry.get("question")),
    }


def split_samples_by_category(samples: list):
    """Splits resolved samples into (general, crypto) so each category gets
    its own calibration table instead of being blended into one average --
    see market_category.py's docstring for why crypto needs this. Pure
    function so it's testable without touching compute_bins or the network."""
    general = [s for s in samples if not s.get("is_crypto")]
    crypto = [s for s in samples if s.get("is_crypto")]
    return general, crypto


def compute_bins(samples: list) -> list:
    """Buckets resolved samples into price ranges (see bucket_edges) and
    computes the calibration stats for each bucket. Pure function of
    `samples` (each a {"reference_price": float, "resolved_yes": bool}
    dict) so it can be tested without hitting the network."""
    edges = bucket_edges()
    last_idx = len(edges) - 1
    bins = []
    for b, (lo, hi) in enumerate(edges):
        bucket_samples = [s for s in samples if lo <= s["reference_price"] < hi or (b == last_idx and s["reference_price"] == 1.0)]
        n = len(bucket_samples)
        k = sum(1 for s in bucket_samples if s["resolved_yes"])
        midpoint = (lo + hi) / 2

        entry = {
            "range": [round(lo, 2), round(hi, 2)],
            "midpoint": round(midpoint, 3),
            "sample_size": n,
        }

        if n >= MIN_SAMPLE_PER_BUCKET:
            actual_rate = k / n
            ci_low, ci_high = wilson_interval(k, n)
            bias_pct = (actual_rate - midpoint) * 100
            significant = not (ci_low <= midpoint <= ci_high)
            entry.update({
                "resolved_yes_rate": round(actual_rate, 4),
                "ci_95_low": round(ci_low, 4),
                "ci_95_high": round(ci_high, 4),
                "bias_pct": round(bias_pct, 2),
                "significant": significant,
            })
        else:
            entry.update({
                "resolved_yes_rate": None,
                "ci_95_low": None,
                "ci_95_high": None,
                "bias_pct": None,
                "significant": False,
            })

        bins.append(entry)
    return bins


def main():
    now = datetime.datetime.now(datetime.timezone.utc)

    log_entries = load_price_log()
    # The oldest logged markets are closest to closing, so checking them
    # first gets the highest yield of "resolved market" per run.
    log_entries.sort(key=lambda e: e.get("logged_at", ""))
    print(f"Found {len(log_entries)} logged markets (docs/price_log.jsonl).")

    # Drop entries too thin to trust as a real probability (see MIN_SAMPLE_LIQUIDITY_USD).
    # Filtered here, in memory, rather than by editing price_log.jsonl itself, since that
    # file is append-only by design (see log_price_snapshot()'s docstring) -- this also
    # saves the rate-limited Gamma API budget below from being spent resolving markets
    # that would just get dropped anyway.
    logged_markets_total = len(log_entries)
    log_entries = filter_by_liquidity(log_entries, MIN_SAMPLE_LIQUIDITY_USD)
    below_liquidity_floor = logged_markets_total - len(log_entries)
    if below_liquidity_floor:
        print(f"Dropped {below_liquidity_floor} logged markets below the "
              f"${MIN_SAMPLE_LIQUIDITY_USD:.0f} liquidity floor.")

    cache = load_resolution_cache()
    cached, to_check, deferred = partition_log_entries(log_entries, cache, MAX_LOG_ENTRIES_TO_CHECK)
    print(f"{len(cached)} already resolved in a previous run (skipped, no API call), "
          f"{len(to_check)} to check this run, {len(deferred)} deferred to a future run.")

    samples = []
    still_open_or_unclear = 0

    for entry in cached:
        outcome_yes = cache[entry["market_id"]]["outcome_yes"]
        if outcome_yes is None:
            still_open_or_unclear += 1
        else:
            samples.append(build_sample(entry, outcome_yes))

    newly_resolved = 0
    for i, entry in enumerate(to_check):
        market_id = entry["market_id"]
        closed, outcome_yes = gamma_client.fetch_market_state(market_id)
        time.sleep(SLEEP_BETWEEN_CALLS)

        if not closed:
            still_open_or_unclear += 1
        else:
            cache[market_id] = {"outcome_yes": outcome_yes, "checked_at": now.isoformat()}
            newly_resolved += 1
            if outcome_yes is None:
                still_open_or_unclear += 1
            else:
                samples.append(build_sample(entry, outcome_yes))

        if (i + 1) % 200 == 0:
            print(f"  ... checked {i + 1}/{len(to_check)} this run ({newly_resolved} newly resolved)")

    save_resolution_cache(cache)

    print(f"{len(samples)} markets resolved and usable in total "
          f"({len(cached)} from cache, {newly_resolved} newly confirmed this run).")
    print(f"{still_open_or_unclear} markets are still open, not yet closed, or resolved ambiguously.")
    if deferred:
        print(f"{len(deferred)} not-yet-resolved markets deferred to a future run "
              f"({MAX_LOG_ENTRIES_TO_CHECK}-per-run budget).")

    # --- Bucketing and statistics ---
    # Crypto gets its own table (see market_category.py) instead of being
    # blended into the general one, so a live crypto market is only judged
    # against genuinely comparable crypto history.
    general_samples, crypto_samples = split_samples_by_category(samples)
    bins = compute_bins(general_samples)
    crypto_bins = compute_bins(crypto_samples)

    output = {
        "generated_at": now.isoformat(),
        "bin_width": BIN_WIDTH,
        "tail_bin_width": TAIL_BIN_WIDTH,
        "tail_zone_width": TAIL_ZONE_WIDTH,
        "min_sample_per_bucket": MIN_SAMPLE_PER_BUCKET,
        "min_sample_liquidity_usd": MIN_SAMPLE_LIQUIDITY_USD,
        "logged_markets_total": logged_markets_total,
        "logged_markets_below_liquidity_floor": below_liquidity_floor,
        "logged_markets_cached": len(cached),
        "logged_markets_checked_this_run": len(to_check),
        "logged_markets_deferred": len(deferred),
        "markets_resolved": len(samples),
        "markets_resolved_general": len(general_samples),
        "markets_resolved_crypto": len(crypto_samples),
        "markets_still_open_or_unclear": still_open_or_unclear,
        "bins": bins,
        "crypto_bins": crypto_bins,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote -> {OUTPUT_PATH}")
    sig_count = sum(1 for b in bins if b["significant"])
    crypto_sig_count = sum(1 for b in crypto_bins if b["significant"])
    print(f"Found a statistically significant gap in {sig_count} general buckets, "
          f"{crypto_sig_count} crypto buckets ({len(crypto_samples)} crypto samples).")
    if len(samples) < MIN_SAMPLE_PER_BUCKET:
        print(f"Note: {len(samples)} markets have resolved so far; even one significant "
              f"bucket needs {MIN_SAMPLE_PER_BUCKET}. Waiting for more markets to close.", file=sys.stderr)


if __name__ == "__main__":
    main()
