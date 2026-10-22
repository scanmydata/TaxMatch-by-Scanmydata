"""Οφειλές ΑΑΔΕ με ταυτότητες/PDF (port του Tax Center aade-debts.js), PDF ταυτότητας χρεωστικής δήλωσης, στοιχεία
επικοινωνίας από το Μητρώο, ψηφιακό τέλος ενοικίων, email προς πελάτες (χειροκίνητα + αυτόματα) — ρητά αιτήματα χρήστη
2026-10-22. Χωρίς δίκτυο: ψεύτικες σελίδες της δομής του Tax Center και ψεύτικο SMTP."""
import json
from datetime import date, datetime, timedelta, timezone

from taxmatch import db, debts, easycheck, mailer, settings_store
from taxmatch.business_profiles import credentials, lookup_aade, service
from taxmatch.mydata import analysis, parse

AFM = "094259216"

_DEBTS = (
    '<table><tr><th>Είδος φόρου</th><th>Ημ/νία Βεβαίωσης</th><th>Συνολικό Ποσό</th><th>Μη ληξιπρόθεσμο Υπόλοιπο</th>'
    '<th>Ληξιπρόθεσμο Υπόλοιπο</th><th>Ενέργειες</th></tr>'
    '<tr><td>ΕΝΦΙΑ 2026</td><td>01/03/2026</td><td>500,00</td><td>300,00</td><td>200,00</td><td>'
    '<input type="radio" id="showInstallmentInfoRadio_0"/><button id="showTObut_0" onclick=\'doDisplayPaymentCode('
    'document.displayPaymentCodeForm,"displayDebtCode.htm","displayDebtInfoAndPay.htm","1101","1","2","3","2026","4",'
    '"555","1","0")\'>Ταυτότητα</button></td></tr></table>'
    '<table id="installmentInfo_0"><tr><th>Α/Α</th><th>Λήξη</th><th>Ποσό</th></tr><tr><td>1</td><td>31/07/2026</td>'
    '<td>100,00</td></tr></table>'
    '<p>Μη Ληξιπρόθεσμο Υπόλοιπο Οφειλών: 300,00</p><p>Ληξιπρόθεσμο Υπόλοιπο Οφειλών: 200,00</p>')
_ARR = (
    '<table><tr><th>Περιγραφή</th><th>Έτος</th><th>Υπόλοιπο</th><th>Ληξιπρόθεσμο Ποσό</th><th>Ενέργειες</th></tr>'
    '<tr><td>Ρύθμιση 24 δόσεων</td><td>2025</td><td>1.200,00</td><td>0,00</td><td><button id="showTObut_3" '
    'onclick="doDisplayPaymentCode(document.displayPaymentCodeForm,\'displayArrangementInfoAndPay.htm\',\'1101\',\'1\','
    '\'2025\',\'777\')">ΤΡΟ</button></td></tr></table>')
_ARR_CODE = (
    "<script>function doViewArrPdf(frm, arnDoy, arnDept, arnYear, arrAA) {\n  frm.elements['arnDoy'].value = arnDoy;\n"
    "  frm.elements['arrAA'].value = arrAA;\n  frm.action = 'arrDebtInfoPdf.htm';\n  frm.submit();\n}</script>"
    '<form name="viewPdfNo" method="post"><input type="hidden" name="withoutAmounts" value="true"/>'
    '<input type="hidden" name="arnDoy"/><input type="hidden" name="arrAA"/></form>'
    '<form name="viewPdf" method="post"><input type="hidden" name="withoutAmounts" value="false"/>'
    '<input type="hidden" name="arnDoy"/><input type="hidden" name="arrAA"/></form>'
    "<button onclick=\"doViewArrPdf(document.viewPdfNo,'1101','1','2025','777')\">Χωρίς ποσά</button>"
    "<button onclick=\"doViewArrPdf(document.viewPdf,'1101','1','2025','777')\">Εκτύπωση</button>"
    '<table><tr><td>Ταυτότητα Ρύθμισης</td><td>094259216 000000777 202500000001</td></tr></table>')


def _blank_pdf() -> bytes:
    import io
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(595, 842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class _FakeAade:
    def __init__(self):
        self.posts = []

    def follow(self, method, url, form=None, headers=None):
        if method == "POST":
            self.posts.append((url, dict(form or {})))
            return {"url": url, "text": "", "is_pdf": True, "content": _blank_pdf()}
        if url.endswith(debts.DEBTS_PAGE):
            return {"url": url, "text": _DEBTS}
        if url.endswith(debts.ARR_PAGE):
            return {"url": url, "text": _ARR}
        if "displayDebtCode.htm" in url:
            assert "mchMcNo=555" in url and "mcLineNo=1" in url
            return {"url": url, "text": "<table><tr><td>Ταυτότητα Οφειλής</td><td>RF12 3456 7890</td></tr></table>"}
        if "displayArrangementCode.htm" in url:
            assert "arrAA=777" in url
            return {"url": url, "text": _ARR_CODE}
        raise AssertionError(url)


def test_debts_fetch_codes_pdfs_installments_and_totals(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    credentials.set_(conn, AFM, "u", "p")
    fake = _FakeAade()
    res = debts.check_client(conn, AFM, want_pdf=True, login_fn=lambda u, p: {"ok": True, "http": fake})
    assert res == {"ok": True, "reason": "", "n": 2, "overdue": 200.0, "non_overdue": 300.0, "errors": []}
    rows = {r["kind"]: r for r in debts.client_debts(conn, AFM)}
    d, a = rows["debt"], rows["arrangement"]
    assert (d["title"], d["overdue"], d["non_overdue"], d["total"], d["code"]) == ("ΕΝΦΙΑ 2026", 200.0, 300.0, 500.0,
                                                                                 "RF12 3456 7890")
    assert d["details"]["installments"]["rows"] == [["1", "31/07/2026", "100,00"]]
    from pypdf import PdfReader
    assert len(PdfReader(d["pdf_path"]).pages) == 2                    # ταυτότητα ΑΑΔΕ + δοσολόγιο του γραφείου
    assert (d["next_due"], d["next_amount"], d["unpaid"], d["instalments"]) == ("31/07/2026", 100.0, 1, 1)
    debt_post = next(f for u, f in fake.posts if u.endswith("debtInfoPdf.htm"))
    assert debt_post["mchMcNo"] == "555" and debt_post["withoutAmounts"] == "false"
    assert a["code"] == "094259216 000000777 202500000001" and a["total"] == 1200.0 and a["pdf_path"]
    arr_post = next(f for u, f in fake.posts if u.endswith("arrDebtInfoPdf.htm"))
    assert arr_post == {"withoutAmounts": "false", "arnDoy": "1101", "arrAA": "777"}   # η φόρμα ΜΕ τα ποσά
    # εξοφλήθηκε: νέα λήψη χωρίς οφειλές τη σβήνει
    empty = _FakeAade()
    empty.follow = lambda m, u, form=None, headers=None: {"url": u, "text": "<table></table>"}
    debts.check_client(conn, AFM, login_fn=lambda u, p: {"ok": True, "http": empty})
    assert debts.client_debts(conn, AFM) == []


def test_debit_declaration_gets_more_details_and_pdf(conn, tmp_path):
    """Φ2 χρεωστική: δόση/προθεσμία/ΔΟΥ + PDF της ταυτότητας από το κουμπί «Εκτύπωση» της σελίδας displayDebtCode."""
    page = ("<script>function doViewPdf(frm, decId) {\n  frm.elements['declarationDatabaseId'].value = decId;\n"
            "  frm.action = 'debtCodePdf.htm';\n  frm.submit();\n}</script>"
            '<form name="pdfForm" method="post"><input type="hidden" name="declarationDatabaseId"/></form>'
            "<button onclick=\"doViewPdf(document.pdfForm,'987654')\">Εκτύπωση</button>"
            "<table><tr><td>Ταυτότητα Οφειλής</td><td>1234567890123</td></tr>"
            "<tr><td>Συνολικό ποσό οφειλής</td><td>1.234,50 €</td></tr>"
            "<tr><td>Ποσό δόσης δήλωσης έως 31/07/2026</td><td>617,25 €</td></tr>"
            "<tr><td>ΔΟΥ</td><td>Α' ΑΘΗΝΩΝ</td></tr></table>")

    class Http:
        posted = []

        def follow(self, method, url, form=None, headers=None):
            if method == "POST":
                self.posted.append((url, form))
                return {"url": url, "text": "", "is_pdf": True, "content": b"%PDF-1.4 to"}
            return {"url": url, "text": page if "displayDebtCode" in url else "<table></table>"}
    ctx = easycheck._Ctx(Http(), AFM)  # noqa: SLF001
    out = easycheck.declaration_amounts(ctx, easycheck.OBLIGATIONS["Φ2"], "987654", "x")
    assert out["total_due"] == 1234.5 and out["installment"] == 617.25 and out["pay_by"] == "31/07/2026"
    assert out["doy"] == "Α' ΑΘΗΝΩΝ" and out["pdf"].endswith(".pdf") and out["pdf_checked"]
    assert Http.posted[0][1] == {"declarationDatabaseId": "987654"}
    service.add(conn, AFM, "ΑΛΦΑ")
    easycheck.store_results(conn, AFM, 2026, [{"obligation": "Φ2", "status": "submitted", "periods": [
        {"period": "3ος Μήνας", "status": "submitted", "period_end": "2026-03-31", "amounts": {**out, "result": "Χρεωστική"}}]}])
    (decl,) = debts.declaration_debts(conn, AFM)
    assert decl["code"] == "1234567890123" and decl["pdf_path"] == out["pdf"] and decl["total"] == 1234.5


def test_contact_from_ldap_info():
    xml = ("<r><mail>OLD@x.gr</mail><mail2>Info@Alfa.gr</mail2><mobilenumber>+30 6912345678</mobilenumber>"
           "<phonenumber>2101234567</phonenumber></r>")
    assert lookup_aade.parse_contact(xml) == {"email": "info@alfa.gr", "mobile": "6912345678", "phone": "2101234567",
                                              "origin": "mail2"}
    assert lookup_aade.parse_contact("<r><mail>όχι-email</mail><mobilenumber>2101234567</mobilenumber></r>") == {
        "email": "", "mobile": "", "phone": "", "origin": ""}


def test_rent_documents_do_not_create_digital_fee_obligation(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    for mark, typ, rent, stamp in (("1", "8.1", 1, 36.0), ("2", "2.1", 0, 12.0)):
        conn.execute("INSERT INTO mydata_docs(afm, mark, direction, invoice_type, issue_date, net, total, stamp, rent, "
                     "fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (AFM, mark, "outgoing", typ, "2026-09-10", 1000, 1000,
                                                                    stamp, rent, "x"))
    fees = analysis.special_fees(conn, AFM, "2026-09")
    assert fees["digital_fee"] == 12.0 and fees["rent_fee"] == 36.0
    import xml.etree.ElementTree as ET
    inv = ET.fromstring(f'<invoice xmlns="{parse.NS[1:-1]}"><invoiceDetails><stampDutyPercentCategory>3'
                        '</stampDutyPercentCategory></invoiceDetails></invoice>')
    assert parse.is_rent(inv, "1.1") and parse.is_rent(ET.fromstring("<invoice/>"), "8.1")
    assert not parse.is_rent(ET.fromstring("<invoice/>"), "2.1")


# ---------------------------------------------------------------- email
def _smtp(conn):
    for k, v in (("smtp_host", "smtp.example.test"), ("smtp_from", "office@example.test"), ("office_name", "ΓΡΑΦΕΙΟ")):
        settings_store.set_value(conn, k, v)


def _with_debt(conn, tmp_path):
    service.add(conn, AFM, "ΑΛΦΑ")
    conn.execute("UPDATE businesses SET email='client@example.test' WHERE afm=?", (AFM,))
    pdf = tmp_path / "to.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    debts.store(conn, AFM, {"overdue": 200.0, "non_overdue": 0.0, "debts": [
        {"kind": "debt", "ref": "r1", "title": "ΕΝΦΙΑ 2026", "overdue": 200.0, "non_overdue": 0.0, "total": 200.0,
         "code": "RF12", "pdf_path": str(pdf), "details": {}}]})


def test_compose_and_send_debts_email_logs_and_attaches(conn, tmp_path):
    _smtp(conn)
    _with_debt(conn, tmp_path)
    msg = mailer.compose_debts(conn, AFM)
    assert msg.to == "client@example.test" and "RF12" in msg.text and "Ληξιπρόθεσμο: 200,00 €" in msg.text
    assert [p.name for p in msg.attachments] == ["to.pdf"] and "συνθηματικ" not in msg.text.lower()
    sent = []
    ok, text = mailer.send(conn, AFM, "debts", msg, transport=lambda c, m: sent.append(m))
    assert ok and sent[0]["To"] == "client@example.test" and sent[0]["From"].endswith("<office@example.test>")
    assert any(part.get_filename() == "to.pdf" for part in sent[0].iter_attachments())
    (log,) = mailer.history(conn, AFM)
    assert log["status"] == "sent" and log["kind"] == "debts" and not log["auto"]

    def boom(c, m):
        raise ConnectionRefusedError("refused")
    ok, text = mailer.send(conn, AFM, "debts", msg, transport=boom)
    assert not ok and "SMTP" in text and mailer.history(conn, AFM)[0]["status"] == "failed"


def test_scheduled_emails_only_when_debts_change_and_only_unsent_news(conn, tmp_path):
    _smtp(conn)
    _with_debt(conn, tmp_path)
    settings_store.set_value(conn, "auto_email_debts", "1")
    settings_store.set_value(conn, "auto_email_news", "1")
    settings_store.set_value(conn, "auto_email_news_freq", "daily")
    published = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO articles(source,title,url,url_hash,published_at,fetched_at,extraction_status,extracted_json) "
                 "VALUES ('t','Νέα ρύθμιση ΚΑΔ','https://x.gr/a','h1',?,?, 'done', ?)",
                 (published, db.utcnow(), json.dumps({"summary": "Αλλαγή για τον ΚΑΔ σας"})))
    art = conn.execute("SELECT id FROM articles WHERE url_hash='h1'").fetchone()["id"]
    conn.execute("INSERT INTO matches(article_id, afm, matched_reason, confidence, created_at) VALUES (?,?,?,?,?)",
                 (art, AFM, "ΚΑΔ 62.01", 1.0, db.utcnow()))
    sent = []
    t = lambda c, m: sent.append(m)  # noqa: E731
    stats = mailer.run_scheduled(conn, transport=t, fetch_debts=lambda c, a: None)
    assert stats["debts"] == 1 and stats["news"] == 1 and "Νέα ρύθμιση ΚΑΔ" in sent[1].get_body(("plain",)).get_content()
    stats = mailer.run_scheduled(conn, transport=t, fetch_debts=lambda c, a: None)
    assert stats["debts"] == 0 and stats["news"] == 0 and len(sent) == 2         # ίδιες οφειλές, νέα ήδη σταλμένα
    conn.execute("UPDATE aade_debts SET overdue=250, total=250 WHERE afm=?", (AFM,))
    assert mailer.run_scheduled(conn, transport=t, fetch_debts=lambda c, a: None)["debts"] == 1
    conn.execute("UPDATE businesses SET email_auto=0 WHERE afm=?", (AFM,))       # εξαιρέθηκε από τα αυτόματα
    conn.execute("UPDATE aade_debts SET overdue=300, total=300 WHERE afm=?", (AFM,))
    assert mailer.run_scheduled(conn, transport=t, fetch_debts=lambda c, a: None)["debts"] == 0


def test_weekly_news_only_on_chosen_day(conn):
    settings_store.set_value(conn, "auto_email_news_freq", "weekly")
    settings_store.set_value(conn, "auto_email_weekday", "1")
    assert mailer.news_due(conn, date(2026, 10, 19)) and not mailer.news_due(conn, date(2026, 10, 20))


def test_declarations_hidden_once_aade_debts_are_known(conn):
    """Ζωντανά (2026-10-22): οι χρεωστικές δηλώσεις της χρονιάς έβγαιναν ΚΑΙ πληρωμένες. Με λήψη οφειλών ΑΑΔΕ, αυτή είναι η
    εικόνα (έχει ήδη τις απλήρωτες Φ2)· χωρίς λήψη, μόνο οι πρόσφατες ως ένδειξη."""
    from datetime import date as _d
    service.add(conn, AFM, "ΑΛΦΑ")
    recent, old = _d.today().replace(day=1).isoformat(), "2025-01-31"
    easycheck.store_results(conn, AFM, 2026, [{"obligation": "Φ2", "status": "submitted", "periods": [
        {"period": "νέα", "status": "submitted", "period_end": recent, "amounts": {"result": "Χρεωστική", "total_due": 10.0}},
        {"period": "παλιά", "status": "submitted", "period_end": old, "amounts": {"result": "Χρεωστική", "total_due": 5.0}}]}])
    assert [d["ref"] for d in debts.client_debts(conn, AFM)] == ["Φ2|νέα"]
    debts.store(conn, AFM, {"overdue": 0.0, "non_overdue": 0.0, "debts": []})
    assert debts.client_debts(conn, AFM) == []
