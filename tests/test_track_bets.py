import datetime
import json
from unittest.mock import patch

import pytest

import track_bets as tb

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def isolate_fact_check_log(tmp_path, monkeypatch):
    """Any place_new_bets() call with a calibration/mispricing candidate
    appends to FACT_CHECK_LOG_PATH (see log_fact_check) -- redirect it to a
    per-test tmp_path for every test in this file so the suite never writes
    into the real docs/fact_check_log.jsonl."""
    monkeypatch.setattr(tb, "FACT_CHECK_LOG_PATH", tmp_path / "fact_check_log.jsonl")


def fresh_log(starting=1000.0):
    return {"starting_bankroll": starting, "bankroll": starting, "bankroll_history": [], "bets": []}


def always_safe_llm(question, recommended_side, deadline_str, now):
    """Stub ask_llm for place_new_bets tests that aren't exercising the
    fact-check gate itself: real network calls have no place in the test
    suite, so every candidate should just sail through as SAFE."""
    return {"verdict": "SAFE", "reason": "stub", "checked_at": now.isoformat()}


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


# --- arb_kelly_stake ----------------------------------------------------

def test_arb_kelly_stake_zero_below_breakeven():
    # breakeven is ARB_HAIRCUT_MAX_PTS / 2 = 1.0%; 0.6% can't clear the
    # haircut's expected cost, matching the one real 0.6%-edge bet that lost.
    stake = tb.arb_kelly_stake(raw_edge_pct=0.6, bankroll_avail=1000, cap_frac=0.08)
    assert stake == 0.0


def test_arb_kelly_stake_caps_out_once_haircut_cannot_reach_the_edge():
    # edge >= ARB_HAIRCUT_MAX_PTS -> the haircut can never exceed it, a
    # guaranteed win by construction, so it takes the full cap directly.
    stake = tb.arb_kelly_stake(raw_edge_pct=5.0, bankroll_avail=1000, cap_frac=0.08)
    assert stake == round(1000 * 0.08, 2)


def test_arb_kelly_stake_positive_but_thin_edge_still_places_a_bet():
    # 1.01% just clears the 1.0% breakeven -- thin, but not zero.
    stake = tb.arb_kelly_stake(raw_edge_pct=1.01, bankroll_avail=1000, cap_frac=0.08)
    assert stake > 0.0


def test_arb_kelly_stake_at_exact_breakeven_is_zero():
    stake = tb.arb_kelly_stake(raw_edge_pct=tb.ARB_HAIRCUT_MAX_PTS / 2, bankroll_avail=1000, cap_frac=0.08)
    assert stake == 0.0


# --- kelly_fraction ------------------------------------------------------

def test_kelly_fraction_positive_when_p_beats_breakeven():
    assert tb.kelly_fraction(0.7, 1.0) == pytest.approx(0.4)  # 0.7 - 0.3/1.0


def test_kelly_fraction_zero_when_no_edge():
    assert tb.kelly_fraction(0.5, 0.1) == 0.0  # 0.5 - 0.5/0.1 is deeply negative


def test_kelly_fraction_zero_when_b_not_positive():
    assert tb.kelly_fraction(0.9, 0.0) == 0.0


def test_kelly_fraction_defaults_to_losing_the_whole_stake():
    assert tb.kelly_fraction(0.7, 1.0) == tb.kelly_fraction(0.7, 1.0, l=1.0)


def test_kelly_fraction_smaller_loss_fraction_needs_less_edge_to_pay_off():
    # A partial loss (l < 1) is more forgiving than losing the whole stake,
    # so the same p and b should allow a smaller p to still show an edge --
    # equivalently, at a fixed marginal p, a smaller l gives a bigger f.
    full_loss = tb.kelly_fraction(0.4, 1.0, l=1.0)
    partial_loss = tb.kelly_fraction(0.4, 1.0, l=0.1)
    assert partial_loss > full_loss


def test_kelly_fraction_zero_when_l_not_positive():
    assert tb.kelly_fraction(0.9, 1.0, l=0.0) == 0.0


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


# --- pooled_track_record / neighbor-bucket pooling -------------------------

def test_pooled_track_record_sums_immediate_neighbors():
    key = tb.prob_bucket_key(0.22)  # bucket [0.20, 0.25)
    track = {
        key - 1: (0, 5),   # [0.15, 0.20)
        key: (1, 3),       # [0.20, 0.25) -- alone, below OWN_TRACK_MIN_SAMPLE
        key + 1: (0, 4),   # [0.25, 0.30)
    }
    wins, total = tb.pooled_track_record(track, key)
    assert (wins, total) == (1, 12)


def test_pooled_track_record_stops_at_radius_and_range_edges():
    # radius=1 by default: a bucket two slots away must not be pooled in.
    key = tb.prob_bucket_key(0.5)
    track = {key: (0, 2), key - 2: (100, 100)}
    wins, total = tb.pooled_track_record(track, key)
    assert (wins, total) == (0, 2)

    # bucket 0 has no key-1 neighbor to pool from -- must not go negative/wrap.
    wins0, total0 = tb.pooled_track_record({0: (0, 2)}, 0)
    assert (wins0, total0) == (0, 2)


def test_kelly_stake_pools_neighbors_when_exact_bucket_below_min_sample():
    # Mirrors the real Sep 2026 case: bucket [0.15,0.20) alone only has 5
    # resolved bets (below OWN_TRACK_MIN_SAMPLE=8), all losses, but its
    # neighbor [0.10,0.15) already has 8 resolved, also all losses. Pooling
    # should let that neighbor evidence suppress the bet instead of falling
    # back to the model's raw, unproven prediction.
    key = tb.prob_bucket_key(0.176)
    track = {key: (0, 5), key - 1: (0, 8)}
    stake = tb.kelly_stake(entry_cost=0.10, predicted_win_prob=0.176,
                            track=track, bankroll_avail=1000, cap_frac=0.05)
    assert stake == 0.0


def test_kelly_stake_pooling_only_kicks_in_below_own_min_sample():
    # Once the exact bucket itself clears OWN_TRACK_MIN_SAMPLE, its own
    # (unpooled) Wilson bound is used -- pooling is a fallback, not blended
    # in on top of an already-sufficient exact-bucket sample.
    key = tb.prob_bucket_key(0.9)
    exact_only = {key: (7, 8)}                    # exact bucket: strong 87.5% win rate, n=8
    polluted_neighbor = {key: (7, 8), key + 1: (0, 50)}  # neighbor is terrible but irrelevant now
    stake_exact = tb.kelly_stake(0.5, 0.9, exact_only, 1000, 1.0)
    stake_polluted = tb.kelly_stake(0.5, 0.9, polluted_neighbor, 1000, 1.0)
    assert stake_exact == stake_polluted


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
    placed, skipped_bankroll, skipped_no_edge, skipped_exposure_cap, skipped_fact_check = \
        tb.place_new_bets(log, results, NOW, ask_llm=always_safe_llm)
    assert placed == 0
    assert len(log["bets"]) == 1  # still just the pre-existing one


def test_place_new_bets_skips_when_bankroll_below_floor():
    log = fresh_log(starting=5.0)  # below STAKE_FLOOR_USD
    results = {"opportunities": [{
        "event_title": "E", "slug": "s", "end_date": "2026-02-01T00:00:00Z",
        "total_cost": 0.9, "edge_pct": 5, "legs": [{"market_id": "m1"}, {"market_id": "m2"}],
    }]}
    placed, skipped_bankroll, skipped_no_edge, skipped_exposure_cap, skipped_fact_check = \
        tb.place_new_bets(log, results, NOW, ask_llm=always_safe_llm)
    assert placed == 0
    assert skipped_bankroll == 1


def test_place_new_bets_places_and_updates_log():
    log = fresh_log()
    results = {"opportunities": [{
        "event_title": "E", "slug": "s", "end_date": "2026-02-01T00:00:00Z",
        "total_cost": 0.9, "edge_pct": 5, "legs": [{"market_id": "m1"}, {"market_id": "m2"}],
    }]}
    placed, skipped_bankroll, skipped_no_edge, skipped_exposure_cap, skipped_fact_check = \
        tb.place_new_bets(log, results, NOW, ask_llm=always_safe_llm)
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
    tb.place_new_bets(log, results, NOW, ask_llm=always_safe_llm)
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
    placed, skipped_bankroll, skipped_no_edge, skipped_exposure_cap, skipped_fact_check = \
        tb.place_new_bets(log, results, NOW, ask_llm=always_safe_llm)
    assert placed == 0
    assert skipped_no_edge == 1
    assert not any(b["slug"] == "new-market" for b in log["bets"])


# --- place_new_bets: portfolio exposure cap ---------------------------------

def test_place_new_bets_skips_when_open_stake_already_at_exposure_cap():
    log = fresh_log()  # bankroll 1000, cap = 350
    log["bets"].append(make_bet(tag="calibration", status="open", stake=350.0))
    results = {"mispricing_signals": [{
        "market_id": "m1", "slug": "s", "days_left": 5, "market_question": "Q?",
        "implied_probability": 0.3, "implied_cost": 0.3084, "edge_pct": 19.16,
        "fair_probability": 0.7, "recommended_side": "YES",
    }]}
    placed, skipped_bankroll, skipped_no_edge, skipped_exposure_cap, skipped_fact_check = \
        tb.place_new_bets(log, results, NOW, ask_llm=always_safe_llm)
    assert placed == 0
    assert skipped_exposure_cap == 1
    assert not any(b["slug"] == "s" for b in log["bets"])


def test_place_new_bets_trims_stake_to_remaining_exposure_room():
    log = fresh_log()  # bankroll 1000, cap = 350
    log["bets"].append(make_bet(tag="calibration", status="open", stake=345.0))  # only $5 of room left
    results = {"mispricing_signals": [{
        "market_id": "m1", "slug": "s", "days_left": 5, "market_question": "Q?",
        "implied_probability": 0.3, "implied_cost": 0.3084, "edge_pct": 19.16,
        "fair_probability": 0.7, "recommended_side": "YES",
    }]}
    placed, skipped_bankroll, skipped_no_edge, skipped_exposure_cap, skipped_fact_check = \
        tb.place_new_bets(log, results, NOW, ask_llm=always_safe_llm)
    # exposure_room ($5) is below STAKE_FLOOR_USD ($10), so this is skipped
    # outright rather than placed with a dust-sized stake.
    assert placed == 0
    assert skipped_exposure_cap == 1


def test_place_new_bets_allows_bet_within_exposure_room():
    log = fresh_log()  # bankroll 1000, cap = 350
    log["bets"].append(make_bet(tag="calibration", status="open", stake=300.0))  # $50 of room left
    results = {"mispricing_signals": [{
        "market_id": "m1", "slug": "s", "days_left": 5, "market_question": "Q?",
        "implied_probability": 0.3, "implied_cost": 0.3084, "edge_pct": 19.16,
        "fair_probability": 0.7, "recommended_side": "YES",
    }]}
    placed, skipped_bankroll, skipped_no_edge, skipped_exposure_cap, skipped_fact_check = \
        tb.place_new_bets(log, results, NOW, ask_llm=always_safe_llm)
    assert placed == 1
    assert skipped_exposure_cap == 0
    assert log["bets"][-1]["stake_usd"] <= 50.0


# --- place_new_bets: LLM fact-check gate ------------------------------------

CAL_RESULTS = {"calibration_signals": [{
    "market_id": "m1", "slug": "s", "days_left": 5, "market_question": "Q?",
    "recommended_side": "YES", "implied_cost": 0.2, "edge_pct": 8,
    "bucket_historical_rate": 0.35,
}]}


def vetoing_llm(question, recommended_side, deadline_str, now):
    return {"verdict": "VETO", "reason": "current facts contradict the recommended side",
            "checked_at": now.isoformat()}


def erroring_llm(question, recommended_side, deadline_str, now):
    return {"verdict": "ERROR", "reason": "API request failed: timeout", "checked_at": now.isoformat()}


def test_place_new_bets_skips_calibration_candidate_on_veto():
    log = fresh_log()
    placed, skipped_bankroll, skipped_no_edge, skipped_exposure_cap, skipped_fact_check = \
        tb.place_new_bets(log, CAL_RESULTS, NOW, ask_llm=vetoing_llm)
    assert placed == 0
    assert skipped_fact_check == 1
    assert log["bets"] == []


def test_place_new_bets_fail_open_on_llm_error():
    # FAIL_OPEN (see llm_fact_check.py) means an ERROR verdict still lets
    # the bet through -- an Anthropic API outage shouldn't silently halt
    # every new calibration/mispricing bet.
    log = fresh_log()
    placed, skipped_bankroll, skipped_no_edge, skipped_exposure_cap, skipped_fact_check = \
        tb.place_new_bets(log, CAL_RESULTS, NOW, ask_llm=erroring_llm)
    assert placed == 1
    assert skipped_fact_check == 0


def test_place_new_bets_stores_fact_check_result_on_the_placed_bet():
    log = fresh_log()
    tb.place_new_bets(log, CAL_RESULTS, NOW, ask_llm=always_safe_llm)
    assert log["bets"][0]["fact_check"]["verdict"] == "SAFE"


def test_place_new_bets_does_not_fact_check_arbitrage_candidates():
    # ARB has no meaningful "recommended side" to fact-check (see
    # place_new_bets's comment) -- a stub that would veto everything must
    # never even be consulted for an arbitrage candidate.
    log = fresh_log()
    results = {"opportunities": [{
        "event_title": "E", "slug": "s", "end_date": "2026-02-01T00:00:00Z",
        "total_cost": 0.9, "edge_pct": 5, "legs": [{"market_id": "m1"}, {"market_id": "m2"}],
    }]}
    placed, skipped_bankroll, skipped_no_edge, skipped_exposure_cap, skipped_fact_check = \
        tb.place_new_bets(log, results, NOW, ask_llm=vetoing_llm)
    assert placed == 1
    assert skipped_fact_check == 0
    assert log["bets"][0]["fact_check"] is None


def test_place_new_bets_logs_every_fact_check_including_vetoed():
    log = fresh_log()
    tb.place_new_bets(log, CAL_RESULTS, NOW, ask_llm=vetoing_llm)
    lines = tb.FACT_CHECK_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["passed"] is False
    assert entry["verdict"] == "VETO"
    assert entry["market_question"] == "Q?"


def costed_llm(verdict, cost_usd=0.02):
    def _ask(question, recommended_side, deadline_str, now):
        return {"verdict": verdict, "reason": "stub", "checked_at": now.isoformat(),
                "input_tokens": 2500, "output_tokens": 90, "web_searches": 1, "cost_usd": cost_usd}
    return _ask


def test_place_new_bets_logs_cost_fields_from_fact_check_result():
    log = fresh_log()
    tb.place_new_bets(log, CAL_RESULTS, NOW, ask_llm=costed_llm("SAFE", cost_usd=0.0234))
    entry = json.loads(tb.FACT_CHECK_LOG_PATH.read_text(encoding="utf-8").strip())
    assert entry["cost_usd"] == 0.0234
    assert entry["input_tokens"] == 2500
    assert entry["web_searches"] == 1


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
