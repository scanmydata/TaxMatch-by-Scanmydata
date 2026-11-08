import os
import sqlite3
import pytest
from pathlib import Path
from taxmatch import db, obligations, easycheck
from taxmatch.business_profiles import service
from taxmatch.mydata import pdfs

def test_chargeline_is_legal_entity_and_not_private():
    b_chargeline = {
        "afm": "802499326",
        "name": "CHARGELINE ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε",
        "legal_form": "ΜΟΝΟΠΡΟΣ ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ ΕΤΑΙΡΕΙΑ",
        "activity_state": "active",
        "status": "ΕΝΕΡΓΗ",
        "kad_main_code": "43.21.00.00",
        "books_category": "Γ",
        "vat_subject": 1,
    }
    assert obligations.is_legal_entity(b_chargeline["legal_form"]) is True
    assert obligations.is_private_person(b_chargeline) is False

def test_mallis_is_private_person(conn):
    now = db.utcnow()
    # Setup company and member using service.add
    service.add(conn, "802499326", "CHARGELINE ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε")
    conn.execute("UPDATE businesses SET legal_form='ΜΟΝΟΠΡΟΣ ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ ΕΤΑΙΡΕΙΑ', status='ΕΝΕΡΓΗ', activity_state='active' WHERE afm='802499326'")

    service.add(conn, "079629198", "ΜΑΛΛΗΣ ΕΛΕΥΘΕΡΙΟΣ ΑΓΓΕΛΟΣ", source="member")
    conn.execute("UPDATE businesses SET legal_form='', status='ΔΙΑΚΟΠΗ', activity_state='ceased' WHERE afm='079629198'")
    conn.execute("INSERT OR REPLACE INTO company_members(company_afm, member_afm, name, role, updated_at) VALUES (?,?,?,?,?)",
                 ("802499326", "079629198", "ΜΑΛΛΗΣ ΕΛΕΥΘΕΡΙΟΣ ΑΓΓ", "ΔΙΑΧΕΙΡΙΣΤΗΣ · ΝΟΜΙΜΟΣ ΕΚΠΡΟΣΩΠΟΣ", now))

    b_mallis = dict(conn.execute("SELECT * FROM businesses WHERE afm='079629198'").fetchone())
    assert obligations.is_legal_entity(b_mallis.get("legal_form") or "") is False
    assert obligations.is_private_person(b_mallis, conn) is True

    # Even if activity_state is not ceased yet, if they are a company member without active business traits
    b_mallis_pending = dict(b_mallis)
    b_mallis_pending["activity_state"] = ""
    b_mallis_pending["status"] = ""
    assert obligations.is_private_person(b_mallis_pending, conn) is True

def test_debt_voucher_pdf_generation():
    pdf_bytes = easycheck._debt_voucher_pdf(
        debt_id="802499326 910102249 629062401277",
        total_due="3.517,92 €",
        period="01/09/2026-30/09/2026",
        doy="ΚΕ.Β.ΕΙΣ. ΑΤΤΙΚΗΣ",
        tax_kind="ΧΡΕΩΣΤΙΚΕΣ ΔΗΛΩΣΕΙΣ ΦΠΑ ΜΕΣΩ INTERNET",
        installment="1.758,96 €",
        pay_by="30/10/2026",
        afm="802499326",
        name="CHARGELINE ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε",
        issued_at="21/10/2026"
    )
    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 1000

def test_parallel_pdf_download_month(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(pdfs.config, "data_dir", lambda: tmp_path)
    afm = "802499326"
    ym = "2026-09"
    now = db.utcnow()

    # Add 5 dummy docs specifying columns
    for i in range(1, 6):
        conn.execute("INSERT INTO mydata_docs (afm, mark, issue_date, direction, issuer_name, issuer_vat, "
                     "counter_name, counter_vat, series, aa, total, url, pdf_status, pdf_path, render_tried, fetched_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (afm, f"MARK{i}", f"2026-09-0{i}", "incoming", "SUPPLIER", "123456789", "CHARGELINE", afm,
                      "A", str(i), 100.0 * i, f"https://example.com/inv{i}", "", "", 0, now))

    def fake_fetch(u):
        return b"%PDF-1.4 dummy invoice"

    counts = pdfs.download_month(conn, afm, ym, fetch=fake_fetch)
    assert counts["ok"] == 5

    # Verify all 5 files were created on disk and updated in DB
    rows = conn.execute("SELECT mark, pdf_status, pdf_path FROM mydata_docs WHERE afm=?", (afm,)).fetchall()
    for r in rows:
        assert r[1] == "ok"
        assert Path(r[2]).exists()
