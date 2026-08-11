import datetime
from unittest.mock import patch

import pytest

import track_bets as tb

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def fresh_log(starting=1000.0):
    return {"starting_bankroll": starting, "bankroll": starting, "bankroll_history": [], "bets": []}


def make_bet(tag="calibration", status="open", stake=100.0, entry_cost=0.5,
             recommended_side="YES", pnl=None, market_ids=("m1",), deadline=None):
    return {
        "bet_id": f"{tag}:x", "tag": tag, "market_question": "Q", "slug": "x", "url": None,
        "placed_at": NOW.isoformat(),
        "deadline": (deadline or (NOW - datetime.timedelta(days=1))).isoformat(),
        "market_ids": list(market_ids), "recommended_side": recommended_side,
        "entry_cost": entry_cost, "edge_pct_at_placement": 10.0, "stake_usd": stake,
        "status": status, "resolved_at": None, "pnl_usd": pnl,
    }


# --- bankroll accounting ---------------------------------------------------

def test_total_bankroll_unaffected_by_open_bets():
    log = fresh_log()
    log["bets"].append(make_bet(status="open", stake=200.0, pnl=None))
    assert tb.total_bankroll(log) == 1000.0


def test_total_bankroll_reflects_realized_pnl():
    log = fresh_log()
    log["bets"].append(make_bet(status="won", pnl=50.0))
    log["bets"].append(make_bet(status="lost", pnl=-30.0))
    assert tb.total_bankroll(log) == 1020.0


def test_available_bankroll_subtracts_open_stake():
    log = fresh_log()
    log["bets"].append(make_bet(status="open", stake=150.0))
    log["bets"].append(make_bet(status="won", pnl=50.0, stake=999))  # stake irrelevant once resolved
    assert tb.available_bankroll(log) == 1000.0 + 50.0 - 150.0


# --- sizing_stake ------------------------------------------------------

def test_sizing_stake_scales_with_edge():
    small = tb.sizing_stake("calibration", edge_pct=0.5, bankroll_avail=1000)
    big = tb.sizing_stake("calibration", edge_pct=1.0, bankroll_avail=1000)
    assert small < big


def test_sizing_stake_respects_floor():
    stake = tb.sizing_stake("calibration", edge_pct=0.01, bankroll_avail=1000)
    assert stake == tb.STAKE_FLOOR_USD


def test_sizing_stake_respects_cap():
    stake = tb.sizing_stake("calibration", edge_pct=1000, bankroll_avail=1000)
    assert stake == round(1000 * tb.STAKE_CAP_FRAC["calibration"], 2)


def test_sizing_stake_arb_has_higher_cap_than_calibration():
    arb = tb.sizing_stake("arbitrage", edge_pct=1000, bankroll_avail=1000)
    cal = tb.sizing_stake("calibration", edge_pct=1000, bankroll_avail=1000)
    assert arb > cal


# --- kelly_fraction ------------------------------------------------------

def test_kelly_fraction_positive_when_p_beats_breakeven():
    assert tb.kelly_fraction(0.7, 1.0) == pytest.approx(0.4)  # 0.7 - 0.3/1.0


def test_kelly_fraction_zero_when_no_edge():
    assert tb.kelly_fraction(0.5, 0.1) == 0.0  # 0.5 - 0.5/0.1 is deeply negative


def test_kelly_fraction_zero_when_b_not_positive():
    assert tb.kelly_fraction(0.9, 0.0) == 0.0


# --- prob_bucket_key -------------------------------------------------------

def test_prob_bucket_key_buckets_by_width():
    assert tb.prob_bucket_key(0.12) == 2  # falls in [0.10, 0.15)


def test_prob_bucket_key_clamps_at_top_bucket():
    assert tb.prob_bucket_key(1.0) == tb.prob_bucket_key(0.999)


# --- own_track_record --------------------------------------------------

def test_own_track_record_counts_resolved_cal_and_mis_by_bucket():
    log = fresh_log()
    for tag, status in [("calibration", "won"), ("calibration", "lost"), ("mispricing", "won")]:
        bet = make_bet(tag=tag, status=status)
        bet["predicted_win_prob"] = 0.97  # all land in the same bucket
        log["bets"].append(bet)
    track = tb.own_track_record(log)
    key = tb.prob_bucket_key(0.97)
    assert track[key] == (2, 3)  # 2 wins out of 3


def test_own_track_record_ignores_arbitrage_open_and_unpredicted_bets():
    log = fresh_log()
    arb = make_bet(tag="arbitrage", status="won"); arb["predicted_win_prob"] = 0.9
    still_open = make_bet(tag="calibration", status="open"); still_open["predicted_win_prob"] = 0.9
    no_prediction = make_bet(tag="calibration", status="won"); no_prediction["predicted_win_prob"] = None
    log["bets"] += [arb, still_open, no_prediction]
    assert tb.own_track_record(log) == {}


# --- kelly_stake ------------------------------------------------------

def test_kelly_stake_matches_predicted_prob_when_no_track_record():
    stake = tb.kelly_stake(entry_cost=0.5, predicted_win_prob=0.7, track={},
                            bankroll_avail=1000, cap_frac=0.05)
    # f = 0.7 - 0.3/1.0 = 0.4; stake = 1000*0.4*0.5 = 200, capped at 1000*0.05 = 50
    assert stake == 50.0


def test_kelly_stake_ignores_track_record_below_min_sample():
    key = tb.prob_bucket_key(0.9)
    thin_track = {key: (1, 3)}  # only 3 samples, below OWN_TRACK_MIN_SAMPLE
    with_track = tb.kelly_stake(0.5, 0.9, thin_track, 1000, 0.05)
    without_track = tb.kelly_stake(0.5, 0.9, {}, 1000, 0.05)
    assert with_track == without_track  # too few samples to override the prediction


def test_kelly_stake_returns_zero_when_track_record_kills_the_edge():
    # entry_cost 0.951 -> b=0.0515, breakeven win prob ~95.1%. A 20-bet own
    # track record at 75% (well below breakeven) should zero the stake out
    # even though the model still predicts 90%.
    key = tb.prob_bucket_key(0.9)
    weak_track = {key: (15, 20)}
    stake = tb.kelly_stake(entry_cost=0.951, predicted_win_prob=0.9,
                            track=weak_track, bankroll_avail=1000, cap_frac=0.05)
    assert stake == 0.0


def test_kelly_stake_discounts_toward_wilson_low_once_min_sample_met():
    key = tb.prob_bucket_key(0.99)
    # 100 resolved bets at 95% -- enough samples that the Wilson lower bound
    # (~0.888) sits comfortably below the model's 0.99 prediction but still
    # above this entry cost's ~80% breakeven, so the edge shrinks rather
    # than disappearing. cap_frac=1.0 (effectively uncapped) so the stake
    # cap doesn't mask the difference the discount makes.
    strong_track = {key: (95, 100)}
    discounted = tb.kelly_stake(0.80, 0.99, strong_track, 1000, 1.0)
    undiscounted = tb.kelly_stake(0.80, 0.99, {}, 1000, 1.0)
    assert 0 < discounted < undiscounted


# --- build_candidates ----------------------------------------------------

def test_build_candidates_arb_requires_market_ids_slug_and_end_date():
    results = {"opportunities": [{"event_title": "E", "slug": "s", "end_date": None,
                                   "total_cost": 0.9, "edge_pct": 5, "legs": [{"market_id": "m1"}]}]}
    assert tb.build_candidates(results, NOW) == []


def test_build_candidates_arb_valid():
    results = {"opportunities": [{
        "event_title": "E", "slug": "s", "end_date": "2026-02-01T00:00:00Z",
        "total_cost": 0.9, "edge_pct": 5,
        "legs": [{"market_id": "m1"}, {"market_id": "m2"}],
    }]}
    candidates = tb.build_candidates(results, NOW)
    assert len(candidates) == 1
    c = candidates[0]
    assert c["bet_id"] == "arbitrage:s"
    assert c["market_ids"] == ["m1", "m2"]
    assert c["recommended_side"] is None
    assert c["entry_cost"] == 0.9


def test_build_candidates_calibration_requires_days_left():
    results = {"calibration_signals": [{"market_id": "m1", "slug": "s", "days_left": None,
                                         "market_question": "Q", "recommended_side": "YES",
                                         "implied_cost": 0.2, "edge_pct": 8}]}
    assert tb.build_candidates(results, NOW) == []


def test_build_candidates_calibration_valid():
    results = {"calibration_signals": [{
        "market_id": "m1", "slug": "s", "days_left": 5, "market_question": "Q?",
        "url": "http://x", "recommended_side": "YES", "implied_cost": 0.2, "edge_pct": 8,
        "bucket_historical_rate": 0.35,
    }]}
    candidates = tb.build_candidates(results, NOW)
    assert len(candidates) == 1
    assert candidates[0]["bet_id"] == "calibration:s:m1"
    assert candidates[0]["deadline"] == (NOW + datetime.timedelta(days=5)).isoformat()
    assert candidates[0]["predicted_win_prob"] == 0.35


def test_build_candidates_calibration_predicted_win_prob_flips_for_no_side():
    results = {"calibration_signals": [{
        "market_id": "m1", "slug": "s", "days_left": 5, "market_question": "Q?",
        "recommended_side": "NO", "implied_cost": 0.8, "edge_pct": 8,
        "bucket_historical_rate": 0.35,
    }]}
    candidates = tb.build_candidates(results, NOW)
    assert candidates[0]["predicted_win_prob"] == 0.65  # 1 - 0.35


def test_build_candidates_arbitrage_has_no_predicted_win_prob():
    results = {"opportunities": [{
        "event_title": "E", "slug": "s", "end_date": "2026-02-01T00:00:00Z",
        "total_cost": 0.9, "edge_pct": 5, "legs": [{"market_id": "m1"}, {"market_id": "m2"}],
    }]}
    candidates = tb.build_candidates(results, NOW)
    assert candidates[0]["predicted_win_prob"] is None


def test_build_candidates_mispricing_entry_cost_flips_with_side():
    # No implied_cost field (older results.json snapshot) -> falls back to
    # computing straight from implied_probability, with no fee added.
    base = {"market_id": "m1", "slug": "s", "days_left": 5, "market_question": "Q?",
            "implied_probability": 0.3, "edge_pct": 20, "fair_probability": 0.7}
    yes_sig = dict(base, recommended_side="YES")
    no_sig = dict(base, recommended_side="NO")
    yes_c = tb.build_candidates({"mispricing_signals": [yes_sig]}, NOW)[0]
    no_c = tb.build_candidates({"mispricing_signals": [no_sig]}, NOW)[0]
    assert yes_c["entry_cost"] == 0.3
    assert no_c["entry_cost"] == 0.7
    assert yes_c["predicted_win_prob"] == 0.7    # fair_probability, side YES
    assert no_c["predicted_win_prob"] == 0.3     # 1 - fair_probability, side NO


def test_build_candidates_mispricing_prefers_fee_inclusive_implied_cost():
    # When results.json carries implied_cost (fee-inclusive, from
    # find_mispricing_signal), that value is used as-is instead of being
    # recomputed from the raw implied_probability.
    sig = {"market_id": "m1", "slug": "s", "days_left": 5, "market_question": "Q?",
           "implied_probability": 0.3, "implied_cost": 0.3084, "edge_pct": 19.16,
           "fair_probability": 0.7, "recommended_side": "YES"}
    c = tb.build_candidates({"mispricing_signals": [sig]}, NOW)[0]
    assert c["entry_cost"] == 0.3084


# --- place_new_bets ------------------------------------------------------

def test_place_new_bets_dedupes_against_existing_bets():
    log = fresh_log()
    log["bets"].append(make_bet(tag="arbitrage"))
    log["bets"][-1]["bet_id"] = "arbitrage:s"
    results = {"opportunities": [{
        "event_title": "E", "slug": "s", "end_date": "2026-02-01T00:00:00Z",
        "total_cost": 0.9, "edge_pct": 5, "legs": [{"market_id": "m1"}, {"market_id": "m2"}],
    }]}
    placed, skipped_bankroll, skipped_no_edge = tb.place_new_bets(log, results, NOW)
    assert placed == 0
    assert len(log["bets"]) == 1  # still just the pre-existing one


def test_place_new_bets_skips_when_bankroll_below_floor():
    log = fresh_log(starting=5.0)  # below STAKE_FLOOR_USD
    results = {"opportunities": [{
        "event_title": "E", "slug": "s", "end_date": "2026-02-01T00:00:00Z",
        "total_cost": 0.9, "edge_pct": 5, "legs": [{"market_id": "m1"}, {"market_id": "m2"}],
    }]}
    placed, skipped_bankroll, skipped_no_edge = tb.place_new_bets(log, results, NOW)
    assert placed == 0
    assert skipped_bankroll == 1


def test_place_new_bets_places_and_updates_log():
    log = fresh_log()
    results = {"opportunities": [{
        "event_title": "E", "slug": "s", "end_date": "2026-02-01T00:00:00Z",
        "total_cost": 0.9, "edge_pct": 5, "legs": [{"market_id": "m1"}, {"market_id": "m2"}],
    }]}
    placed, skipped_bankroll, skipped_no_edge = tb.place_new_bets(log, results, NOW)
    assert placed == 1
    assert skipped_bankroll == 0
    assert skipped_no_edge == 0
    assert log["bets"][0]["status"] == "open"
    assert log["bets"][0]["stake_usd"] > 0


def test_place_new_bets_carries_predicted_win_prob_into_stored_bet():
    log = fresh_log()
    results = {"calibration_signals": [{
        "market_id": "m1", "slug": "s", "days_left": 5, "market_question": "Q?",
        "recommended_side": "YES", "implied_cost": 0.2, "edge_pct": 8,
        "bucket_historical_rate": 0.35,
    }]}
    tb.place_new_bets(log, results, NOW)
    assert log["bets"][0]["predicted_win_prob"] == 0.35


def test_place_new_bets_skips_calibration_signal_when_own_track_record_kills_kelly_edge():
    log = fresh_log()
    # Seed a poor own track record (75% win rate) in the same predicted-prob
    # bucket the new signal falls into. Its entry cost implies a ~95.1%
    # breakeven win rate, so Kelly should refuse to size this bet at all,
    # even though the model still predicts 90%.
    for _ in range(15):
        bet = make_bet(tag="calibration", status="won", pnl=1.0)
        bet["predicted_win_prob"] = 0.90
        log["bets"].append(bet)
    for _ in range(5):
        bet = make_bet(tag="calibration", status="lost", pnl=-10.0)
        bet["predicted_win_prob"] = 0.90
        log["bets"].append(bet)

    results = {"calibration_signals": [{
        "market_id": "new1", "slug": "new-market", "days_left": 5, "market_question": "Q?",
        "recommended_side": "YES", "implied_cost": 0.951, "edge_pct": 4.79,
        "bucket_historical_rate": 0.90,
    }]}
    placed, skipped_bankroll, skipped_no_edge = tb.place_new_bets(log, results, NOW)
    assert placed == 0
    assert skipped_no_edge == 1
    assert not any(b["slug"] == "new-market" for b in log["bets"])


# --- resolve_open_bets (network mocked) ------------------------------------

def test_resolve_open_bets_skips_bets_before_deadline_plus_grace():
    log = fresh_log()
    log["bets"].append(make_bet(deadline=NOW + datetime.timedelta(hours=1)))  # deadline in the future
    resolved = tb.resolve_open_bets(log, NOW)
    assert resolved == 0
    assert log["bets"][0]["status"] == "open"


def test_resolve_open_bets_calibration_win():
    log = fresh_log()
    log["bets"].append(make_bet(tag="calibration", recommended_side="YES", entry_cost=0.2, stake=100))
    with patch.object(tb.gamma_client, "fetch_market_state", return_value=(True, True)):
        with patch.object(tb.time, "sleep"):
            resolved = tb.resolve_open_bets(log, NOW)
    assert resolved == 1
    bet = log["bets"][0]
    assert bet["status"] == "won"
    assert bet["pnl_usd"] == round(100 * (1 / 0.2 - 1), 2)


def test_resolve_open_bets_calibration_loss():
    log = fresh_log()
    log["bets"].append(make_bet(tag="calibration", recommended_side="YES", entry_cost=0.2, stake=100))
    with patch.object(tb.gamma_client, "fetch_market_state", return_value=(True, False)):
        with patch.object(tb.time, "sleep"):
            tb.resolve_open_bets(log, NOW)
    bet = log["bets"][0]
    assert bet["status"] == "lost"
    assert bet["pnl_usd"] == -100.0


def test_resolve_open_bets_void_when_ambiguous():
    log = fresh_log()
    log["bets"].append(make_bet(tag="mispricing"))
    with patch.object(tb.gamma_client, "fetch_market_state", return_value=(True, None)):
        with patch.object(tb.time, "sleep"):
            tb.resolve_open_bets(log, NOW)
    bet = log["bets"][0]
    assert bet["status"] == "void"
    assert bet["pnl_usd"] == 0.0


def test_resolve_open_bets_still_open_when_market_not_closed_yet():
    log = fresh_log()
    log["bets"].append(make_bet(tag="mispricing"))
    with patch.object(tb.gamma_client, "fetch_market_state", return_value=(False, None)):
        with patch.object(tb.time, "sleep"):
            resolved = tb.resolve_open_bets(log, NOW)
    assert resolved == 0
    assert log["bets"][0]["status"] == "open"


def test_resolve_open_bets_arb_win_after_haircut():
    log = fresh_log()
    # entry_cost=0.9 -> raw edge = (1/0.9 - 1)*100 = 11.11%; haircut max 2pts, so always a net win
    log["bets"].append(make_bet(tag="arbitrage", entry_cost=0.9, stake=100, market_ids=("m1", "m2")))
    with patch.object(tb.gamma_client, "fetch_market_state", return_value=(True, True)):
        with patch.object(tb.time, "sleep"):
            with patch.object(tb.random, "uniform", return_value=1.0):  # fixed 1pt haircut
                tb.resolve_open_bets(log, NOW)
    bet = log["bets"][0]
    assert bet["status"] == "won"
    expected_edge = (1 / 0.9 - 1) * 100 - 1.0
    assert bet["pnl_usd"] == round(100 * (expected_edge / 100), 2)


def test_resolve_open_bets_arb_can_lose_if_haircut_exceeds_thin_edge():
    log = fresh_log()
    # entry_cost=0.995 -> raw edge = 0.5%, a max haircut of 2pts will exceed it
    log["bets"].append(make_bet(tag="arbitrage", entry_cost=0.995, stake=100, market_ids=("m1",)))
    with patch.object(tb.gamma_client, "fetch_market_state", return_value=(True, True)):
        with patch.object(tb.time, "sleep"):
            with patch.object(tb.random, "uniform", return_value=2.0):  # max haircut
                tb.resolve_open_bets(log, NOW)
    bet = log["bets"][0]
    assert bet["status"] == "lost"
    assert bet["pnl_usd"] < 0


# --- record_bankroll_snapshot ---------------------------------------------

def test_record_bankroll_snapshot_appends_new_date():
    log = fresh_log()
    tb.record_bankroll_snapshot(log, NOW)
    assert len(log["bankroll_history"]) == 1
    assert log["bankroll_history"][0]["date"] == NOW.date().isoformat()


def test_record_bankroll_snapshot_overwrites_same_date():
    log = fresh_log()
    tb.record_bankroll_snapshot(log, NOW)
    log["bets"].append(make_bet(status="won", pnl=50.0))
    tb.record_bankroll_snapshot(log, NOW)
    assert len(log["bankroll_history"]) == 1
    assert log["bankroll_history"][0]["bankroll"] == 1050.0
