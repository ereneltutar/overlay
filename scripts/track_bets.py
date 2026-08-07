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

Bet sizing: stake = bankroll_available * STAKE_K * (edge_pct / 100),
floored at STAKE_FLOOR_USD and capped at a fraction of bankroll_available
per bet (STAKE_CAP_FRAC), so bigger edges get bigger stakes but a single
bet can never seriously damage the bankroll.

ARB is close to a mathematically guaranteed win by construction (that's
the definition of arbitrage), so a pure-math simulation would show ARB
winning almost every time and tell us little. To keep it honest, ARB
wins get a small random execution haircut (0 to ARB_HAIRCUT_MAX_PTS
points off the edge, simulating real fees/slippage) applied at
resolution time; if the haircut exceeds the edge, that bet is recorded
as a loss.

Output: docs/bet_log.json (bankroll, full bet history, daily bankroll
        snapshots for the archive page's sparkline)
"""

import datetime
import json
import random
import sys
import time
from pathlib import Path

import gamma_client

# --- Tunable parameters -----------------------------------------------
STARTING_BANKROLL = 1000.00
STAKE_K = 3.0                  # stake = bankroll_available * STAKE_K * edge_pct/100
STAKE_FLOOR_USD = 10.0         # never bet less than this
STAKE_CAP_FRAC = {             # never bet more than this fraction of available bankroll
    "arbitrage": 0.08,
    "calibration": 0.05,
    "mispricing": 0.05,
}
ARB_HAIRCUT_MAX_PTS = 2.0      # simulated execution slippage/fees, 0 to this many points off the ARB edge

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


def resolve_open_bets(log: dict, now: datetime.datetime) -> int:
    resolved_count = 0
    for bet in log["bets"]:
        if bet["status"] != "open":
            continue
        deadline = datetime.datetime.fromisoformat(bet["deadline"])
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


def sizing_stake(tag: str, edge_pct: float, bankroll_avail: float) -> float:
    cap = bankroll_avail * STAKE_CAP_FRAC[tag]
    raw = bankroll_avail * STAKE_K * (edge_pct / 100)
    return round(max(min(raw, cap), STAKE_FLOOR_USD), 2)


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
        })

    for sig in results.get("calibration_signals", []):
        if not sig.get("market_id") or not sig.get("slug") or sig.get("days_left") is None:
            continue
        deadline = now + datetime.timedelta(days=sig["days_left"])
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
        })

    for sig in results.get("mispricing_signals", []):
        if not sig.get("market_id") or not sig.get("slug") or sig.get("days_left") is None:
            continue
        entry_cost = sig["implied_probability"] if sig["recommended_side"] == "YES" \
            else 1 - sig["implied_probability"]
        if entry_cost <= 0:
            continue
        deadline = now + datetime.timedelta(days=sig["days_left"])
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
        })

    return candidates


def place_new_bets(log: dict, results: dict, now: datetime.datetime):
    existing_ids = {bet["bet_id"] for bet in log["bets"]}
    placed_count = 0
    skipped_low_bankroll = 0

    for c in build_candidates(results, now):
        if c["bet_id"] in existing_ids:
            continue
        bankroll_avail = available_bankroll(log)
        if bankroll_avail < STAKE_FLOOR_USD:
            skipped_low_bankroll += 1
            continue
        stake = min(sizing_stake(c["tag"], c["edge_pct"], bankroll_avail), round(bankroll_avail, 2))

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
            "stake_usd": stake,
            "status": "open",
            "resolved_at": None,
            "pnl_usd": None,
        })
        existing_ids.add(c["bet_id"])
        placed_count += 1

    return placed_count, skipped_low_bankroll


def record_bankroll_snapshot(log: dict, now: datetime.datetime):
    today = now.date().isoformat()
    bankroll = round(total_bankroll(log), 2)
    history = log["bankroll_history"]
    if history and history[-1]["date"] == today:
        history[-1]["bankroll"] = bankroll
    else:
        history.append({"date": today, "bankroll": bankroll})


def main():
    now = datetime.datetime.now(datetime.timezone.utc)

    if not RESULTS_PATH.exists():
        print("docs/results.json not found, skipping bet tracking.", file=sys.stderr)
        return

    results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    log = load_bet_log()

    resolved_count = resolve_open_bets(log, now)
    placed_count, skipped = place_new_bets(log, results, now)

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
    if skipped:
        print(f"Skipped {skipped} signals: available bankroll below the ${STAKE_FLOOR_USD:.0f} stake floor.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
