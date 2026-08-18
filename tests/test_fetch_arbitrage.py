import datetime
import json

import fetch_arbitrage as fa

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def leg(ask, negrisk=True, accepting=True, liquidity=1000, volume=10000, market_id="m1", title="A",
        fees_enabled=False, fee_rate=None, token_id=None):
    m = {
        "negRisk": negrisk,
        "acceptingOrders": accepting,
        "bestAsk": ask,
        "liquidityNum": liquidity,
        "volume24hr": volume,
        "id": market_id,
        "groupItemTitle": title,
        "feesEnabled": fees_enabled,
        "clobTokenIds": json.dumps([token_id or f"tok-{market_id}", f"tok-{market_id}-no"]),
    }
    if fee_rate is not None:
        m["feeSchedule"] = {"rate": fee_rate}
    return m


def make_event(markets, end_date="2026-02-01T00:00:00Z", slug="evt", title="Event"):
    return {"markets": markets, "endDate": end_date, "slug": slug, "title": title}


def deep_book(price, size=100000):
    """A single order-book level with plenty of size at `price` -- walking
    it for ARB_TARGET_FILL_USD yields exactly `price` back out, so tests
    that don't care about the real-depth check get the same numbers the
    old bestAsk-only math produced."""
    return {"asks": [{"price": str(price), "size": str(size)}]}


def fetch_book_for(markets, overrides=None):
    """Builds a fetch_book callable for find_opportunity(). By default every
    leg's simulated order book has full depth at its own bestAsk. Pass
    `overrides` (market_id -> book dict, or market_id -> None to simulate a
    failed/empty fetch) to test the real-depth-check path itself."""
    overrides = overrides or {}
    by_token = {}
    for m in markets:
        token_id = fa.leg_token_id(m)
        by_token[token_id] = overrides[m["id"]] if m["id"] in overrides else deep_book(m["bestAsk"])

    def fetch(tid):
        return by_token.get(tid)
    return fetch


# --- parse_iso ---------------------------------------------------------

def test_parse_iso_none_and_empty():
    assert fa.parse_iso(None) is None
    assert fa.parse_iso("") is None


def test_parse_iso_valid_with_z_suffix():
    d = fa.parse_iso("2026-03-01T12:00:00Z")
    assert d == datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.timezone.utc)


def test_parse_iso_invalid_string():
    assert fa.parse_iso("not-a-date") is None


# --- find_bin ------------------------------------------------------------

BINS = [
    {"range": [0.0, 0.5]},
    {"range": [0.5, 1.0]},
]


def test_find_bin_matches_lower_range():
    assert fa.find_bin(0.2, BINS) is BINS[0]


def test_find_bin_matches_upper_range():
    assert fa.find_bin(0.7, BINS) is BINS[1]


def test_find_bin_price_exactly_one_matches_last_bucket():
    assert fa.find_bin(1.0, BINS) is BINS[1]


def test_find_bin_no_match_returns_none():
    assert fa.find_bin(2.0, BINS) is None


# --- find_opportunity ------------------------------------------------------

def test_find_opportunity_needs_at_least_two_negrisk_legs():
    markets = [leg(0.4, market_id="a")]
    event = make_event(markets)
    assert fa.find_opportunity(event, NOW, fetch_book=fetch_book_for(markets)) is None


def test_find_opportunity_ignores_non_negrisk_and_non_accepting_legs():
    markets = [
        leg(0.4, market_id="a"),
        leg(0.4, market_id="b", negrisk=False),
        leg(0.4, market_id="c", accepting=False),
    ]
    event = make_event(markets)
    assert fa.find_opportunity(event, NOW, fetch_book=fetch_book_for(markets)) is None


def test_find_opportunity_rejects_ask_out_of_bounds():
    markets = [leg(0.0, market_id="a"), leg(0.5, market_id="b")]
    assert fa.find_opportunity(make_event(markets), NOW, fetch_book=fetch_book_for(markets)) is None
    markets = [leg(1.0, market_id="a"), leg(0.5, market_id="b")]
    assert fa.find_opportunity(make_event(markets), NOW, fetch_book=fetch_book_for(markets)) is None


def test_find_opportunity_below_min_edge_returns_none():
    # total ask 0.999 -> edge_pct = (1-0.999)/0.999*100 = 0.1%, below MIN_EDGE_PCT=0.5
    markets = [leg(0.5, market_id="a"), leg(0.499, market_id="b")]
    assert fa.find_opportunity(make_event(markets), NOW, fetch_book=fetch_book_for(markets)) is None


def test_find_opportunity_below_min_liquidity_returns_none():
    markets = [leg(0.4, market_id="a", liquidity=10), leg(0.4, market_id="b", liquidity=10)]
    assert fa.find_opportunity(make_event(markets), NOW, fetch_book=fetch_book_for(markets)) is None


def test_find_opportunity_valid_case():
    markets = [
        leg(0.5, market_id="a", liquidity=600, title="Alpha"),
        leg(0.45, market_id="b", liquidity=700, title="Beta"),
    ]
    event = make_event(markets, end_date="2026-01-11T00:00:00Z", slug="my-event", title="My Event")
    opp = fa.find_opportunity(event, NOW, fetch_book=fetch_book_for(markets))
    assert opp is not None
    assert opp["event_title"] == "My Event"
    assert opp["slug"] == "my-event"
    assert opp["url"] == "https://polymarket.com/event/my-event"
    assert opp["days_left"] == 10
    assert opp["num_outcomes"] == 2
    assert opp["total_cost"] == 0.95
    assert opp["edge_pct"] == round((1 - 0.95) / 0.95 * 100, 2)
    assert opp["min_outcome_liquidity"] == 600
    assert opp["min_outcome_volume_24h"] == 10000
    assert [l["ask"] for l in opp["legs"]] == [0.45, 0.5]  # sorted ascending
    assert all("market_id" in l for l in opp["legs"])


def test_find_opportunity_below_min_volume_returns_none():
    # Regression: liquidityNum is resting order-book depth and can be nonzero
    # even on a market nobody's actually trading (a lone stale/phantom ask).
    # Every leg must clear the same-day volume floor too, or the arb isn't
    # really fillable even though it clears the liquidity check.
    markets = [
        leg(0.4, market_id="a", liquidity=1000, volume=10000),
        leg(0.4, market_id="b", liquidity=1000, volume=100),
    ]
    assert fa.find_opportunity(make_event(markets), NOW, fetch_book=fetch_book_for(markets)) is None


def test_find_opportunity_falls_back_to_ticker_then_unknown():
    markets = [leg(0.4, market_id="a"), leg(0.4, market_id="b")]
    event = make_event(markets, slug=None, title=None)
    event["ticker"] = "TICK"
    opp = fa.find_opportunity(event, NOW, fetch_book=fetch_book_for(markets))
    assert opp["event_title"] == "TICK"
    assert opp["url"] is None


# --- find_opportunity real-depth check (CLOB order book) ------------------

def test_find_opportunity_phantom_quote_thin_book_returns_none():
    # Regression: this is the exact bug that shipped an 800%-edge "arb" on a
    # 6-outcome long-tail market. bestAsk/liquidityNum/volume24hr can all
    # look fine while the real order book has only a few dollars of size at
    # that price -- not enough to fill ARB_TARGET_FILL_USD ($100).
    markets = [
        leg(0.01, market_id="a", liquidity=1000, volume=10000),
        leg(0.02, market_id="b", liquidity=1000, volume=10000),
    ]
    thin_book = {"asks": [{"price": "0.01", "size": "50"}]}  # only $0.50 of real depth
    fetch_book = fetch_book_for(markets, overrides={"a": thin_book})
    assert fa.find_opportunity(make_event(markets), NOW, fetch_book=fetch_book) is None


def test_find_opportunity_walks_multiple_book_levels_for_real_cost():
    # Real depth check should average across levels, not just read the top
    # of book: half the target fills at 0.01, the rest costs more.
    markets = [
        leg(0.01, market_id="a", liquidity=1000, volume=10000),
        leg(0.10, market_id="b", liquidity=1000, volume=10000),
    ]
    stepped_book = {"asks": [
        {"price": "0.01", "size": "5000"},   # $50 worth
        {"price": "0.03", "size": "5000"},   # covers the rest of the $100 target
    ]}
    fetch_book = fetch_book_for(markets, overrides={"a": stepped_book})
    opp = fa.find_opportunity(make_event(markets), NOW, fetch_book=fetch_book)
    assert opp is not None
    leg_a = next(l for l in opp["legs"] if l["market_id"] == "a")
    # $50 at 0.01 (5000 shares) + $50 at 0.03 (1666.67 shares) = $100 / 6666.67 shares
    assert leg_a["sticker_ask"] == 0.01
    assert leg_a["ask"] == round(100 / (5000 + 50 / 0.03), 4)


def test_find_opportunity_none_when_book_unreachable():
    markets = [leg(0.4, market_id="a"), leg(0.4, market_id="b")]
    fetch_book = fetch_book_for(markets, overrides={"a": None})
    assert fa.find_opportunity(make_event(markets), NOW, fetch_book=fetch_book) is None


def test_find_opportunity_none_when_missing_token_id():
    markets = [leg(0.4, market_id="a"), leg(0.4, market_id="b")]
    markets[0]["clobTokenIds"] = None
    fetch_book = fetch_book_for(markets)
    assert fa.find_opportunity(make_event(markets), NOW, fetch_book=fetch_book) is None


def test_find_opportunity_real_edge_below_floor_after_depth_check():
    # Sticker asks clear MIN_EDGE_PCT, but the real fillable price on one leg
    # is worse than the sticker price, dragging the real edge below the floor.
    markets = [
        leg(0.49, market_id="a", liquidity=1000, volume=10000),
        leg(0.49, market_id="b", liquidity=1000, volume=10000),
    ]
    worse_book = {"asks": [{"price": "0.55", "size": "100000"}]}
    fetch_book = fetch_book_for(markets, overrides={"a": worse_book})
    assert fa.find_opportunity(make_event(markets), NOW, fetch_book=fetch_book) is None


# --- walk_ask_book ----------------------------------------------------

def test_walk_ask_book_single_level_returns_that_price():
    asks = [{"price": "0.10", "size": "10000"}]
    assert fa.walk_ask_book(asks, 100) == 0.10


def test_walk_ask_book_sums_across_levels_cheapest_first():
    # unsorted input on purpose -- function must sort ascending itself
    asks = [{"price": "0.05", "size": "2000"}, {"price": "0.02", "size": "1000"}]
    # $20 at 0.02 (1000 shares) fully consumes the cheap level, remaining $80
    # needs 1600 shares at 0.05 -> total 2600 shares for $100
    price = fa.walk_ask_book(asks, 100)
    assert price == 100 / 2600


def test_walk_ask_book_insufficient_depth_returns_none():
    # only $0.50 of real size behind a phantom-looking resting quote
    asks = [{"price": "0.01", "size": "50"}]
    assert fa.walk_ask_book(asks, 100) is None


def test_walk_ask_book_empty_asks_returns_none():
    assert fa.walk_ask_book([], 100) is None


def test_walk_ask_book_skips_zero_or_negative_price_levels():
    asks = [{"price": "0", "size": "999999"}, {"price": "0.10", "size": "10000"}]
    assert fa.walk_ask_book(asks, 100) == 0.10


def test_walk_ask_book_malformed_level_returns_none():
    assert fa.walk_ask_book([{"price": "not-a-number", "size": "10"}], 100) is None


# --- leg_token_id -------------------------------------------------------

def test_leg_token_id_parses_json_string_and_takes_first():
    market = {"clobTokenIds": json.dumps(["yes-tok", "no-tok"])}
    assert fa.leg_token_id(market) == "yes-tok"


def test_leg_token_id_accepts_already_parsed_list():
    market = {"clobTokenIds": ["yes-tok", "no-tok"]}
    assert fa.leg_token_id(market) == "yes-tok"


def test_leg_token_id_missing_field_returns_none():
    assert fa.leg_token_id({}) is None


def test_leg_token_id_malformed_json_returns_none():
    assert fa.leg_token_id({"clobTokenIds": "{not valid json"}) is None


def test_leg_token_id_empty_list_returns_none():
    assert fa.leg_token_id({"clobTokenIds": json.dumps([])}) is None


# --- estimate_taker_fee -----------------------------------------------

def test_estimate_taker_fee_zero_when_fees_disabled():
    market = {"feesEnabled": False, "feeSchedule": {"rate": 0.04}}
    assert fa.estimate_taker_fee(market, 0.5) == 0.0


def test_estimate_taker_fee_zero_when_no_fee_schedule():
    market = {"feesEnabled": True}
    assert fa.estimate_taker_fee(market, 0.5) == 0.0


def test_estimate_taker_fee_matches_polymarket_formula():
    # fee = rate * shares * price * (1 - price)
    market = {"feesEnabled": True, "feeSchedule": {"rate": 0.04}}
    assert fa.estimate_taker_fee(market, 0.5) == 0.04 * 1 * 0.5 * 0.5
    assert fa.estimate_taker_fee(market, 0.5, shares=10) == 0.04 * 10 * 0.5 * 0.5


def test_estimate_taker_fee_peaks_at_50_cents():
    market = {"feesEnabled": True, "feeSchedule": {"rate": 0.04}}
    fee_50 = fa.estimate_taker_fee(market, 0.50)
    fee_20 = fa.estimate_taker_fee(market, 0.20)
    fee_80 = fa.estimate_taker_fee(market, 0.80)
    assert fee_50 > fee_20
    assert fee_50 > fee_80
    assert round(fee_20, 10) == round(fee_80, 10)  # symmetric around 0.5


def test_estimate_taker_fee_zero_at_price_extremes():
    market = {"feesEnabled": True, "feeSchedule": {"rate": 0.04}}
    assert fa.estimate_taker_fee(market, 0.0) == 0.0
    assert fa.estimate_taker_fee(market, 1.0) == 0.0


# --- find_opportunity fee-awareness -------------------------------------

def test_find_opportunity_total_cost_includes_fees_when_enabled():
    markets = [
        leg(0.5, market_id="a", liquidity=600, fees_enabled=True, fee_rate=0.04),
        leg(0.45, market_id="b", liquidity=700, fees_enabled=True, fee_rate=0.04),
    ]
    event = make_event(markets)
    opp = fa.find_opportunity(event, NOW, fetch_book=fetch_book_for(markets))
    assert opp is not None
    expected_fee = (0.04*0.5*0.5) + (0.04*0.45*0.55)
    assert opp["ask_cost"] == 0.95
    assert opp["total_fee"] == round(expected_fee, 4)
    assert opp["total_cost"] == round(0.95 + expected_fee, 4)
    # edge_pct should be computed against the fee-inclusive cost, not ask_cost alone
    assert opp["edge_pct"] == round((1 - opp["total_cost"]) / opp["total_cost"] * 100, 2)


def test_find_opportunity_fees_can_erase_an_edge_that_looked_valid_ignoring_them():
    # ask-only edge is (1-0.995)/0.995*100 = 0.503%, just above MIN_EDGE_PCT=0.5,
    # but a real fee on both legs should push the fee-inclusive edge below the floor
    markets = [
        leg(0.50, market_id="a", liquidity=600, fees_enabled=True, fee_rate=0.04),
        leg(0.495, market_id="b", liquidity=700, fees_enabled=True, fee_rate=0.04),
    ]
    event = make_event(markets)
    assert fa.find_opportunity(event, NOW, fetch_book=fetch_book_for(markets)) is None


def test_find_opportunity_mixed_fee_status_per_leg():
    # one leg has fees enabled, the other doesn't - only the enabled leg should
    # contribute a nonzero fee, confirming fees are evaluated per-market, not
    # assumed uniform across an event
    markets = [
        leg(0.5, market_id="a", liquidity=600, fees_enabled=True, fee_rate=0.04),
        leg(0.45, market_id="b", liquidity=700, fees_enabled=False),
    ]
    event = make_event(markets)
    opp = fa.find_opportunity(event, NOW, fetch_book=fetch_book_for(markets))
    assert opp["total_fee"] == round(0.04*0.5*0.5, 4)


# --- find_calibration_signal -------------------------------------------

def sig_bins(significant=True, bias_pct=10.0, resolved_yes_rate=0.6, sample_size=40):
    return [{
        "range": [0.4, 0.5],
        "significant": significant,
        "bias_pct": bias_pct,
        "resolved_yes_rate": resolved_yes_rate,
        "sample_size": sample_size,
    }]


def test_find_calibration_signal_price_out_of_bounds():
    market = {"lastTradePrice": 0, "liquidityNum": 1000, "volume24hr": 10000}
    assert fa.find_calibration_signal(market, {}, sig_bins(), NOW) is None


def test_find_calibration_signal_low_liquidity():
    market = {"lastTradePrice": 0.45, "liquidityNum": 10, "volume24hr": 10000}
    assert fa.find_calibration_signal(market, {}, sig_bins(), NOW) is None


def test_find_calibration_signal_low_volume():
    # Regression: liquidityNum measures resting order-book depth, which a market
    # can have with zero real trades. Most of Polymarket's long-tail auto-
    # generated props never get organically traded, so lastTradePrice is null
    # and the price falls back to bestAsk -- a lone resting quote, not a real
    # probability. This is the fix for CAL repeatedly re-monopolizing on those
    # phantom-quote markets even after the liquidity floor alone was raised;
    # MIS already had this exact guard (test_find_mispricing_signal_low_volume_returns_none).
    market = {"lastTradePrice": 0.45, "liquidityNum": 1000, "volume24hr": 100}
    assert fa.find_calibration_signal(market, {}, sig_bins(), NOW) is None


def test_find_calibration_signal_bucket_not_significant():
    market = {"lastTradePrice": 0.45, "liquidityNum": 1000, "volume24hr": 10000}
    assert fa.find_calibration_signal(market, {}, sig_bins(significant=False), NOW) is None


def test_find_calibration_signal_positive_bias_recommends_yes():
    market = {"lastTradePrice": 0.45, "liquidityNum": 1000, "volume24hr": 10000, "id": "mk1", "question": "Q?"}
    event = {"endDate": "2026-01-11T00:00:00Z", "slug": "s", "title": "T"}
    sig = fa.find_calibration_signal(market, event, sig_bins(bias_pct=10.0, resolved_yes_rate=0.6), NOW)
    assert sig is not None
    assert sig["recommended_side"] == "YES"
    assert sig["implied_cost"] == 0.45
    assert sig["market_id"] == "mk1"
    assert sig["days_left"] == 10


def test_find_calibration_signal_negative_bias_recommends_no():
    market = {"lastTradePrice": 0.45, "liquidityNum": 1000, "volume24hr": 10000, "id": "mk1"}
    event = {"endDate": "2026-01-11T00:00:00Z", "slug": "s"}
    sig = fa.find_calibration_signal(market, event, sig_bins(bias_pct=-10.0, resolved_yes_rate=0.2), NOW)
    assert sig is not None
    assert sig["recommended_side"] == "NO"
    assert sig["implied_cost"] == 0.55  # 1 - 0.45


def test_find_calibration_signal_below_min_edge_returns_none():
    # implied_cost=0.59, true_rate=0.6 -> edge = (0.6-0.59)*100 = 1pt, below MIN_CALIBRATION_EDGE_PCT=2.0
    market = {"lastTradePrice": 0.59, "liquidityNum": 1000, "volume24hr": 10000}
    sig = fa.find_calibration_signal(market, {}, sig_bins(bias_pct=1.0, resolved_yes_rate=0.6), NOW)
    assert sig is None


def test_find_calibration_signal_edge_pct_is_a_point_gap_not_a_ratio():
    # true_rate=0.6, cost=0.001 (near-zero) -> a ratio-over-cost formula would
    # blow up to tens of thousands of percent; the point-gap formula instead
    # stays bounded at (0.6-0.001)*100 ~= 59.9, matching find_mispricing_signal's
    # edge_pts convention. This is the fix for the bug that let one low-cost
    # bucket's inflated edge_pct monopolize the Top-N ranking.
    market = {"lastTradePrice": 0.999, "liquidityNum": 1000, "volume24hr": 10000}
    bins = [{"range": [0.95, 1.0], "significant": True, "bias_pct": -10.0,
             "resolved_yes_rate": 0.4, "sample_size": 40}]
    sig = fa.find_calibration_signal(market, {}, bins, NOW)
    assert sig is not None
    assert sig["edge_pct"] < 100


def test_find_calibration_signal_implied_cost_includes_real_taker_fee():
    # price=0.45, fee = rate * price * (1-price) = 0.04 * 0.45 * 0.55 = 0.0099
    market = {"lastTradePrice": 0.45, "liquidityNum": 1000, "volume24hr": 10000, "feesEnabled": True,
              "feeSchedule": {"rate": 0.04}}
    event = {"endDate": "2026-01-11T00:00:00Z", "slug": "s"}
    sig = fa.find_calibration_signal(market, event, sig_bins(bias_pct=10.0, resolved_yes_rate=0.6), NOW)
    assert sig is not None
    expected_fee = round(0.04 * 0.45 * 0.55, 4)
    assert sig["fee"] == expected_fee
    assert sig["implied_cost"] == round(0.45 + expected_fee, 4)
    # fee-inclusive edge is strictly worse than the no-fee edge would have been
    no_fee_edge = (0.6 - 0.45) * 100
    assert sig["edge_pct"] < no_fee_edge


# --- find_mispricing_signal ----------------------------------------------

def mis_bins(resolved_yes_rate=0.5, sample_size=40):
    return [{"range": [0.2, 0.35], "resolved_yes_rate": resolved_yes_rate, "sample_size": sample_size}]


def test_find_mispricing_signal_low_volume_returns_none():
    market = {"lastTradePrice": 0.3, "volume24hr": 100}
    assert fa.find_mispricing_signal(market, {}, mis_bins(), NOW) is None


def test_find_mispricing_signal_no_bucket_data_returns_none():
    market = {"lastTradePrice": 0.3, "volume24hr": 10000}
    empty_bins = [{"range": [0.2, 0.35], "resolved_yes_rate": None, "sample_size": 5}]
    assert fa.find_mispricing_signal(market, {}, empty_bins, NOW) is None


def test_find_mispricing_signal_below_min_edge_returns_none():
    # implied=0.3, fair=0.35 -> 5pt gap, below MISPRICING_MIN_EDGE_PTS=15
    market = {"lastTradePrice": 0.3, "volume24hr": 10000}
    assert fa.find_mispricing_signal(market, {}, mis_bins(resolved_yes_rate=0.35), NOW) is None


def test_find_mispricing_signal_valid_case_recommends_correct_side():
    # implied=0.3, fair=0.5 -> 20pt gap, fair > implied -> YES
    market = {"lastTradePrice": 0.3, "volume24hr": 10000, "id": "mk2", "question": "Q?"}
    event = {"endDate": (NOW + datetime.timedelta(days=5)).isoformat(), "slug": "s"}
    sig = fa.find_mispricing_signal(market, event, mis_bins(resolved_yes_rate=0.5), NOW)
    assert sig is not None
    assert sig["recommended_side"] == "YES"
    assert sig["edge_pct"] == 20.0
    assert sig["market_id"] == "mk2"
    assert sig["low_sample_warning"] is False  # sample_size=40 >= 30


def test_find_mispricing_signal_low_sample_warning_flag():
    market = {"lastTradePrice": 0.3, "volume24hr": 10000}
    event = {"endDate": (NOW + datetime.timedelta(days=5)).isoformat(), "slug": "s"}
    sig = fa.find_mispricing_signal(market, event, mis_bins(resolved_yes_rate=0.5, sample_size=10), NOW)
    assert sig["low_sample_warning"] is True


def test_find_mispricing_signal_long_horizon_needs_bigger_edge():
    # 40 days out, 20pt gap: passes MIN_EDGE_PTS(15) but fails at horizon check
    # since 20 <= LONGTERM_MIN_EDGE_PTS(25)
    market = {"lastTradePrice": 0.3, "volume24hr": 10000}
    event = {"endDate": (NOW + datetime.timedelta(days=40)).isoformat(), "slug": "s"}
    assert fa.find_mispricing_signal(market, event, mis_bins(resolved_yes_rate=0.5), NOW) is None


def test_find_mispricing_signal_long_horizon_with_big_edge_included():
    # 40 days out, implied=0.1 fair=0.5 -> 40pt gap, exceeds LONGTERM_MIN_EDGE_PTS(25)
    market = {"lastTradePrice": 0.1, "volume24hr": 10000}
    event = {"endDate": (NOW + datetime.timedelta(days=40)).isoformat(), "slug": "s"}
    bins = [{"range": [0.05, 0.2], "resolved_yes_rate": 0.5, "sample_size": 40}]
    sig = fa.find_mispricing_signal(market, event, bins, NOW)
    assert sig is not None


def test_find_mispricing_signal_implied_cost_includes_real_taker_fee():
    # implied=0.3, fair=0.5 -> YES side; fee = 0.04 * 0.3 * 0.7 = 0.0084
    market = {"lastTradePrice": 0.3, "volume24hr": 10000, "feesEnabled": True,
              "feeSchedule": {"rate": 0.04}}
    event = {"endDate": (NOW + datetime.timedelta(days=5)).isoformat(), "slug": "s"}
    sig = fa.find_mispricing_signal(market, event, mis_bins(resolved_yes_rate=0.5), NOW)
    assert sig is not None
    expected_fee = round(0.04 * 0.3 * 0.7, 4)
    assert sig["fee"] == expected_fee
    assert sig["implied_cost"] == round(0.3 + expected_fee, 4)
    # fee eats into the raw 20pt gap
    assert sig["edge_pct"] < 20.0
