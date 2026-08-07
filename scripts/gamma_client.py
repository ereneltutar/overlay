"""
Shared Gamma API client
-------------------------
Small helpers used by more than one script, kept in one place instead
of copy-pasted per script. calibration_scan.py and track_bets.py each
had their own near-identical copy of the market-resolution-checking
logic (same retry/backoff behavior, same ambiguous-outcome handling,
just a different return shape); this is the single source of truth for
that logic so a fix or a tuning change only has to be made once.
"""

import json
import time

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
REQUEST_TIMEOUT = 20
RESOLUTION_THRESHOLD = 0.98    # if outcomePrices isn't above/below this, treat it as "not cleanly resolved"
MAX_RETRIES_ON_429 = 2
BACKOFF_SECONDS_ON_429 = 8


def fetch_market_state(market_id: str):
    """Fetches a single market's CURRENT state from the Gamma API. Returns
    (closed, outcome_yes): closed is False if the market isn't closed yet
    (or the request failed); outcome_yes is True/False if closed with a
    clean resolution, or None if closed but resolved ambiguously (voided /
    50-50) or not closed at all."""
    attempt = 0
    while True:
        try:
            resp = requests.get(f"{GAMMA_BASE}/markets/{market_id}", timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            return False, None
        if resp.status_code == 429:
            if attempt >= MAX_RETRIES_ON_429:
                return False, None
            attempt += 1
            time.sleep(BACKOFF_SECONDS_ON_429 * attempt)
            continue
        try:
            resp.raise_for_status()
            market = resp.json()
        except (requests.RequestException, ValueError):
            return False, None
        break

    if not market.get("closed"):
        return False, None

    raw_prices = market.get("outcomePrices")
    if not raw_prices:
        return True, None
    try:
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        yes_price = float(prices[0])
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        return True, None

    if yes_price >= RESOLUTION_THRESHOLD:
        return True, True
    if yes_price <= (1 - RESOLUTION_THRESHOLD):
        return True, False
    return True, None  # closed but ambiguous / cancelled
