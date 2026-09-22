"""Καπνοδοκιμές του native GUI (PySide6): ανοίγει πραγματικά widgets (offscreen, χωρίς οθόνη) πάνω σε απομονωμένη
βάση και ελέγχει ότι δεν σκάει και ότι δείχνει σωστά δεδομένα. ΔΕΝ προσομοιώνει κλικ χρήστη (αυτό επαληθεύτηκε
οπτικά, χειροκίνητα, σε πραγματικό παράθυρο Windows) — εδώ ελέγχεται η κατασκευή/φόρτωση των widgets."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")     # πριν από ΚΑΘΕ import PySide6 σε αυτό το process

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
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


def test_settings_page_shows_saved_status_for_credential_fields_without_revealing_them(window, conn):
    settings_store.set_value(conn, "groq_api_key", "gsk_dummy_value_not_real")
    window.reload_settings()
    assert "Αποθηκευμένο" in window._settings_status["groq_api_key"].text()
    assert "δεν έχει οριστεί" in window._settings_status["openrouter_api_key"].text()
    # ΠΟΤΕ δεν ξαναγεμίζει το ίδιο το πεδίο με το μυστικό — μόνο η ετικέτα κατάστασης το δείχνει.
    assert window._settings_fields["groq_api_key"].text() == ""


def test_llm_model_refresh_populates_combo_and_keeps_current_selection(window, conn, monkeypatch):
    settings_store.set_value(conn, "llm_provider", "groq")
    settings_store.set_value(conn, "groq_api_key", "gsk_dummy_value_not_real")
    settings_store.set_value(conn, "llm_model_groq", "llama-3.3-70b-versatile")
    window.reload_settings()

    from taxmatch.extraction import llm_extract as llm_extract_mod
    monkeypatch.setattr(llm_extract_mod, "list_models",
                        lambda client, timeout=30: ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"])

    window._refresh_llm_models()
    # Το run_task τρέχει σε πραγματικό QThread: περιμένουμε το event loop να προλάβει το αποτέλεσμα.
    import time
    for _ in range(50):
        QApplication.processEvents()
        if window.llm_model_groq.count() > 0:
            break
        time.sleep(0.05)
    assert window.llm_model_groq.count() == 2
    assert window._combo_model_value(window.llm_model_groq) == "llama-3.3-70b-versatile"


def test_calendar_page_shows_month_grid_with_day_drilldown(window, conn):
    from datetime import date
    window._cal_month = date(2026, 7, 1)          # μήνας χωρίς εγγραφές feed -> μόνο κανόνες (βλ. taxheaven_calendar)
    window.cal_news.setChecked(False)              # χωρίς δίκτυο σε αυτό το test: μόνο rules/general ήδη στη βάση
    window.reload_calendar()
    assert window.cal_stack.currentIndex() == 0    # προεπιλογή: μηνιαίο grid
    assert window.cal_grid.count() in (35, 42)      # 5 ή 6 εβδομάδες x 7 ημέρες, πάντα πλήρες grid

    by_day: dict = {}
    for ev in window._cal_events:
        by_day.setdefault(ev["date"], []).append(ev)
    assert by_day, "το test προϋποθέτει τουλάχιστον μία υποχρέωση τον Ιούλιο 2026 (κανόνες ΦΠΑ/ΑΠΔ κ.λπ.)"
    day_str = sorted(by_day)[0]

    window._open_cal_day_date(date.fromisoformat(day_str))
    assert window.cal_stack.currentIndex() == 1    # κλικ σε ημέρα -> ημερήσια προβολή
    assert window.cal_list.count() >= 1
    assert all(ev["date"] == day_str for ev in
              (window.cal_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(window.cal_list.count())))
    window._close_cal_day()
    assert window.cal_stack.currentIndex() == 0    # «Πίσω στον μήνα» -> ξαναγυρνά στο grid


def test_calendar_day_cell_click_opens_that_day(window, conn):
    from datetime import date
    window._cal_month = date(2026, 7, 1)
    window.cal_news.setChecked(False)
    window.reload_calendar()
    from taxmatch.gui.main_window import _DayCell
    cells = [window.cal_grid.itemAt(i).widget() for i in range(window.cal_grid.count())]
    clickable = [c for c in cells if isinstance(c, _DayCell) and c.isEnabled() and "\n" in c.text()]
    assert clickable, "πρέπει να υπάρχει τουλάχιστον μία ημέρα με υποχρέωση, με ενεργό κελί"
    clickable[0].click()
    assert window.cal_stack.currentIndex() == 1


def test_news_page_lists_articles(window, conn):
    import json
    now = db.utcnow()
    conn.execute("INSERT INTO articles(source,title,url,url_hash,published_at,fetched_at,extraction_status,extracted_json) "
                "VALUES ('taxheaven_new','Τίτλος','https://x/1','h1',?,?,'done',?)",
                (now, now, json.dumps({"summary": "s", "scope": {"type": "all"}, "relevant": True})))
    window.news_only.setCurrentIndex(window.news_only.findData("all"))
    assert window.news_table.rowCount() == 1
    assert window.news_table.item(0, 2).text() == "Τίτλος"


def test_quick_client_search_opens_single_match_detail(window, conn, monkeypatch):
    service.add(conn, AFM, "ΚΑΦΕΤΕΡΙΑ")
    service.add(conn, "123456783", "ΒΙΒΛΙΟΠΩΛΕΙΟ")
    window.reload_clients()
    opened = []
    monkeypatch.setattr(window, "open_client_detail", lambda afm: opened.append(afm))
    window.quick_search.setText("καφε")
    window._quick_client_search()
    assert opened == [AFM]
    assert window.stack.currentWidget() is window._pages["clients"]


def test_toast_shows_side_flash_message_and_can_be_dismissed(window):
    from taxmatch.gui.toast import toast

    toast(window, "Δοκιμαστικό μήνυμα", "ok", ms=0)
    host = window._toast_host
    assert not host.isHidden()                     # host.show() κλήθηκε (isVisible() θέλει και ορατό MainWindow)
    assert host._box.count() == 1
    host._box.itemAt(0).widget().close_btn.click()
    assert host._box.count() == 0
    assert host.isHidden()


def test_dashboard_shows_last_run_label(window, conn):
    conn.execute("INSERT INTO runs(trigger, started_at, finished_at, status) VALUES ('manual', ?, ?, 'ok')",
                (db.utcnow(), db.utcnow()))
    window.reload_dashboard()
    assert "Τελευταία ενημέρωση" in window.last_update_label.text()


def test_tour_steps_all_target_existing_visible_widgets(window):
    """Κάθε βήμα της ξενάγησης πρέπει να καταλήγει σε widget που ΥΠΑΡΧΕΙ — αν είναι κρυμμένο (π.χ. πίσω από άλλη
    σελίδα ή QStackedWidget), το Tour απλώς δεν φωτίζει τίποτα (βλ. `tour._target_rect`), οπότε ένα λάθος target
    δεν σκάει ποτέ μόνο του· πρέπει να ελεγχθεί ρητά, όπως εδώ."""
    window.show()  # isVisible() στο Tour χρειάζεται ολόκληρη την αλυσίδα γονέων ορατή
    try:
        for step in window._tour_steps():
            if step.before:
                step.before()
            widget = step.target()
            assert widget is not None, step.title
            assert widget.isVisible(), f"{step.title}: το target δεν είναι ορατό μετά το before()"
    finally:
        window.hide()


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
