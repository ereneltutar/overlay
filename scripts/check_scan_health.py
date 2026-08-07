#!/usr/bin/env python3
"""
Scan Health Monitor
--------------------
Tracks scanned_events over time and flags anomalies that wouldn't
otherwise raise an error, the exact way the /events pagination ceiling
silently truncated results to exactly 2100 for 44 consecutive daily
runs before anyone noticed: no exception, no crash, just a quietly
wrong number every single day.

Reads docs/results.json (today's scan output), appends a snapshot to
docs/scan_health.json (rolling history, deduped by date), and checks
three patterns:

1. ZERO_EVENTS: scanned_events is 0 (the scan produced nothing at all)
2. STUCK: scanned_events is identical across the last STUCK_WINDOW
   snapshots. A real active-event count fluctuates day to day as
   markets open and close; an exact repeat this many times running is
   the signature of a hard ceiling, not a stable market.
3. SUDDEN_DROP: scanned_events fell by more than SUDDEN_DROP_FRACTION
   relative to the trailing average of the prior snapshots, which
   could mean a partial API failure or a filter change gone wrong.

Output: docs/scan_health.json (updated history). Prints one line per
anomaly found to stdout (no output at all means healthy) and exits
with status 1 if anything was found, so a CI step can act on it
without parsing this script's internals.
"""

import json
import sys
from pathlib import Path

STUCK_WINDOW = 3              # this many identical consecutive values counts as "stuck"
SUDDEN_DROP_FRACTION = 0.5    # flag a drop of more than this fraction vs the trailing average
MIN_HISTORY_FOR_DROP_CHECK = 3
HISTORY_KEEP_DAYS = 90

RESULTS_PATH = Path(__file__).resolve().parent.parent / "docs" / "results.json"
HEALTH_PATH = Path(__file__).resolve().parent.parent / "docs" / "scan_health.json"


def load_health() -> dict:
    if HEALTH_PATH.exists():
        try:
            return json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"history": []}


def save_health(health: dict):
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_PATH.write_text(json.dumps(health, indent=2, ensure_ascii=False), encoding="utf-8")


def record_snapshot(health: dict, date: str, scanned_events: int, opportunities: int,
                     calibration_signals: int, mispricing_signals: int) -> dict:
    """Appends or overwrites today's snapshot in health['history'] (deduped by
    date, same pattern as track_bets.py's bankroll_history), then trims to the
    last HISTORY_KEEP_DAYS entries. Returns the updated health dict."""
    entry = {
        "date": date,
        "scanned_events": scanned_events,
        "opportunities": opportunities,
        "calibration_signals": calibration_signals,
        "mispricing_signals": mispricing_signals,
    }
    history = health["history"]
    if history and history[-1]["date"] == date:
        history[-1] = entry
    else:
        history.append(entry)
    health["history"] = history[-HISTORY_KEEP_DAYS:]
    return health


def detect_anomalies(history: list) -> list:
    """Pure function: given health['history'] (oldest first, today last),
    returns a list of human-readable anomaly strings. Empty list = healthy."""
    if not history:
        return []

    anomalies = []
    latest = history[-1]
    scanned = latest["scanned_events"]

    if scanned == 0:
        anomalies.append(
            "ZERO_EVENTS: today's scan found 0 events. The scan pipeline is "
            "likely broken (API error, auth issue, or an empty response being "
            "treated as valid)."
        )
        return anomalies  # no point checking the other patterns against a zero

    if len(history) >= STUCK_WINDOW:
        recent = [h["scanned_events"] for h in history[-STUCK_WINDOW:]]
        if len(set(recent)) == 1:
            anomalies.append(
                f"STUCK: scanned_events has been exactly {scanned} for the last "
                f"{STUCK_WINDOW} scans in a row. A real active-market count "
                f"fluctuates daily; an exact repeat this many times is the "
                f"signature of a hard API pagination ceiling silently "
                f"truncating results (this is exactly how the old 2100-event "
                f"ceiling went unnoticed for 44 days)."
            )

    prior = history[:-1]
    if len(prior) >= MIN_HISTORY_FOR_DROP_CHECK:
        trailing_avg = sum(h["scanned_events"] for h in prior) / len(prior)
        if trailing_avg > 0 and scanned < trailing_avg * (1 - SUDDEN_DROP_FRACTION):
            drop_pct = (1 - scanned / trailing_avg) * 100
            anomalies.append(
                f"SUDDEN_DROP: scanned_events ({scanned}) is {drop_pct:.0f}% "
                f"below the trailing average ({trailing_avg:.0f} over the last "
                f"{len(prior)} scans). Could be a partial API failure or an "
                f"unintended filter change."
            )

    return anomalies


def main():
    if not RESULTS_PATH.exists():
        print("ZERO_EVENTS: docs/results.json not found, the scan step likely failed.")
        sys.exit(1)

    results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    date = results.get("generated_at", "")[:10]

    health = load_health()
    health = record_snapshot(
        health,
        date=date,
        scanned_events=results.get("scanned_events", 0),
        opportunities=len(results.get("opportunities", [])),
        calibration_signals=len(results.get("calibration_signals", [])),
        mispricing_signals=len(results.get("mispricing_signals", [])),
    )
    save_health(health)

    anomalies = detect_anomalies(health["history"])
    for a in anomalies:
        print(a)

    if anomalies:
        sys.exit(1)
    print(f"Scan health OK: {results.get('scanned_events', 0)} events scanned, "
          f"no anomalies against the last {len(health['history'])} snapshots.")


if __name__ == "__main__":
    main()
