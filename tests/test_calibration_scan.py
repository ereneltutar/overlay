import calibration_scan as cs
import fetch_arbitrage as fa


# --- wilson_interval -------------------------------------------------------

def test_wilson_interval_zero_samples():
    assert cs.wilson_interval(0, 0) == (0.0, 0.0)


def test_wilson_interval_all_yes_stays_within_bounds():
    lo, hi = cs.wilson_interval(50, 50)
    assert 0.0 < lo <= 1.0
    assert hi == 1.0


def test_wilson_interval_half_yes_is_centered_near_half():
    lo, hi = cs.wilson_interval(50, 100)
    assert lo < 0.5 < hi


def test_wilson_interval_wider_for_smaller_n():
    lo_small, hi_small = cs.wilson_interval(5, 10)
    lo_big, hi_big = cs.wilson_interval(500, 1000)
    assert (hi_small - lo_small) > (hi_big - lo_big)


# --- compute_bins ----------------------------------------------------------

def make_samples(price, yes_count, no_count):
    return [{"reference_price": price, "resolved_yes": True} for _ in range(yes_count)] + \
           [{"reference_price": price, "resolved_yes": False} for _ in range(no_count)]


def test_compute_bins_returns_28_buckets_for_default_widths():
    # 18 mid-range 0.05-wide buckets ([0.05,0.95)) plus 5 finer 0.01-wide
    # buckets at each tail ([0,0.05) and [0.95,1.0]).
    bins = cs.compute_bins([])
    assert len(bins) == 28
    assert bins[0]["range"] == [0.0, 0.01]
    assert bins[-1]["range"] == [0.99, 1.0]


def test_compute_bins_below_min_sample_has_null_stats():
    samples = make_samples(0.42, yes_count=5, no_count=5)  # n=10, below MIN_SAMPLE_PER_BUCKET=30
    bins = cs.compute_bins(samples)
    bucket = next(b for b in bins if b["range"] == [0.4, 0.45])
    assert bucket["sample_size"] == 10
    assert bucket["resolved_yes_rate"] is None
    assert bucket["significant"] is False


def test_compute_bins_at_min_sample_computes_stats():
    samples = make_samples(0.42, yes_count=25, no_count=5)  # n=30, at threshold
    bins = cs.compute_bins(samples)
    bucket = next(b for b in bins if b["range"] == [0.4, 0.45])
    assert bucket["sample_size"] == 30
    assert bucket["resolved_yes_rate"] == round(25 / 30, 4)
    assert bucket["bias_pct"] is not None


def test_compute_bins_price_exactly_one_falls_in_last_bucket():
    samples = make_samples(1.0, yes_count=30, no_count=0)
    bins = cs.compute_bins(samples)
    last = bins[-1]
    assert last["range"] == [0.99, 1.0]
    assert last["sample_size"] == 30


def test_compute_bins_significant_when_far_from_midpoint():
    # bucket [0.40, 0.45), midpoint 0.425; samples resolve YES far more often
    # than the midpoint implies, with a large enough n for a tight interval
    samples = make_samples(0.42, yes_count=29, no_count=1)  # n=30, 96.7% yes vs midpoint 42.5%
    bins = cs.compute_bins(samples)
    bucket = next(b for b in bins if b["range"] == [0.4, 0.45])
    assert bucket["significant"] is True
    assert bucket["bias_pct"] > 0


def test_compute_bins_not_significant_near_midpoint():
    # resolved rate matches the midpoint closely -> not significant
    samples = make_samples(0.425, yes_count=15, no_count=15)
    bins = cs.compute_bins(samples)
    bucket = next(b for b in bins if b["range"] == [0.4, 0.45])
    assert bucket["significant"] is False


def test_compute_bins_extreme_tail_bucket_does_not_blend_with_near_certain_price():
    # Regression for the bug that made the simulator lose big: a wide top
    # bucket used to average markets priced 95%-100% into one historical
    # rate, so a market priced at 99.9% (effectively decided) got judged
    # against the SAME rate as one priced at 95% (genuinely uncertain).
    # With tail bins split to 0.01 width, a market priced at 0.999 falls in
    # [0.99, 1.0) and must not be pooled with samples priced around 0.95.
    uncertain_95 = make_samples(0.951, yes_count=22, no_count=8)   # noisy, near 95%
    near_certain_999 = make_samples(0.999, yes_count=30, no_count=0)  # always resolves YES
    bins = cs.compute_bins(uncertain_95 + near_certain_999)

    bucket_95 = next(b for b in bins if b["range"] == [0.95, 0.96])
    bucket_999 = next(b for b in bins if b["range"] == [0.99, 1.0])

    assert bucket_95["sample_size"] == 30
    assert bucket_999["sample_size"] == 30
    assert bucket_999["resolved_yes_rate"] == 1.0
    # The two buckets must be scored independently, not blended into one
    # [0.95, 1.0) average.
    assert bucket_95["resolved_yes_rate"] != bucket_999["resolved_yes_rate"]


# --- partition_log_entries --------------------------------------------------
# The core fix for a real bug: a fixed "check the oldest N" cap re-verifies
# already-resolved markets forever and, once the log exceeds N entries,
# never reaches anything past position N since the oldest N never change.
# These confirm the cache-aware split spends the per-run budget only on
# markets whose resolution isn't already known.

def entry(market_id, price=0.4):
    return {"market_id": market_id, "price": price, "logged_at": f"2026-08-{market_id}"}


def test_partition_cached_entries_never_consume_the_check_budget():
    entries = [entry("01"), entry("02"), entry("03")]
    cache = {"01": {"outcome_yes": True, "checked_at": "x"}}
    cached, to_check, deferred = cs.partition_log_entries(entries, cache, max_to_check=10)
    assert [e["market_id"] for e in cached] == ["01"]
    assert [e["market_id"] for e in to_check] == ["02", "03"]
    assert deferred == []


def test_partition_respects_max_to_check_budget():
    entries = [entry(f"{i:02d}") for i in range(5)]
    cached, to_check, deferred = cs.partition_log_entries(entries, {}, max_to_check=2)
    assert len(to_check) == 2
    assert len(deferred) == 3
    # oldest-first: the budget goes to the first two entries in list order
    assert [e["market_id"] for e in to_check] == ["00", "01"]
    assert [e["market_id"] for e in deferred] == ["02", "03", "04"]


def test_partition_cached_entries_dont_count_against_budget_even_if_earlier():
    # a resolved entry sitting ahead of un-resolved ones shouldn't eat into
    # the budget meant for entries that still need an API call
    entries = [entry("01"), entry("02"), entry("03")]
    cache = {"01": {"outcome_yes": True, "checked_at": "x"}}
    cached, to_check, deferred = cs.partition_log_entries(entries, cache, max_to_check=1)
    assert [e["market_id"] for e in cached] == ["01"]
    assert [e["market_id"] for e in to_check] == ["02"]
    assert [e["market_id"] for e in deferred] == ["03"]


def test_partition_all_cached_means_nothing_to_check():
    entries = [entry("01"), entry("02")]
    cache = {"01": {"outcome_yes": True, "checked_at": "x"}, "02": {"outcome_yes": None, "checked_at": "x"}}
    cached, to_check, deferred = cs.partition_log_entries(entries, cache, max_to_check=10)
    assert len(cached) == 2
    assert to_check == []
    assert deferred == []


# --- filter_by_liquidity ----------------------------------------------------
# Regression for the bug that made the simulator lose ~52% of its bankroll
# right after the tail-bucket fix above shipped: price_log.jsonl was logged
# with a looser liquidity floor ($50, CALIBRATION_LOG_MIN_LIQUIDITY) than the
# floor live signals require ($100, MIN_CALIBRATION_LIQUIDITY_USD), so
# single-trade, $50-99-liquidity markets -- priced near 0 or 1 with no real
# conviction behind it -- entered the [0.99, 1.0] bucket's historical rate
# and dragged it from ~100% down to ~74%, making genuinely well-calibrated,
# high-liquidity live markets look like a 26-point edge.

def liquidity_entry(market_id, liquidity):
    return {"market_id": market_id, "price": 0.995, "liquidity": liquidity}


def test_filter_by_liquidity_drops_entries_below_floor():
    entries = [liquidity_entry("01", 49), liquidity_entry("02", 50), liquidity_entry("03", 5000)]
    kept = cs.filter_by_liquidity(entries, min_liquidity=50)
    assert [e["market_id"] for e in kept] == ["02", "03"]


def test_filter_by_liquidity_treats_missing_liquidity_as_zero():
    entries = [{"market_id": "01", "price": 0.995}]
    assert cs.filter_by_liquidity(entries, min_liquidity=50) == []


# --- build_sample / split_samples_by_category ------------------------------
# Regression for the crypto calibration blind spot: live paper-trading data
# (Aug 18-24 2026) showed crypto threshold markets ("will Bitcoin be above
# $X") went 0-for-9 despite calibration flagging them as a significant edge,
# because they were pooled into the same price-only bucket as sports/weather.
# These confirm crypto samples get tagged and split out into their own table.

def log_entry(market_id, price, question, liquidity=1000):
    return {"market_id": market_id, "price": price, "question": question, "liquidity": liquidity}


def test_build_sample_tags_crypto_market():
    sample = cs.build_sample(log_entry("01", 0.4, "Will Bitcoin reach $65,000 on August 18?"), True)
    assert sample == {"reference_price": 0.4, "resolved_yes": True, "is_crypto": True}


def test_build_sample_tags_non_crypto_market():
    sample = cs.build_sample(log_entry("02", 0.4, "Will Fulham FC win on 2026-08-24?"), False)
    assert sample == {"reference_price": 0.4, "resolved_yes": False, "is_crypto": False}


def test_split_samples_by_category_separates_crypto_from_general():
    samples = [
        cs.build_sample(log_entry("01", 0.4, "Will Bitcoin reach $65,000?"), True),
        cs.build_sample(log_entry("02", 0.4, "Will Fulham FC win?"), False),
        cs.build_sample(log_entry("03", 0.5, "Will Ethereum reach $2,400?"), True),
    ]
    general, crypto = cs.split_samples_by_category(samples)
    assert len(general) == 1
    assert len(crypto) == 2
    assert all(s["is_crypto"] for s in crypto)
    assert all(not s["is_crypto"] for s in general)


def test_split_samples_by_category_feeds_compute_bins_independently():
    # A crypto sample and a non-crypto sample at the same price must not end
    # up pooled into the same bucket's historical rate.
    crypto_samples = [cs.build_sample(log_entry(str(i), 0.42, "Will Bitcoin reach $65,000?"), True)
                       for i in range(30)]
    general_samples = [cs.build_sample(log_entry(str(i), 0.42, "Will Fulham FC win?"), False)
                        for i in range(100, 130)]
    general, crypto = cs.split_samples_by_category(general_samples + crypto_samples)
    general_bins = cs.compute_bins(general)
    crypto_bins = cs.compute_bins(crypto)

    general_bucket = next(b for b in general_bins if b["range"] == [0.4, 0.45])
    crypto_bucket = next(b for b in crypto_bins if b["range"] == [0.4, 0.45])
    assert general_bucket["resolved_yes_rate"] == 0.0
    assert crypto_bucket["resolved_yes_rate"] == 1.0


def test_calibration_log_floor_matches_live_signal_floor():
    # CALIBRATION_LOG_MIN_LIQUIDITY (what gets logged as training data) must
    # never be looser than MIN_CALIBRATION_LIQUIDITY_USD (what a live signal
    # requires) or MIN_SAMPLE_LIQUIDITY_USD (this script's own re-filter) --
    # otherwise thin markets that could never fire a live signal themselves
    # sneak back into the historical rate that scores every live signal. Same
    # logic for the 24h-volume floors: CALIBRATION_LOG_MIN_VOLUME_24H must
    # never be looser than MIN_CALIBRATION_VOLUME_24H_USD, or untraded,
    # bestAsk-only "phantom quote" markets sneak back into the training data
    # even though they could never fire a live signal (see
    # MIN_CALIBRATION_VOLUME_24H_USD's docstring in fetch_arbitrage.py).
    assert fa.CALIBRATION_LOG_MIN_LIQUIDITY >= fa.MIN_CALIBRATION_LIQUIDITY_USD
    assert cs.MIN_SAMPLE_LIQUIDITY_USD >= fa.MIN_CALIBRATION_LIQUIDITY_USD
    assert fa.CALIBRATION_LOG_MIN_VOLUME_24H >= fa.MIN_CALIBRATION_VOLUME_24H_USD

