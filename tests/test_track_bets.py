import datetime
from unittest.mock import patch

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
    }]}
    candidates = tb.build_candidates(results, NOW)
    assert len(candidates) == 1
    assert candidates[0]["bet_id"] == "calibration:s:m1"
    assert candidates[0]["deadline"] == (NOW + datetime.timedelta(days=5)).isoformat()


def test_build_candidates_mispricing_entry_cost_flips_with_side():
    base = {"market_id": "m1", "slug": "s", "days_left": 5, "market_question": "Q?",
            "implied_probability": 0.3, "edge_pct": 20}
    yes_sig = dict(base, recommended_side="YES")
    no_sig = dict(base, recommended_side="NO")
    yes_c = tb.build_candidates({"mispricing_signals": [yes_sig]}, NOW)[0]
    no_c = tb.build_candidates({"mispricing_signals": [no_sig]}, NOW)[0]
    assert yes_c["entry_cost"] == 0.3
    assert no_c["entry_cost"] == 0.7


# --- place_new_bets ------------------------------------------------------

def test_place_new_bets_dedupes_against_existing_bets():
    log = fresh_log()
    log["bets"].append(make_bet(tag="arbitrage"))
    log["bets"][-1]["bet_id"] = "arbitrage:s"
    results = {"opportunities": [{
        "event_title": "E", "slug": "s", "end_date": "2026-02-01T00:00:00Z",
        "total_cost": 0.9, "edge_pct": 5, "legs": [{"market_id": "m1"}, {"market_id": "m2"}],
    }]}
    placed, skipped = tb.place_new_bets(log, results, NOW)
    assert placed == 0
    assert len(log["bets"]) == 1  # still just the pre-existing one


def test_place_new_bets_skips_when_bankroll_below_floor():
    log = fresh_log(starting=5.0)  # below STAKE_FLOOR_USD
    results = {"opportunities": [{
        "event_title": "E", "slug": "s", "end_date": "2026-02-01T00:00:00Z",
        "total_cost": 0.9, "edge_pct": 5, "legs": [{"market_id": "m1"}, {"market_id": "m2"}],
    }]}
    placed, skipped = tb.place_new_bets(log, results, NOW)
    assert placed == 0
    assert skipped == 1


def test_place_new_bets_places_and_updates_log():
    log = fresh_log()
    results = {"opportunities": [{
        "event_title": "E", "slug": "s", "end_date": "2026-02-01T00:00:00Z",
        "total_cost": 0.9, "edge_pct": 5, "legs": [{"market_id": "m1"}, {"market_id": "m2"}],
    }]}
    placed, skipped = tb.place_new_bets(log, results, NOW)
    assert placed == 1
    assert skipped == 0
    assert log["bets"][0]["status"] == "open"
    assert log["bets"][0]["stake_usd"] > 0


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
    with patch.object(tb, "fetch_market_state", return_value=(True, True)):
        with patch.object(tb.time, "sleep"):
            resolved = tb.resolve_open_bets(log, NOW)
    assert resolved == 1
    bet = log["bets"][0]
    assert bet["status"] == "won"
    assert bet["pnl_usd"] == round(100 * (1 / 0.2 - 1), 2)


def test_resolve_open_bets_calibration_loss():
    log = fresh_log()
    log["bets"].append(make_bet(tag="calibration", recommended_side="YES", entry_cost=0.2, stake=100))
    with patch.object(tb, "fetch_market_state", return_value=(True, False)):
        with patch.object(tb.time, "sleep"):
            tb.resolve_open_bets(log, NOW)
    bet = log["bets"][0]
    assert bet["status"] == "lost"
    assert bet["pnl_usd"] == -100.0


def test_resolve_open_bets_void_when_ambiguous():
    log = fresh_log()
    log["bets"].append(make_bet(tag="mispricing"))
    with patch.object(tb, "fetch_market_state", return_value=(True, None)):
        with patch.object(tb.time, "sleep"):
            tb.resolve_open_bets(log, NOW)
    bet = log["bets"][0]
    assert bet["status"] == "void"
    assert bet["pnl_usd"] == 0.0


def test_resolve_open_bets_still_open_when_market_not_closed_yet():
    log = fresh_log()
    log["bets"].append(make_bet(tag="mispricing"))
    with patch.object(tb, "fetch_market_state", return_value=(False, None)):
        with patch.object(tb.time, "sleep"):
            resolved = tb.resolve_open_bets(log, NOW)
    assert resolved == 0
    assert log["bets"][0]["status"] == "open"


def test_resolve_open_bets_arb_win_after_haircut():
    log = fresh_log()
    # entry_cost=0.9 -> raw edge = (1/0.9 - 1)*100 = 11.11%; haircut max 2pts, so always a net win
    log["bets"].append(make_bet(tag="arbitrage", entry_cost=0.9, stake=100, market_ids=("m1", "m2")))
    with patch.object(tb, "fetch_market_state", return_value=(True, True)):
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
    with patch.object(tb, "fetch_market_state", return_value=(True, True)):
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
