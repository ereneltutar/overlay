# Overlay

Runs automatically every morning. Scans open Polymarket events with a
deadline inside a set window and looks for three separate kinds of
mispriced positions, then lists them as ticket cards on a static dashboard.

**Cost: $0, indefinitely.** It runs on GitHub Actions (cron) and GitHub
Pages (static hosting). No server to run, nothing to keep awake.

## The three signals

| Tag | What it finds | Riskless? |
|---|---|---|
| **ARB** | negRisk event groups where buying every mutually exclusive outcome at the current best ask costs less than $1.00 total | Mathematically yes, in practice no (fees, slippage, thin liquidity) |
| **CAL** | Price buckets where a statistically significant gap exists between price and outcome, measured across resolved markets (Wilson 95% CI, n≥30) | No — a tendency across many repeated positions, not a single-bet guarantee |
| **MIS** | Markets with ≥$5,000 in 24h volume where price differs from that bucket's historical resolution rate by 15+ points, no significance test required | No |

None of this is investment advice.

## How it works

```
GitHub Actions, every morning at 06:00 TR time
   └─ scripts/fetch_arbitrage.py  → scans the Polymarket Gamma API
        ├─ writes docs/results.json and commits it to the repo
        └─ appends price snapshots to docs/price_log.jsonl (for calibration)

GitHub Actions, every Monday
   └─ scripts/calibration_scan.py → checks which logged markets have
        since closed, matches them against their real outcome, and
        writes docs/calibration.json

GitHub Actions, every morning, right after fetch_arbitrage.py
   └─ scripts/track_bets.py → paper-trades the signals from today's
        results.json against a fixed $1,000 bankroll, and checks
        whether previously placed bets have resolved, writing
        docs/bet_log.json

GitHub Actions, every morning, right after track_bets.py
   └─ scripts/check_scan_health.py → tracks scanned_events over time
        in docs/scan_health.json and flags anomalies (zero events, a
        suspiciously stuck count, or a sudden drop). Files or updates
        a GitHub issue labeled scan-health when something looks wrong,
        closes it automatically once things recover

GitHub Pages (Actions-based deploy)
   └─ docs/index.html reads results.json/calibration.json/price_log.jsonl/
        scan_health.json client-side and renders the ledger, filters,
        calibration curve, and a signals-found-per-day trend
   └─ docs/archive.html reads bet_log.json and renders the paper-trading
        track record: bankroll over time, win rate by tag, bet-by-bet ledger
```

The calculation logic is documented at the top of each script. In short,
for ARB: if an event has N mutually exclusive options and buying all of
their "Yes" sides at the current best ask sums to under $1.00, it gets
listed as a ticket.

CAL and MIS both need `docs/calibration.json` to exist before they
produce anything, which means the calibration scan has to complete at
least once. Since that scan is forward-looking (it waits for markets
logged today to close weeks later), expect the calibration curve to
stay empty for a while after first setup.

## Paper-trading archive

Every ARB/CAL/MIS signal gets a simulated bet, sized to its edge (bigger
edge, bigger stake, capped so no single bet can seriously damage the
bankroll) and drawn from a fixed $1,000 starting bankroll. `docs/archive.html`
shows the running track record: bankroll over time, win rate broken out
by tag, and every bet with its entry price, stake, and outcome.

ARB is close to a mathematically guaranteed win by construction, so its
wins get a small random execution haircut (0-2 points off the edge,
simulating real fees/slippage) applied at resolution — otherwise it
would just read as a permanent win streak and tell you nothing. The
headline win-rate stat on the archive page counts CAL and MIS only,
since those are the signals actually worth validating; ARB is tracked
separately alongside them. A market that resolves ambiguously or gets
cancelled is marked void: the stake is returned and it's excluded from
win-rate math.

The headline win rate is shown with a 95% Wilson confidence interval
(e.g. "50% [15-85%] (n=4)"), not a bare percentage, so a handful of
early bets doesn't read as more conclusive than it is. A "Predicted vs
Realized" chart buckets resolved CAL/MIS bets by the win probability
predicted at placement time and plots that against how often each
bucket actually won — the real test of whether the signals are
accurate or just optimistic, separate from whether the bankroll
happens to be up. Max drawdown is tracked alongside the bankroll chart.

Because this reuses the same forward-looking pattern as calibration
(log now, check back once the deadline passes), the archive fills in
slowly and starts empty. It's meant to build an honest record over
weeks and months, not simulate results for its own sake.

## Scan health monitoring

The scanner used to hit a hard, undocumented pagination ceiling on the
Gamma API (capped at exactly 2,100 events) for 44 consecutive daily
runs before anyone noticed, because a silently truncated result set
doesn't raise an error — it just returns a quietly wrong number, every
day, forever. `scripts/check_scan_health.py` exists so that failure
mode gets caught in days, not weeks.

It keeps a rolling history of `scanned_events` in `docs/scan_health.json`
and flags three patterns after each run: zero events scanned (the scan
produced nothing), a value stuck identically for several runs in a row
(the exact signature of a hard ceiling), or a sudden drop against the
trailing average. Any anomaly gets filed as a GitHub issue labeled
`scan-health` (or added as a comment if one's already open); the issue
closes itself automatically once a later run comes back healthy.

## Setup (10 minutes)

1. **Create a new GitHub repo.** Public is easiest — Actions minutes are
   free and unlimited on public repos. Private works too but draws from
   a monthly minutes quota.
2. Copy everything in this folder to the repo root and push:
   ```
   git add .
   git commit -m "initial setup"
   git push
   ```
3. **Turn on GitHub Pages with the Actions build:** Repo → Settings →
   Pages → under "Build and deployment," set Source to *GitHub Actions*.
   The `.github/workflows/pages.yml` workflow in this repo handles the
   rest — it deploys `docs/` on every push that touches it. The site
   goes live at `https://your-username.github.io/repo-name/` within
   about a minute of the first push.
4. **Trigger the first scan manually:** Repo → Actions → "Daily
   Polymarket Scan" → "Run workflow." This produces the first
   `results.json` and commits it. After that it runs automatically
   every morning.
5. **Trigger the first calibration scan too** (optional but recommended):
   Repo → Actions → "Weekly Calibration Scan" → "Run workflow." CAL and
   MIS signals stay empty until this has run at least once.

## Tunable parameters

Near the top of `scripts/fetch_arbitrage.py`:

| Parameter | What it does | Default |
|---|---|---|
| `DAYS_AHEAD` | Scan events with a deadline at most this many days out | 30 |
| `MIN_EDGE_PCT` | Hide ARB edges below this percent (noise filter) | 0.5 |
| `MIN_LIQUIDITY_USD` | Minimum liquidity required per leg | 50 |
| `MIN_CALIBRATION_EDGE_PCT` | Hide CAL edges below this percent | 2.0 |
| `MISPRICING_MIN_EDGE_PTS` | Minimum point gap for a MIS signal | 15.0 |
| `MISPRICING_MIN_VOLUME_24H` | Minimum 24h volume for a MIS signal | 5000 |

In `scripts/calibration_scan.py`:

| Parameter | What it does | Default |
|---|---|---|
| `BIN_WIDTH` | Bucket width for the calibration curve | 0.05 (20 buckets) |
| `MIN_SAMPLE_PER_BUCKET` | Samples needed before a bucket counts as significant | 30 |

In `scripts/track_bets.py`:

| Parameter | What it does | Default |
|---|---|---|
| `STARTING_BANKROLL` | Simulated starting bankroll | $1,000 |
| `STAKE_K` | Sizing multiplier: stake = bankroll_available x K x edge_pct/100 | 3.0 |
| `STAKE_FLOOR_USD` | Never bet less than this | $10 |
| `STAKE_CAP_FRAC` | Max fraction of available bankroll per bet (per tag) | 8% ARB, 5% CAL/MIS |
| `ARB_HAIRCUT_MAX_PTS` | Simulated execution slippage/fees on ARB wins | 0-2 points off the edge |

To change the schedule, edit the `cron:` line in
`.github/workflows/daily-scan.yml` or `calibration-scan.yml` (both use
UTC; `0 3 * * *` = 06:00 Turkey time).

## Development / testing

The pure calculation logic (opportunity/signal detection, bet sizing,
bankroll accounting, calibration bucketing, anomaly detection) has a
pytest suite in `tests/`, covering the deterministic functions in
isolation with synthetic data — no network calls, no live API
dependency. Network-touching functions (market resolution lookups) are
tested with `unittest.mock` instead of hitting the real Gamma API.

```
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

`.github/workflows/tests.yml` runs the suite plus `scripts/lint_workflows.py`
(a YAML sanity check for every workflow file) on every push and PR
against `main` — a plain `run: |` block with inconsistent indentation
parses fine locally but fails at push time in a way that's easy to miss,
so this catches it before merge instead of live in the Actions tab.

## Important limitations, please read

- **This is a scanning/monitoring tool, not a money printer.** The ARB
  edge shown is a theoretical gap computed from order-book "ask" prices
  at scan time. Once you actually try to trade it: liquidity may not
  cover the full size, the price may move before your order fills
  (slippage), and Polymarket could introduce fees later. On liquid,
  popular events, gaps like this usually get closed by bots within
  seconds to minutes. This tool is realistically more useful for
  catching short-lived opportunities in thinner, longer-tail events, or
  for general market monitoring.
- Only **negRisk (multi-outcome) event groups** are scanned for ARB.
  For plain binary Yes/No markets, the Gamma API doesn't return a real
  ask price for the NO side as a separate field, and the CLOB order
  book endpoint has a known stale-data problem, so binary markets were
  deliberately left out. This could be added later.
- CAL and MIS use the same historical bucket table as their "fair
  probability" proxy. They don't run an independent forecasting model.
  Small sample sizes are flagged (`low_sample_warning`) but MIS doesn't
  require statistical significance the way CAL does.
- This tool gives **no investment advice**. It only processes public
  market data.

## License / liability

For personal use. Do your own research before trading real money.
