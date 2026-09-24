"""Αυτόματη λήψη εγγράφων δέουσας επιμέλειας (taxmatch/aml/retrieval.py) — ΜΟΝΟ με ψεύτικο HTTP, καμία κλήση δικτύου."""
from datetime import date

from taxmatch.aml import content, kmpd_pdf, retrieval, store
from taxmatch.business_profiles import credentials, service

AFM = "094259216"
PDF = b"%PDF-1.4 " + b"x" * 800


class FakeHttp:
    """follow(method, url, form, headers) -> απάντηση από τον πρώτο κανόνα (υποσυμβολοσειρά URL) που ταιριάζει."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def follow(self, method, url, form=None, headers=None):
        self.calls.append((method, url, form))
        for needle, resp in self.routes:
            if needle in url:
                r = resp(method, url, form) if callable(resp) else resp
                if isinstance(r, bytes):
                    return {"url": url, "status": 200, "text": "", "content": r, "ct": "application/pdf", "is_pdf": True}
                return {"url": url, "status": 200, "text": r, "content": r.encode(), "ct": "text/html", "is_pdf": False}
        return {"url": url, "status": 404, "text": "", "content": b"", "ct": "", "is_pdf": False}


def test_registry_prefers_business_section_with_legal_flag():
    http = FakeHttp([("getMhtrwoFusikou", "<x/>"), ("getMhtrwoEpixeirhshs", "<hmenarxhs>2020</hmenarxhs>"),
                     ("getPrintPDFepixSection", PDF), ("comregistry", "<afm>1</afm>")])
    data, section = retrieval.fetch_registry(http, AFM)
    assert data == PDF and section == "business"
    assert any("/1/1/1/1/1/1/1/1/1/0/0/0/0/0/0/4" in u for _m, u, _f in http.calls)


def test_registry_natural_person_without_business_uses_personal_section():
    http = FakeHttp([("getMhtrwoFusikou", f"<afm>{AFM}</afm>"), ("getMhtrwoEpixeirhshs", "<x/>"),
                     ("getPrintPDFepixSection", PDF), ("comregistry", "")])
    data, section = retrieval.fetch_registry(http, AFM)
    assert data == PDF and section == "person"


def test_fenp_parsers():
    landing = ('<tr class="tblRow1"><td>01/01/2025 - 31/12/2025</td><td>x</td><td><div onclick="doDisplayDeclarationsList('
               'document.f, "incomeN", "2026", "Y", "01/01/2025", "31/12/2025", "01/01/2025", "31/12/2025");">'
               'Επεξεργασία Δηλώσεων</div></td></tr>')
    params = retrieval.fenp_list_params(landing, 2026)
    assert params == {"declarationType": "incomeN", "year": "2026", "periodType": "Y", "periodStart": "01/01/2025",
                      "periodEnd": "31/12/2025", "effectivePeriodStart": "01/01/2025", "effectivePeriodEnd": "31/12/2025"}
    assert retrieval.fenp_list_params(landing, 2025) is None       # άλλη χρήση
    listing = ("function doViewPdfTaxisnet(frm, a, b){}  "
               "<a onclick=\"doViewPdfTaxisnet(document.x, '123', 'incomeN');\">Προβολή</a>")
    assert retrieval.fenp_pdf_params(listing) == {"declarationDatabaseId": "123", "declarationType": "incomeN"}


def test_income_fp_skips_disabled_forms():
    menu = '<input name="PBE1_PRINT_PDF" type="button"><input name="PBE3_PRINT_PDF" disabled="disabled">'
    http = FakeHttp([("income-menuPrint.do", PDF), ("income-menu.do", menu)])
    got = retrieval.fetch_income_fp(http, 2025)
    assert [f for f, _ in got] == ["E1"]
    assert retrieval.fetch_income_fp(http, 2020) == []              # παλιά έτη: άλλος μηχανισμός, δεν υποστηρίζεται


def test_jsf_helpers():
    page = ('<input name="javax.faces.ViewState" value="VS1"/><table><tr data-rk="rk7"><td>ΔΟΚΙΜΗ ΙΚΕ</td>'
            '<td>094259216</td></tr></table><button id="form1:j_idt99"><span>Είσοδος</span></button>')
    assert retrieval.jsf_viewstate(page) == "VS1"
    assert retrieval.jsf_data_rows(page) == [("rk7", ["ΔΟΚΙΜΗ ΙΚΕ", AFM])]
    assert retrieval.jsf_button_id(page, "Είσοδος") == "form1:j_idt99"
    assert retrieval.jsf_partial_redirect('<redirect url="/x/selectrole.xhtml?a=1&amp;b=2"/>') == "/x/selectrole.xhtml?a=1&b=2"


def test_kmpd_full_flow_with_fake_portal():
    base = "https://webapps.gsis.gr/dsae/boregistry/faces/pages/mainmenu/"

    def selectrole(method, url, form):
        if method == "POST":
            return '<partial-response><redirect url="' + base + 'entrance.xhtml"/></partial-response>'
        return ('<input name="javax.faces.ViewState" value="V2"/><tr data-rk="r1"><td>ΔΟΚΙΜΗ ΙΚΕ</td><td>' + AFM +
                '</td></tr><button id="form1:enter1">Είσοδος</button>')

    http = FakeHttp([
        ("auth_cred_submit", '<input name="javax.faces.ViewState" value="V"/>'),
        ("index.xhtml", '<partial-response><redirect url="' + base + 'selectrole.xhtml"/></partial-response>'),
        ("selectrole.xhtml", selectrole),
        ("entrance.xhtml", lambda m, u, f: PDF if m == "POST" else '<input name="javax.faces.ViewState" value="V3"/>'),
        ("boregistry", '<form action="https://login.gsis.gr/oam/server/auth_cred_submit">'
                       '<input name="request_id" value="R"/></form>'),
    ])
    orig = http.follow

    def follow(method, url, form=None, headers=None):
        r = orig(method, url, form, headers)
        if "auth_cred_submit" in url:
            r["url"] = base + "index.xhtml"          # μετά το login, redirect πίσω στην εφαρμογή
        return r
    http.follow = follow
    r = retrieval.fetch_kmpd("rep", "pw", AFM, http_factory=lambda: http)
    assert r["ok"] and r["name"] == "ΔΟΚΙΜΗ ΙΚΕ" and [k for k, _ in r["files"]] == ["kmpd", "kmpd_cert"]
    posted = [f for m, u, f in http.calls if m == "POST" and "entrance.xhtml" in u]
    assert "form1:tabViewMainForm:print" in posted[0] and "form1:tabViewMainForm:print_bebaiosi" in posted[1]
    sel = next(f for m, u, f in http.calls if m == "POST" and "selectrole" in u)
    assert sel["form1:radioDT_selection"] == "r1" and sel["javax.faces.source"] == "form1:enter1"


def test_kmpd_wrong_password():
    login_page = '<form action="/oam/auth_cred_submit"><input name="request_id" value="R"/></form>'
    http = FakeHttp([("auth_cred_submit", '<input name="request_id"/><input name="password"/>'), ("boregistry", login_page)])
    assert retrieval.fetch_kmpd("u", "p", AFM, http_factory=lambda: http)["reason"] == "InvalidCredentials"


def test_rep_credentials_are_encrypted_and_never_exposed(conn):
    assert retrieval.set_rep_credentials(conn, AFM, "repuser", "S3cret!") is True
    row = conn.execute("SELECT taxis_user, taxis_pass FROM aml_rep_credentials WHERE afm=?", (AFM,)).fetchone()
    assert "S3cret" not in row["taxis_pass"] and row["taxis_pass"].startswith("enc:")
    assert retrieval.get_rep_credentials(conn, AFM) == ("repuser", "S3cret!")
    retrieval.set_rep_credentials(conn, AFM, "repuser2", "")            # κενός κωδικός: μένει ο παλιός
    assert retrieval.get_rep_credentials(conn, AFM) == ("repuser2", "S3cret!")
    assert retrieval.rep_user(conn, AFM) == "repuser2"


def test_retrieve_for_client_saves_files_and_marks_documents(conn, monkeypatch):
    service.add(conn, AFM, "ΔΟΚΙΜΗ ΙΚΕ")
    conn.execute("UPDATE businesses SET legal_form='ΙΚΕ' WHERE afm=?", (AFM,))
    credentials.set_(conn, AFM, "client", "pw")
    retrieval.set_rep_credentials(conn, AFM, "rep", "pw2")
    monkeypatch.setattr(retrieval, "fetch_registry", lambda http, afm: (PDF, "business"))
    monkeypatch.setattr(retrieval, "fetch_income_n", lambda http, ref: PDF if ref == 2025 else None)
    monkeypatch.setattr(kmpd_pdf, "parse_owners", lambda data: {
        "entity_afm": AFM, "registration_no": "42", "modified": "01/02/2026", "printed": "24/09/2026",
        "rep": {"afm": "111111111", "name": "ΔΟΚΙΜΑΣΤΙΚΟΣ ΕΚΠΡΟΣΩΠΟΣ"},
        "owners": [{"aa": "1", "afm": AFM, "type": "ΝΠ", "name": "ΔΟΚΙΜΗ ΙΚΕ", "roles": "", "title": "", "percent": "",
                    "percent_initial": "", "votes": "", "other": ""},
                   {"aa": "1.1", "afm": "111111111", "type": "ΦΠ", "name": "ΔΟΚΙΜΑΣΤΙΚΟΣ ΕΚΠΡΟΣΩΠΟΣ",
                    "roles": "Διαχειριστής", "title": "Εταιρικά μερίδια", "percent": "100", "percent_initial": "100",
                    "votes": "100", "other": "ΟΧΙ"}]})
    seen = {}

    def kmpd(user, pw, afm):
        seen["creds"] = (user, pw, afm)
        return {"ok": True, "name": "ΔΟΚΙΜΗ ΙΚΕ", "files": [("kmpd", PDF), ("kmpd_cert", PDF)]}

    res = retrieval.retrieve_for_client(conn, AFM, login=lambda u, p: {"ok": True, "http": object()}, kmpd=kmpd,
                                        today=date(2026, 9, 24))
    assert res["errors"] == []
    assert sorted(f["kind"] for f in res["files"]) == ["income_n", "kmpd", "kmpd_cert", "registry"]
    prof = store.get_profile(conn, AFM)                              # ΚΜΠΔ → δικαιούχοι + εκπρόσωπος στον φάκελο
    assert [(u["afm"], u["percent"]) for u in prof["ubos"]] == [("111111111", "100")]
    assert prof["rep"]["name"] == "ΔΟΚΙΜΑΣΤΙΚΟΣ ΕΚΠΡΟΣΩΠΟΣ" and prof["ubo_state"] == "pending"
    assert "αρ. καταχώρισης 42" in prof["ubo_notes"] and res["ubo_import"]["added"] == 1
    assert seen["creds"] == ("rep", "pw2", AFM)                     # ΚΜΠΔ με κωδικούς ΕΚΠΡΟΣΩΠΟΥ
    assert {"aade", "tax_return", "ubo_registry"} <= set(store.get_profile(conn, AFM)["docs"])
    files = retrieval.list_files(conn, AFM)
    assert len(files) == 4 and any("χρήση 2024" in f["filename"] for f in files)
    for f in files:
        with open(f["path"], "rb") as fh:
            assert fh.read(4) == b"%PDF"


def test_retrieve_for_client_reports_missing_credentials_and_natural_kmpd(conn):
    service.add(conn, AFM, "ΙΔΙΩΤΗΣ")
    res = retrieval.retrieve_for_client(conn, AFM, legal=False)
    assert res["files"] == []
    assert any("κωδικοί TAXISnet του πελάτη" in e for e in res["errors"])
    assert any("ΚΜΠΔ" in e and "νομικά πρόσωπα" in e for e in res["errors"])


def test_auto_docs_are_in_the_document_lists():
    for kind in ("company", "partnership"):
        assert {"aade", "tax_return", "ubo_registry"} <= set(dict(content.DOCUMENTS_BY_KIND[kind]))
    assert "tax_return" in dict(content.DOCUMENTS_BY_KIND["sole"])


# ------------------------------------------------------------------ PDF του ΚΜΠΔ (kmpd_pdf.py)
# Ψεύτικα στοιχεία στη διάταξη που βγάζει το pypdf (layout mode) από την πραγματική «Εκτύπωση δικαιούχων».
KMPD_PLAIN = """Βεβαιώνεται ότι το νομικό πρόσωπο/νομική οντότητα: ΔΟΚΙΜΗ ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε με ΑΦΜ:094259216 υπέβαλε τη σχετική
Αριθμός καταχώρισης:1234567
Ημερομηνία αρχικής υποβολής:  27/02/2026 16:07:1
Ημερομηνία τελευταίας τροποποίησης: 28/02/2026 16:07:13
Ημερομηνία εκτύπωσης : 23/09/2026 15:16:36
ΣΤΟΙΧΕΙΑ ΝΟΜΙΚΗΣ ΟΝΤΟΤΗΤΑΣ
ΑΦΜ: 094259216  Επωνυμία: ΔΟΚΙΜΗ ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε
 ΣΤΟΙΧΕΙΑ  ΕΚΠΡΟΣΩΠΟΥ
ΑΦΜ: 111111111  Επώνυμο: ΠΑΠΑΔΟΠΟΥΛΟΣ  Όνομα :  ΓΙΩΡΓΟΣ
Οδός: ΟΔΟΣ ΔΟΚΙΜΗΣ  Αρ.:  4-6
T.K: 15344,  Πόλη: ΑΘΗΝΑ
Περιοχή: ΑΤΤΙΚΗΣ Τηλέφωνο: 2100000000, Kινητό: 6900000000
ΔΙΚΑΙΟΥΧΟΙ
"""
KMPD_LAYOUT = """
     A/A AΦΜ/VAT           ΦΠ/ΝΠ    ΟΝΟΜ/ΝΥΜΟ-ΕΠΩΝΥΜΙΑ                                         ΙΔΙΟΤΗΤΕΣ         ΤΙΤΛΟΣ            ΚΑΤΟΧΗ(%)   ΚΑΤΟΧΗ            ΨΗΦΟΙ(%)   ΑΛΛΑ
                                                                                                                                               ΑΡΧΙΚΗΣ(%)
     1   094259216         Ν.Π.      ΔΟΚΙΜΗ ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε                                  ---               ---
     1.1 111111111         Φ.Π.     .... ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ ΝΙΚΟΛΑΟΣ                         Διαχειριστής,     Εταιρικά μερίδια  60.00 %     60,00%            60.00 %    ΟΧΙ
                                                                                               Εταίρος
     1.2 800000001         Ν.Π.     .... ΜΗΤΡΙΚΗ ΑΕ                                            Εταίρος           Εταιρικά μερίδια  40.00 %     40,00%            40.00 %    ΟΧΙ
     1.2.1 222222222       Φ.Π.     ........ ΙΩΑΝΝΟΥ ΜΑΡΙΑ                                     Μέτοχος           Μετοχές           100.00 %    100,00%           70.00 %    ΟΧΙ

                                                                                  Σελίδα : 1 από
"""


def test_kmpd_owners_table_parsing():
    rows = kmpd_pdf.parse_owners_text(KMPD_LAYOUT)
    assert [(r["aa"], r["type"], r["afm"]) for r in rows] == [("1", "ΝΠ", AFM), ("1.1", "ΦΠ", "111111111"),
                                                              ("1.2", "ΝΠ", "800000001"), ("1.2.1", "ΦΠ", "222222222")]
    assert rows[1]["name"] == "ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ ΝΙΚΟΛΑΟΣ"            # χωρίς τις τελείες εσοχής
    assert rows[1]["roles"] == "Διαχειριστής, Εταίρος"                   # ιδιότητα σε δεύτερη γραμμή
    assert (rows[1]["title"], rows[1]["percent"], rows[1]["votes"]) == ("Εταιρικά μερίδια", "60", "60")
    assert rows[0]["roles"] == "" and rows[0]["percent"] == ""           # «---» = κενό


def test_kmpd_header_and_ubos():
    head = kmpd_pdf.parse_header(KMPD_PLAIN)
    assert (head["registration_no"], head["entity_afm"], head["modified"]) == ("1234567", AFM, "28/02/2026")
    assert head["entity_name"] == "ΔΟΚΙΜΗ ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε"
    assert head["rep"] == {"afm": "111111111", "name": "ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ", "address": "ΟΔΟΣ ΔΟΚΙΜΗΣ 4-6, 15344 ΑΘΗΝΑ",
                           "phone": "6900000000"}
    ubos = kmpd_pdf.to_ubos({**head, "owners": kmpd_pdf.parse_owners_text(KMPD_LAYOUT)})
    assert [u["afm"] for u in ubos] == ["111111111", "222222222"]        # μόνο φυσικά πρόσωπα
    assert "μέσω ΜΗΤΡΙΚΗ ΑΕ" in ubos[1]["control"] and "ψήφοι 70%" in ubos[1]["control"]


def test_kmpd_merge_keeps_user_data():
    existing = [{"name": "ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ", "afm": "111111111", "pep": "domestic", "percent": "50"}]
    found = [{"name": "ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ ΝΙΚΟΛΑΟΣ", "afm": "111111111", "percent": "60", "control": "Εταίρος"},
             {"name": "ΙΩΑΝΝΟΥ ΜΑΡΙΑ", "afm": "222222222", "percent": "40", "control": ""}]
    rows, added, updated = kmpd_pdf.merge_ubos(existing, found)
    assert (added, updated) == (1, 1)
    assert rows[0]["pep"] == "domestic" and rows[0]["percent"] == "60"
    assert rows[0]["name"] == "ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ"                    # το όνομα του χρήστη δεν αντικαθίσταται


def test_kmpd_import_rejects_other_afm_and_non_pdf(conn, monkeypatch):
    assert retrieval.import_kmpd_owners(conn, AFM, b"not a pdf")["ok"] is False
    monkeypatch.setattr(kmpd_pdf, "parse_owners", lambda data: {"entity_afm": "999999999", "owners": []})
    r = retrieval.import_kmpd_owners(conn, AFM, PDF)
    assert r["ok"] is False and "999999999" in r["reason"]
