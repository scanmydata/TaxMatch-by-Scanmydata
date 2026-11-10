import os
import sqlite3
import pytest
from pathlib import Path
from taxmatch import db, obligations, easycheck
from taxmatch.business_profiles import service
from taxmatch.mydata import pdfs

def test_chargeline_is_legal_entity_and_not_private():
    b_chargeline = {
        "afm": "800000002",
        "name": "CHRONOTEST ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε",
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
    service.add(conn, "800000002", "CHRONOTEST ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε")
    conn.execute("UPDATE businesses SET legal_form='ΜΟΝΟΠΡΟΣ ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ ΕΤΑΙΡΕΙΑ', status='ΕΝΕΡΓΗ', activity_state='active' WHERE afm='800000002'")

    service.add(conn, "070000005", "ΜΑΡΚΟΥ ΝΙΚΟΛΑΟΣ ΑΓΓΕΛΟΣ", source="member")
    conn.execute("UPDATE businesses SET legal_form='', status='ΔΙΑΚΟΠΗ', activity_state='ceased' WHERE afm='070000005'")
    conn.execute("INSERT OR REPLACE INTO company_members(company_afm, member_afm, name, role, updated_at) VALUES (?,?,?,?,?)",
                 ("800000002", "070000005", "ΜΑΡΚΟΥ ΝΙΚΟΛΑΟΣ ΑΓΓ", "ΔΙΑΧΕΙΡΙΣΤΗΣ · ΝΟΜΙΜΟΣ ΕΚΠΡΟΣΩΠΟΣ", now))

    b_markou = dict(conn.execute("SELECT * FROM businesses WHERE afm='070000005'").fetchone())
    assert obligations.is_legal_entity(b_markou.get("legal_form") or "") is False
    assert obligations.is_private_person(b_markou, conn) is True

    # Even if activity_state is not ceased yet, if they are a company member without active business traits
    b_markou_pending = dict(b_markou)
    b_markou_pending["activity_state"] = ""
    b_markou_pending["status"] = ""
    assert obligations.is_private_person(b_markou_pending, conn) is True

def test_no_synthetic_debt_voucher_any_more():
    """Η ταυτότητα οφειλής είναι ΠΑΝΤΑ το πρωτότυπο της ΑΑΔΕ (ρητό αίτημα 2026-11-10) — όχι δικό μας έντυπο."""
    assert not hasattr(easycheck, "_debt_voucher_pdf")


def test_parallel_pdf_download_month(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(pdfs.config, "data_dir", lambda: tmp_path)
    afm = "800000002"
    ym = "2026-09"
    now = db.utcnow()

    # Add 5 dummy docs specifying columns
    for i in range(1, 6):
        conn.execute("INSERT INTO mydata_docs (afm, mark, issue_date, direction, issuer_name, issuer_vat, "
                     "counter_name, counter_vat, series, aa, total, url, pdf_status, pdf_path, render_tried, fetched_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (afm, f"MARK{i}", f"2026-09-0{i}", "incoming", "SUPPLIER", "123456789", "CHRONOTEST", afm,
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


def test_display_declarations_list_with_doDisplayDebtCode_downloads_both_pdfs(conn, tmp_path, monkeypatch):
    from taxmatch import config
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    afm = "800000002"
    service.add(conn, afm, "CHRONOTEST ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε")

    user_html_snippet = '''
    <table class="displayTable">
    <tr class="tblRow1">
        <td>TAXISnet</td>
        <td>01/08/2026 - 31/08/2026</td>
        <td>274364936</td>
        <td>15/09/2026</td>
        <td>Αρχική</td>
        <td>Οριστική</td>
        <td>Χ</td>
        <td>
            <div class="navbtn" onmouseover="this.className='navbtnOver'" onmouseout="this.className='navbtn'" onclick="doDisplayDebtCode(document.displayDebtCodeForm,&quot;vatF2&quot;,&quot;274364936&quot;,&quot;displayDeclarationsList.htm?declarationType=vatF2&amp;year=2026&amp;periodType=oneMonth&amp;periodStart=01%2F08%2F2026&amp;periodEnd=31%2F08%2F2026&amp;effectivePeriodStart=01%2F08%2F2026&amp;effectivePeriodEnd=31%2F08%2F2026&quot;);">
                Ταυτότητα Οφειλής
            </div>
        </td>
    </tr>
    </table>
    '''

    debt_code_html = '''
    <html>
    <body>
    <table>
        <tr><td>Ταυτότητα Οφειλής</td><td>800000002 910102202 629053900890</td></tr>
        <tr><td>Συνολικό ποσό οφειλής</td><td>3.543,17 €</td></tr>
        <tr><td>Ποσό δόσης δήλωσης της 30/09/2026</td><td>1.771,59 €</td></tr>
        <tr><td>ΔΟΥ</td><td>ΚΕ.ΦΟ.ΔΕ. ΑΤΤΙΚΗΣ</td></tr>
        <tr><td>Είδος Φόρου</td><td>ΧΡΕΩΣΤΙΚΕΣ ΔΗΛΩΣΕΙΣ ΦΠΑ ΜΕΣΩ INTERNET</td></tr>
        <tr><td>Ημερολογιακή Περίοδος</td><td>01/08/2026-31/08/2026</td></tr>
        <tr><td>Ημ/νία Έκδοσης</td><td>15/09/2026</td></tr>
    </table>
    </body>
    </html>
    '''

    class FakeHttp:
        def follow(self, method, url, data=None):
            if "viewPdf.htm" in url:
                return {"is_pdf": True, "content": b"%PDF-1.4 Fake Form Submission"}
            if "debtCodePdf.htm" in url or "debtInfoPdf.htm" in url:
                return {"is_pdf": True, "content": b"%PDF-1.4 Official AADE Debt Code"}
            if "displayDebtCode.htm" in url:
                return {"ok": True, "text": debt_code_html, "content": debt_code_html.encode("utf-8")}
            return {"ok": True, "text": user_html_snippet, "content": user_html_snippet.encode("utf-8")}

    class FakeCtx(easycheck._Ctx):
        printed: list = []

        def __init__(self):
            super().__init__(FakeHttp(), afm, True)
            self.conn = conn

        def print_page(self, url, must_contain=""):          # ο αόρατος browser τυπώνει τη σελίδα της ΑΑΔΕ
            self.printed.append(url)
            return b"%PDF-1.4 AADE printed page"

        def get(self, url):
            if "displayDebtCode.htm" in url:
                return debt_code_html
            return user_html_snippet

    ctx = FakeCtx()
    o = easycheck.OBLIGATIONS["Φ2"]
    args = {"year": "2026", "periodType": "oneMonth", "periodStart": "01/08/2026", "periodEnd": "31/08/2026"}

    submitted, amounts = easycheck._period_details(ctx, o, args)

    assert submitted is None or submitted is True
    assert amounts.get("result") == "Χρεωστική"
    assert amounts.get("debt_id") == "800000002 910102202 629053900890"

    # Ελέγχουμε ότι ΚΑΙ το έντυπο ΚΑΙ η ταυτότητα οφειλής κατέβηκαν ως PDF
    sub_pdf = amounts.get("submission_pdf")
    debt_pdf = amounts.get("pdf")
    assert sub_pdf is not None and Path(sub_pdf).exists()
    assert debt_pdf is not None and Path(debt_pdf).exists()
    assert "submission_pdfs" in amounts
    assert len(amounts["submission_pdfs"]) == 2
    assert amounts.get("pdf_src") == "aade" and "displayDebtCode.htm" in ctx.printed[-1]


def test_html_only_displayDebtCode_generates_and_saves_both_pdfs(conn, tmp_path, monkeypatch):
    from taxmatch import config
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    afm = "800000002"
    service.add(conn, afm, "CHRONOTEST ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε")

    user_html_snippet = '''
    <table class="displayTable">
    <tr class="tblRow1">
        <td>TAXISnet</td>
        <td>01/08/2026 - 31/08/2026</td>
        <td>274364936</td>
        <td>15/09/2026</td>
        <td>Αρχική</td>
        <td>Οριστική</td>
        <td>Χ</td>
        <td>
            <div class="navbtn" onclick="doDisplayDebtCode(document.displayDebtCodeForm,&quot;vatF2&quot;,&quot;274364936&quot;,&quot;displayDeclarationsList.htm?declarationType=vatF2&quot;);">
                Ταυτότητα Οφειλής
            </div>
        </td>
    </tr>
    </table>
    '''

    debt_code_html = '''
    <html>
    <body>
    <table>
        <tr><td>Ταυτότητα Οφειλής</td><td>800000002 910102202 629053900890</td></tr>
        <tr><td>Συνολικό ποσό οφειλής</td><td>3.543,17 €</td></tr>
        <tr><td>Ποσό δόσης δήλωσης της 30/09/2026</td><td>1.771,59 €</td></tr>
        <tr><td>ΔΟΥ</td><td>ΚΕ.ΦΟ.ΔΕ. ΑΤΤΙΚΗΣ</td></tr>
        <tr><td>Είδος Φόρου</td><td>ΧΡΕΩΣΤΙΚΕΣ ΔΗΛΩΣΕΙΣ ΦΠΑ ΜΕΣΩ INTERNET</td></tr>
        <tr><td>Ημερολογιακή Περίοδος</td><td>01/08/2026-31/08/2026</td></tr>
        <tr><td>Ημ/νία Έκδοσης</td><td>15/09/2026</td></tr>
    </table>
    </body>
    </html>
    '''

    class FakeHttpHtmlOnly:
        def follow(self, method, url, data=None):
            if "viewPdf.htm" in url:
                return {"is_pdf": True, "content": b"%PDF-1.4 Fake Form Submission"}
            if "debtCodePdf.htm" in url or "debtInfoPdf.htm" in url:
                return {"ok": False, "status_code": 404, "is_pdf": False, "content": b""}
            if "displayDebtCode.htm" in url:
                return {"ok": True, "text": debt_code_html, "content": debt_code_html.encode("utf-8")}
            return {"ok": True, "text": user_html_snippet, "content": user_html_snippet.encode("utf-8")}

    class FakeCtx(easycheck._Ctx):
        printed: list = []

        def __init__(self):
            super().__init__(FakeHttpHtmlOnly(), afm, True)
            self.conn = conn

        def print_page(self, url, must_contain=""):          # ο αόρατος browser τυπώνει τη σελίδα της ΑΑΔΕ
            self.printed.append(url)
            return b"%PDF-1.4 AADE printed page"

        def get(self, url):
            if "displayDebtCode.htm" in url:
                return debt_code_html
            return user_html_snippet

    ctx = FakeCtx()
    o = easycheck.OBLIGATIONS["Φ2"]
    args = {"year": "2026", "periodType": "oneMonth", "periodStart": "01/08/2026", "periodEnd": "31/08/2026"}

    submitted, amounts = easycheck._period_details(ctx, o, args)

    assert submitted is None or submitted is True
    assert amounts.get("result") == "Χρεωστική"
    assert amounts.get("debt_id") == "800000002 910102202 629053900890"

    sub_pdf = amounts.get("submission_pdf")
    debt_pdf = amounts.get("pdf")
    assert sub_pdf is not None and Path(sub_pdf).exists()
    assert debt_pdf is not None and Path(debt_pdf).exists()
    # Και τα δύο είναι αποθηκευμένα στον φάκελο submissions/<afm>/<year>/
    assert "submissions" in str(debt_pdf)
    assert Path(debt_pdf).name.startswith("TO_")
    assert "submission_pdfs" in amounts
    assert len(amounts["submission_pdfs"]) == 2
    assert amounts.get("pdf_src") == "aade" and "displayDebtCode.htm" in ctx.printed[-1]

