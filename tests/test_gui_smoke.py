"""Καπνοδοκιμές του native GUI (PySide6): ανοίγει πραγματικά widgets (offscreen, χωρίς οθόνη) πάνω σε απομονωμένη
βάση και ελέγχει ότι δεν σκάει και ότι δείχνει σωστά δεδομένα. ΔΕΝ προσομοιώνει κλικ χρήστη (αυτό επαληθεύτηκε
οπτικά, χειροκίνητα, σε πραγματικό παράθυρο Windows) — εδώ ελέγχεται η κατασκευή/φόρτωση των widgets."""
import os
from pathlib import Path

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
    # reload_calendar() (καλείται ήδη στο __init__) ξεκινά ένα background QThread· το σήμα «finished» παραδίδεται
    # μόνο όταν το event loop το αντλήσει (queued connection ανάμεσα σε threads). Αν κλείσουμε το `conn` πριν
    # προλάβει να παραδοθεί, ο callback (`done` -> `_render_calendar_from_db`) θα σκάσει ΑΡΓΟΤΕΡΑ πάνω σε κλειστό
    # `conn` — π.χ. στο πρώτο processEvents() ενός ΕΠΟΜΕΝΟΥ test. Περιμένουμε πρώτα να τελειώσει πραγματικά το
    # thread (`thread.wait`) και μετά αδειάζουμε το event loop, ώστε ο callback να τρέξει όσο το `conn` ακόμη ζει.
    for task in list(getattr(win, "_tasks", [])):
        task.thread.wait(3000)
    for _ in range(5):
        QApplication.processEvents()
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


def _pump_until(condition, tries=50, step=0.05):
    import time
    for _ in range(tries):
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(step)
    return False


def test_llm_model_refresh_populates_combo_and_keeps_current_selection(window, conn, monkeypatch):
    settings_store.set_value(conn, "llm_provider", "groq")
    settings_store.set_value(conn, "groq_api_key", "gsk_dummy_value_not_real")
    settings_store.set_value(conn, "llm_model_groq", "llama-3.3-70b-versatile")
    window.reload_settings()
    assert window.llm_model_groq.count() == 1  # μόνο το αποθηκευμένο μοντέλο, πριν από οποιοδήποτε refresh

    from taxmatch.extraction import llm_extract as llm_extract_mod
    monkeypatch.setattr(llm_extract_mod, "list_models",
                        lambda client, timeout=30: ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"])

    window._refresh_llm_models()
    # Το run_task τρέχει σε πραγματικό QThread: περιμένουμε το event loop να προλάβει το αποτέλεσμα. Το combo
    # ξεκινά ήδη με 1 στοιχείο (το αποθηκευμένο μοντέλο) — περιμένουμε συγκεκριμένα τα 2 του refresh, όχι απλώς
    # "μη άδειο".
    assert _pump_until(lambda: window.llm_model_groq.count() == 2)
    assert window._combo_model_value(window.llm_model_groq) == "llama-3.3-70b-versatile"


def test_start_lookup_uses_its_own_db_connection_not_the_uis(window, conn):
    """`_start_lookup`'s `work()` χρησιμοποιούσε το `self.conn` (φτιαγμένο στο UI thread) μέσα σε πραγματικό
    background QThread· το sqlite3 απαγορεύει χρήση μιας σύνδεσης από ΑΛΛΟ thread από αυτό που τη δημιούργησε
    («SQLite objects created in a thread can only be used in that same thread») — αυτό το test τρέχει το
    πραγματικό background thread (όχι mock) και επιβεβαιώνει ότι ΔΕΝ σκάει."""
    service.add(conn, AFM, "ΔΟΚΙΜΗ")
    window._start_lookup([AFM])

    def finished() -> bool:
        text = window.run_status.text()
        return text == "Η ανάκτηση στοιχείων ολοκληρώθηκε." or text.startswith("Σφάλμα")

    assert _pump_until(finished)
    assert window.run_status.text() == "Η ανάκτηση στοιχείων ολοκληρώθηκε."


def test_test_llm_uses_its_own_db_connection_not_the_uis(window, conn):
    """Ίδιο πρόβλημα με το `_start_lookup` παραπάνω: το `_test_llm`'s `work()` διάβαζε ρυθμίσεις μέσω του
    `self.conn` (UI thread) μέσα σε background QThread. Χωρίς κλειδί API, το σωστό αποτέλεσμα είναι ένα σαφές
    μήνυμα «δεν έχει οριστεί API key» — ΟΧΙ το τεχνικό σφάλμα cross-thread sqlite3."""
    settings_store.set_value(conn, "openrouter_api_key", "")
    settings_store.set_value(conn, "groq_api_key", "")
    window._toast_host = None  # νέο host ώστε να μετρήσουμε καθαρά τα toasts αυτού του test
    window._test_llm()
    assert _pump_until(lambda: window._toast_host is not None and window._toast_host._box.count() > 0)
    # Το toast δεν εκθέτει το κείμενό του ως attribute δημόσια· το πρώτο widget της γραμμής του είναι το QLabel.
    label = window._toast_host._box.itemAt(0).widget().layout().itemAt(0).widget()
    assert "thread" not in label.text().lower() and "sqlite" not in label.text().lower()
    assert "API key" in label.text() or "δεν έχει οριστεί" in label.text()


def test_test_llm_success_clears_the_stale_error_banner(window, conn, monkeypatch):
    """Το πάνω banner («Η ανάλυση άρθρων με LLM δεν δουλεύει: …») διαβάζει το `llm_last_error` — έμενε μόνιμα ορατό
    ακόμη και μετά από ΕΠΙΤΥΧΗΜΕΝΗ «Δοκιμή LLM», γιατί το gui/ (σε αντίθεση με το web/) δεν το καθάριζε ποτέ σε
    επιτυχία. Πρέπει να εξαφανίζεται αμέσως, χωρίς να χρειαστεί να ξανανοίξει η εφαρμογή."""
    settings_store.set_value(conn, "openrouter_api_key", "sk-or-dummy")
    settings_store.set_value(conn, "llm_last_error", "παλιό σφάλμα πιστοποίησης")
    window._reload_notices()
    assert window.notices_box.count() >= 1

    from taxmatch.extraction.llm_extract import LLMClient
    monkeypatch.setattr(LLMClient, "complete_json", lambda self, *a, **k: '{"ok": true}')

    window._test_llm()
    # Περιμένουμε το ΠΑΡΑΤΗΡΗΣΙΜΟ αποτέλεσμα στο UI (όχι απλώς τη γραφή στη βάση από το background thread — αυτή
    # γίνεται ορατή σε ΑΛΛΕΣ συνδέσεις πριν προλάβει να τρέξει το on_done/_reload_notices() στο UI thread).
    assert _pump_until(lambda: window.notices_box.count() == 0)
    assert settings_store.get(conn, "llm_last_error") == ""


def test_reload_calendar_does_not_block_on_slow_network(window, conn, monkeypatch):
    """Το `ensure_month_synced` (HTTP GET στο taxheaven.gr) έτρεχε ΣΥΓΧΡΟΝΑ μέσα στο `reload_calendar()` — και επειδή
    αυτό καλείται συχνά (εκκίνηση, αλλαγή μήνα, αλλαγή φίλτρου) πάγωνε όλο το παράθυρο σε κάθε αργό δίκτυο. Αυτό το
    test προσομοιώνει «αργό δίκτυο» (μπλοκάρει μέχρι να το απελευθερώσουμε) και επιβεβαιώνει ότι το reload_calendar()
    επιστρέφει σχεδόν ακαριαία παρ' όλα αυτά — δηλαδή ο συγχρονισμός τρέχει σε background thread, όχι στο UI thread."""
    import threading
    import time

    from taxmatch.ingestion import taxheaven_calendar

    started = threading.Event()
    release = threading.Event()

    def slow_sync(*_a, **_k):
        started.set()
        release.wait(timeout=5)
        return False

    monkeypatch.setattr(taxheaven_calendar, "ensure_month_synced", slow_sync)
    t0 = time.monotonic()
    window.reload_calendar()
    elapsed = time.monotonic() - t0
    release.set()  # ελευθέρωσε το background thread ώστε να μην κρεμάσει τον teardown του test
    assert elapsed < 0.5, f"το reload_calendar() μπλόκαρε {elapsed:.2f}s περιμένοντας δίκτυο — έπρεπε να τρέξει στο background"
    assert started.wait(timeout=2), "ο συγχρονισμός έπρεπε να έχει ξεκινήσει έστω σε background thread"


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


def test_calendar_cells_show_real_event_titles_not_just_a_count(window, conn):
    """Το κελί μιας ημέρας με υποχρεώσεις πρέπει να δείχνει τους ΠΡΑΓΜΑΤΙΚΟΥΣ τίτλους (chips), όπως έδειχνε το
    παλιό web UI (`.cal .ev`) — όχι απλώς «3 υποχρεώσεις»."""
    from datetime import date
    window._cal_month = date(2026, 7, 1)
    window.cal_news.setChecked(False)
    window.reload_calendar()
    from taxmatch.gui.main_window import _DayCell
    by_day: dict = {}
    for ev in window._cal_events:
        by_day.setdefault(ev["date"], []).append(ev)
    day_str, evs = sorted(by_day.items())[0]
    cells = [window.cal_grid.itemAt(i).widget() for i in range(window.cal_grid.count())]
    match = next(c for c in cells if isinstance(c, _DayCell) and c.layout().count() > 2
                and str(int(day_str[-2:])) == c.layout().itemAt(0).widget().text())
    chip_texts = [match.layout().itemAt(i).widget().text() for i in range(1, match.layout().count() - 1)
                 if match.layout().itemAt(i).widget() is not None]
    assert any(ev["title"] in chip_texts for ev in evs[:3]), "το κελί πρέπει να δείχνει τον πραγματικό τίτλο του event"


def test_calendar_day_cell_click_opens_that_day(window, conn):
    from datetime import date
    window._cal_month = date(2026, 7, 1)
    window.cal_news.setChecked(False)
    window.reload_calendar()
    from taxmatch.gui.main_window import _DayCell
    cells = [window.cal_grid.itemAt(i).widget() for i in range(window.cal_grid.count())]
    # Κελί με events: μπορεί να κάνει κλικ (_clickable) και δείχνει πάνω από τον αριθμό ημέρας τουλάχιστον 1 chip.
    clickable = [c for c in cells if isinstance(c, _DayCell) and c._clickable and c.layout().count() > 2]
    assert clickable, "πρέπει να υπάρχει τουλάχιστον μία ημέρα με υποχρέωση, με ενεργό κελί που δείχνει events"
    clickable[0].clicked.emit()
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


def test_run_task_callbacks_run_on_the_ui_thread_not_the_worker_thread(qapp):
    """on_progress/on_done/on_error αγγίζουν widgets (toast, combo.addItem, ...) — αν έτρεχαν στο background
    thread (π.χ. επειδή είναι plain closures/lambdas χωρίς δικό τους QObject thread affinity, η Qt δεν μπορεί να
    τα παραδώσει queued αυτόματα), θα πάγωνε/χαλούσε το πραγματικό (όχι offscreen) παράθυρο — βλ. σχόλιο στο
    `workers.Task`. Το test ελέγχει ρητά ΣΕ ΠΟΙΟ thread τρέχει το callback, όχι μόνο ότι φτάνει."""
    import threading

    from PySide6.QtCore import QEventLoop, QTimer

    from taxmatch.gui.workers import run_task

    main_thread = threading.get_ident()
    seen: dict[str, int] = {}
    loop = QEventLoop()

    def work(_progress):
        return threading.get_ident()

    def done(worker_thread_id: int) -> None:
        seen["callback_thread"] = threading.get_ident()
        seen["worker_thread"] = worker_thread_id
        loop.quit()

    task = run_task(None, work, on_done=done)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert seen["callback_thread"] == main_thread
    assert seen["worker_thread"] != main_thread
    del task


def test_client_dialog_excel_button_sets_path_and_accepts(qapp, conn, monkeypatch):
    """«Νέος πελάτης» -> «Εισαγωγή από Excel…» (ίδιο pattern με το etimologio bridge): δεν κάνει μόνο του την
    εισαγωγή — απλώς κλείνει τον διάλογο σαν Accepted με `excel_path` ορισμένο, ώστε ο καλών
    (MainWindow.on_add_client) να ξέρει να τρέξει την εισαγωγή Excel αντί να δημιουργήσει έναν πελάτη."""
    from PySide6.QtWidgets import QDialog, QFileDialog

    from taxmatch.gui.client_dialog import ClientDialog

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("C:/fake/clients.xlsx", "")))
    dlg = ClientDialog(conn)
    dlg._pick_excel()
    assert dlg.excel_path == "C:/fake/clients.xlsx"
    assert dlg.result() == QDialog.DialogCode.Accepted


def test_add_client_dialog_with_excel_path_routes_to_excel_import(window, conn, monkeypatch):
    """Το `on_add_client` πρέπει να ελέγχει ΠΡΩΤΑ το `excel_path` του διαλόγου — αν είναι ορισμένο, τρέχει την
    εισαγωγή Excel αντί να ψάξει `result_afm` (που θα ήταν κενό σε αυτό το μονοπάτι)."""
    from taxmatch.gui import main_window as mw_mod

    calls = []

    class FakeDialog:
        def __init__(self, conn, parent):
            self.excel_path = "C:/fake/clients.xlsx"
            self.result_afm = ""

        def exec(self):
            from PySide6.QtWidgets import QDialog
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw_mod, "ClientDialog", FakeDialog)
    monkeypatch.setattr(window, "_import_excel_from_path", lambda path: calls.append(path))

    window.on_add_client()
    assert len(calls) == 1 and calls[0] == Path("C:/fake/clients.xlsx")
