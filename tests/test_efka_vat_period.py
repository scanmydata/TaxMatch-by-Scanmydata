"""ΑΠΔ από την οικονομική καρτέλα εργοδότη e-ΕΦΚΑ, ΑΜΕ/κωδικοί ΙΚΑ εργοδότη από Excel, περίοδος ΦΠΑ για μη
εγκατεστημένους / Β΄ κατηγορίας σε μηνιαίο (Α.1049/2025), προεπισκόπηση εισαγωγής, έσοδα/έξοδα myDATA (ρητά αιτήματα
χρήστη 2026-10-22). Όλα χωρίς δίκτυο — ψεύτικο HTTP με τη δομή των μεθοδολογιών του χρήστη."""
from datetime import date

from taxmatch import deadlines, efka, notices, obligations
from taxmatch.business_profiles import credentials, import_excel, service
from taxmatch.business_profiles.vat_profile import interpret_vat_profile
from taxmatch.mydata import analysis

AFM = "094259216"


# ---------------------------------------------------------------- ΦΠΑ
def test_non_established_without_books_is_quarterly():
    """Κυπριακή LTD πελάτη (πραγματικό Μητρώο, 2026-10-22): «ΜΗ ΕΓΚΑΤΕΣΤΗΜΕΝΟΙ ΤΗΣ ΕΕ», «ΕΞΟΜΟΙΟΥΜΕΝΟ ΜΕ ΚΑΝΟΝΙΚΟ ΚΑΘΕΣΤΩΣ», NAI
    λατινικά — πριν η περίοδος έμενε κενή. Άρθρο 38 ΚΦΠΑ: όσοι δεν τηρούν βιβλία = τρίμηνο."""
    v = interpret_vat_profile({"ypagwghfpa": "NAI", "kathestwsfpa": "ΕΞΟΜΟΙΟΥΜΕΝΟ ΜΕ ΚΑΝΟΝΙΚΟ ΚΑΘΕΣΤΩΣ",
                               "kathgoriabibliwn": "ΜΗ ΕΓΚΑΤΕΣΤΗΜΕΝΟΙ ΤΗΣ ΕΕ"})
    assert v == {"vat_subject": True, "books_category": "", "books_category_raw": "ΜΗ ΕΓΚΑΤΕΣΤΗΜΕΝΟΙ ΤΗΣ ΕΕ",
                 "vat_period_type": "quarterly"}
    # Ήδη αποθηκευμένος πελάτης (χωρίς νέα ανάκτηση): από το books_category_raw
    assert obligations.vat_period({"vat_subject": 1, "books_category": "",
                                   "books_category_raw": "ΜΗ ΕΓΚΑΤΕΣΤΗΜΕΝΟΙ ΤΗΣ ΕΕ"}) == "quarterly"


def test_b_category_started_after_2024_is_monthly_by_law():
    assert interpret_vat_profile({"ypagwghfpa": "ΝΑΙ", "kathgoriabibliwn": "Β", "hmenarxhs": "15/03/2025"})[
        "vat_period_type"] == "monthly"
    assert interpret_vat_profile({"ypagwghfpa": "ΝΑΙ", "kathgoriabibliwn": "Β", "hmenarxhs": "15/03/2019"})[
        "vat_period_type"] == "quarterly"
    assert obligations.vat_period({"vat_subject": 1, "books_category": "Β", "start_date": "02/01/2024"}) == "monthly"


def test_quarter_option_eligibility_and_reminder():
    young = {"vat_subject": 1, "books_category": "Β", "start_date": "15/03/2025", "activity_state": "active"}
    opt = obligations.quarter_option(young, date(2026, 10, 22))
    assert opt["eligible_from"] == date(2027, 4, 1) and not opt["eligible_now"]
    assert "04/2027" in obligations.vat_option_note(young, date(2026, 10, 22))
    old = {**young, "start_date": "15/03/2024"}
    opt = obligations.quarter_option(old, date(2026, 10, 22))
    assert opt["eligible_now"] and opt["deadline"] == date(2026, 10, 29)        # προτελευταία εργάσιμη Οκτωβρίου
    assert "ΕΠΑΝΕΛΘΕΙ" in obligations.vat_option_note(old, date(2026, 10, 22))
    # Υπενθύμιση στο Ημερολόγιο ΜΟΝΟ από τον μήνα που επιτρέπεται η επιλογή
    when = [o.date for o in obligations.occurrences(date(2026, 1, 1), date(2027, 6, 30), young)
            if o.rule_id == "vat_quarter_option"]
    assert when == [date(2027, 4, 28)]
    assert obligations.quarter_option({**young, "vat_period_type": "quarterly"}) is None   # ήδη στο τρίμηνο
    assert obligations.quarter_option({**young, "books_category": "Γ"}) is None


def test_notice_lists_b_clients_that_can_return_to_quarterly(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    conn.execute("UPDATE businesses SET books_category='Β', vat_subject=1, activity_state='active', start_date='10/01/2024' "
                 "WHERE afm=?", (AFM,))
    texts = [n["text"] for n in notices.collect(conn)]
    assert any("τριμηνιαίο" in t and "ΑΛΦΑ" in t for t in texts)


# ---------------------------------------------------------------- e-ΕΦΚΑ: ΑΠΔ
def test_period_month_is_tolerant():
    assert efka.period_month("09/2026") == "2026-09"
    assert efka.period_month("01/09/2026 - 30/09/2026") == "2026-09"
    assert efka.period_month("2026-09") == "2026-09"
    assert efka.period_month("Σεπτέμβριος 2026") == "2026-09"
    assert efka.period_month("202609") == "2026-09"
    assert efka.period_month("—") == ""


_ROW = ('<tr data-ri="{i}" class="ui-widget-content"><td>{d}</td><td>{k}</td><td>{doc}</td><td>{p}</td><td>{deb}</td>'
        '<td>0,00</td><td>0,00</td><td><button>Επιλογή</button></td></tr>')


def _lines(rows):
    return "".join(_ROW.format(i=i, **r) for i, r in enumerate(rows))


_TX_PAGE = ('<form id="transactionsForm" name="transactionsForm" action="/ssp.efka.emp-transactions-ext/secure/'
            'transactions.xhtml"><input type="hidden" name="transactionsForm" value="transactionsForm"/>'
            '<input name="dateFrom_input" value=""/><input name="dateTo_input" value=""/>'
            '<input type="hidden" name="jakarta.faces.ViewState" value="VS1"/>'
            '<button id="j_idt135" name="j_idt135" type="submit"><span>Εμφάνιση</span></button>'
            '<button id="j_idt137" name="j_idt137" title="Εκτύπωση Κινήσεων"></button></form>')


class _FakeEfka:
    """Ψεύτικος e-ΕΦΚΑ: σελίδα εισόδου → GSIS → ρόλος → landing → καρτέλα (2 σελίδες κινήσεων)."""

    def __init__(self, ame_ok=True):
        self.ame_ok = ame_ok
        self.forms = []

    def follow(self, method, url, form=None, headers=None):
        self.forms.append((url, dict(form or {})))
        if url == efka.HOME:
            return {"url": url, "text": '<a id="social-taxisnet-employer" href="/broker/taxisnet-employer">Είσοδος Εργοδότη</a>'}
        if "broker/taxisnet-employer" in url:
            return {"url": "https://oauth2.gsis.gr/oauth2server/login.jsp", "text": "login"}
        if url == efka.GSIS_CHECK:
            return {"url": "https://kc/realms/EFKA/login-actions", "text":
                    '<form id="kc-form-select-role" action="/realms/EFKA/login-actions/role?x=1"></form>'}
        if "login-actions/role" in url:
            ok = self.ame_ok and form.get("ame") == "0020012345" and form.get("role") == "external-employer"
            return {"url": efka.SVC + "home", "text": '<div id="viewsPanel">Καλώς ήρθατε</div>' if ok else "σφάλμα"}
        if url == efka.TX and method == "GET":
            return {"url": url, "text": _TX_PAGE}
        if form and form.get("jakarta.faces.source") == "j_idt135":
            assert form["dateFrom_input"] == "01/01/2026"
            body = _lines([{"d": "28/02/2026", "k": "100-ΑΠΔ", "doc": "A1", "p": "01/2026", "deb": "1.200,50"},
                           {"d": "05/03/2026", "k": "301-ΓΕΤΕ", "doc": "G1", "p": "02/2026", "deb": "10,00"}])
            return {"url": url, "text": f'<partial-response><changes><update id="transactionsForm"><![CDATA[{body}'
                                        '<span>(Σελίδα 1 από 2)</span>]]></update><update id="j_id1:jakarta.faces.ViewState:0">'
                                        '<![CDATA[VS2]]></update></changes></partial-response>'}
        if form and form.get("jakarta.faces.source") == "linesTable":
            assert form["jakarta.faces.ViewState"] == "VS2" and form["linesTable_first"] == "2"
            body = _lines([{"d": "31/03/2026", "k": "100-ΑΠΔ", "doc": "A2", "p": "02/2026", "deb": "980,00"}])
            return {"url": url, "text": f'<partial-response><changes><update id="linesTable"><![CDATA[{body}]]>'
                                        '</update></changes></partial-response>'}
        raise AssertionError(f"απρόσμενο αίτημα {method} {url}")


def test_check_apd_reads_employer_card_and_stores_periods(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    credentials.set_(conn, AFM, "user", "pass")
    efka.set_(conn, AFM, ame="20012345", source="excel")                      # το Excel έχασε τα μηδενικά μπροστά
    fake = _FakeEfka()

    res = efka.check_apd(conn, AFM, 2026, today=date(2026, 5, 15),
                         login_fn=lambda u, p, a, ame: efka.employer_login(u, p, a, ame, http_factory=lambda: fake))
    assert res["ok"], res["reason"]
    periods = {p["period_end"][:7]: p for p in res["entry"]["periods"]}
    assert periods["2026-01"]["status"] == "submitted" and periods["2026-01"]["amounts"] == {"total_due": 1200.5}
    assert periods["2026-02"]["status"] == "submitted"                        # 2η σελίδα κινήσεων
    assert periods["2026-03"]["status"] == "not_submitted"                    # προθεσμία 30/4 πέρασε χωρίς ΑΠΔ
    assert "2026-04" not in periods                                           # δεν έχει λήξει ακόμη
    biz = {**dict(conn.execute("SELECT * FROM businesses WHERE afm=?", (AFM,)).fetchone()),
           **deadlines._checks_for(conn, AFM)}  # noqa: SLF001
    assert obligations.submission("apd", biz, "2026-01")["status"] == "submitted"
    assert obligations.submission("apd", biz, "2026-03")["status"] == "not_submitted"


def test_check_apd_wrong_ame_is_reported_not_crashing(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    credentials.set_(conn, AFM, "user", "pass")
    efka.set_(conn, AFM, ame="0099999999")
    res = efka.check_apd(conn, AFM, 2026, today=date(2026, 5, 15),
                         login_fn=lambda u, p, a, ame: efka.employer_login(u, p, a, ame,
                                                                          http_factory=lambda: _FakeEfka()))
    assert not res["ok"] and "ΑΜΕ" in res["reason"]


def test_should_check_apd_only_for_employers(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    assert not efka.should_check_apd(conn, AFM)                               # άγνωστο, χωρίς ΑΜΕ: όχι
    conn.execute("UPDATE businesses SET employer=1 WHERE afm=?", (AFM,))
    assert efka.should_check_apd(conn, AFM)
    conn.execute("UPDATE businesses SET employer=0 WHERE afm=?", (AFM,))
    efka.set_(conn, AFM, ame="0020012345")
    assert not efka.should_check_apd(conn, AFM)                               # ρητά χωρίς μισθοδοσία


def test_parse_ames_from_pisterg():
    page = ('<tbody id="AMEsForm:AMEsResults_data"><tr data-ri="0"><td>0020012345</td><td>ΑΛΦΑ ΙΚΕ</td><td>x</td>'
            '<td>1</td></tr></tbody>')
    assert [{k: a[k] for k in ("ame", "name")} for a in efka.parse_ames(page)] == [{"ame": "0020012345", "name": "ΑΛΦΑ ΙΚΕ"}]


def test_ika_employer_credentials_are_encrypted(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    efka.set_(conn, AFM, user="ika_u", password="ika_SECRET")
    raw = conn.execute("SELECT ika_user, ika_pass FROM efka_credentials WHERE afm=?", (AFM,)).fetchone()
    assert raw["ika_pass"].startswith("enc:1:") and "ika_SECRET" not in raw["ika_pass"]
    assert efka.ika_credentials(conn, AFM) == ("ika_u", "ika_SECRET")
    efka.set_(conn, AFM, ame="123")                                           # ΑΜΕ μόνο: οι κωδικοί μένουν
    assert efka.ika_credentials(conn, AFM) == ("ika_u", "ika_SECRET") and efka.get_ame(conn, AFM) == "0000000123"


# ---------------------------------------------------------------- εισαγωγή Excel
def test_import_reads_ika_employer_and_ame_and_previews_existing(conn):
    head = ("Α.Φ.Μ.", "Επωνυμία", "Όνομα χρήστη εργοδότη Ι.Κ.Α.", "Συνθηματικό εργοδότη Ι.Κ.Α.", "ΑΜΕ")
    res = import_excel.parse_rows([head, (AFM, "ΑΛΦΑ ΙΚΕ", "iu", "ip", "20012345"), ("123456783", "ΒΗΤΑ", None, None, None)])
    a, b = res.rows
    assert (a.ika_user, a.ika_pass, a.ame) == ("iu", "ip", "20012345") and not b.has_ika
    service.add(conn, AFM, "ΑΛΦΑ")
    preview = {p["afm"]: p for p in service.preview_import(conn, res)}
    assert preview[AFM]["action"] == "update" and "κωδικοί ΙΚΑ εργοδότη (νέοι)" in preview[AFM]["changes"]
    assert preview["123456783"]["action"] == "new"
    out = service.import_result(conn, res)
    assert out["ika"] == 1 and efka.get_ame(conn, AFM) == "0020012345"
    assert service.preview_import(conn, res)[0]["action"] == "same"            # ίδιο αρχείο ξανά: χωρίς αλλαγές


def test_block_format_reads_ika_employer_row():
    rows = [(None, "00250 ΠΑΡΑΔΕΙΓΜΑ Ο Ε 123456783"),
            (None, "Taxis Net", None, None, "taxis_u", None, "taxis_p"),
            (None, "Ι.Κ.Α. Εργοδότη", None, None, "ika_u", None, "ika_p"),
            (None, "Ι.Κ.Α. Ασφαλισμένου", None, None, "no_u", None, "no_p")]
    r = import_excel.parse_rows(rows).rows[0]
    assert (r.ika_user, r.ika_pass) == ("ika_u", "ika_p")


# ---------------------------------------------------------------- myDATA: έσοδα / έξοδα
def test_income_vs_expense_classification():
    assert analysis.is_income({"direction": "outgoing", "invoice_type": "2.1"})
    assert not analysis.is_income({"direction": "outgoing", "invoice_type": "14.3"})     # καταχώρηση λήπτη = έξοδο
    assert not analysis.is_income({"direction": "incoming", "invoice_type": "1.1"})


# ---------------------------------------------------------------- Φ4 / Φ5 από το myDATA (αναφορά χρήστη 2026-10-23)
def _ic_doc(conn, ym, invoice_type, direction):
    conn.execute("INSERT INTO mydata_docs(afm, mark, direction, invoice_type, issue_date, net, total, fetched_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (AFM, f"m{invoice_type}{ym}", direction, invoice_type, f"{ym}-10", 500, 500, "x"))
    conn.execute("INSERT OR IGNORE INTO mydata_sync(afm, ym, synced_at, n_docs) VALUES (?,?,?,1)", (AFM, ym, "x"))


def test_vies_acquisition_only_is_checked_against_f5_not_f4(conn):
    """Εταιρεία με ΜΟΝΟ απόκτηση (14.1) τον Ιούλιο: έβγαινε «Φ4 ΔΕΝ υποβλήθηκε» — το σωστό έντυπο είναι το Φ5."""
    service.add(conn, AFM, "ΑΛΦΑ ΙΚΕ")
    _ic_doc(conn, "2026-07", "14.1", "incoming")
    flags = analysis.month_flags(conn)[AFM]["2026-07"]
    assert flags["vies"] and flags["vies_in"] and not flags["vies_out"]
    biz = {"mydata": {"2026-07": flags},
           "checks": {"periods": {("Φ4", "2026-07"): {"status": "not_submitted", "amounts": {}},
                                  ("Φ5", "2026-07"): {"status": "submitted", "amounts": {}}}, "years": {}}}
    assert obligations.vies_forms(biz, "2026-07") == (("Φ5",), True)
    assert obligations.submission("vies", biz, "2026-07")["status"] == "submitted"
    assert "αποκτήσεις/λήψεις (Φ5)" in obligations.liability("vies", {**biz, "activity_state": "active", "vat_subject": 1},
                                                             "2026-07")[1]
    # παραδόσεις ΚΑΙ αποκτήσεις: θέλει και τα δύο
    _ic_doc(conn, "2026-08", "14.1", "incoming")
    _ic_doc(conn, "2026-08", "2.2", "outgoing")
    both = {"mydata": {"2026-08": analysis.month_flags(conn)[AFM]["2026-08"]},
            "checks": {"periods": {("Φ4", "2026-08"): {"status": "not_submitted", "amounts": {}},
                                   ("Φ5", "2026-08"): {"status": "submitted", "amounts": {}}}, "years": {}}}
    assert obligations.vies_forms(both, "2026-08") == (("Φ4", "Φ5"), True)
    assert obligations.submission("vies", both, "2026-08")["status"] == "not_submitted"
    # χωρίς στοιχεία myDATA: αρκεί ένα από τα δύο
    unknown = {"checks": both["checks"]}
    assert obligations.vies_forms(unknown, "2026-08") == (("Φ4", "Φ5"), False)
    assert obligations.submission("vies", unknown, "2026-08")["status"] == "submitted"


def test_vat_quarter_notice_filters_clients_and_can_be_marked_read(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    conn.execute("UPDATE businesses SET books_category='Β', vat_subject=1, activity_state='active', start_date='10/01/2024' "
                 "WHERE afm=?", (AFM,))
    n = next(n for n in notices.collect(conn) if n.get("dismiss") == "vat_quarter")
    assert n["afms"] == [AFM]
    assert not notices.vat_quarter_read(conn)
    notices.mark_vat_quarter_read(conn)
    assert notices.vat_quarter_read(conn)
    assert not any(n.get("dismiss") == "vat_quarter" for n in notices.collect(conn))
    # νέος επιλέξιμος πελάτης → η ειδοποίηση ξαναεμφανίζεται
    service.add(conn, "123456783", "ΒΗΤΑ")
    conn.execute("UPDATE businesses SET books_category='Β', vat_subject=1, activity_state='active', start_date='10/01/2024' "
                 "WHERE afm='123456783'")
    assert any(n.get("dismiss") == "vat_quarter" for n in notices.collect(conn))


def test_parse_ames_keeps_apd_code():
    page = ('<tbody id="AMEsForm:AMEsResults_data"><tr data-ri="0"><td>0020012345</td><td>ΑΛΦΑ ΙΚΕ</td><td>x</td>'
            '<td>123</td><td>user</td><td>a@b</td></tr></tbody>')
    assert efka.parse_ames(page) == [{"ame": "0020012345", "name": "ΑΛΦΑ ΙΚΕ", "apd_code": "123"}]
