#!/usr/bin/env python3
"""
Experiment Status (frozen-rules pass/fail test)
------------------------------------------------
Mechanically scores the pre-registered test in EXPERIMENT.md so the verdict
is arithmetic, not judgment.

Why this exists: until Oct 2026 the strategy was improved by a
lose-money / study-the-losers / add-a-rule / reset-the-bankroll loop. Two
runs lost about half the bankroll ($1000 -> $471 in Sep, $1000 -> $501 in
Oct) and each was followed by a fix and a reset. Every fix was reasonable
on its own, but a loop that only patches past losses always looks good in
hindsight and says nothing about whether there is an edge going forward.
On 2026-10-30 the rules were frozen and this test was written down BEFORE
any post-freeze bet was placed, so the answer can't be fitted after the
fact.

Experiment 2 (from 2026-11-12). Experiment 1 was ended early, without a
verdict, because its own rules made the checkpoint unreachable: the
own-track-record correction in track_bets.py shut off every probability
bucket once it had 8 resolved bets unless the bucket's 95% Wilson LOWER
bound beat the entry price (a perfect 8/8 bucket still failed at a 0.82
entry), and a shut bucket never bets again, so its record can't recover.
From Nov 9 the daily scan placed 0 of 20 signals. That diagnosis comes
from the formula, not from which bets won. Experiment 2 runs the same
rules with that correction off (OWN_TRACK_ADJUSTMENT_ENABLED = False) and
the same test. The bankroll was NOT reset, so the baseline is the bankroll
on the switch date and the kill switch is half of it. Experiment 1's bets
stay in bet_log.json and move the bankroll, but not the metric.

The metric is sizing-independent: for each settled CAL/MIS bet placed after
FREEZE_AT, x = (1 if won else 0) - entry_cost. entry_cost already includes
the taker fee, so mean(x) is the average net edge per $1 of contract, in
probability points. Zero means break-even. A bet strategy with no edge
lands around zero (minus whatever the paper fills flatter real fills by).

Verdicts:
  RUNNING  not enough evidence yet. Interim numbers are printed for
           information only; acting on them is exactly the peeking this
           test exists to prevent. There is no early PASS.
  FAIL     kill switch: the bankroll fell below KILL_BANKROLL at any point
           after the freeze (a third ~50% drawdown), or the checkpoint was
           reached without passing.
  PASS     at the checkpoint (MIN_SETTLED_BETS settled, or DEADLINE,
           whichever first): the one-sided 95% lower bound of mean(x) is
           above zero AND the bankroll is above FREEZE_BANKROLL.

ARB bets are excluded: they have a different payoff (partial haircut, not
all-or-nothing) and haven't fired in months. Bets placed before FREEZE_AT
(including the open ones carried over) are excluded from the metric, but
they still move the bankroll, so they count toward the kill switch.

Output: one summary block on stdout. Exit status is always 0; the verdict
is in the text and in the VERDICT line, so a workflow step can grep it.
"""

import datetime
import json
import math
from pathlib import Path

# Pre-registered on 2026-10-30. Changing any of these after post-freeze bets
# exist invalidates the test -- see EXPERIMENT.md.
# Experiment 2 re-registered these on 2026-11-12 (Experiment 1 used
# 2026-10-31 / $1048.23 / $524.12). MIN_SETTLED_BETS and DEADLINE are unchanged.
EXPERIMENT = 2
FREEZE_AT = "2026-11-12T00:00:00+00:00"  # first daily scan with the own-track correction off is Nov 12
FREEZE_BANKROLL = 1198.68                 # docs/bet_log.json bankroll on 2026-11-12, no reset
KILL_BANKROLL = 599.34                    # half of FREEZE_BANKROLL
MIN_SETTLED_BETS = 300                    # enough to tell a ~5-point edge from luck at these prices
DEADLINE = "2027-04-30T00:00:00+00:00"    # evaluate whatever exists by then, even if < MIN_SETTLED_BETS
Z_ONE_SIDED_95 = 1.645

BET_LOG_PATH = Path(__file__).resolve().parent.parent / "docs" / "bet_log.json"


def parse_ts(ts: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def post_freeze_settled(log: dict) -> list:
    """Settled (won/lost) CAL/MIS bets placed at or after FREEZE_AT. Void
    bets are dropped: they return the stake and carry no information."""
    freeze = parse_ts(FREEZE_AT)
    return [b for b in log["bets"]
            if b["tag"] in ("calibration", "mispricing")
            and b["status"] in ("won", "lost")
            and parse_ts(b["placed_at"]) >= freeze]


def edge_stats(bets: list) -> dict:
    """Mean net edge per bet in probability points, its standard error and
    the one-sided 95% lower bound. Lower bound is None below 2 bets."""
    xs = [(1.0 if b["status"] == "won" else 0.0) - b["entry_cost"] for b in bets]
    n = len(xs)
    if n == 0:
        return {"n": 0, "wins": 0, "mean": None, "se": None, "lower": None}
    mean = sum(xs) / n
    if n < 2:
        se = lower = None
    else:
        var = sum((x - mean) ** 2 for x in xs) / (n - 1)
        se = math.sqrt(var / n)
        lower = mean - Z_ONE_SIDED_95 * se
    return {"n": n, "wins": sum(1 for b in bets if b["status"] == "won"),
            "mean": mean, "se": se, "lower": lower}


def min_bankroll_since_freeze(log: dict) -> float:
    """Lowest recorded bankroll on or after the freeze date, including the
    current figure."""
    freeze_date = FREEZE_AT[:10]
    points = [h["bankroll"] for h in log.get("bankroll_history", []) if h["date"] >= freeze_date]
    points.append(log["bankroll"])
    return min(points)


def verdict(log: dict, now: datetime.datetime) -> tuple:
    """Returns (verdict, reason, stats)."""
    stats = edge_stats(post_freeze_settled(log))
    low_water = min_bankroll_since_freeze(log)
    if low_water < KILL_BANKROLL:
        return "FAIL", f"kill switch: bankroll hit ${low_water:.2f} (< ${KILL_BANKROLL:.2f})", stats

    at_checkpoint = stats["n"] >= MIN_SETTLED_BETS or now >= parse_ts(DEADLINE)
    if not at_checkpoint:
        return "RUNNING", f"{stats['n']}/{MIN_SETTLED_BETS} settled post-freeze bets", stats

    if stats["lower"] is not None and stats["lower"] > 0 and log["bankroll"] > FREEZE_BANKROLL:
        return "PASS", "edge lower bound > 0 and bankroll above freeze level", stats
    return "FAIL", "checkpoint reached without a demonstrated edge", stats


def fmt_pts(v):
    return "n/a" if v is None else f"{v * 100:+.2f} pts"


def main():
    log = json.loads(BET_LOG_PATH.read_text(encoding="utf-8"))
    now = datetime.datetime.now(datetime.timezone.utc)
    v, reason, s = verdict(log, now)
    print(f"Experiment {EXPERIMENT}: frozen since {FREEZE_AT[:10]}, checkpoint at {MIN_SETTLED_BETS} settled bets or {DEADLINE[:10]}.")
    print(f"Settled post-freeze CAL/MIS bets: {s['n']} ({s['wins']} won)")
    print(f"Mean net edge per bet: {fmt_pts(s['mean'])}  (one-sided 95% lower bound: {fmt_pts(s['lower'])})")
    print(f"Bankroll: ${log['bankroll']:.2f} (freeze ${FREEZE_BANKROLL:.2f}, kill switch ${KILL_BANKROLL:.2f})")
    if v == "RUNNING":
        print("Interim numbers are informational only; don't act on them.")
    print(f"VERDICT: {v} -- {reason}")


if __name__ == "__main__":
    main()
