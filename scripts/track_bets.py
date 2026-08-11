#!/usr/bin/env python3
"""
Paper-Trading Archive
----------------------
Simulates playing the signals this project already finds with a fixed
$1,000 starting bankroll, so the ARB/CAL/MIS signals build an honest,
visible track record instead of staying purely theoretical.

Runs right after fetch_arbitrage.py, reading the results.json that script
just wrote, in two passes:

1) RESOLVE: for every currently open bet whose market has passed its
   deadline (plus a grace window for resolution lag), check the Gamma API
   for the real outcome and settle it: won, lost, or void (ambiguous /
   cancelled market, stake returned, excluded from win-rate stats).

2) PLACE: for every ARB/CAL/MIS signal in today's results.json that
   hasn't already been bet on (deduped by tag+slug+market, checked
   against every bet ever logged, not just open ones, so a market that
   stays in the scan for weeks doesn't get bet on twice), size a stake
   and log it as open.

Bet sizing differs by tag because the payoffs aren't the same shape, but
both use fractional Kelly against the ACTUAL resolution math below rather
than sizing off edge_pct alone (see kelly_fraction).

ARB is close to a mathematically guaranteed win by construction (that's
the definition of arbitrage), so a pure-math simulation would show ARB
winning almost every time and tell us little. To keep it honest, ARB
wins get a small random execution haircut (0 to ARB_HAIRCUT_MAX_PTS
points off the edge, simulating real fees/slippage) applied at
resolution time; if the haircut exceeds the edge, that bet is recorded
as a loss instead. That means a thin ARB edge (say 1%) sits close to a
coin flip against a haircut that can run up to 2 points, while a wide
edge (say 5%) can never lose. Sizing purely off edge_pct (the old
approach) staked both the same way. arb_kelly_stake() instead derives
the real win probability and average win/loss size straight from that
Uniform(0, ARB_HAIRCUT_MAX_PTS) haircut model, and skips the bet
entirely once the edge doesn't clear the haircut's expected cost.

CAL/MIS have a different asymmetric payoff: a win only returns
(1/entry_cost - 1) on the stake (often just a few percent) while a loss
forfeits the entire stake. kelly_stake() weighs the win probability and
that payout asymmetry directly, and automatically discounts the win
probability once Overlay's own resolved bets in that probability bucket
show a realized rate below what was predicted -- so a bucket that keeps
underperforming its own prediction gets sized down or shut off entirely
on its own, no manual retuning required.

Output: docs/bet_log.json (bankroll, full bet history, daily bankroll
        snapshots for the archive page's sparkline)
"""

import argparse
import datetime
import json
import math
import random
import sys
import time
from pathlib import Path

import gamma_client

# --- Tunable parameters -----------------------------------------------
STARTING_BANKROLL = 1000.00
STAKE_FLOOR_USD = 10.0         # available bankroll must clear this before even trying to size a bet
STAKE_CAP_FRAC = {             # never bet more than this fraction of available bankroll
    "arbitrage": 0.08,
    "calibration": 0.05,
    "mispricing": 0.05,
}
ARB_HAIRCUT_MAX_PTS = 2.0      # simulated execution slippage/fees, 0 to this many points off the ARB edge
                                # (see arb_kelly_stake: this also defines the ARB breakeven edge, H/2)

# Both ARB and CAL/MIS stake sizing (arb_kelly_stake, kelly_stake) use
# fractional Kelly against the tag's real resolution math instead of sizing
# off edge_pct alone, since edge_pct alone says nothing about how a miss
# actually costs relative to a win for that tag.
PROB_BIN_WIDTH = 0.05          # same bucket width calibration_scan.py uses for market-price buckets
OWN_TRACK_MIN_SAMPLE = 8       # need at least this many of Overlay's own resolved CAL/MIS bets in a
                                # probability bucket before that bucket's realized win rate overrides
                                # the model's predicted one (small samples stay noisy either way, so
                                # this keeps the model's number until there's enough of our own history
                                # to say something with real confidence)
KELLY_FRACTION = 0.5           # half-Kelly: full Kelly is provably optimal long-run growth but swings
                                # hard on estimation error; half-Kelly trades some growth for a much
                                # smaller drawdown when the win-probability estimate is off

DEADLINE_GRACE_HOURS = 12      # wait this long past the deadline before checking (resolution isn't instant)
SLEEP_BETWEEN_CALLS = 1.15
# ----------------------------------------------------------------------------

RESULTS_PATH = Path(__file__).resolve().parent.parent / "docs" / "results.json"
BET_LOG_PATH = Path(__file__).resolve().parent.parent / "docs" / "bet_log.json"


def load_bet_log() -> dict:
    if BET_LOG_PATH.exists():
        try:
            return json.loads(BET_LOG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "starting_bankroll": STARTING_BANKROLL,
        "bankroll": STARTING_BANKROLL,
        "bankroll_history": [],
        "bets": [],
    }


def save_bet_log(log: dict):
    BET_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    BET_LOG_PATH.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")


def realized_pnl_total(log: dict) -> float:
    return sum(b["pnl_usd"] for b in log["bets"] if b["pnl_usd"] is not None)


def total_bankroll(log: dict) -> float:
    """Account value at cost basis: starting bankroll plus every realized
    win/loss so far. Only moves when a bet resolves."""
    return log["starting_bankroll"] + realized_pnl_total(log)


def available_bankroll(log: dict) -> float:
    """Cash on hand: total account value minus stake currently tied up in
    open bets. This is what a new bet sizes against."""
    open_stake = sum(b["stake_usd"] for b in log["bets"] if b["status"] == "open")
    return total_bankroll(log) - open_stake


def parse_deadline(deadline_str: str) -> datetime.datetime:
    """Same Z-suffix normalization as fetch_arbitrage.parse_iso(). ARB bet
    deadlines come straight from the Gamma API's endDate field (e.g.
    "2026-08-08T14:00:00Z"); plain fromisoformat() only started accepting
    a bare "Z" suffix in Python 3.11, so this keeps parsing correct even
    if the runtime ever changes."""
    return datetime.datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))


def resolve_open_bets(log: dict, now: datetime.datetime) -> int:
    resolved_count = 0
    for bet in log["bets"]:
        if bet["status"] != "open":
            continue
        deadline = parse_deadline(bet["deadline"])
        if now < deadline + datetime.timedelta(hours=DEADLINE_GRACE_HOURS):
            continue

        # One clean resolution among the bet's markets is enough to confirm
        # the group settled normally (an ARB bet spans several legs of the
        # same event; CAL/MIS bets have exactly one).
        outcome_yes = None
        all_closed = True
        for market_id in bet["market_ids"]:
            closed, oy = gamma_client.fetch_market_state(market_id)
            time.sleep(SLEEP_BETWEEN_CALLS)
            if not closed:
                all_closed = False
            if oy is not None:
                outcome_yes = oy
                break

        if not all_closed and outcome_yes is None:
            continue  # still waiting on the market; check again next run

        resolved_count += 1
        bet["resolved_at"] = now.isoformat()

        if outcome_yes is None:
            bet["status"] = "void"
            bet["pnl_usd"] = 0.0
            continue

        if bet["tag"] == "arbitrage":
            raw_edge_pct = (1 / bet["entry_cost"] - 1) * 100
            haircut_pts = random.uniform(0, ARB_HAIRCUT_MAX_PTS)
            pnl = bet["stake_usd"] * ((raw_edge_pct - haircut_pts) / 100)
        else:
            won_side = (bet["recommended_side"] == "YES" and outcome_yes) or \
                       (bet["recommended_side"] == "NO" and not outcome_yes)
            pnl = bet["stake_usd"] * (1 / bet["entry_cost"] - 1) if won_side else -bet["stake_usd"]

        bet["pnl_usd"] = round(pnl, 2)
        bet["status"] = "won" if pnl > 0 else "lost"

    return resolved_count


def wilson_interval(k: int, n: int, z: float = 1.96):
    """95% Wilson score confidence interval. Duplicated from
    calibration_scan.py on purpose (same convention as gamma_client-style
    self-containment): each script stays runnable on its own."""
    if n == 0:
        return (0.0, 0.0)
    p_hat = k / n
    denom = 1 + (z ** 2) / n
    center = (p_hat + (z ** 2) / (2 * n)) / denom
    margin = (z / denom) * math.sqrt((p_hat * (1 - p_hat) / n) + (z ** 2) / (4 * n ** 2))
    return (max(0.0, center - margin), min(1.0, center + margin))


def prob_bucket_key(predicted_win_prob: float) -> int:
    """Buckets a predicted win probability into a PROB_BIN_WIDTH-wide bin."""
    num_bins = int(round(1 / PROB_BIN_WIDTH))
    return min(int(predicted_win_prob / PROB_BIN_WIDTH), num_bins - 1)


def own_track_record(log: dict) -> dict:
    """Buckets Overlay's own resolved CAL/MIS bets by predicted_win_prob (the
    same combined CAL+MIS grouping the archive page's "Predicted vs Realized"
    panel already uses), so kelly_stake() can check whether a bucket's win
    rate is actually holding up in practice, not just what it predicted
    going in. Returns {bucket_key: (wins, total)}."""
    buckets = {}
    for bet in log["bets"]:
        if bet["tag"] not in ("calibration", "mispricing"):
            continue
        if bet["status"] not in ("won", "lost"):
            continue
        if bet.get("predicted_win_prob") is None:
            continue
        key = prob_bucket_key(bet["predicted_win_prob"])
        wins, total = buckets.get(key, (0, 0))
        buckets[key] = (wins + (1 if bet["status"] == "won" else 0), total + 1)
    return buckets


def kelly_fraction(p: float, b: float, l: float = 1.0) -> float:
    """Kelly criterion optimal bet fraction for a two-outcome payoff: with
    probability p you gain fraction b of the stake, with probability (1-p)
    you lose fraction l of the stake (l defaults to 1.0 -- lose the whole
    stake -- which is the CAL/MIS case; arb_kelly_stake passes a smaller l
    since an ARB loss is usually a partial haircut, not the whole stake).
    Returns 0 once the edge implied by p, b and l is gone or negative."""
    if b <= 0 or l <= 0:
        return 0.0
    return max(0.0, p / l - (1 - p) / b)


def kelly_stake(entry_cost: float, predicted_win_prob: float, track: dict,
                 bankroll_avail: float, cap_frac: float) -> float:
    """Fractional-Kelly stake sizing for CAL/MIS bets.

    Sizing purely off edge_pct has no idea how asymmetric a CAL/MIS payoff
    is: a win returns a few percent of the stake, a loss forfeits all of it.
    A bucket can carry a real percentage edge and still be a bad bet once
    that asymmetry is weighed -- Kelly does that weighing directly instead
    of trusting edge_pct alone.

    predicted_win_prob already comes calibration-corrected against the
    market's own history (see calibration_scan.py). Before trusting it
    further, this looks up OWN_TRACK_MIN_SAMPLE+ of Overlay's own resolved
    bets in the same probability bucket (own_track_record); if their
    Wilson-lower-bound win rate is BELOW the prediction, that more
    conservative number is used instead. This only ever pulls the
    probability down, never up, and only once there's enough of Overlay's
    own history to say so with real confidence -- so a couple of early
    losses can't overreact, but a bucket that keeps genuinely
    underperforming its prediction automatically gets sized down or shut
    off entirely, with no manual threshold to update.

    Returns 0.0 (place_new_bets then skips the bet) if the resulting Kelly
    fraction is <= 0, i.e. there's no edge left once realized performance is
    priced in.
    """
    key = prob_bucket_key(predicted_win_prob)
    wins, total = track.get(key, (0, 0))
    p = predicted_win_prob
    if total >= OWN_TRACK_MIN_SAMPLE:
        ci_low, _ = wilson_interval(wins, total)
        p = min(p, ci_low)

    b = (1 / entry_cost) - 1
    f = kelly_fraction(p, b)
    if f <= 0:
        return 0.0

    stake = bankroll_avail * f * KELLY_FRACTION
    cap = bankroll_avail * cap_frac
    return round(min(stake, cap), 2)


def arb_kelly_stake(raw_edge_pct: float, bankroll_avail: float, cap_frac: float) -> float:
    """Fractional-Kelly stake sizing for ARB, derived from the exact same
    resolution-time haircut model resolve_open_bets() applies
    (haircut_pts ~ Uniform(0, ARB_HAIRCUT_MAX_PTS)) -- not a separate
    assumption, just solving Kelly for the payoff that model already implies:

      - P(win) = P(haircut < edge) = edge / ARB_HAIRCUT_MAX_PTS
      - given a win, the average payout is edge/2 points (a uniform haircut
        below edge averages edge/2)
      - given a loss, the average cost is (ARB_HAIRCUT_MAX_PTS - edge)/2
        points (a uniform haircut above edge averages (edge+H)/2, so the net
        average loss is edge - (edge+H)/2 = (H-edge)/2)

    If edge_pct >= ARB_HAIRCUT_MAX_PTS, the haircut can never reach it -- a
    guaranteed win by construction of this resolution model, no loss branch
    exists -- so this returns the cap directly.

    Below that, b and l (the win/loss fractions above) are both small
    relative to cap_frac, since a thin ARB edge only swings a couple of
    percentage points either way. Once there's ANY positive edge left after
    weighing p against that swing, full Kelly saturates the position cap
    almost immediately -- which is the expected, correct behavior for a
    low-variance-per-dollar edge like this; STAKE_CAP_FRAC is exactly the
    deliberate ceiling on how far that gets to run. Below breakeven
    (edge_pct <= ARB_HAIRCUT_MAX_PTS / 2, where the expected haircut wipes
    out the expected edge), it returns 0.0 and place_new_bets skips the bet.
    """
    cap = bankroll_avail * cap_frac
    if raw_edge_pct >= ARB_HAIRCUT_MAX_PTS:
        return round(cap, 2)

    p = raw_edge_pct / ARB_HAIRCUT_MAX_PTS
    avg_win_frac = (raw_edge_pct / 2) / 100
    avg_loss_frac = ((ARB_HAIRCUT_MAX_PTS - raw_edge_pct) / 2) / 100

    f = kelly_fraction(p, avg_win_frac, avg_loss_frac)
    if f <= 0:
        return 0.0

    stake = bankroll_avail * f * KELLY_FRACTION
    return round(min(stake, cap), 2)


def build_candidates(results: dict, now: datetime.datetime) -> list:
    candidates = []

    for opp in results.get("opportunities", []):
        market_ids = [leg.get("market_id") for leg in opp.get("legs", []) if leg.get("market_id")]
        if not market_ids or not opp.get("slug") or not opp.get("end_date"):
            continue
        candidates.append({
            "bet_id": f"arbitrage:{opp['slug']}",
            "tag": "arbitrage",
            "market_ids": market_ids,
            "market_question": opp["event_title"],
            "slug": opp["slug"],
            "url": opp.get("url"),
            "deadline": opp["end_date"],
            "recommended_side": None,
            "entry_cost": opp["total_cost"],
            "edge_pct": opp["edge_pct"],
            # ARB has no single "win probability" in the same sense as
            # CAL/MIS (it's structurally close to guaranteed by construction,
            # not a probabilistic bet), so it's left out of the predicted-vs-
            # realized calibration check rather than given a fake number.
            "predicted_win_prob": None,
        })

    for sig in results.get("calibration_signals", []):
        if not sig.get("market_id") or not sig.get("slug") or sig.get("days_left") is None:
            continue
        deadline = now + datetime.timedelta(days=sig["days_left"])
        actual_rate = sig["bucket_historical_rate"]
        predicted_win_prob = actual_rate if sig["recommended_side"] == "YES" else 1 - actual_rate
        candidates.append({
            "bet_id": f"calibration:{sig['slug']}:{sig['market_id']}",
            "tag": "calibration",
            "market_ids": [sig["market_id"]],
            "market_question": sig["market_question"],
            "slug": sig["slug"],
            "url": sig.get("url"),
            "deadline": deadline.isoformat(),
            "recommended_side": sig["recommended_side"],
            "entry_cost": sig["implied_cost"],
            "edge_pct": sig["edge_pct"],
            "predicted_win_prob": round(predicted_win_prob, 4),
        })

    for sig in results.get("mispricing_signals", []):
        if not sig.get("market_id") or not sig.get("slug") or sig.get("days_left") is None:
            continue
        # implied_cost is fee-inclusive (see find_mispricing_signal); fall back
        # to the raw price for older results.json snapshots that predate it.
        entry_cost = sig.get("implied_cost")
        if entry_cost is None:
            entry_cost = sig["implied_probability"] if sig["recommended_side"] == "YES" \
                else 1 - sig["implied_probability"]
        if entry_cost <= 0:
            continue
        deadline = now + datetime.timedelta(days=sig["days_left"])
        fair_prob = sig["fair_probability"]
        predicted_win_prob = fair_prob if sig["recommended_side"] == "YES" else 1 - fair_prob
        candidates.append({
            "bet_id": f"mispricing:{sig['slug']}:{sig['market_id']}",
            "tag": "mispricing",
            "market_ids": [sig["market_id"]],
            "market_question": sig["market_question"],
            "slug": sig["slug"],
            "url": sig.get("url"),
            "deadline": deadline.isoformat(),
            "recommended_side": sig["recommended_side"],
            "entry_cost": round(entry_cost, 4),
            "edge_pct": sig["edge_pct"],
            "predicted_win_prob": round(predicted_win_prob, 4),
        })

    return candidates


def place_new_bets(log: dict, results: dict, now: datetime.datetime):
    existing_ids = {bet["bet_id"] for bet in log["bets"]}
    track = own_track_record(log)
    placed_count = 0
    skipped_low_bankroll = 0
    skipped_no_edge = 0

    for c in build_candidates(results, now):
        if c["bet_id"] in existing_ids:
            continue
        bankroll_avail = available_bankroll(log)
        if bankroll_avail < STAKE_FLOOR_USD:
            skipped_low_bankroll += 1
            continue

        if c["tag"] == "arbitrage":
            stake = arb_kelly_stake(c["edge_pct"], bankroll_avail, STAKE_CAP_FRAC["arbitrage"])
        else:
            stake = kelly_stake(c["entry_cost"], c["predicted_win_prob"], track,
                                 bankroll_avail, STAKE_CAP_FRAC[c["tag"]])
        if stake <= 0:
            skipped_no_edge += 1
            continue
        stake = min(stake, round(bankroll_avail, 2))

        log["bets"].append({
            "bet_id": c["bet_id"],
            "tag": c["tag"],
            "market_question": c["market_question"],
            "slug": c["slug"],
            "url": c["url"],
            "placed_at": now.isoformat(),
            "deadline": c["deadline"],
            "market_ids": c["market_ids"],
            "recommended_side": c["recommended_side"],
            "entry_cost": c["entry_cost"],
            "edge_pct_at_placement": c["edge_pct"],
            "predicted_win_prob": c["predicted_win_prob"],
            "stake_usd": stake,
            "status": "open",
            "resolved_at": None,
            "pnl_usd": None,
        })
        existing_ids.add(c["bet_id"])
        placed_count += 1

    return placed_count, skipped_low_bankroll, skipped_no_edge


def record_bankroll_snapshot(log: dict, now: datetime.datetime):
    today = now.date().isoformat()
    bankroll = round(total_bankroll(log), 2)
    history = log["bankroll_history"]
    if history and history[-1]["date"] == today:
        history[-1]["bankroll"] = bankroll
    else:
        history.append({"date": today, "bankroll": bankroll})


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resolve-only", action="store_true",
        help="Only settle open bets against their markets; skip placing new "
             "ones and don't require docs/results.json to exist. Meant for "
             "a tighter-cadence workflow than the once-a-day full scan, so "
             "a bet whose market closes mid-day doesn't sit shown as "
             "'pending' for up to 24h waiting on tomorrow's run.")
    return parser.parse_args()


def main():
    args = parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)

    results = None
    if RESULTS_PATH.exists():
        results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    elif not args.resolve_only:
        print("docs/results.json not found, skipping bet tracking.", file=sys.stderr)
        return

    log = load_bet_log()

    resolved_count = resolve_open_bets(log, now)
    placed_count, skipped_bankroll, skipped_no_edge = (0, 0, 0)
    if not args.resolve_only:
        placed_count, skipped_bankroll, skipped_no_edge = place_new_bets(log, results, now)

    log["bankroll"] = round(total_bankroll(log), 2)
    record_bankroll_snapshot(log, now)
    save_bet_log(log)

    won = sum(1 for b in log["bets"] if b["status"] == "won")
    lost = sum(1 for b in log["bets"] if b["status"] == "lost")
    void = sum(1 for b in log["bets"] if b["status"] == "void")
    open_n = sum(1 for b in log["bets"] if b["status"] == "open")
    print(f"Bets: placed {placed_count} new, resolved {resolved_count} this run. "
          f"All-time: {won} won / {lost} lost / {void} void / {open_n} open. "
          f"Bankroll: ${log['bankroll']:.2f} (started at ${log['starting_bankroll']:.2f}).")
    if skipped_bankroll:
        print(f"Skipped {skipped_bankroll} signals: available bankroll below the ${STAKE_FLOOR_USD:.0f} stake floor.",
              file=sys.stderr)
    if skipped_no_edge:
        print(f"Skipped {skipped_no_edge} signals: Kelly sizing found no edge once each tag's real "
              f"resolution math (haircut model for ARB, own realized win rate for CAL/MIS) was priced in.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
