"""
Shared market-category classifier
----------------------------------
calibration_scan.py's bucketing is purely price-based: it pools every
resolved market at a given price into one historical win rate, regardless of
what the market is about. That's fine for most categories, but crypto
threshold markets ("will Bitcoin be above $X on date Y") turned out to be a
bad fit for the pool -- live paper-trading data (Aug 18-24 2026) shows they
went 0 wins out of 9 resolved bets (-$163 of a -$139 total account loss),
despite calibration flagging them as having a statistically significant
edge. Crypto markets trade in deep, continuously-arbitraged order books
where the current price already reflects available information about as
well as it can; a category-blind historical base rate built mostly from
sports/weather/politics markets reads that efficiency as an exploitable
edge that isn't really there.

is_crypto_market() lets calibration_scan.py build a separate calibration
table for crypto (see split_samples_by_category()) and lets
fetch_arbitrage.py route a live market to the matching table, so crypto only
gets a signal once there's crypto-specific historical evidence for it --
same "wait for real evidence" philosophy as the tail-bucket fix.
"""

import re

CRYPTO_KEYWORDS = re.compile(
    r"\bbitcoin\b|\bethereum\b|\bsolana\b|\bxrp\b|\bbtc\b|\beth\b|"
    r"\bdogecoin\b|\blitecoin\b|\bcrypto\b",
    re.IGNORECASE,
)


def is_crypto_market(question: str) -> bool:
    """True if a market question is about a cryptocurrency price/threshold.
    Pure function of the question text so it's testable without a network
    call or any market/event object shape assumptions."""
    return bool(CRYPTO_KEYWORDS.search(question or ""))
