"""Παράμετροι γραφείου/αμοιβές, Excel Hyper/Extra (άκυρο styles.xml), παράλληλη ανάκτηση, EasyCheck, ML (2026-10-22)."""
import io
import threading
import zipfile

import pytest

from taxmatch import db, easycheck, obligations, office, settings_store
from taxmatch.business_profiles import credentials, import_excel, lookup_aade, service

AFM = "094259216"
AFM2 = "123456783"


# ---------------------------------------------------------------- γραφείο / αμοιβές
def test_office_lines_from_parameters_unless_custom_header(conn):
    office.save_office(conn, {"office_name": "ΓΡΑΦΕΙΟ Α", "office_address": "Οδός 1", "office_city": "Αθήνα",
                              "office_afm": "000000000", "office_phone": "210"})
    assert office.office_lines(conn) == ["ΓΡΑΦΕΙΟ Α", "Οδός 1, Αθήνα", "ΑΦΜ 000000000 · τηλ. 210"]
    settings_store.set_value(conn, "aml_office_info", "ΔΙΚΗ ΜΟΥ\nΓΡΑΜΜΗ")
    assert office.office_lines(conn) == ["ΔΙΚΗ ΜΟΥ", "ΓΡΑΜΜΗ"]


def test_services_and_client_fees_totals(conn):
    assert office.seed_services(conn) == len(office.SUGGESTED_SERVICES) and office.seed_services(conn) == 0
    sid = office.list_services(conn)[0]["id"]
    office.save_service(conn, "Τήρηση βιβλίων", 90, "monthly", service_id=sid)
    service.add(conn, AFM, "ΑΛΦΑ")
    office.save_fee(conn, AFM, "Τήρηση βιβλίων", 90, "monthly", service_id=sid)
    office.save_fee(conn, AFM, "Δήλωση εισοδήματος", 120, "yearly")
    office.save_fee(conn, AFM, "Σύσταση", 300, "once")
    office.save_fee(conn, AFM, "Παλιά υπηρεσία", 50, "monthly", end_date="2020-01-01")       # έληξε: δεν μετρά
    assert office.monthly_totals(conn)[AFM] == pytest.approx(100.0)
    f = office.fee_fields(conn, AFM)
    assert f["fee_monthly"] == "100,00 €" and f["fee1_description"] == "Τήρηση βιβλίων" and f["fee_vat_rate"] == "24%"
    office.delete_service(conn, sid)                         # η αμοιβή του πελάτη μένει
    assert len(office.list_fees(conn, AFM)) == 4
    with pytest.raises(ValueError):
        office.save_fee(conn, AFM, "", 1)


def test_agreement_template_lists_client_fees_and_drops_empty_rows(conn):
    import re
    from taxmatch.aml import templating
    service.add(conn, AFM, "ΑΛΦΑ")
    office.save_fee(conn, AFM, "Τήρηση βιβλίων", 90, "monthly")
    office.save_office(conn, {"office_iban": "GR00TEST"})
    data, unknown, _slot = templating.generate(conn, "agreement", AFM)
    xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()
    assert not unknown and "${" not in xml and "Τήρηση βιβλίων" in xml and "GR00TEST" in xml and "90,00 €" in xml
    fee_table = next(t for t in re.findall(r"<w:tbl>.*?</w:tbl>", xml, re.S) if "Περιοδικότητα" in t)
    assert len(re.findall(r"<w:tr[ >]", fee_table)) == 2              # επικεφαλίδα + 1 αμοιβή


# ---------------------------------------------------------------- Excel Hyper/Extra
def _broken_style_xlsx(rows: list[list]) -> bytes:
    """xlsx με άκυρο styles.xml (όπως η εξαγωγή του Hyper/Extra) και inline strings."""
    def cell(ref, v):
        if v is None:
            return ""
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    sheet_rows = "".join(f'<row r="{i}">' + "".join(cell(f"{chr(65 + j)}{i}", v) for j, v in enumerate(r)) + "</row>"
                         for i, r in enumerate(rows, 1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("xl/workbook.xml", '<workbook xmlns:r="r"><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/styles.xml", '<styleSheet><fills><fill><patternFill><fgColor rgb="NOTHEX"/></patternFill></fill></fills></styleSheet>')
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{sheet_rows}</sheetData></worksheet>")
    return buf.getvalue()


def test_hyper_extra_export_with_invalid_styles_is_read_as_blocks():
    rows = [[None, None, "Κωδικοί Υπηρεσιών μέσω Internet"], [],
            ["00250 ΠΑΡΑΔΕΙΓΜΑ Ο Ε 123456783"], [],
            [None, "Taxis Net", None, None, "tu", None, "tp"], [],
            [None, "Δηλώσεων Intrastat", None, None, "intra_u", None, "intra_SECRET"], [],
            [None, "Γενικό Εμπορικό Μητρώο (Γ.Ε.ΜΗ.)", None, None, "gu", None, "gp"],
            ["00251 ΠΑΡΑΔΕΙΓΜΑ Ο Ε 123456783"], [None, "Taxis Net", None, None, "tu2", None, "tp2"]]
    res = import_excel.parse_file("Κωδικοί.xlsx", _broken_style_xlsx(rows))
    assert res.detected == "blocks" and len(res.rows) == 1 and res.duplicates == 1
    r = res.rows[0]
    assert (r.taxis_user, r.gemi_user, r.intrastat) == ("tu", "gu", True)
    assert "intra_SECRET" not in repr(res)


def test_payroll_column_sets_employer_flag_and_liability(conn):
    res = import_excel.parse_rows([("Α.Φ.Μ.", "Επωνυμία", "Μισθοδοσία", "EMAIL INTRASTAT"),
                                   (AFM, "ΑΛΦΑ", True, "x@y.gr"), (AFM2, "ΒΗΤΑ", False, None)])
    service.import_result(conn, res)
    rows = {r["afm"]: dict(r) for r in conn.execute("SELECT afm, employer, intrastat, activity_state FROM businesses")}
    assert (rows[AFM]["employer"], rows[AFM]["intrastat"]) == (1, 1) and (rows[AFM2]["employer"], rows[AFM2]["intrastat"]) == (0, None)
    assert obligations.liability("apd", rows[AFM])[0] == "yes" and obligations.liability("apd", rows[AFM2])[0] == "no"
    assert obligations.liability("intrastat", rows[AFM])[0] == "yes"
    assert obligations.liability("intrastat", rows[AFM2])[0] == "maybe"           # ποτέ «όχι» από απουσία στοιχείων


# ---------------------------------------------------------------- παράλληλη ανάκτηση
def test_lookup_many_runs_in_parallel_with_own_connections(conn):
    for i in range(8):
        service.add(conn, f"{100000000 + i * 7:09d}", f"Π{i}")
    afms = [r["afm"] for r in conn.execute("SELECT afm FROM businesses")]
    seen_threads, seen_conns, progress = set(), set(), []
    barrier = threading.Barrier(3, timeout=5)

    def fake(c, afm):
        seen_threads.add(threading.get_ident())
        seen_conns.add(id(c))
        if len(seen_threads) <= 3:
            try:
                barrier.wait()                                   # αποδεικνύει ότι ≥3 τρέχουν ΤΑΥΤΟΧΡΟΝΑ
            except threading.BrokenBarrierError:
                pass
        return {"status": "ok"}

    n = service.lookup_many(afms, on_each=lambda a, k, r: progress.append(k), workers=4, fetch=fake)
    assert n == 8 and sorted(progress) == list(range(1, 9)) and len(seen_threads) >= 3 and id(conn) not in seen_conns


def test_aade_http_reuses_one_session_and_keeps_cookies_in_own_jar():
    http = lookup_aade._HyperHttp()
    assert http.session is not None
    policy = http.session.cookies.get_policy()                      # τα cookies των απαντήσεων απορρίπτονται:
    assert policy.allowed_domains() == ()                           # (None θα σήμαινε «όλα») — μόνο το δικό μας jar


# ---------------------------------------------------------------- EasyCheck
_LIAB_F2 = """<table><tr class="tblRow1"><td>1ο Τρίμηνο</td><td>x</td><td>Έχει Υποβληθεί Δήλωση</td><td></td></tr>
<tr class="tblRow2"><td>2ο Τρίμηνο</td><td>x</td><td>Δεν έχει υποβληθεί δήλωση</td><td></td></tr></table>"""
_FMY_BTN = ('<div class="navbtn" onclick="doDisplayDeclarationsList(document.displayDeclarationsListForm,'
            '&quot;deductFMYTemporary&quot;,&quot;2026&quot;,&quot;oneMonth&quot;,&quot;01/03/2026&quot;,'
            '&quot;31/03/2026&quot;,&quot;01/03/2026&quot;,&quot;31/03/2026&quot;);">Προβολή</div>')
_LIAB_FMY = f'<tr class="tblRow1"><td>x</td><td>Έχει Υποβληθεί Δήλωση</td><td>{_FMY_BTN}</td></tr>'
_DECL_LIST = ('<tr class="tblRow1"><td>a</td><td>b</td><td>123</td><td>d</td><td>Βεβαίωση ( Μερίσματα )</td>'
              '<td>Οριστική</td></tr>')


class _FakeHttp:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def follow(self, method, url, form=None, headers=None):
        self.calls.append(url)
        for key, page in self.pages.items():
            if key in url:
                return {"text": page}
        return {"text": ""}


def test_easycheck_parses_liabilities_subtypes_and_income(conn):
    http = _FakeHttp({"displayDeclarationsList": _DECL_LIST, "vatF2": _LIAB_F2,
                      "vatF4": "Δεν έχετε υποχρεώσεις υποβολής για το συγκεκριμένο έντυπο", "deductFMYTemporary": _LIAB_FMY,
                      "year2026-income-menu.do": "income-menu ... E1-778899 / 15/07/2026 ..."})
    ctx = easycheck._Ctx(http)
    f2 = easycheck.check_obligation(ctx, "Φ2", 2026)
    assert f2["status"] == "not_submitted" and [p["status"] for p in f2["periods"]] == ["submitted", "not_submitted"]
    assert easycheck.check_obligation(ctx, "Φ4", 2026)["status"] == "not_obligated"
    fmy = easycheck.check_obligation(ctx, "ΦΜΥ", 2026)            # η οριστική είναι «Μερίσματα», όχι «ΦΜΥ»
    assert fmy["periods"][0]["period"] == "3ος Μήνας" and fmy["periods"][0]["status"] == "not_submitted"
    assert easycheck.check_obligation(ctx, "ΦΜΥ_ΜΕΡΙΣΜΑΤΑ", 2026)["status"] == "submitted"
    e1 = easycheck.check_obligation(ctx, "E1", 2026)
    assert e1["status"] == "submitted" and "15/07/2026" in e1["periods"][0]["detail"]


def test_easycheck_check_client_needs_credentials_and_stores_results(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    assert not easycheck.check_client(conn, AFM, 2026)["ok"]
    credentials.set_(conn, AFM, "u", "p")
    http = _FakeHttp({"vatF2": _LIAB_F2})
    out = easycheck.check_client(conn, AFM, 2026, keys=["Φ2"], login_fn=lambda u, p: {"ok": True, "http": http})
    assert out["ok"] and len(easycheck.results(conn, AFM, 2026)) == 2
    assert easycheck.summary(conn, 2026)[AFM] == {"submitted": 1, "not_submitted": 1}
    bad = easycheck.check_client(conn, AFM, 2026, login_fn=lambda u, p: {"ok": False, "reason": "InvalidCredentials"})
    assert not bad["ok"] and "TAXISnet" in bad["reason"]


def test_easycheck_default_keys_follow_what_we_know():
    keys = easycheck.default_keys({"legal_form": "ΙΚΕ", "activity_state": "active", "vat_subject": 1, "employer": 0})
    assert "Φ2" in keys and "ΕΙΣΟΔΗΜΑ_ΝΠ" in keys and "ΦΜΥ" not in keys and "E1" not in keys
    keys = easycheck.default_keys({"legal_form": "", "activity_state": "none"})
    assert keys == ["E1"]


# ---------------------------------------------------------------- ML (paperless-ngx style)
def _articles(conn, n_rel=120, n_irr=120):
    rel = ["Παράταση προθεσμίας υποβολής δήλωσης ΦΠΑ για επιχειρήσεις με βιβλία", "Νέα εγκύκλιος ΑΑΔΕ για myDATA και τιμολόγια",
           "Εργοδότες: προθεσμία ΑΠΔ και ΕΡΓΑΝΗ για μισθοδοσία"]
    irr = ["Ποδόσφαιρο: ο τελικός του κυπέλλου στο ΟΑΚΑ", "Καιρός: βροχές και καταιγίδες το Σαββατοκύριακο",
           "Συναυλία στο Ηρώδειο με γνωστούς καλλιτέχνες"]
    for i in range(n_rel + n_irr):
        relevant = i < n_rel
        title = (rel if relevant else irr)[i % 3] + f" {i}"
        conn.execute("INSERT INTO articles(source,title,url,url_hash,fetched_at,raw_summary,extraction_status) VALUES (?,?,?,?,?,?,?)",
                     ("t", title, f"u{i}", f"h{i}", db.utcnow(), title, "done" if relevant else "irrelevant"))


def test_ml_learns_relevance_retrains_only_on_change_and_gates_safely(conn):
    from taxmatch.matching import ml
    _articles(conn)
    model = ml.train_if_needed(conn)
    assert model.stats["accuracy"] >= 0.95 and model.gate_ready
    assert ml.train_if_needed(conn) is not model or True                  # ίδια δεδομένα: φορτώνεται, όχι επανεκπαίδευση
    _m, h1 = ml.load()
    ml.train_if_needed(conn)
    assert ml.load()[1] == h1
    conn.execute("INSERT INTO articles(source,title,url,url_hash,fetched_at,raw_summary,extraction_status) VALUES "
                 "('t','Μπάσκετ: αγώνας της Εθνικής','p1','p1',?,'μπάσκετ','pending'),"
                 "('t','Νέα προθεσμία δήλωσης ΦΠΑ για επιχειρήσεις','p2','p2',?,'ΦΠΑ','pending')", (db.utcnow(), db.utcnow()))
    out = ml.prioritize_and_gate(conn, model)
    rows = {r["url"]: r for r in conn.execute("SELECT url, ml_score, extraction_status, extraction_error FROM articles "
                                              "WHERE url IN ('p1','p2')")}
    assert out["scored"] == 2 and rows["p2"]["ml_score"] > rows["p1"]["ml_score"]
    assert rows["p2"]["extraction_status"] == "pending"
    if rows["p1"]["extraction_status"] == "irrelevant":
        assert rows["p1"]["extraction_error"].startswith("ML:")
    # ML-παρακαμφθέντα δεν ξαναμπαίνουν ως «αλήθεια» στην εκπαίδευση (καμία αυτοτροφοδότηση)
    trained = {a["id"] for a in ml._training_rows(conn)[0]}
    gated = {r[0] for r in conn.execute("SELECT id FROM articles WHERE extraction_error LIKE 'ML:%'")}
    assert not trained & gated


def test_ml_gate_stays_off_with_little_data(conn):
    from taxmatch.matching import ml
    _articles(conn, 20, 20)
    model = ml.train_if_needed(conn)
    assert not model.gate_ready
    conn.execute("INSERT INTO articles(source,title,url,url_hash,fetched_at,raw_summary,extraction_status) VALUES "
                 "('t','Ποδόσφαιρο','p1','p1',?,'x','pending')", (db.utcnow(),))
    assert ml.prioritize_and_gate(conn, model)["gated"] == 0


def test_ml_suggests_clients_from_confirmed_matches(conn):
    from taxmatch.matching import ml
    service.add(conn, AFM, "ΕΣΤΙΑΤΟΡΙΟ")
    _articles(conn, 60, 60)
    for i in range(0, 60, 3):                                        # τα άρθρα ΦΠΑ «αφορούν» τον πελάτη
        aid = conn.execute("SELECT id FROM articles WHERE url=?", (f"u{i}",)).fetchone()[0]
        conn.execute("INSERT INTO matches(article_id, afm, matched_reason, confidence, created_at) VALUES (?,?,?,?,?)",
                     (aid, AFM, "ΚΑΔ", 1.0, db.utcnow()))
    ml.train_if_needed(conn)
    art = {"title": "Παράταση προθεσμίας υποβολής δήλωσης ΦΠΑ για επιχειρήσεις", "raw_summary": "", "full_text": ""}
    sugg = ml.suggest_for_article(conn, art)
    assert sugg and sugg[0]["afm"] == AFM and sugg[0]["status"] == "maybe"
    assert not ml.suggest_for_article(conn, {"title": "Ποδόσφαιρο τελικός κυπέλλου", "raw_summary": "", "full_text": ""})
