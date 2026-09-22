import crypto_price_gate as cpg


# --- asset_id_for_question ------------------------------------------------

def test_asset_id_for_question_matches_ethereum():
    assert cpg.asset_id_for_question("Will Ethereum dip to $1,700 in September?") == "ethereum"


def test_asset_id_for_question_matches_eth_abbreviation():
    assert cpg.asset_id_for_question("Will ETH reach $3,300 in September?") == "ethereum"


def test_asset_id_for_question_matches_dogecoin():
    assert cpg.asset_id_for_question("Will Dogecoin reach $0.15 in September?") == "dogecoin"


def test_asset_id_for_question_does_not_match_substring_false_positive():
    # Regression: "eth" must not match inside "Elizabeth" (same guard as
    # market_category.is_crypto_market).
    assert cpg.asset_id_for_question("Will Elizabeth win the election?") is None


def test_asset_id_for_question_no_match_returns_none():
    assert cpg.asset_id_for_question("Will Fulham FC win on 2026-08-24?") is None


def test_asset_id_for_question_none_or_empty_returns_none():
    assert cpg.asset_id_for_question(None) is None
    assert cpg.asset_id_for_question("") is None


# --- extract_price_target --------------------------------------------------

def test_extract_price_target_simple():
    assert cpg.extract_price_target("Will Dogecoin reach $0.15 in September?") == 0.15


def test_extract_price_target_thousands_separator():
    assert cpg.extract_price_target("Will Ethereum dip to $1,700 in September?") == 1700.0


def test_extract_price_target_no_dollar_amount_returns_none():
    assert cpg.extract_price_target("Will Fulham FC win on 2026-08-24?") is None


def test_extract_price_target_none_returns_none():
    assert cpg.extract_price_target(None) is None


# --- max_plausible_move_pct -------------------------------------------------

def test_max_plausible_move_pct_scales_with_sqrt_days():
    move_1_day = cpg.max_plausible_move_pct("ethereum", 1)
    move_4_days = cpg.max_plausible_move_pct("ethereum", 4)
    # sqrt(4)/sqrt(1) = 2
    assert round(move_4_days / move_1_day, 4) == 2.0


def test_max_plausible_move_pct_unknown_asset_uses_default_vol():
    bound = cpg.max_plausible_move_pct("some-unlisted-coin", 9)
    assert bound == cpg.MAX_VOL_MULTIPLE * cpg.DEFAULT_DAILY_VOL * 3.0  # sqrt(9) = 3


def test_max_plausible_move_pct_negative_days_treated_as_zero():
    assert cpg.max_plausible_move_pct("ethereum", -5) == 0.0


# --- fetch_live_prices -------------------------------------------------

def test_fetch_live_prices_empty_ids_returns_empty_dict():
    assert cpg.fetch_live_prices([]) == {}


# --- passes_price_sanity_gate ------------------------------------------

def test_passes_price_sanity_gate_non_crypto_question_always_passes():
    assert cpg.passes_price_sanity_gate("Will Fulham FC win?", 9, {}) is True


def test_passes_price_sanity_gate_missing_live_price_passes():
    # Can't evaluate (CoinGecko unreachable / asset not fetched this run) ->
    # never block a signal on missing data.
    q = "Will Ethereum dip to $1,700 in September?"
    assert cpg.passes_price_sanity_gate(q, 9, {}) is True


def test_passes_price_sanity_gate_no_dollar_target_passes():
    assert cpg.passes_price_sanity_gate("Will Ethereum crash in September?", 9, {"ethereum": 2733.0}) is True


def test_passes_price_sanity_gate_vetoes_implausible_eth_dip():
    # Live diagnostic case: ETH ~$2,733, target $1,700, ~9 days left ->
    # required move ~-37.8%, bound = 3 * 0.04 * sqrt(9) = 36%. Implausible.
    q = "Will Ethereum dip to $1,700 in September?"
    assert cpg.passes_price_sanity_gate(q, 9, {"ethereum": 2733.0}) is False


def test_passes_price_sanity_gate_vetoes_implausible_doge_rally():
    # Live diagnostic case: DOGE ~$0.09, target $0.15, ~9 days left ->
    # required move ~+66.7%, bound = 3 * 0.06 * sqrt(9) = 54%. Implausible.
    q = "Will Dogecoin reach $0.15 in September?"
    assert cpg.passes_price_sanity_gate(q, 9, {"dogecoin": 0.09}) is False


def test_passes_price_sanity_gate_allows_plausible_eth_rally():
    # Live diagnostic case: ETH ~$2,733, target $3,300, ~9 days left ->
    # required move ~+20.7%, bound = 36%. Plausible -- gate should not veto,
    # leaving the bucket calibration math to decide.
    q = "Will Ethereum reach $3,300 in September?"
    assert cpg.passes_price_sanity_gate(q, 9, {"ethereum": 2733.0}) is True


def test_passes_price_sanity_gate_target_at_current_price_always_passes():
    q = "Will Ethereum reach $2,733 in September?"
    assert cpg.passes_price_sanity_gate(q, 9, {"ethereum": 2733.0}) is True


def test_passes_price_sanity_gate_zero_or_missing_current_price_passes():
    q = "Will Ethereum dip to $1,700 in September?"
    assert cpg.passes_price_sanity_gate(q, 9, {"ethereum": 0}) is True
