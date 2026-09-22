"""Καπνοδοκιμές του native GUI (PySide6): ανοίγει πραγματικά widgets (offscreen, χωρίς οθόνη) πάνω σε απομονωμένη
βάση και ελέγχει ότι δεν σκάει και ότι δείχνει σωστά δεδομένα. ΔΕΝ προσομοιώνει κλικ χρήστη (αυτό επαληθεύτηκε
οπτικά, χειροκίνητα, σε πραγματικό παράθυρο Windows) — εδώ ελέγχεται η κατασκευή/φόρτωση των widgets."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")     # πριν από ΚΑΘΕ import PySide6 σε αυτό το process

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from taxmatch import db, settings_store  # noqa: E402
from taxmatch.business_profiles import service  # noqa: E402

AFM = "094259216"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Ποτέ πραγματικό δίκτυο σε αυτά τα tests: το `MainWindow.__init__` καλεί `reload_all()` -> `reload_calendar()`
    -> `taxheaven_calendar.ensure_month_synced` αυτόματα, στην κατασκευή."""
    from taxmatch.ingestion import taxheaven_calendar
    monkeypatch.setattr(taxheaven_calendar, "ensure_month_synced", lambda *a, **k: False)


@pytest.fixture
def window(qapp, conn):
    from taxmatch.gui.main_window import MainWindow
    win = MainWindow()
    yield win
    win.conn.close()
    win.deleteLater()


def test_main_window_builds_all_pages_without_crashing(window):
    assert set(window._pages.keys()) == {"dashboard", "clients", "calendar", "news", "settings"}
    assert window.stack.count() == 5


def test_dashboard_shows_kpis(window, conn):
    service.add(conn, AFM, "ΔΟΚΙΜΗ ΑΕ")
    window.reload_dashboard()
    assert window._kpi_labels["clients"].text() == "1"


def test_clients_table_reflects_database(window, conn):
    service.add(conn, AFM, "ΔΟΚΙΜΗ ΑΕ")
    service.set_kads(conn, AFM, [{"code": "47.11.10.01"}])
    conn.execute("UPDATE businesses SET legal_form='ΑΕ', activity_state='active' WHERE afm=?", (AFM,))
    window.reload_clients()
    assert window.client_table.rowCount() == 1
    from taxmatch.gui.main_window import _C_AFM, _C_NAME, _C_KAD
    assert window.client_table.item(0, _C_AFM).text() == AFM
    assert window.client_table.item(0, _C_NAME).text() == "ΔΟΚΙΜΗ ΑΕ"
    assert window.client_table.item(0, _C_KAD).text() == "47.11.10.01"


def test_client_search_filter_hides_non_matching_rows(window, conn):
    service.add(conn, AFM, "ΚΑΦΕΤΕΡΙΑ")
    service.add(conn, "123456783", "ΒΙΒΛΙΟΠΩΛΕΙΟ")
    window.reload_clients()
    window.client_search.setText("καφε")
    assert window.client_table.rowCount() == 2
    visible = [r for r in range(2) if not window.client_table.isRowHidden(r)]
    assert len(visible) == 1


def test_settings_page_loads_values_from_store(window, conn):
    settings_store.set_value(conn, "llm_provider", "openrouter")
    settings_store.set_value(conn, "daily_time", "09:30")
    window.reload_settings()
    assert window.llm_provider.currentData() == "openrouter"
    assert window.sched_time.text() == "09:30"


def test_calendar_page_lists_rule_based_deadlines(window, conn):
    from datetime import date
    window._cal_month = date(2026, 7, 1)          # μήνας χωρίς εγγραφές feed -> μόνο κανόνες (βλ. taxheaven_calendar)
    window.cal_news.setChecked(False)              # χωρίς δίκτυο σε αυτό το test: μόνο rules/general ήδη στη βάση
    window.reload_calendar()
    assert window.cal_list.count() >= 1


def test_news_page_lists_articles(window, conn):
    import json
    now = db.utcnow()
    conn.execute("INSERT INTO articles(source,title,url,url_hash,published_at,fetched_at,extraction_status,extracted_json) "
                "VALUES ('taxheaven_new','Τίτλος','https://x/1','h1',?,?,'done',?)",
                (now, now, json.dumps({"summary": "s", "scope": {"type": "all"}, "relevant": True})))
    window.news_only.setCurrentIndex(window.news_only.findData("all"))
    assert window.news_table.rowCount() == 1
    assert window.news_table.item(0, 2).text() == "Τίτλος"


def test_run_task_executes_in_background_and_delivers_result(qapp):
    from PySide6.QtCore import QEventLoop, QTimer
    from taxmatch.gui.workers import run_task

    results = []
    loop = QEventLoop()

    def work(progress):
        progress("μισή δουλειά")
        return 42

    task = run_task(None, work, on_done=lambda r: (results.append(r), loop.quit()),
                    on_error=lambda m: (results.append(("error", m)), loop.quit()))
    QTimer.singleShot(5000, loop.quit)               # ασφάλεια: μην κρεμάσει το test αν κάτι πάει στραβά
    loop.exec()
    assert results == [42]
    del task
