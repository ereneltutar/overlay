from unittest.mock import patch

import calibration_scan as cs


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


def test_compute_bins_returns_20_buckets_for_default_width():
    bins = cs.compute_bins([])
    assert len(bins) == 20
    assert bins[0]["range"] == [0.0, 0.05]
    assert bins[-1]["range"] == [0.95, 1.0]


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
    assert last["range"] == [0.95, 1.0]
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


# --- fetch_market_resolution -----------------------------------------------
# The actual HTTP/retry/ambiguous-outcome behavior lives in gamma_client now
# and is covered by tests/test_gamma_client.py; these just confirm the thin
# wrapper here delegates and translates (closed, outcome_yes) correctly.

def test_fetch_market_resolution_delegates_to_gamma_client():
    with patch.object(cs.gamma_client, "fetch_market_state", return_value=(True, True)):
        assert cs.fetch_market_resolution("m1") is True
    with patch.object(cs.gamma_client, "fetch_market_state", return_value=(True, False)):
        assert cs.fetch_market_resolution("m1") is False


def test_fetch_market_resolution_none_when_not_closed():
    with patch.object(cs.gamma_client, "fetch_market_state", return_value=(False, None)):
        assert cs.fetch_market_resolution("m1") is None


def test_fetch_market_resolution_none_when_closed_but_ambiguous():
    with patch.object(cs.gamma_client, "fetch_market_state", return_value=(True, None)):
        assert cs.fetch_market_resolution("m1") is None
