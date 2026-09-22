"""Πλήρες ημερολόγιο taxheaven (σελίδα, όχι soft_dat.xml) — βλ. σχόλια στο ingestion/taxheaven_calendar.py για το γιατί."""
from datetime import date, datetime, timedelta, timezone

import pytest

from taxmatch.ingestion import taxheaven_calendar as thc
from tests.fakes import FakeResponse, FakeSession, calendar_page


def test_fetch_month_parses_events_and_converts_dates():
    page = calendar_page("07", "2026", [
        {"Title": "Υποβολή δήλωσης ΦΠΑ", "Date": "07/15/2026", "url": "https://www.taxheaven.gr/calendar/event/1",
         "count": 0, "count_url": "x"},
        {"Title": "Υποβολή Intrastat", "Date": "07/28/2026", "url": "https://www.taxheaven.gr/calendar/event/2"},
    ])
    s = FakeSession().route("taxheaven.gr/calendar", FakeResponse(page, headers={"content-type": "text/html"}))
    items = thc.fetch_month(s, 2026, 7)
    assert len(items) == 2
    assert items[0].title == "Υποβολή δήλωσης ΦΠΑ" and items[0].due_date == "2026-07-15"
    assert items[0].guid == items[0].url == "https://www.taxheaven.gr/calendar/event/1"
    assert items[1].due_date == "2026-07-28"


def test_fetch_month_skips_rows_without_title_or_url():
    page = calendar_page("07", "2026", [
        {"Title": "", "Date": "07/15/2026", "url": "https://x/1"},
        {"Title": "Χωρίς url", "Date": "07/16/2026", "url": ""},
        {"Title": "Έγκυρο", "Date": "07/17/2026", "url": "https://x/2"},
    ])
    s = FakeSession().route("taxheaven.gr/calendar", FakeResponse(page, headers={"content-type": "text/html"}))
    items = thc.fetch_month(s, 2026, 7)
    assert [i.title for i in items] == ["Έγκυρο"]


def test_fetch_month_raises_on_unexpected_page_shape():
    s = FakeSession().route("taxheaven.gr/calendar", FakeResponse("<html>δεν υπάρχει calendarData εδώ</html>"))
    with pytest.raises(ValueError):
        thc.fetch_month(s, 2026, 7)


def test_rolling_months_window():
    months = thc.rolling_months(date(2026, 1, 15))
    assert months == [(2025, 12), (2026, 1), (2026, 2), (2026, 3), (2026, 4)]     # γύρω από τη γιορτή του έτους
    assert thc.is_rolling(2025, 12, date(2026, 1, 15))
    assert not thc.is_rolling(2026, 7, date(2026, 1, 15))


def test_ensure_month_synced_first_time_and_skips_far_month_afterwards(conn):
    page = calendar_page("07", "2026", [{"Title": "Α", "Date": "07/15/2026", "url": "https://x/1"}])
    s = FakeSession().route("taxheaven.gr/calendar", FakeResponse(page, headers={"content-type": "text/html"}))
    assert thc.ensure_month_synced(conn, s, 2026, 7) is True
    assert conn.execute("SELECT COUNT(*) FROM obligations_general").fetchone()[0] == 1
    assert conn.execute("SELECT event_count FROM calendar_sync WHERE year_month='2026-07'").fetchone()[0] == 1
    # δεύτερη φορά, ίδια μέρα: δεν ξαναχτυπά το δίκτυο (μήνας μακρινός ως προς το "σήμερα" -> μία φορά αρκεί)
    calls_before = len(s.calls)
    assert thc.ensure_month_synced(conn, s, 2026, 7) is False
    assert len(s.calls) == calls_before


def test_ensure_month_synced_refreshes_rolling_month_when_stale(conn, monkeypatch):
    monkeypatch.setattr(thc, "rolling_months", lambda today=None: [(2026, 9)])   # ο μήνας μας είναι "κοντινός"
    page = calendar_page("09", "2026", [{"Title": "Α", "Date": "09/15/2026", "url": "https://x/1"}])
    s = FakeSession().route("taxheaven.gr/calendar", FakeResponse(page, headers={"content-type": "text/html"}))
    assert thc.ensure_month_synced(conn, s, 2026, 9) is True
    fresh_ts = conn.execute("SELECT synced_at FROM calendar_sync WHERE year_month='2026-09'").fetchone()[0]
    # φρέσκο ακόμη: δεν ξανακατεβαίνει
    assert thc.ensure_month_synced(conn, s, 2026, 9) is False
    # μπαγιάτικο (πάνω από STALE_AFTER): ξανακατεβαίνει
    old = (datetime.now(timezone.utc) - thc.STALE_AFTER - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE calendar_sync SET synced_at=? WHERE year_month='2026-09'", (old,))
    assert thc.ensure_month_synced(conn, s, 2026, 9) is True
    assert conn.execute("SELECT synced_at FROM calendar_sync WHERE year_month='2026-09'").fetchone()[0] >= fresh_ts


def test_ensure_month_synced_network_failure_does_not_raise_and_keeps_old_data(conn):
    s = FakeSession()  # καμία διαδρομή -> 404 σε κάθε αίτημα
    assert thc.ensure_month_synced(conn, s, 2026, 7) is False
    assert conn.execute("SELECT COUNT(*) FROM calendar_sync").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM obligations_general").fetchone()[0] == 0


def test_sync_rolling_window_covers_all_months_and_counts_errors(conn, monkeypatch):
    monkeypatch.setattr(thc, "rolling_months", lambda today=None: [(2026, 6), (2026, 7)])
    good = calendar_page("07", "2026", [{"Title": "Α", "Date": "07/15/2026", "url": "https://x/1"}])
    s = FakeSession().route("taxheaven.gr/calendar", FakeResponse(good, headers={"content-type": "text/html"}))
    out = thc.sync_rolling_window(conn, s)
    assert out == {"months": 2, "refreshed": 2, "errors": 0}
    assert {r[0] for r in conn.execute("SELECT year_month FROM calendar_sync")} == {"2026-06", "2026-07"}
