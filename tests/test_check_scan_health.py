import check_scan_health as csh


def snap(date, scanned, opp=0, cal=0, mis=0):
    return {"date": date, "scanned_events": scanned, "opportunities": opp,
            "calibration_signals": cal, "mispricing_signals": mis}


# --- record_snapshot -------------------------------------------------------

def test_record_snapshot_appends_new_date():
    health = {"history": []}
    csh.record_snapshot(health, "2026-01-01", 100, 1, 0, 0)
    assert len(health["history"]) == 1
    assert health["history"][0]["date"] == "2026-01-01"


def test_record_snapshot_overwrites_same_date():
    health = {"history": [snap("2026-01-01", 100)]}
    csh.record_snapshot(health, "2026-01-01", 150, 2, 0, 0)
    assert len(health["history"]) == 1
    assert health["history"][0]["scanned_events"] == 150


def test_record_snapshot_trims_to_keep_window(monkeypatch):
    monkeypatch.setattr(csh, "HISTORY_KEEP_DAYS", 5)
    health = {"history": [snap(f"2026-01-{d:02d}", 100) for d in range(1, 10)]}
    csh.record_snapshot(health, "2026-01-10", 100, 0, 0, 0)
    assert len(health["history"]) == 5
    assert health["history"][-1]["date"] == "2026-01-10"


# --- detect_anomalies -------------------------------------------------------

def test_detect_anomalies_empty_history_is_healthy():
    assert csh.detect_anomalies([]) == []


def test_detect_anomalies_zero_events_flagged():
    history = [snap("2026-01-01", 0)]
    anomalies = csh.detect_anomalies(history)
    assert any("ZERO_EVENTS" in a for a in anomalies)


def test_detect_anomalies_healthy_varying_history():
    history = [snap("2026-01-01", 1000), snap("2026-01-02", 1050), snap("2026-01-03", 980)]
    assert csh.detect_anomalies(history) == []


def test_detect_anomalies_stuck_identical_values_flagged():
    history = [snap(f"2026-01-{d:02d}", 2100) for d in range(1, 6)]
    anomalies = csh.detect_anomalies(history)
    assert any("STUCK" in a for a in anomalies)


def test_detect_anomalies_stuck_not_flagged_below_window():
    # only 2 identical values, below STUCK_WINDOW=3
    history = [snap("2026-01-01", 100), snap("2026-01-02", 2100), snap("2026-01-03", 2100)]
    anomalies = csh.detect_anomalies(history)
    assert not any("STUCK" in a for a in anomalies)


def test_detect_anomalies_sudden_drop_flagged():
    history = [snap(f"2026-01-{d:02d}", 5000) for d in range(1, 5)] + [snap("2026-01-05", 1000)]
    anomalies = csh.detect_anomalies(history)
    assert any("SUDDEN_DROP" in a for a in anomalies)


def test_detect_anomalies_moderate_dip_not_flagged():
    # a ~20% dip should not trip the 50% SUDDEN_DROP_FRACTION threshold
    history = [snap(f"2026-01-{d:02d}", 1000) for d in range(1, 5)] + [snap("2026-01-05", 820)]
    anomalies = csh.detect_anomalies(history)
    assert not any("SUDDEN_DROP" in a for a in anomalies)


def test_detect_anomalies_insufficient_history_skips_drop_check():
    # too little prior history to compute a meaningful trailing average
    history = [snap("2026-01-01", 5000), snap("2026-01-02", 100)]
    anomalies = csh.detect_anomalies(history)
    assert not any("SUDDEN_DROP" in a for a in anomalies)


def test_detect_anomalies_zero_events_short_circuits_other_checks():
    # even though this would also look "stuck" or "dropped", ZERO_EVENTS is
    # the only anomaly reported since it supersedes the others
    history = [snap(f"2026-01-{d:02d}", 5000) for d in range(1, 5)] + [snap("2026-01-05", 0)]
    anomalies = csh.detect_anomalies(history)
    assert len(anomalies) == 1
    assert "ZERO_EVENTS" in anomalies[0]
