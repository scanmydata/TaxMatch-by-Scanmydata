"""Χρονοπρογραμματισμός όλων των εργασιών (scheduled_jobs.py) + χρονικό όριο ανάλυσης άρθρων."""
import time
from datetime import date

from taxmatch import db, scheduled_jobs as sj, scheduler_win, settings_store


def test_frequency_defaults_and_legacy_settings(conn):
    assert sj.freq(conn, "news") == "daily" and sj.freq(conn, "submissions") == "weekly"
    assert sj.freq(conn, "debts") == "off"
    settings_store.set_value(conn, "daily_debts", "1")             # παλιό τσεκ «Λήψη οφειλών στον καθημερινό έλεγχο»
    assert sj.freq(conn, "debts") == "daily"
    sj.set_freq(conn, "debts", "weekly")
    assert sj.freq(conn, "debts") == "weekly" and settings_store.get(conn, "daily_debts") == "1"
    sj.set_freq(conn, "requests", "off")
    assert settings_store.get(conn, "daily_requests") == "0"


def test_is_due_daily_weekly_monthly(conn):
    thu, mon = date(2026, 10, 29), date(2026, 11, 2)
    assert sj.is_due(conn, "news", thu)
    settings_store.set_value(conn, "sched_last_news", "2026-10-29T06:00:00Z")
    assert not sj.is_due(conn, "news", thu) and sj.is_due(conn, "news", date(2026, 10, 30))
    # εβδομαδιαία (Δευτέρα): όχι Πέμπτη χωρίς ιστορικό, ναι Δευτέρα· χαμένη Δευτέρα → τρέχει μετά από 7 ημέρες
    assert not sj.is_due(conn, "submissions", thu) and sj.is_due(conn, "submissions", mon)
    settings_store.set_value(conn, "sched_last_submissions", "2026-10-26T06:00:00Z")
    assert not sj.is_due(conn, "submissions", thu)
    assert sj.is_due(conn, "submissions", mon) and sj.is_due(conn, "submissions", date(2026, 11, 3))
    # μηνιαία από την 1η: μία φορά τον μήνα
    assert sj.is_due(conn, "lookup", thu)
    settings_store.set_value(conn, "sched_last_lookup", "2026-10-01T06:00:00Z")
    assert not sj.is_due(conn, "lookup", thu) and sj.is_due(conn, "lookup", date(2026, 11, 1))
    sj.set_freq(conn, "lookup", "off")
    assert not sj.is_due(conn, "lookup", date(2026, 11, 1))


def test_run_due_is_independent_per_job_and_records_results(conn, monkeypatch):
    calls = []

    def boom(_c):
        calls.append("mydata")
        raise RuntimeError("δίκτυο")
    monkeypatch.setattr(sj, "RUNNERS", {**{j.id: (lambda c, j=j.id: calls.append(j) or "ok") for j in sj.JOBS},
                                        "mydata": boom})
    out = sj.run_due(conn, today=date(2026, 11, 2))                  # Δευτέρα, 2η του μήνα
    assert calls == ["news", "mydata", "submissions", "lookup", "emails"]   # οφειλές/αιτήματα: ανενεργά
    assert out["mydata"][0] is False and out["emails"][0] is True    # η αποτυχία δεν σταμάτησε τις επόμενες
    assert settings_store.get(conn, "sched_result_mydata").startswith("✗ σφάλμα: δίκτυο")
    assert settings_store.get(conn, "sched_result_news") == "✓ ok"
    assert settings_store.get(conn, "sched_last_wake")


def test_run_due_force_runs_all_enabled(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(sj, "RUNNERS", {j.id: (lambda c, j=j.id: calls.append(j) or "ok") for j in sj.JOBS})
    for j in sj.JOBS:
        settings_store.set_value(conn, f"sched_last_{j.id}", db.utcnow())
    sj.set_freq(conn, "debts", "monthly")
    sj.run_due(conn, force=True)
    assert "debts" in calls and "requests" not in calls


def test_extraction_stops_at_deadline(conn):
    from taxmatch.extraction import llm_extract
    conn.execute("INSERT INTO articles(source, title, url, url_hash, published_at, fetched_at) VALUES (?,?,?,?,?,?)",
                 ("t", "Τίτλος", "u", "h", db.utcnow(), db.utcnow()))

    class Client:
        model = "m"
    out = llm_extract.extract_pending(conn, Client(), 10, 30, deadline=time.monotonic() - 1, sleep=lambda s: None)
    assert out["time_limit"] and out["processed"] == 0 and out["remaining"] == 1


def test_task_xml_allows_long_runs():
    xml = scheduler_win.task_xml(r"C:\x\TaxMatch.exe", "--daily", "08:00")
    assert "<ExecutionTimeLimit>PT6H</ExecutionTimeLimit>" in xml and f"v{scheduler_win.TASK_VERSION}" in xml


def test_refresh_if_outdated_never_runs_outside_the_installed_app(conn):
    assert scheduler_win.refresh_if_outdated(conn, "08:00") is False
