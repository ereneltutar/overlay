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

Sports got the same treatment on 2026-10-26. By then sports/esports game
markets (moneylines, spreads, O/U, exact scores, halftime props) were ~84%
of the general table's resolved samples, so the general table was
effectively a sports table: every significant bucket from 15% to 95% showed
YES resolving below its price (outcomes[0] -- the first-listed team, the
Over, the spread favorite -- systematically overpriced), and CAL/MIS
therefore recommended NO on everything. That bias was then applied to
non-sports Yes/No questions too, e.g. NO at 5.7c on a safe-seat House race
priced as a 16% shot, on the strength of sports data. With sports split
out, a political/economic Yes/No market is only judged against the ~1.9k
non-sports samples, and most of those buckets aren't significant yet.
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


# Game markets: "A vs. B" (moneylines, draws, esports maps), spreads,
# totals, exact scores, half/halftime props, and the "Will X win on
# <date>?" legs of a soccer 3-way. Futures like "Will Argentina win the
# 2026 FIFA World Cup?" deliberately stay general -- they're season-long
# Yes/No questions, not single-game lines.
SPORTS_PATTERN = re.compile(
    r"\bvs\.?(?=\s)|\bspread:|\bO/U\b|over/under|^exact score:|"
    r"\bhalftime\b|\bto win the (first|second) half\b|\bsecond half draw\b|"
    r"\bwin on \d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)


def is_sports_market(question: str) -> bool:
    """True if a market question is a single sports/esports game line.
    Pure function of the question text, like is_crypto_market."""
    return bool(SPORTS_PATTERN.search(question or ""))


def category(question: str) -> str:
    """Which calibration table a market belongs to: "crypto", "sports" or
    "general". Crypto wins ties, so a crypto "Up or Down" or a
    "BTC vs. ETH" question stays in the crypto table."""
    if is_crypto_market(question):
        return "crypto"
    if is_sports_market(question):
        return "sports"
    return "general"
