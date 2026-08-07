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
"""

import datetime
import json
import math
import sys
import time
from pathlib import Path

import gamma_client

# --- Tunable parameters -----------------------------------------------
BIN_WIDTH = 0.05              # bucket width (0.05 = 20 buckets)
MIN_SAMPLE_PER_BUCKET = 30    # buckets with fewer samples than this have no statistical confidence
MAX_LOG_ENTRIES_TO_CHECK = 2500  # runtime / rate-limit safety cap (Gamma API ~60 requests/min)
SLEEP_BETWEEN_CALLS = 1.15    # ~52 requests/min, a safe margin under the Gamma API's ~60/min limit
# ----------------------------------------------------------------------------

PRICE_LOG_PATH = Path(__file__).resolve().parent.parent / "docs" / "price_log.jsonl"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "calibration.json"


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


def fetch_market_resolution(market_id: str):
    """True/False if the market has closed with a clean resolution; None if
    it's still open, hasn't closed yet, or resolved ambiguously (e.g.
    50-50/cancelled), meaning "not known yet," not an error. Thin wrapper
    around the shared gamma_client.fetch_market_state (closed, outcome_yes)
    pair, kept as its own function since callers here only care about the
    resolution, not whether the market has closed."""
    closed, outcome_yes = gamma_client.fetch_market_state(market_id)
    return outcome_yes if closed else None


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


def compute_bins(samples: list) -> list:
    """Buckets resolved samples into BIN_WIDTH-wide price ranges and computes
    the calibration stats for each bucket. Pure function of `samples`
    (each a {"reference_price": float, "resolved_yes": bool} dict) so it can
    be tested without hitting the network."""
    num_bins = int(round(1 / BIN_WIDTH))
    bins = []
    for b in range(num_bins):
        lo = b * BIN_WIDTH
        hi = lo + BIN_WIDTH
        bucket_samples = [s for s in samples if lo <= s["reference_price"] < hi or (b == num_bins - 1 and s["reference_price"] == 1.0)]
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
    print(f"Found {len(log_entries)} logged markets (docs/price_log.jsonl).")

    # The oldest logged markets are closest to closing, so checking them first
    # gets the highest yield of "resolved market" per run.
    log_entries.sort(key=lambda e: e.get("logged_at", ""))
    if len(log_entries) > MAX_LOG_ENTRIES_TO_CHECK:
        log_entries = log_entries[:MAX_LOG_ENTRIES_TO_CHECK]
        print(f"Capped at {MAX_LOG_ENTRIES_TO_CHECK} entries for runtime safety.")

    samples = []
    still_open_or_unclear = 0

    for i, entry in enumerate(log_entries):
        resolved_yes = fetch_market_resolution(entry["market_id"])
        time.sleep(SLEEP_BETWEEN_CALLS)

        if resolved_yes is None:
            still_open_or_unclear += 1
            continue

        samples.append({"reference_price": entry["price"], "resolved_yes": resolved_yes})

        if (i + 1) % 200 == 0:
            print(f"  ... checked {i + 1}/{len(log_entries)} "
                  f"({len(samples)} resolved, {still_open_or_unclear} still open/unclear)")

    print(f"{len(samples)} markets resolved and usable in total.")
    print(f"{still_open_or_unclear} markets are still open, not yet closed, or resolved ambiguously.")

    # --- Bucketing and statistics (identical logic to v1) ---
    bins = compute_bins(samples)

    output = {
        "generated_at": now.isoformat(),
        "bin_width": BIN_WIDTH,
        "min_sample_per_bucket": MIN_SAMPLE_PER_BUCKET,
        "logged_markets_total": len(load_price_log()),  # before the checked count, before the cap
        "logged_markets_checked": len(log_entries),
        "markets_resolved": len(samples),
        "markets_still_open_or_unclear": still_open_or_unclear,
        "bins": bins,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote -> {OUTPUT_PATH}")
    sig_count = sum(1 for b in bins if b["significant"])
    print(f"Found a statistically significant gap in {sig_count} buckets.")
    if len(samples) < MIN_SAMPLE_PER_BUCKET:
        print(f"Note: {len(samples)} markets have resolved so far; even one significant "
              f"bucket needs {MIN_SAMPLE_PER_BUCKET}. Waiting for more markets to close.", file=sys.stderr)


if __name__ == "__main__":
    main()
