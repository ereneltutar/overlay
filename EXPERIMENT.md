# Frozen-rules experiment (started 2026-10-30)

## Why

Until now Overlay improved itself by a loop: lose money, study the losing bets,
add a rule that would have blocked them, reset the bankroll. Two runs lost about
half the bankroll ($1000 → $471 in Sep 2026, $1000 → $501 in Oct 2026) and each
was followed by a fix and a reset. Each fix was reasonable, but a loop that only
patches past losses always looks good in hindsight. It can't tell us whether
there's an edge going forward.

So the rules are frozen and the pass/fail test below is written down **before**
any post-freeze bet exists. The question it answers: **do Overlay's CAL/MIS bets
beat their own entry price, net of fees, on bets it hasn't seen yet?**

## Frozen baseline

- Code: commit `38a8bb3` (includes the past-date gate from `9f24ca7`).
- Bankroll at freeze: **$1,048.23**.
- Bets counted: CAL and MIS bets **placed on or after 2026-10-31 00:00 UTC**,
  the first daily scan with the past-date gate. The 12 bets still open at the
  freeze are excluded from the metric but still count toward the bankroll.

## The test

Run `python scripts/experiment_status.py`. It does all of this mechanically.

For each settled post-freeze bet: `x = (1 if won else 0) − entry_cost`.
`entry_cost` already includes the taker fee, so the mean of `x` is the average
net edge per bet in probability points. Zero means break-even. The metric
doesn't depend on stake sizing.

| Verdict | Condition |
|---|---|
| **RUNNING** | Fewer than 300 settled post-freeze bets and before 2027-04-30. Interim numbers are for information only. **There is no early PASS.** |
| **FAIL (kill switch)** | The bankroll drops below **$524.12** (half the freeze bankroll) at any point. Can fire any time. |
| **PASS** | At the checkpoint (**300 settled bets or 2027-04-30, whichever comes first**): the one-sided 95% lower bound of the mean edge is **above 0**, **and** the bankroll is above $1,048.23. |
| **FAIL** | The checkpoint is reached and PASS isn't met. This includes the deadline arriving with too few bets, since a strategy that can't find enough bets isn't usable either. |

What it takes to pass: at typical prices around $0.45–0.55, 300 bets give a
standard error of about 2.9 points. The lower bound clears zero only if the
real edge is roughly **5 points or more**. A smaller real edge would likely be
eaten by real-money spreads anyway.

## What the freeze allows and forbids

**Forbidden until the verdict:**
- Changing signal generation, gates, thresholds, constants, sizing or ranking.
- Resetting the bankroll.
- Removing or editing bets in `docs/bet_log.json`. The Oct 30 removals were the
  last ones.
- Changing the test: its constants, its metric or this document's criteria.

**Allowed, and each one must be logged below:**
- Keeping the pipeline running: crashes, API or schema changes, workflow
  breakage, dashboard-only changes.
- Fixing bets that are invalid by construction, such as betting on an event
  that's already decided or mapping to the wrong outcome side. The test for
  whether a fix qualifies: it must be justifiable **without looking at whether
  any bet won or lost**.
- The weekly calibration refresh keeps running. It's part of the frozen
  procedure.

## After the verdict

- **PASS:** move to a small real-money phase ($100–200) and compare real fill
  prices with paper `entry_cost` before scaling up.
- **FAIL:** the price-bucket approach doesn't have a usable edge. Stop. Don't
  patch-and-reset; any new idea starts as a new experiment with its own
  pre-written test.

## Idea backlog (for the next experiment)

Major strategy ideas raised during the freeze are recorded here, not implemented.
Changing bet selection or sizing mid-test voids it, unless the user explicitly
decides to end the test early.

| Date | Idea | Evidence | Status |
|---|---|---|---|
| — | — | — | — |

## Change log during the freeze

| Date | Commit | Change | Why it's allowed |
|---|---|---|---|
| — | — | — | — |
