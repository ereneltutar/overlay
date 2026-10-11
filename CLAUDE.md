# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Overlay scans Polymarket (Gamma API, plus the CLOB API for order books) for three kinds of signals: **ARB** (negRisk baskets costing < $1), **CAL** (statistically significant favorite-longshot bias per price bucket), and **MIS** (high-volume markets far from their bucket's historical rate). It then paper-trades them against a simulated $1,000 bankroll. There is no server. GitHub Actions cron runs the Python scripts, commits JSON into `docs/`, and GitHub Pages serves `docs/` as a static dashboard.

## Commands

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v                                   # full suite (no network; API calls are mocked)
python -m pytest tests/test_track_bets.py -k exposure_cap    # single file / test subset
python scripts/lint_workflows.py                             # YAML sanity check for .github/workflows/*.yml (CI runs it)
python scripts/track_bets.py --resolve-only                  # settle open bets only, no new placements
```

`tests/conftest.py` puts `scripts/` on `sys.path`, so tests import modules directly (`import track_bets as tb`). The scripts are flat modules, not a package.

## Pipeline and data flow

All state lives as committed files in `docs/`. Each script reads the previous stage's output.

1. **`fetch_arbitrage.py`** (daily, 03:00 UTC) scans events and writes `docs/results.json` (ARB, CAL, MIS and momentum signals). It also appends price snapshots of markets about 7 days from close to `docs/price_log.jsonl`, which is append-only and feeds calibration.
2. **`track_bets.py`** (same daily job) has two passes. RESOLVE settles open bets via `gamma_client.fetch_market_state`. PLACE sizes new bets from `results.json` and writes `docs/bet_log.json`. `resolve-bets.yml` also runs it with `--resolve-only` every 4h.
3. **`fact_check_budget.py`** and **`check_scan_health.py`** run in the same daily job. The health check exits 1 on anomalies, and the workflow then files or closes a `scan-health` GitHub issue.
4. **`calibration_scan.py`** (weekly, Monday) matches `price_log.jsonl` entries against real outcomes, caches them in `resolution_cache.json`, and writes `docs/calibration.json`. CAL and MIS produce nothing until this file exists. The scan is forward-looking by design, because Polymarket's historical price endpoint returns empty data for most markets.
5. **`docs/index.html` + `app.js`** render the dashboard. **`archive.html` + `archive.js`** render the paper-trading record from `bet_log.json`. Everything renders client-side with no build step.

Shared modules:
- `gamma_client.py`: the single resolution-check implementation used by both calibration and bet tracking.
- `market_category.py`: crypto markets get their **own calibration table**, so a crypto market must route to the crypto table and never to the general pool.
- `crypto_price_gate.py`: a hard veto when the move a crypto market needs, measured against the live CoinGecko spot price, is implausible.
- `llm_fact_check.py`: Claude web-search veto, fail-open. **Currently disabled** (`LLM_FACT_CHECK_ENABLED = False` in `track_bets.py`) because the user stopped funding API credits. Don't propose features that depend on paid APIs.

### Bet placement (`track_bets.place_new_bets`)

Candidates are sorted by `signal_strength` (full-Kelly fraction after Overlay's own track record is priced in). They then pass these gates in order: dedupe by `bet_id` and by open `market_id` (CAL and MIS often flag the same market), bankroll floor, Kelly > 0, `MAX_OPEN_STAKE_FRAC` exposure cap, `MAX_OPEN_BETS_PER_CLUSTER` correlation cap, then the fact-check. Any gate that doesn't need the fact-check verdict must run before it, so vetoed-anyway candidates never cost an API call.

Sizing is half-Kelly capped per tag by `STAKE_CAP_FRAC`. CAL/MIS win probability is pulled down to the Wilson lower bound of Overlay's own resolved bets in that bucket (or pooled neighbor buckets) once `OWN_TRACK_MIN_SAMPLE` is reached. ARB uses a haircut model (`ARB_HAIRCUT_MAX_PTS`) that matches the resolution-time haircut.

`place_new_bets` returns a stats **dict**, not a tuple. A new counter must be added in three places: `place_new_bets`'s initial dict, the default dict in `main()`, and the stderr summary in `main()`.

## Conventions and gotchas

- **The cron bots commit to `main` several times a day** ("Daily scan", "Resolve bets", "Weekly calibration"). Always `git fetch` and compare with `origin/main` before analyzing `docs/bet_log.json` or other data, and before pushing.
- **Bankroll resets:** reset `docs/bet_log.json` to a clean $1,000 when bet selection logic changes, meaning signal generation or which bets qualify. Pure sizing or ranking changes don't require a reset. Always ask the user before resetting. Example: in the Oct 11 2026 commit, the CAL/MIS stake cap change (5% → 2.5%) and Kelly-strength ranking were sizing/ranking only, but the same-market dedupe in that same commit did change which bets qualify. The user deliberately kept the bankroll anyway, so the track record from Oct 11 onward mixes pre- and post-dedupe selection.
- The README's tunable-parameter tables have drifted from the code (e.g. MIS edge floor, liquidity minimums, and `STAKE_K`, which was replaced by Kelly sizing). **The constants at the top of each script are the source of truth.** Most of them carry comments explaining the incident that set their value, so read those before changing one.
- Module docstrings record why each design exists (e.g. the Gamma pagination ceiling, crypto's 0-for-9 streak, correlated-sample clustering). Keep that style: document the reasoning and the incident, not only the mechanics.
- Workflow `run: |` blocks have broken from de-indentation several times. Run `scripts/lint_workflows.py` after editing any workflow.
- Pushes made with `GITHUB_TOKEN` don't trigger `pages.yml`, so each data-committing workflow dispatches it explicitly. Keep that step when adding a new data-writing workflow.
- Secrets live in GitHub Actions secrets; any local .env is gitignored. Never ask the user to paste keys or tokens into chat; reference them by variable name only.
- Gamma API rate limit is about 60 req/min. Scripts that make one call per market sleep `SLEEP_BETWEEN_CALLS = 1.15` between calls.
