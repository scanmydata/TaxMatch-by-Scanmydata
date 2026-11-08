import pytest
from PySide6.QtWidgets import QApplication, QWidget
from taxmatch import db, efka
from taxmatch.business_profiles import credentials, service
from taxmatch.gui.checks_dialog import ChecksDialog


def test_has_ika_credentials(conn):
    afm = "802499326"
    service.add(conn, afm, "CHARGELINE ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε")

    # Αρχικά χωρίς κωδικούς ΙΚΑ
    assert efka.has_ika_credentials(conn, afm) is False
    assert efka.has_ika_credentials(conn, afm, "1234567890") is False

    # Προσθήκη γενικών κωδικών ΙΚΑ στο efka_credentials
    efka.set_(conn, afm, user="testuser", password="testpass")
    assert efka.has_ika_credentials(conn, afm) is True
    assert efka.has_ika_credentials(conn, afm, "1234567890") is True

    # Αν προσθέσουμε άλλο ΑΜΕ με δικούς του κωδικούς
    afm2 = "094259216"
    service.add(conn, afm2, "ΕΤΑΙΡΕΙΑ 2")
    efka.upsert_ame(conn, afm2, "9876543210", kind="koinon", name="Κεντρικό", user="ameuser", password="amepass")
    assert efka.has_ika_credentials(conn, afm2) is True
    assert efka.has_ika_credentials(conn, afm2, "9876543210") is True


def test_check_apd_entries_without_ika_credentials_fails_immediately_without_calling_login(conn):
    afm = "802499326"
    service.add(conn, afm, "CHARGELINE ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε")
    conn.execute("UPDATE businesses SET employer=1 WHERE afm=?", (afm,))
    efka.upsert_ame(conn, afm, "1234567890", kind="koinon", name="Κεντρικό")

    # TAXISnet credentials υπάρχουν
    credentials.set_(conn, afm, "taxis_user", "taxis_pass")

    # Αλλά ΔΕΝ υπάρχουν κωδικοί ΙΚΑ
    assert efka.has_ika_credentials(conn, afm) is False

    calls = []
    class FakeHttp:
        def follow(self, *args, **kwargs):
            calls.append(args)
            return {"url": "http", "text": "<html></html>"}

    entries = efka.check_apd_entries(conn, afm, 2026, http_factory=lambda: FakeHttp())

    # Δεν έγινε καμία κλήση δικτύου (καμία καθυστέρηση / αναμονή)
    assert len(calls) == 0

    # Και επιστράφηκε άμεσα σαφές σφάλμα για έλλειψη κωδικών ΙΚΑ
    assert len(entries) == 1
    assert entries[0]["status"] == "error"
    assert "κωδικοί ΙΚΑ" in entries[0]["detail"]


def test_checks_dialog_shows_ika_prompt_and_missing_employers():
    app = QApplication.instance() or QApplication([])
    w = QWidget()
    scope = "2 επιλεγμένοι πελάτες"
    incomplete = {}
    names = {"802499326": "CHARGELINE", "094259216": "ΑΛΦΑ"}
    missing_ika = ["802499326"]

    dlg = ChecksDialog(w, scope, incomplete, names, missing_ika=missing_ika)
    assert hasattr(dlg, "ika_note")
    assert "ΑΠΔ" in dlg.ika_note.text()
    assert "κωδικοί ΙΚΑ" in dlg.ika_note.text()

    # Ελέγχουμε ότι εμφανίζεται η προειδοποίηση για τους εργοδότες που δεν έχουν κωδικούς ΙΚΑ
    all_labels = [c.text() for c in dlg.findChildren(type(dlg.ika_note)) if hasattr(c, "text")]
    assert any("CHARGELINE" in t and "κωδικούς ΙΚΑ" in t for t in all_labels)
    dlg.deleteLater()
    w.deleteLater()
