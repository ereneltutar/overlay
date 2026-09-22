#!/usr/bin/env python3
"""
Fact-Check Budget Report
-------------------------
llm_fact_check.py's calls run against Anthropic API billing (separate from
any Claude.ai subscription -- see track_bets.py's FACT_CHECK_LOG_PATH),
which is real money on a card, not a bundled subscription quota. Every
check track_bets.py makes is already logged to docs/fact_check_log.jsonl
(see log_fact_check()) with its estimated cost (see
llm_fact_check.extract_usage); this script is purely a read-only rollup of
that log into something a person can glance at, run right after
track_bets.py in the daily workflow.

Output: docs/fact_check_budget.json (today's totals, a per-day history, and
        running month-to-date / all-time totals), plus a human-readable
        summary printed to stdout so it shows up directly in the GitHub
        Actions run log without needing to open the JSON file.

This has no bearing on whether a bet gets placed -- it's an accounting
report, not a gate (that's passes_fact_check's job). If it ever disagrees
with your actual Anthropic Console invoice, trust the invoice: these are
estimates from response token/search counts against hardcoded pricing
constants (see llm_fact_check.py), not a live billing API.
"""

import datetime
import json
import sys
from pathlib import Path

import llm_fact_check

FACT_CHECK_LOG_PATH = Path(__file__).resolve().parent.parent / "docs" / "fact_check_log.jsonl"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "fact_check_budget.json"


def load_fact_check_log() -> list:
    """Reads docs/fact_check_log.jsonl. Returns an empty list if the file
    doesn't exist yet (no fact-checks have ever run) -- same convention as
    fetch_arbitrage.load_price_log()."""
    entries = []
    if not FACT_CHECK_LOG_PATH.exists():
        return entries
    with FACT_CHECK_LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def entry_date(entry: dict):
    """The calendar date (UTC) an entry's checked_at falls on, or None if
    it's missing/unparseable -- callers skip those rather than crash on an
    old or hand-edited log line. Pure function."""
    checked_at = entry.get("checked_at")
    if not checked_at:
        return None
    try:
        return datetime.datetime.fromisoformat(checked_at.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def summarize(entries: list) -> dict:
    """Aggregates a list of fact_check_log.jsonl entries into one totals
    dict. Pure function, no network/file access, so it works the same on
    "one day's entries" or "everything ever logged."""
    return {
        "calls": len(entries),
        "vetoes": sum(1 for e in entries if e.get("verdict") == "VETO"),
        "errors": sum(1 for e in entries if e.get("verdict") == "ERROR"),
        "input_tokens": sum(e.get("input_tokens", 0) for e in entries),
        "output_tokens": sum(e.get("output_tokens", 0) for e in entries),
        "web_searches": sum(e.get("web_searches", 0) for e in entries),
        "cost_usd": round(sum(e.get("cost_usd", 0.0) for e in entries), 6),
    }


def group_by_date(entries: list) -> dict:
    """{date: [entries]} for every entry with a parseable checked_at. Pure
    function."""
    by_date = {}
    for entry in entries:
        d = entry_date(entry)
        if d is None:
            continue
        by_date.setdefault(d, []).append(entry)
    return by_date


def build_report(entries: list, today: datetime.date) -> dict:
    """Builds the full docs/fact_check_budget.json payload: a per-day
    history (oldest first), today's totals on their own, and running
    month-to-date / all-time totals. Pure function of `entries` and
    `today`, so it's testable without touching the filesystem or clock."""
    by_date = group_by_date(entries)
    daily = [
        {"date": d.isoformat(), **summarize(day_entries)}
        for d, day_entries in sorted(by_date.items())
    ]

    today_entries = by_date.get(today, [])
    month_entries = [e for d, es in by_date.items() if d.year == today.year and d.month == today.month for e in es]

    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "pricing_reference": {
            "input_token_cost_per_million_usd": llm_fact_check.INPUT_TOKEN_COST_PER_MILLION,
            "output_token_cost_per_million_usd": llm_fact_check.OUTPUT_TOKEN_COST_PER_MILLION,
            "web_search_cost_per_search_usd": llm_fact_check.WEB_SEARCH_COST_PER_SEARCH,
        },
        "today": {"date": today.isoformat(), **summarize(today_entries)},
        "month_to_date": {"month": today.strftime("%Y-%m"), **summarize(month_entries)},
        "all_time": summarize(entries),
        "daily": daily,
    }


def format_report_text(report: dict) -> str:
    """Human-readable version of build_report()'s output, printed to stdout
    so the numbers show up directly in the GitHub Actions run log. Pure
    function of the report dict."""
    t, m, a = report["today"], report["month_to_date"], report["all_time"]
    lines = [
        "Fact-check budget report",
        "-------------------------",
        f"Today ({report['today']['date']}): {t['calls']} calls, {t['vetoes']} veto(es), "
        f"{t['errors']} error(s), {t['web_searches']} searches, "
        f"{t['input_tokens'] + t['output_tokens']:,} tokens -> ${t['cost_usd']:.4f}",
        f"Month-to-date ({m['month']}): {m['calls']} calls -> ${m['cost_usd']:.4f}",
        f"All-time: {a['calls']} calls, {a['vetoes']} veto(es) -> ${a['cost_usd']:.4f}",
    ]
    return "\n".join(lines)


def main():
    entries = load_fact_check_log()
    today = datetime.datetime.now(datetime.timezone.utc).date()
    report = build_report(entries, today)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(format_report_text(report))
    if not entries:
        print("Note: docs/fact_check_log.jsonl doesn't exist yet or is empty -- "
              "no fact-checks have run.", file=sys.stderr)


if __name__ == "__main__":
    main()
