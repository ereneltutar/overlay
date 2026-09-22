import datetime

import fact_check_budget as fcb


def entry(date_str, verdict="SAFE", input_tokens=1000, output_tokens=100, web_searches=1, cost=0.03):
    return {
        "bet_id": "calibration:x:1", "market_question": "Q?", "recommended_side": "YES",
        "passed": verdict != "VETO", "verdict": verdict, "reason": "r",
        "checked_at": f"{date_str}T12:00:00+00:00",
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "web_searches": web_searches, "cost_usd": cost,
    }


# --- entry_date -----------------------------------------------------------

def test_entry_date_parses_checked_at():
    assert fcb.entry_date(entry("2026-09-22")) == datetime.date(2026, 9, 22)


def test_entry_date_missing_returns_none():
    assert fcb.entry_date({}) is None


def test_entry_date_unparseable_returns_none():
    assert fcb.entry_date({"checked_at": "not-a-date"}) is None


# --- summarize --------------------------------------------------------

def test_summarize_empty_list():
    result = fcb.summarize([])
    assert result == {"calls": 0, "vetoes": 0, "errors": 0, "input_tokens": 0,
                       "output_tokens": 0, "web_searches": 0, "cost_usd": 0.0}


def test_summarize_counts_vetoes_and_errors_separately():
    entries = [entry("2026-09-22", verdict="SAFE"), entry("2026-09-22", verdict="VETO"),
               entry("2026-09-22", verdict="ERROR", cost=0.0)]
    result = fcb.summarize(entries)
    assert result["calls"] == 3
    assert result["vetoes"] == 1
    assert result["errors"] == 1


def test_summarize_sums_tokens_and_cost():
    entries = [entry("2026-09-22", input_tokens=1000, output_tokens=100, web_searches=1, cost=0.03),
               entry("2026-09-22", input_tokens=2000, output_tokens=200, web_searches=2, cost=0.05)]
    result = fcb.summarize(entries)
    assert result["input_tokens"] == 3000
    assert result["output_tokens"] == 300
    assert result["web_searches"] == 3
    assert result["cost_usd"] == 0.08


def test_summarize_missing_cost_fields_default_to_zero():
    # Older log line / stub result without cost fields shouldn't crash the rollup.
    bare = {"verdict": "SAFE", "checked_at": "2026-09-22T12:00:00+00:00"}
    result = fcb.summarize([bare])
    assert result["cost_usd"] == 0.0
    assert result["input_tokens"] == 0


# --- group_by_date -------------------------------------------------------

def test_group_by_date_buckets_by_calendar_day():
    entries = [entry("2026-09-21"), entry("2026-09-22"), entry("2026-09-22")]
    grouped = fcb.group_by_date(entries)
    assert len(grouped[datetime.date(2026, 9, 21)]) == 1
    assert len(grouped[datetime.date(2026, 9, 22)]) == 2


def test_group_by_date_skips_unparseable_entries():
    entries = [entry("2026-09-22"), {"checked_at": None}]
    grouped = fcb.group_by_date(entries)
    assert sum(len(v) for v in grouped.values()) == 1


# --- build_report ----------------------------------------------------

def test_build_report_today_month_and_all_time():
    entries = [
        entry("2026-08-15", cost=0.10),   # different month
        entry("2026-09-01", cost=0.05),   # same month, different day
        entry("2026-09-22", cost=0.03),   # today
        entry("2026-09-22", cost=0.04),   # today, second call
    ]
    today = datetime.date(2026, 9, 22)
    report = fcb.build_report(entries, today)

    assert report["today"]["calls"] == 2
    assert report["today"]["cost_usd"] == 0.07
    assert report["month_to_date"]["calls"] == 3  # Sep 1 + Sep 22 x2, not Aug
    assert round(report["month_to_date"]["cost_usd"], 2) == 0.12
    assert report["all_time"]["calls"] == 4
    assert round(report["all_time"]["cost_usd"], 2) == 0.22
    assert len(report["daily"]) == 3  # one entry per distinct calendar day
    assert report["daily"][0]["date"] == "2026-08-15"  # oldest first


def test_build_report_no_entries_today_still_has_zeroed_today_block():
    today = datetime.date(2026, 9, 22)
    report = fcb.build_report([], today)
    assert report["today"]["calls"] == 0
    assert report["today"]["date"] == "2026-09-22"
    assert report["daily"] == []


def test_build_report_includes_pricing_reference():
    report = fcb.build_report([], datetime.date(2026, 9, 22))
    assert report["pricing_reference"]["input_token_cost_per_million_usd"] > 0


# --- format_report_text -------------------------------------------------

def test_format_report_text_includes_key_numbers():
    today = datetime.date(2026, 9, 22)
    report = fcb.build_report([entry("2026-09-22", cost=0.05)], today)
    text = fcb.format_report_text(report)
    assert "2026-09-22" in text
    assert "$0.0500" in text
    assert "1 calls" in text


# --- load_fact_check_log (filesystem, isolated) ----------------------------

def test_load_fact_check_log_missing_file_returns_empty_list(monkeypatch, tmp_path):
    monkeypatch.setattr(fcb, "FACT_CHECK_LOG_PATH", tmp_path / "nonexistent.jsonl")
    assert fcb.load_fact_check_log() == []


def test_load_fact_check_log_reads_jsonl(monkeypatch, tmp_path):
    log_path = tmp_path / "fact_check_log.jsonl"
    log_path.write_text('{"verdict": "SAFE"}\n{"verdict": "VETO"}\n', encoding="utf-8")
    monkeypatch.setattr(fcb, "FACT_CHECK_LOG_PATH", log_path)
    entries = fcb.load_fact_check_log()
    assert len(entries) == 2
    assert entries[0]["verdict"] == "SAFE"


def test_load_fact_check_log_skips_malformed_lines(monkeypatch, tmp_path):
    log_path = tmp_path / "fact_check_log.jsonl"
    log_path.write_text('{"verdict": "SAFE"}\nnot valid json\n', encoding="utf-8")
    monkeypatch.setattr(fcb, "FACT_CHECK_LOG_PATH", log_path)
    assert len(fcb.load_fact_check_log()) == 1
