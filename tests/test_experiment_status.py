import datetime

import experiment_status as es

BEFORE_DEADLINE = datetime.datetime(2026, 12, 1, tzinfo=datetime.timezone.utc)
AFTER_DEADLINE = datetime.datetime(2027, 5, 1, tzinfo=datetime.timezone.utc)


def bet(status, cost=0.5, tag="calibration", placed_at="2026-11-01T09:00:00+00:00"):
    return {"tag": tag, "status": status, "entry_cost": cost, "placed_at": placed_at}


def make_log(bets, bankroll=1100.0, history=None):
    return {"bets": bets, "bankroll": bankroll, "bankroll_history": history or []}


# --- post_freeze_settled ----------------------------------------------------

def test_post_freeze_settled_excludes_pre_freeze_open_void_and_arb():
    log = make_log([
        bet("won"),
        bet("lost", tag="mispricing"),
        bet("won", placed_at="2026-10-30T09:18:00+00:00"),  # placed before the freeze
        bet("open"),
        bet("void"),
        bet("won", tag="arbitrage"),
    ])
    assert len(es.post_freeze_settled(log)) == 2


# --- edge_stats ---------------------------------------------------------------

def test_edge_stats_mean_is_win_rate_minus_cost():
    # 3 wins / 4 at cost 0.5 -> mean x = 0.75 - 0.5 = 0.25
    s = es.edge_stats([bet("won"), bet("won"), bet("won"), bet("lost")])
    assert s["n"] == 4 and s["wins"] == 3
    assert abs(s["mean"] - 0.25) < 1e-9
    assert s["lower"] < s["mean"]


def test_edge_stats_empty_and_single():
    assert es.edge_stats([])["mean"] is None
    s = es.edge_stats([bet("won")])
    assert s["mean"] == 0.5 and s["lower"] is None


# --- verdict ------------------------------------------------------------------

def test_verdict_running_before_checkpoint_even_with_great_numbers():
    # No early PASS: peeking and stopping on a hot streak is what this test prevents.
    log = make_log([bet("won")] * 50)
    v, _, _ = es.verdict(log, BEFORE_DEADLINE)
    assert v == "RUNNING"


def test_verdict_kill_switch_fires_any_time():
    log = make_log([], bankroll=900.0,
                   history=[{"date": "2026-11-15", "bankroll": 500.0}])
    v, reason, _ = es.verdict(log, BEFORE_DEADLINE)
    assert v == "FAIL" and "kill switch" in reason


def test_verdict_kill_switch_ignores_pre_freeze_history():
    log = make_log([], history=[{"date": "2026-09-20", "bankroll": 471.0}])
    v, _, _ = es.verdict(log, BEFORE_DEADLINE)
    assert v == "RUNNING"


def test_verdict_pass_at_checkpoint_with_real_edge():
    # 180/300 at cost 0.5 -> mean +10 pts, SE ~2.8 pts, lower bound well above 0
    bets = [bet("won")] * 180 + [bet("lost")] * 120
    v, _, s = es.verdict(make_log(bets, bankroll=1200.0), BEFORE_DEADLINE)
    assert s["n"] == 300 and s["lower"] > 0
    assert v == "PASS"


def test_verdict_fail_at_checkpoint_when_break_even():
    bets = [bet("won")] * 150 + [bet("lost")] * 150
    v, _, _ = es.verdict(make_log(bets, bankroll=1200.0), BEFORE_DEADLINE)
    assert v == "FAIL"


def test_verdict_fail_when_edge_ok_but_bankroll_below_freeze():
    bets = [bet("won")] * 180 + [bet("lost")] * 120
    v, _, _ = es.verdict(make_log(bets, bankroll=1000.0), BEFORE_DEADLINE)
    assert v == "FAIL"


def test_verdict_deadline_forces_evaluation_below_min_sample():
    v, reason, _ = es.verdict(make_log([bet("won"), bet("lost")]), AFTER_DEADLINE)
    assert v == "FAIL" and "checkpoint" in reason
