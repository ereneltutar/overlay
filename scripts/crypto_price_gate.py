"""
Live crypto-price plausibility gate
------------------------------------
calibration_scan.py's crypto table (see market_category.py) is still purely
PRICE-bucketed: it pools every resolved crypto threshold market whose
market-implied probability fell in a given range into one historical win
rate, with no idea how far in dollar terms the live spot price actually sits
from THIS market's target, or how many days are left to get there. Two
crypto markets can share the same 4-cent market price while one needs a
plausible 5% move to resolve YES and the other needs an effectively
impossible 40% move in a week -- the bucket can't tell them apart, and a
favorite-longshot signal computed mostly from the first kind gets misapplied
to the second. Giving crypto its own table (the market_category.py fix)
addressed a different failure mode (blending crypto into sports/weather/
politics history) but didn't add any live-price awareness, so this gap
persisted even after that fix.

A live diagnostic run on 2026-09-22 found exactly this: of 4 open crypto
calibration bets, 3 required a move (ETH dip to $1,700 from a live ~$2,733 =
-38%; DOGE to $0.15 from a live ~$0.09 = +67%; the third similarly) that's
essentially impossible within the market's remaining ~8 days given crypto's
own realistic historical volatility -- yet calibration/mispricing signaled
and staked real paper-trading money on all of them anyway, purely because
the bucket table has no access to live spot prices or the market's actual
strike/target.

This module is a hard VETO, not a probability model: given the live spot
price (from CoinGecko's free, no-API-key /simple/price endpoint) and the
target price parsed out of the market's question text, if the required move
is far outside a generous multiple of what a random walk at the asset's
approximate historical daily volatility could plausibly cover in the days
remaining, the signal is dropped regardless of what the price-bucket
calibration table says. It doesn't replace the bucket approach or try to
out-price the market on markets it can't rule out this way; it only catches
the cases where the bucket's blindness to live price is producing an
obviously wrong bet.

NOTE: the volatility constants below are a reasoned starting point (typical
BTC/ETH/altcoin daily-vol ranges), not backtested against price_log.jsonl --
unlike MISPRICING_MIN_EDGE_PTS's history (see fetch_arbitrage.py), there's
no diagnostic sweep behind this threshold yet. MAX_VOL_MULTIPLE is
deliberately loose (hard to trigger by accident) until one exists.
"""

import re

import requests

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
REQUEST_TIMEOUT = 15

# keyword (word-boundary matched, case-insensitive) -> CoinGecko coin id.
# Keys intentionally mirror market_category.CRYPTO_KEYWORDS so every market
# that reaches this gate through is_crypto_market() is also resolvable to a
# live price; an asset this map doesn't recognize just skips the gate (see
# asset_id_for_question) rather than blocking the signal.
ASSET_COINGECKO_IDS = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "solana": "solana",
    "xrp": "ripple",
    "dogecoin": "dogecoin",
    "litecoin": "litecoin",
}

# Rough historical daily volatility by CoinGecko id (not backtested -- see
# module docstring). DEFAULT_DAILY_VOL covers any id not listed here.
ASSET_DAILY_VOL = {
    "bitcoin": 0.035,
    "ethereum": 0.04,
    "solana": 0.06,
    "ripple": 0.05,
    "dogecoin": 0.06,
    "litecoin": 0.05,
}
DEFAULT_DAILY_VOL = 0.05

# Generous multiple on a sqrt(time)-scaled random walk: a required move
# beyond this many "volatility units" is treated as implausible enough to
# hard-veto the signal. Deliberately loose (see module docstring) so this
# only catches egregious cases, not merely unlikely ones the calibration
# math is supposed to be allowed to bet on.
MAX_VOL_MULTIPLE = 3.0

PRICE_TARGET_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def asset_id_for_question(question: str):
    """Returns the CoinGecko coin id for the first recognized asset keyword
    in `question`, or None if none matched. Word-boundary matched (so "eth"
    doesn't match inside "Elizabeth", matching market_category.py's own
    false-positive guard). Pure function, no network call."""
    q = question or ""
    for keyword, coin_id in ASSET_COINGECKO_IDS.items():
        if re.search(rf"\b{re.escape(keyword)}\b", q, re.IGNORECASE):
            return coin_id
    return None


def extract_price_target(question: str):
    """Parses the first $-prefixed number out of `question` as a float, or
    None if there isn't one. Handles thousands separators ($1,700 -> 1700.0).
    Pure function, no network call."""
    match = PRICE_TARGET_RE.search(question or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def max_plausible_move_pct(coin_id: str, days_remaining) -> float:
    """The largest |percent move| treated as plausible for `coin_id` within
    `days_remaining`, under a sqrt(time)-scaled random walk at its assumed
    daily volatility (see ASSET_DAILY_VOL / MAX_VOL_MULTIPLE). Pure
    function."""
    daily_vol = ASSET_DAILY_VOL.get(coin_id, DEFAULT_DAILY_VOL)
    days = max(days_remaining or 0.0, 0.0) ** 0.5
    return MAX_VOL_MULTIPLE * daily_vol * days


def fetch_live_prices(coin_ids) -> dict:
    """Fetches current USD spot prices for every id in `coin_ids` in ONE
    CoinGecko request (free, no API key; /simple/price accepts a
    comma-joined ids param) rather than one request per market -- a daily
    scan can have several crypto candidates sharing the same handful of
    assets (BTC/ETH/DOGE/...), and CoinGecko's free tier has a real rate
    limit. Returns {} on any failure so every caller degrades uniformly to
    "can't verify, don't veto" rather than partially."""
    ids = sorted(set(coin_ids))
    if not ids:
        return {}
    try:
        resp = requests.get(
            f"{COINGECKO_BASE}/simple/price",
            params={"ids": ",".join(ids), "vs_currencies": "usd"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            coin_id: float(data[coin_id]["usd"])
            for coin_id in ids
            if isinstance(data.get(coin_id), dict) and "usd" in data[coin_id]
        }
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return {}


def passes_price_sanity_gate(question: str, days_remaining, live_prices: dict) -> bool:
    """True unless `question` is a crypto threshold market whose required
    move (live spot price in `live_prices` -> the $ target parsed from the
    question) is implausible within days_remaining (see module docstring).

    Returns True whenever the gate can't be evaluated -- asset not
    recognized, no $ target found in the question, or `live_prices` is
    missing that asset (CoinGecko unreachable, or the caller never fetched
    it). This is a veto on CONFIRMED-implausible bets, not a requirement to
    prove every bet plausible; a signal should never be blocked just
    because this gate had no data to check it against."""
    coin_id = asset_id_for_question(question)
    if coin_id is None:
        return True
    target_price = extract_price_target(question)
    if target_price is None:
        return True
    current_price = live_prices.get(coin_id)
    if not current_price or current_price <= 0:
        return True

    required_move_pct = abs(target_price - current_price) / current_price
    bound = max_plausible_move_pct(coin_id, days_remaining)
    return required_move_pct <= bound
