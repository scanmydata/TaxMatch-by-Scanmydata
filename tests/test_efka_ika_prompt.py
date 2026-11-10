import pytest
from PySide6.QtWidgets import QApplication, QWidget
from taxmatch import db, efka
from taxmatch.business_profiles import credentials, service
from taxmatch.gui.checks_dialog import ChecksDialog


def test_has_ika_credentials(conn):
    afm = "800000002"
    service.add(conn, afm, "CHRONOTEST ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε")

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


def test_apd_login_uses_taxisnet_first_ika_only_as_fallback(conn, monkeypatch):
    """Η «Είσοδος Εργοδότη» του e-ΕΦΚΑ έχει ΜΟΝΟ TAXISnet (ζωντανά 2026-11-10): οι κωδικοί TAXISnet του πελάτη πρώτα —
    η v0.14.2 δοκίμαζε ΜΟΝΟ «κωδικούς ΙΚΑ» και έβγαζε «Λάθος κωδικοί» σε κάθε εργοδότη."""
    afm = "800000002"
    service.add(conn, afm, "CHRONOTEST ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε")
    efka.upsert_ame(conn, afm, "1234567890", kind="koinon", name="Κεντρικό")
    tried = []
    monkeypatch.setattr(efka, "services_login", lambda u, p, a, **kw: tried.append(u) or {"ok": False,
                                                                                        "reason": "InvalidCredentials"})
    assert efka._login_for_ame(conn, afm, "1234567890", lambda: None)["reason"] == efka.REASONS_EL["NoCredentials"]
    credentials.set_(conn, afm, "taxis_user", "taxis_pass")
    efka.set_(conn, afm, user="ika_user", password="ika_pass")
    efka._login_for_ame(conn, afm, "1234567890", lambda: None)
    assert tried == ["taxis_user", "ika_user"]


def test_checks_dialog_shows_ika_prompt_and_missing_employers():
    app = QApplication.instance() or QApplication([])
    w = QWidget()
    scope = "2 επιλεγμένοι πελάτες"
    incomplete = {}
    names = {"800000002": "CHRONOTEST", "094259216": "ΑΛΦΑ"}
    missing_ika = ["800000002"]

    dlg = ChecksDialog(w, scope, incomplete, names, missing_ika=missing_ika)
    assert hasattr(dlg, "ika_note")
    assert "ΑΠΔ" in dlg.ika_note.text()
    assert "TAXISnet" in dlg.ika_note.text()

    # Ελέγχουμε ότι εμφανίζεται η προειδοποίηση για τους εργοδότες που δεν έχουν κωδικούς ΙΚΑ
    all_labels = [c.text() for c in dlg.findChildren(type(dlg.ika_note)) if hasattr(c, "text")]
    assert any("CHRONOTEST" in t and "κωδικούς TAXISnet" in t for t in all_labels)
    dlg.deleteLater()
    w.deleteLater()
