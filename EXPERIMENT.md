# Frozen-rules experiment

**Current: Experiment 2, started 2026-11-12.** Experiment 1 (2026-10-30 to
2026-11-12) was ended early without a verdict; see below.

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

## Experiment 1 (2026-10-30 to 2026-11-12): ended early, no verdict

Baseline was commit `38a8bb3`, bankroll $1,048.23, kill switch $524.12, bets
counted from 2026-10-31. It ended at 31 settled bets (interim, informational
only) and a bankroll of $1,198.68.

**Why it was ended:** its own rules made the 300-bet checkpoint unreachable.
`track_bets.py` replaced a bucket's predicted win probability with the 95%
Wilson *lower* bound of Overlay's own record in that bucket once it had 8
resolved bets (pooled with neighbors). Kelly then needs that lower bound to
beat the entry price, which takes far more evidence than 8 bets: a perfect
8/8 bucket has a lower bound of 0.676 and was shut off at a 0.82 entry, and a
bucket that truly wins 57% at a 0.50 entry needs about 200 bets to reopen. A
shut bucket places no bets, so its record never changes. By 2026-11-09 every
signalled bucket was shut, and the daily scans placed 0 of 20 signals for
four days running. The test would have ended in a FAIL for too few bets,
which says nothing about edge. This diagnosis follows from the formula alone
(the perfect-record bucket shows it), not from which bets won or lost.

## Experiment 2 baseline (frozen 2026-11-12)

- Code: Experiment 1's rules with one change: `OWN_TRACK_ADJUSTMENT_ENABLED =
  False` in `track_bets.py`. CAL/MIS bets are sized from the model's predicted
  win probability as is. Everything else (signals, gates, caps, half-Kelly,
  2.5% stake cap) is unchanged. Commit: see the change log.
- **No bankroll reset.** Bankroll at the switch: **$1,198.68**. All earlier
  bets stay in `bet_log.json` untouched.
- Bets counted: CAL and MIS bets **placed on or after 2026-11-12 00:00 UTC**.
  Experiment 1's bets (including the one still open on Nov 12) are excluded
  from the metric but still move the bankroll.
- Checkpoint is unchanged: 300 settled bets or 2027-04-30.

## The test

Run `python scripts/experiment_status.py`. It does all of this mechanically.

For each settled post-freeze bet: `x = (1 if won else 0) − entry_cost`.
`entry_cost` already includes the taker fee, so the mean of `x` is the average
net edge per bet in probability points. Zero means break-even. The metric
doesn't depend on stake sizing.

| Verdict | Condition |
|---|---|
| **RUNNING** | Fewer than 300 settled post-freeze bets and before 2027-04-30. Interim numbers are for information only. **There is no early PASS.** |
| **FAIL (kill switch)** | The bankroll drops below **$599.34** (half the Experiment 2 baseline) at any point. Can fire any time. |
| **PASS** | At the checkpoint (**300 settled bets or 2027-04-30, whichever comes first**): the one-sided 95% lower bound of the mean edge is **above 0**, **and** the bankroll is above $1,198.68. |
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
| 2026-11-12 | Bring back a track-record correction that can't lock a bucket shut: e.g. a Bayesian posterior mean (model prior + own results) instead of the Wilson lower bound, plus small fixed-stake "probe" bets so a down-weighted bucket keeps producing data. | Experiment 1 shutdown above. | For the experiment after Experiment 2. |

## Change log during the freeze

| Date | Commit | Change | Why it's allowed |
|---|---|---|---|
| 2026-11-12 | (this commit) | Ended Experiment 1, started Experiment 2: own-track correction off, new baseline $1,198.68, kill switch $599.34, bets counted from 2026-11-12. | Not a freeze exception. The user explicitly decided to end the test early and re-register, as this document allows. |
