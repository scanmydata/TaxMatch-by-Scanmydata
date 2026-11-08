"""Έλεγχοι για τις νέες λειτουργίες:
1. Αναγνώριση νομικής μορφής (π.χ. ΜΟΝΟΠΡΟΣΩΠΗ ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ ΕΤΑΙΡΕΙΑ - CHARGELINE) ως νομικό πρόσωπο
   (και όχι ιδιώτης), ώστε να εμφανίζεται η καρτέλα myDATA.
2. Αυτόματη ανάκτηση ΑΜΕ (e-ΕΦΚΑ) στην ενημέρωση στοιχείων πελάτη (lookup_and_store) και σωστή δημιουργία υποχρεώσεων μισθοδοσίας.
3. Βελτιωμένη απεικόνιση και ενημέρωση των ποσοστών στα flash messages (ProgressToast / JobQueue).
"""
import pytest
from PySide6.QtWidgets import QApplication, QWidget
from taxmatch import db, efka, obligations
from taxmatch.business_profiles import credentials, service
from taxmatch.gui.job_queue import JobQueue
from taxmatch.gui.toast import ProgressToast


def test_chargeline_is_legal_entity_not_private():
    form = "ΜΟΝΟΠΡΟΣΩΠΗ ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ ΕΤΑΙΡΕΙΑ"
    b = {"afm": "802499326", "legal_form": form, "activity_state": "active"}
    assert obligations.is_legal_entity(form) is True
    assert obligations.is_private_person(b) is False

    # Άλλες πλήρεις ονομασίες νομικών μορφών
    assert obligations.is_legal_entity("ΑΝΩΝΥΜΗ ΕΤΑΙΡΕΙΑ") is True
    assert obligations.is_legal_entity("ΕΤΑΙΡΕΙΑ ΠΕΡΙΟΡΙΣΜΕΝΗΣ ΕΥΘΥΝΗΣ") is True
    assert obligations.is_legal_entity("ΟΜΟΡΡΥΘΜΗ ΕΤΑΙΡΕΙΑ") is True
    assert obligations.is_legal_entity("ΕΤΕΡΟΡΡΥΘΜΗ ΕΤΑΙΡΕΙΑ") is True

    # Πραγματικός ιδιώτης παραμένει ιδιώτης
    assert obligations.is_private_person({"legal_form": "ΙΔΙΩΤΗΣ", "activity_state": "none"}) is True
    assert obligations.is_private_person({"legal_form": "ΑΤΟΜΙΚΗ", "activity_state": "ceased"}) is True


def test_lookup_and_store_automatically_retrieves_ame_and_sets_employer(conn):
    afm = "802499326"
    service.add(conn, afm, "CHARGELINE ΜΟΝΟΠΡΟΣΩΠΗ ΙΚΕ")
    credentials.set_(conn, afm, "user", "pass")

    # Αρχικά employer είναι None (άγνωστο)
    row = conn.execute("SELECT employer FROM businesses WHERE afm=?", (afm,)).fetchone()
    assert row["employer"] is None

    # Dummy AADE fetcher
    def fake_aade(*args, **kwargs):
        return {
            "ok": True,
            "name": "CHARGELINE ΜΟΝΟΠΡΟΣΩΠΗ ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ ΕΤΑΙΡΕΙΑ",
            "legal_form": "ΜΟΝΟΠΡΟΣΩΠΗ ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ ΕΤΑΙΡΕΙΑ",
            "doy": "ΙΒ' ΑΘΗΝΩΝ",
            "address": "Δοκιμή 1",
            "kind": "ΝΟΜΙΚΟ ΠΡΟΣΩΠΟ",
            "business_state": "active",
            "all_tags": {},
            "kads": [{"code": "43210000", "descr": "Ηλεκτρολογικές", "is_main": True}],
        }

    # Dummy AME fetcher που βρίσκει ΑΜΕ
    ame_called = []
    def fake_ame_fetch(c, a):
        ame_called.append(a)
        efka.upsert_ame(c, a, "1234567890", kind="koinon", name="Κεντρικό", source="efka")
        efka.set_(c, a, ame="1234567890", source="efka")
        return {"ok": True, "ame": "1234567890", "all": [{"ame": "1234567890", "kind": "Κοινών Επιχειρήσεων"}]}

    out = service.lookup_and_store(conn, afm, aade_fetch=fake_aade, ame_fetch=fake_ame_fetch)
    assert afm in ame_called
    assert "e-ΕΦΚΑ (ΑΜΕ)" in out["sources"]

    # Ελέγχουμε ότι το employer ενημερώθηκε σε 1
    row = conn.execute("SELECT employer FROM businesses WHERE afm=?", (afm,)).fetchone()
    assert row["employer"] == 1

    # Και οι υποχρεώσεις μισθοδοσίας παράγονται ως "yes" (ενεργές)
    b_updated = dict(conn.execute("SELECT * FROM businesses WHERE afm=?", (afm,)).fetchone())
    assert obligations.liability("apd", b_updated, "2026-08")[0] == "yes"
    assert obligations.liability("fmy_wages", b_updated, "2026-08")[0] == "yes"
    assert obligations.liability("ergani_e4", b_updated)[0] == "yes"


def test_progress_toast_initializes_with_total_and_updates_percentage():
    app = QApplication.instance() or QApplication([])
    w = QWidget()
    toast = ProgressToast(w, "Λήψη δεδομένων…", total=10, done=0)
    assert toast._total == 10
    assert toast._done == 0
    item = w._toast_host.layout().itemAt(0).widget()
    assert item.bar.maximum() == 10
    assert item.bar.value() == 0
    assert "0% (0/10)" in item.bar.format()
    assert "0% (0/10)" in item.label.text()

    # Ενημέρωση προόδου
    toast.update("Λήψη 4/10", done=4)
    assert item.bar.value() == 4
    assert "40% (4/10)" in item.bar.format()
    assert "40% (4/10)" in item.label.text()

    toast.finish("Ολοκληρώθηκε", "ok")
    assert item.bar.isHidden()
    w.deleteLater()


def test_mydata_payroll_and_expenses_categorization():
    from taxmatch.mydata import analysis

    # 17.1 Μισθοδοσία
    payroll_doc = {"direction": "outgoing", "invoice_type": "17.1", "net": 1250.0, "vat": 0.0}
    assert analysis.is_income(payroll_doc) is False

    # 17.2 Αποσβέσεις
    deprec_doc = {"direction": "outgoing", "invoice_type": "17.2", "net": 300.0, "vat": 0.0}
    assert analysis.is_income(deprec_doc) is False

    # 17.5 & 17.6 Τακτοποίηση εξόδων
    assert analysis.is_income({"direction": "outgoing", "invoice_type": "17.5"}) is False
    assert analysis.is_income({"direction": "outgoing", "invoice_type": "17.6"}) is False

    # 1.1 Κανονικό τιμολόγιο πώλησης -> Έσοδο
    sale_doc = {"direction": "outgoing", "invoice_type": "1.1", "net": 2000.0, "vat": 480.0}
    assert analysis.is_income(sale_doc) is True

    # Εισερχόμενο παραστατικό -> Έξοδο
    assert analysis.is_income({"direction": "incoming", "invoice_type": "1.1"}) is False


def test_client_table_keyboard_search_jumps_past_arrows():
    from PySide6.QtWidgets import QApplication, QTableWidgetItem
    from taxmatch.gui.main_window import _ClientTableWidget, _C_NAME, _CLIENT_COLS

    app = QApplication.instance() or QApplication([])
    table = _ClientTableWidget(3, len(_CLIENT_COLS))

    item0 = QTableWidgetItem("ΑΛΦΑ ΑΕ")
    item1 = QTableWidgetItem("▼ CHARGELINE ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε")
    item2 = QTableWidgetItem("↳ ΜΑΛΛΗΣ ΕΛΕΥΘΕΡΙΟΣ")

    table.setItem(0, _C_NAME, item0)
    table.setItem(1, _C_NAME, item1)
    table.setItem(2, _C_NAME, item2)

    table.setCurrentCell(0, _C_NAME)
    assert table.currentRow() == 0

    # Πάτημα 'C' οδηγεί απευθείας στην CHARGELINE παρόλο που έχει το '▼ ' μπροστά
    table.keyboardSearch("C")
    assert table.currentRow() == 1

    # Πάτημα 'M' οδηγεί στον ΜΑΛΛΗ παρόλο που έχει το '↳ ' μπροστά
    table.keyboardSearch("M")
    assert table.currentRow() == 2

    # Πάτημα 'Ψ' (διάταξη ελληνικού πληκτρολογίου στο πλήκτρο C)
    table.setCurrentCell(0, _C_NAME)
    table.keyboardSearch("Ψ")
    assert table.currentRow() == 1

    # Πάτημα 'Χ' (αναζήτηση CH)
    table.setCurrentCell(0, _C_NAME)
    table.keyboardSearch("Χ")
    assert table.currentRow() == 1

    table.deleteLater()
