"""Ιδιώτες/μέλη (ΓΕΜΗ «Διαγραφή» → ιδιώτης, ΑΜΚΑ από myAMKA, καρτέλα χωρίς εργοδότη/myDATA/μέλη, Ε1/ΕΝΦΙΑ), ανάλυση
ΚΕΑΟ (μελλοντικές δόσεις), Intrastat (κατώφλια ΕΛ.ΣΤΑΤ, έλεγχος στο «Αρχείο Δηλώσεων»), PDF υποβληθέντων δηλώσεων,
αστερίσκοι ειδοποιήσεων ανά πελάτη, ΤΕΚΑ με επιλογή (ρητά αιτήματα χρήστη 2026-10-27). Χωρίς δίκτυο."""
import json
from datetime import date

from taxmatch import easycheck, efka, intrastat, keao, members, notices, obligations, settings_store
from taxmatch.business_profiles import credentials, service
from taxmatch.mydata import analysis

AFM = "094259216"


# ---------------------------------------------------------------- μέλος με διαγραμμένη ατομική = ιδιώτης
def test_cached_gemi_deleted_individual_becomes_private_person(conn):
    service.add(conn, "123456783", "(ΔΙΑΓΡΑΦΗΚΕ)ΕΛΕΥΘΕΡΙΟΣ ΔΟΚΙΜΟΣ ΤΟΥ ΓΕΩΡΓΙΟΥ")
    raw = {"business_portal": {"search": {"afm": "123456783", "coNameEl": "(ΔΙΑΓΡΑΦΗΚΕ)ΕΛΕΥΘΕΡΙΟΣ ΔΟΚΙΜΟΣ ΤΟΥ ΓΕΩΡΓΙΟΥ",
                                          "legalType": {"descr": "ΑΤΟΜΙΚΗ"}, "status": {"descr": "Διαγραφή"},
                                          "lastStatusChange": "2025-02-19"}}}
    conn.execute("UPDATE businesses SET legal_form='ΑΤΟΜΙΚΗ', lookup_raw=? WHERE afm='123456783'", (json.dumps(raw),))
    conn.execute("INSERT INTO business_kad(afm, code, descr, is_main) VALUES ('123456783','42.22.23.00','x',1)")
    service.add(conn, AFM, "ΑΛΦΑ ΙΚΕ")
    conn.execute("INSERT INTO company_members(company_afm, member_afm, name, role, share, since, source, updated_at) "
                 "VALUES (?, '123456783', 'ΔΟΚΙΜΟΣ ΕΛΕΥΘΕΡΙΟΣ', 'ΜΕΛΟΣ · ΔΙΑΧΕΙΡΙΣΤΗΣ · ΝΟΜΙΜΟΣ ΕΚΠΡΟΣΩΠΟΣ', '100%', '', "
                 "'aade', '')", (AFM,))
    assert service.reapply_cached_gemi(conn) == 1
    b = dict(conn.execute("SELECT * FROM businesses WHERE afm='123456783'").fetchone())
    assert (b["name"], b["legal_form"], b["activity_state"], b["cease_date"]) == (
        "ΔΟΚΙΜΟΣ ΕΛΕΥΘΕΡΙΟΣ", "ΙΔΙΩΤΗΣ", "ceased", "19/02/2025")
    assert not conn.execute("SELECT 1 FROM business_kad WHERE afm='123456783'").fetchone()
    assert obligations.is_private_person(b)
    assert members.short_role(members.member_roles(conn)["123456783"]) == "Νόμιμος εκπρόσωπος"
    assert service.reapply_cached_gemi(conn) == 0                         # μία φορά
    assert service.clean_gemi_name("(ΔΙΑΓΡΑΦΗΚΕ)ΠΑΠΑΣ ΙΩΑΝΝΗΣ") == "ΠΑΠΑΣ ΙΩΑΝΝΗΣ"


def test_private_person_obligations_income_tax_and_enfia():
    private = {"activity_state": "none", "legal_form": "ΙΔΙΩΤΗΣ"}
    assert obligations.liability("income_tax_return", private)[1] == "Ε1"
    assert obligations.liability("vat_monthly", private)[0] == "no"
    assert obligations.liability("income_tax_instalment", private)[0] == "maybe"
    assert obligations.liability("enfia", private)[0] == "maybe"
    assert obligations._period_income_instalment(2026, 7) == "1η δόση — φορολογικό έτος 2025"     # noqa: SLF001
    assert obligations._period_income_instalment(2027, 2) == "8η δόση — φορολογικό έτος 2025"     # noqa: SLF001
    assert obligations._period_enfia(2026, 3) == "1η δόση ΕΝΦΙΑ 2026"                            # noqa: SLF001
    assert obligations._period_enfia(2027, 2) == "12η δόση ΕΝΦΙΑ 2026"                           # noqa: SLF001


# ---------------------------------------------------------------- ΑΜΚΑ από το myAMKA
class _AmkaHttp:
    def __init__(self):
        self.calls = []

    def follow(self, method, url, form=None, headers=None):
        self.calls.append(url)
        if url.endswith("/oauth/token"):
            return {"url": url, "status": 200, "text": "TOKEN123"}
        return {"url": url, "status": 200, "text": "<html>ok</html>"}

    def post_raw(self, url, body, headers=None):
        assert headers["Authorization"] == "ctaf2 TOKEN123" and url.endswith("CTZ_GetPersonAmka")
        return {"url": url, "status": 200, "text": json.dumps({"amkaList": [{"amka": "01018012345"}], "unreadMessages": 0})}


def test_fetch_amka_from_myamka_and_store_encrypted(conn):
    service.add(conn, AFM, "ΠΑΠΑΣ")
    credentials.set_(conn, AFM, "user", "pass")
    res = efka.retrieve_amka(conn, AFM, http_factory=_AmkaHttp)
    assert res == {"ok": True, "amka": "01018012345"}
    assert efka.get_amka(conn, AFM) == "01018012345"
    raw = conn.execute("SELECT amka FROM efka_credentials WHERE afm=?", (AFM,)).fetchone()["amka"]
    assert raw.startswith("enc:1:")
    assert efka.retrieve_amka(conn, AFM, http_factory=_AmkaHttp)["existing"]       # δεν ξαναζητείται


# ---------------------------------------------------------------- ΚΕΑΟ: νέα ρύθμιση με μελλοντικές δόσεις
def test_keao_new_regulation_counts_future_instalments():
    inst = [{"no": str(n), "due": f"31/{m:02d}/2027", "amount": "173,04", "paid": "0,00", "balance": "0,00",
             "increments": "0,00"} for n, m in ((1, 1), (2, 3))]
    inst.insert(0, {"no": "0", "due": "30/09/2026", "amount": "100,00", "paid": "100,00", "balance": "0,00",
                    "increments": "0,00"})
    reg = {"status": "Ενεργή", "instalments": inst, "total": "446,08", "info": "1 28/09/2026", "type": "795 Ν. 5313/26"}
    owed, _inc, pending = keao.regulation_owed(reg)
    assert owed == 346.08 and len(pending) == 2
    assert keao.unpaid({"amount": "79,68", "paid": "79,36", "balance": "0,32"}) == 0.32
    assert keao.carrier_title("Ληξιπρόθεσμο - ΟΠΣ-ΙΚΑ - ΙΔΡΥΜΑ ΚΟΙΝΩΝΙΚΩΝ ΑΣΦΑΛΙΣΕΩΝ") == (
        "ΟΠΣ-ΙΚΑ", "ΙΔΡΥΜΑ ΚΟΙΝΩΝΙΚΩΝ ΑΣΦΑΛΙΣΕΩΝ · Ληξιπρόθεσμο")
    assert keao.carrier_title("ΕΦΚΑ (ΠΡΩΗΝ ΙΚΑ) - ΕΡΓΟΔΟΤΗΣ") == ("ΕΦΚΑ (ΠΡΩΗΝ ΙΚΑ)", "ΕΡΓΟΔΟΤΗΣ")


# ---------------------------------------------------------------- Intrastat
def test_intrastat_previous_year_uses_current_year_threshold(conn, monkeypatch):
    """ΕΛ.ΣΤΑΤ «Κατώφλια 2026»: οι αφίξεις του 2025 συγκρίνονται με το κατώφλι του 2026 (250.000), όχι του 2025."""
    flows = {f"2025-{m:02d}": {"goods_in": 18_000.0} for m in range(1, 13)}               # 216.000 το 2025
    monkeypatch.setattr(analysis, "ic_flows", lambda c, a, ym: {"goods_out": 0.0, "services_out": 0.0,
                                                                "services_in": 0.0, "goods_out_b2c": 0.0,
                                                                **flows.get(ym, {"goods_in": 0.0})})
    synced = set(flows) | {"2026-01"}
    res = analysis.intrastat(conn, AFM, "2026-01", synced)
    assert res["arrivals"] is not True                    # 216.000 > 200.000 (2025) αλλά < 250.000 (2026)
    flows.update({f"2025-{m:02d}": {"goods_in": 22_000.0} for m in range(1, 13)})        # 264.000
    res = analysis.intrastat(conn, AFM, "2026-01", synced, {})
    assert res["arrivals"] is True and res["flows_label"] == "αφίξεις"


_ARCHIVE = """<html><form method="post" action="history.aspx"><input type="hidden" name="io1" value="">
<select name="io2" onchange="post(0)"><option value="2025">2025</option><option value="2026" selected>2026</option></select>
<table><tr><td>Πρωτόκολλο</td><td>*</td><td>Ροή</td></tr>
<tr><td><a href="javascript:post(1,0,555)">202608/0012</a></td><td>Κανονική</td><td>Άφιξη</td><td>4</td><td>1.000</td>
<td>1.000</td><td>20/09/2026</td><td></td></tr>
<tr><td>202607/0009</td><td>Κανονική</td><td>Αποστολή</td><td>2</td><td>500</td><td>500</td><td>10/08/2026</td><td></td></tr>
</table></form></html>"""


def test_intrastat_archive_parse_and_entry():
    rows = intrastat.parse_archive(_ARCHIVE)
    assert [(r["protocol"], r["flow"]) for r in rows] == [("202608/0012", "Άφιξη"), ("202607/0009", "Αποστολή")]
    assert rows[0]["link"] == "javascript:post(1,0,555)"
    e = intrastat.entry(rows, 2026, {"2026-07": {"arrivals"}, "2026-08": {"arrivals"}}, today=date(2026, 10, 27))
    by = {p["period"]: p for p in e["periods"]}
    assert by["2026-08"]["status"] == "submitted"
    assert by["2026-07"]["status"] == "not_submitted" and "Άφιξη" in by["2026-07"]["detail"]
    assert e["status"] == "not_submitted"


class _ElstatHttp:
    """Ψεύτικη ΕΛ.ΣΤΑΤ: login → history (ΑΦΜ με post(0,0,77)) → αρχείο έτους."""

    def __init__(self):
        self.posts = []

    def follow(self, method, url, form=None, headers=None):
        if method == "POST":
            self.posts.append((url, dict(form or {})))
        if url.endswith("login.aspx"):
            return {"url": url, "text": '<form method="post" action="login"><input name="io1"><input type="password" name="io2"></form>'}
        if url.endswith("/login"):
            assert form == {"io1": "office@x.gr", "io2": "pw"}
            return {"url": "https://eurostat.statistics.gr/default.aspx", "text": "<html>Καλωσορίσατε</html>"}
        if url.endswith("history.aspx") and method == "GET":
            return {"url": url, "text": f'<form method="post" action="history.aspx"><input type="hidden" name="io1" value="">'
                                        f'</form><a href="javascript:post(0,0,77)">{AFM}</a>'}
        if method == "POST" and form.get("io1") == "77":
            return {"url": url, "text": _ARCHIVE.replace(" selected", "")}
        return {"url": url, "text": _ARCHIVE}


def test_intrastat_check_with_office_credentials(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    conn.execute("UPDATE businesses SET intrastat=1 WHERE afm=?", (AFM,))
    assert intrastat.check_entries(conn, AFM, 2026) == []                              # χωρίς κωδικούς γραφείου
    settings_store.set_value(conn, "intrastat_user", "office@x.gr")
    settings_store.set_value(conn, "intrastat_pass", "pw")
    fake = _ElstatHttp()
    [e] = intrastat.check_entries(conn, AFM, 2026, http_factory=lambda: fake)
    assert e["obligation"] == "INTRASTAT" and {p["period"] for p in e["periods"]} == {"2026-07", "2026-08"}
    assert any(f.get("io2") == "2026" for _u, f in fake.posts)                          # επιλογή έτους με post(0)
    easycheck.store_results(conn, AFM, 2026, [e])
    assert obligations.RULE_CHECKS["intrastat"] == ("INTRASTAT",)


def test_vies_ignores_b2c_distance_sales(conn, monkeypatch):
    monkeypatch.setattr(analysis, "ic_flows", lambda c, a, ym: {"goods_out": 0.0, "services_out": 0.0, "goods_in": 0.0,
                                                                "services_in": 0.0, "goods_out_b2c": 900.0})
    assert analysis.vies(conn, AFM, "2026-08", {"2026-08"}) is False


# ---------------------------------------------------------------- PDF υποβληθείσας δήλωσης
def test_submission_pdf_is_saved(conn, tmp_path, monkeypatch):
    from taxmatch import config

    class Http:
        def follow(self, method, url, form=None, headers=None):
            assert url.endswith("/vat/protected/viewPdf.htm") and form == {"declarationDatabaseId": "999",
                                                                            "declarationType": "vatF2"}
            return {"url": url, "is_pdf": True, "content": b"%PDF-1.4 x", "text": ""}
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    ctx = easycheck._Ctx(Http(), AFM, want_pdf=True)                                    # noqa: SLF001
    path = easycheck.submission_pdf(ctx, easycheck.OBLIGATIONS["Φ2"], "999",
                                    {"year": "2026", "periodStart": "01/08/2026"})
    assert path.endswith(".pdf") and open(path, "rb").read().startswith(b"%PDF")
    assert f"submissions" in path and AFM in path


# ---------------------------------------------------------------- αστερίσκοι ανά πελάτη / ΤΕΚΑ με επιλογή
def test_notice_marks_stay_until_client_card_opened(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    conn.execute("UPDATE businesses SET books_category='Β', vat_subject=1, activity_state='active', start_date='10/01/2024' "
                 "WHERE afm=?", (AFM,))
    notices.mark_vat_quarter_read(conn)
    assert notices.marks(conn, "vat") == {AFM}
    notices.clear_marks(conn, AFM)
    assert notices.marks(conn, "vat") == set()


def test_teka_only_when_chosen_in_profile():
    biz = {"activity_state": "active", "employer": 1, "teka": 0,
           "checks": {"years": {("ΑΠΔ_ΤΕΚΑ", 2026): "submitted"}}}
    assert obligations.liability("apd_teka", biz, "2026-08")[0] == "no"
    biz["teka"] = 1
    assert obligations.liability("apd_teka", biz, "2026-08")[0] == "yes"
