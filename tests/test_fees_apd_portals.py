"""Τέλη από το myDATA (σακούλα/πλαστικά/παρεπιδημούντες/διαμονή), έλεγχοι στις πύλες Plastic και dae.govapp.gr, ΑΠΔ
e-ΕΦΚΑ / ΤΕΚΑ ανά ΑΜΕ, κανόνας ΚΑΔ παρεπιδημούντων, κατάσταση ΓΕΜΗ «Διαγραφή», χρήση (ρητά αιτήματα χρήστη
2026-10-27). Χωρίς δίκτυο — ψεύτικο HTTP με τη δομή των runner configs / TaxSystem."""
import base64
import json
from datetime import date

from taxmatch import deadlines, easycheck, efka, fee_portals, fiscal, notices, obligations, parepid
from taxmatch.business_profiles import credentials, lookup_business_portal as portal, service
from taxmatch.mydata import analysis, parse

AFM = "094259216"


# ---------------------------------------------------------------- ΓΕΜΗ «Διαγραφή» → διακοπή
def test_gemi_deleted_status_means_ceased_with_date():
    c = portal.parse_company({"afm": AFM, "coNameEl": "ΠΑΠΑΣ", "legalType": {"id": 16, "descr": "ΑΤΟΜΙΚΗ"},
                              "status": {"id": 17, "descr": "Διαγραφή"}, "lastStatusChange": "2025-02-19",
                              "incorporationDate": "2021-10-26"})
    assert (c["state"], c["status_date"], c["start_date"]) == ("ceased", "19/02/2025", "26/10/2021")
    assert portal.business_state("Ενεργή") == "active" and portal.business_state("") == ""


def test_lookup_without_aade_takes_ceased_state_from_gemi(conn, monkeypatch):
    from taxmatch import settings_store
    service.add(conn, AFM, "ΠΑΠΑΣ")
    settings_store.set_value(conn, "business_portal_key", "k")
    monkeypatch.setattr(portal, "lookup", lambda afm, key, session=None: {
        "company": portal.parse_company({"afm": afm, "coNameEl": "ΠΑΠΑΣ", "legalType": {"descr": "ΑΤΟΜΙΚΗ"},
                                         "status": {"descr": "Διαγραφή"}, "lastStatusChange": "2025-02-19"}),
        "raw": {}})
    service.lookup_and_store(conn, AFM, None, lambda *a: {"ok": False, "reason": "NoRegistry"})
    b = conn.execute("SELECT activity_state, cease_date FROM businesses WHERE afm=?", (AFM,)).fetchone()
    assert (b["activity_state"], b["cease_date"]) == ("ceased", "19/02/2025")


# ---------------------------------------------------------------- myDATA → τέλη
_INV = """<InvoicesDoc xmlns="http://www.aade.gr/myDATA/invoice/v1.0"><invoice><mark>1</mark>
<invoiceHeader><series>A</series><aa>1</aa><issueDate>2026-08-10</issueDate><invoiceType>11.1</invoiceType></invoiceHeader>
<invoiceDetails><lineNumber>1</lineNumber><netValue>10</netValue><feesPercentCategory>8</feesPercentCategory><feesAmount>0.14</feesAmount></invoiceDetails>
<invoiceDetails><lineNumber>2</lineNumber><netValue>5</netValue><feesPercentCategory>18</feesPercentCategory><feesAmount>0.03</feesAmount></invoiceDetails>
<invoiceSummary><totalNetValue>15</totalNetValue><totalVatAmount>0</totalVatAmount><totalGrossValue>15.17</totalGrossValue></invoiceSummary>
</invoice></InvoicesDoc>"""


def test_fee_categories_parsed_and_flagged(conn):
    from taxmatch.mydata import store
    docs = parse.parse_page(_INV.encode("utf-8"), "outgoing")
    assert docs[0]["taxes"] == {"fees": {"8": 0.14, "18": 0.03}}
    service.add(conn, AFM, "ΑΛΦΑ")
    store._store(conn, AFM, docs, ["2026-08"])                 # noqa: SLF001 — ίδια αποθήκευση με τη λήψη
    flags = analysis.month_flags(conn)[AFM]["2026-08"]
    assert flags["plastic_bag"] and flags["parepid"] and not flags["plastics"]
    assert easycheck.fee_keys(conn, AFM, 2026) == {"ΠΕΡΙΒΑΛΛΟΝ"}
    assert fee_portals.wanted(conn, AFM, 2026) == {"ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ"}


def test_quarterly_fee_rule_uses_any_month_of_the_quarter():
    biz = {"activity_state": "active", "mydata": {"2026-07": {"plastic_bag": False}, "2026-08": {"plastic_bag": True}}}
    assert obligations.liability("plastic_bag_fee", biz, "2026-09")[0] == "yes"
    assert obligations.liability("plastic_bag_fee", {**biz, "mydata": {"2026-07": {"plastic_bag": False}}},
                                 "2026-09")[0] == "no"
    assert [r.id for r in obligations.RULES if r.id in ("plastic_bag_fee", "plastics_fee", "parepid_fee", "apd_teka")] \
        == ["apd_teka", "plastic_bag_fee", "plastics_fee", "parepid_fee"]


# ---------------------------------------------------------------- ΚΑΔ → πιθανός υπόχρεος παρεπιδημούντων
def test_parepid_kad_rule_notice_and_liability(conn):
    assert parepid.kad_matches(["56.10.11.02"])[0]["code"] == "56.10.11.02"
    assert parepid.kad_matches(["47.11"]) == []
    service.add(conn, AFM, "ΤΑΒΕΡΝΑ")
    conn.execute("UPDATE businesses SET activity_state='active' WHERE afm=?", (AFM,))
    service.set_kads(conn, AFM, [{"code": "56.10.11.02", "descr": "", "is_main": True}])
    n = next(n for n in notices.collect(conn) if n.get("dismiss") == "parepid")
    assert n["afms"] == [AFM]
    notices.mark_parepid_read(conn)
    assert not any(n.get("dismiss") == "parepid" for n in notices.collect(conn))
    biz = {"activity_state": "active", "kad_codes": "56.10.11.02", "mydata": {"2026-08": {"parepid": False}}}
    status, reason = obligations.liability("parepid_fee", biz, "2026-08")
    assert status == "maybe" and "56.10.11.02" in reason
    assert obligations.liability("parepid_fee", {**biz, "kad_codes": "47.11"}, "2026-08")[0] == "no"


# ---------------------------------------------------------------- Plastic (εφαρμογή ΑΑΔΕ)
def _res(value) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return json.dumps({"result": raw})


class _FakePlastic:
    def __init__(self):
        self.calls = []

    def follow(self, method, url, form=None, headers=None):
        self.calls.append(url)
        if "getToken" in url:
            return {"url": url, "status": 200, "text": _res("tok")}
        if "getIsActiveEpitid" in url:
            q = int(url.split("/")[-1].split("?")[0])
            return {"url": url, "status": 200, "text": json.dumps({"isActive": q != 2})}
        if "getMarginalDateData" in url:
            return {"url": url, "status": 200, "text": json.dumps({"targetDate": "2026-04-30T21:00:00Z[UTC]"})}
        return {"url": url, "status": 200, "text": "<html></html>"}

    def post_raw(self, url, body, headers=None):
        assert headers["Authorization"] == "tok"
        if "getuserdata" in url:
            return {"url": url, "status": 200, "text": _res({"afm": AFM})}
        if "getSearchApplication" in url:
            return {"url": url, "status": 200, "text": _res([{"etos": 2026, "trimhno": 1, "synolo": 12.4,
                                                               "transId": 7}])}
        return {"url": url, "status": 204, "text": ""}


def test_plastics_entry_per_quarter():
    e = fee_portals.plastics_entry(_FakePlastic(), AFM, 2026, today=date(2026, 10, 27), want_pdf=False)
    by = {p["period"]: p["status"] for p in e["periods"]}
    assert by == {"Α΄ τρίμηνο 2026": "submitted", "Β΄ τρίμηνο 2026": "not_obligated",
                  "Γ΄ τρίμηνο 2026": "not_submitted"}
    assert e["periods"][0]["amounts"]["total_due"] == 12.4 and e["status"] == "not_submitted"


# ---------------------------------------------------------------- dae.govapp.gr
class _FakeDae:
    def follow(self, method, url, form=None, headers=None):
        if url.endswith("/api/Helper/Periodoi"):
            return {"url": url, "status": 200, "text": json.dumps([
                {"id": 8, "perigrafi": "Αύγουστος 2026", "periodikiDilosiApo": "2026-08-01T00:00:00",
                 "periodikiDilosiEos": "2026-08-31T00:00:00", "typosPeriodouYpovolis": 1},
                {"id": 9, "perigrafi": "Σεπτέμβριος 2026", "periodikiDilosiApo": "2026-09-01T00:00:00",
                 "periodikiDilosiEos": "2026-09-30T00:00:00", "typosPeriodouYpovolis": 1}])}
        if url.endswith("GetPendingPeriodikes"):
            return {"url": url, "status": 200, "text": json.dumps([{"periodId": 9,
                                                                     "teleutaiaImeraPrinProstimo": "2026-10-31"}])}
        raise AssertionError(url)

    def post_raw(self, url, body, headers=None):
        if url.endswith("GetCompletedPeriods"):
            return {"url": url, "status": 200, "text": json.dumps({"data": [{"id": 8}]})}
        if url.endswith("GetYpovolesPeriodou"):
            return {"url": url, "status": 200, "text": json.dumps({"data": [
                {"id": 1, "posoVevaiosis": 33.44, "posoYpoloipo": 0, "tautotitaOfeilis": "RF123",
                 "teleutaiaImeraPrinProstimo": "2026-09-30"}], "recordsFiltered": 1})}
        raise AssertionError(url)


def test_parepid_entry_pending_and_submitted():
    e = fee_portals.parepid_entry(_FakeDae(), 2026)
    by = {p["period"]: p for p in e["periods"]}
    assert by["Αύγουστος 2026"]["status"] == "submitted"
    assert by["Αύγουστος 2026"]["amounts"] == {"total_due": 33.44, "payment_id": "RF123", "pay_by": "2026-09-30"}
    assert by["Σεπτέμβριος 2026"]["status"] == "not_submitted" and e["status"] == "not_submitted"


# ---------------------------------------------------------------- ΑΠΔ e-ΕΦΚΑ / ΤΕΚΑ ανά ΑΜΕ
_SUBMISSIONS = ('<span id="resultTpte">1234567890</span><tbody id="submissionTable_data">'
                '<tr><td>101</td><td>Κανονική</td><td>1/2026 - 1/2026</td><td>ΥΠΟΒΟΛΗ</td><td></td></tr>'
                '<tr><td>102</td><td>Κανονική</td><td>2/2026 - 2/2026</td><td>ΑΠΟΡΡΙΨΗ</td><td></td></tr>'
                '</tbody><span class="ui-paginator-current">(Σελίδα 1 από 1)</span>'
                '<input name="jakarta.faces.ViewState" value="vs"/>')


def test_apd_submissions_parse_and_entry():
    parsed = efka.parse_submissions(_SUBMISSIONS)
    assert [r["yms"] for r in parsed["rows"]] == [["2026-01"], ["2026-02"]] and parsed["payment_id"] == "1234567890"
    e = efka.submissions_entry("ΑΠΔ", "0020012345", parsed["rows"], 2026, parsed["payment_id"], today=date(2026, 4, 15))
    by = {p["period"]: p["status"] for p in e["periods"]}
    assert e["obligation"] == "ΑΠΔ#0020012345"
    assert by["Ιανουάριος 2026"] == "submitted" and by["Φεβρουάριος 2026"] == "not_submitted"
    teka = efka.submissions_entry("ΑΠΔ_ΤΕΚΑ", "0020012345", [], 2026, today=date(2026, 4, 15))
    assert teka["status"] == "not_obligated"


def test_per_ame_checks_merge_into_the_rule(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    for ame, status in (("0020000001", "submitted"), ("0020000002", "not_submitted")):
        easycheck.store_results(conn, AFM, 2026, [{"obligation": f"ΑΠΔ#{ame}", "label": "", "status": status,
                                                   "periods": [{"period": "Ιανουάριος 2026", "status": status,
                                                                "period_end": "2026-01-31", "period_type": "month"}]}])
    biz = deadlines._checks_for(conn, AFM)                     # noqa: SLF001
    hit = biz["checks"]["periods"][("ΑΠΔ", "2026-01")]
    assert hit["status"] == "not_submitted" and set(hit["ames"]) == {"0020000001", "0020000002"}
    assert obligations.submission("apd", biz, "2026-01")["status"] == "not_submitted"


def test_ames_with_own_credentials_are_encrypted(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    efka.upsert_ame(conn, AFM, "20000009", kind="oikodom", name="ΕΡΓΟ Α", user="ergo1", password="p1")
    raw = conn.execute("SELECT ika_user, ika_pass FROM efka_ames WHERE afm=?", (AFM,)).fetchone()
    assert raw["ika_pass"].startswith("enc:1:") and "p1" not in raw["ika_pass"]
    assert efka.ame_credentials(conn, AFM, "0020000009") == ("ergo1", "p1")
    a = efka.list_ames(conn, AFM)[0]
    assert a["ame"] == "0020000009" and a["has_pass"] and "ika_pass" not in a


# ---------------------------------------------------------------- χρήση
def test_fiscal_year_rolls_over_with_the_calendar():
    assert fiscal.resolve_year(2025, 2026, date(2026, 1, 10)) == 2025       # επέλεξε πέρσι, μέσα στο ίδιο έτος
    assert fiscal.resolve_year(2025, 2025, date(2026, 1, 10)) == 2026       # νέος χρόνος → νέα χρήση
    assert fiscal.year_choices([2023], date(2026, 5, 1)) == [2026, 2025, 2023]


def test_fee_declarations_not_submitted_only_where_mydata_has_the_fee(conn):
    """TAXISnet δείχνει «Δεν έχει υποβληθεί» σε ΚΑΘΕ περίοδο H6/H7 για όλους (ζωντανά 2026-10-27) — κρατάμε μόνο όσες
    έχουν το τέλος στο myDATA, και πάντα τις υποβληθείσες."""
    from taxmatch.mydata import store
    service.add(conn, AFM, "ΑΛΦΑ")
    store._store(conn, AFM, parse.parse_page(_INV.encode("utf-8"), "outgoing"), ["2026-08"])   # noqa: SLF001
    entry = {"obligation": "ΠΕΡΙΒΑΛΛΟΝ", "label": "", "status": "not_submitted", "periods": [
        {"period": "1ο Τρίμηνο 2026", "status": "not_submitted", "period_end": "2026-03-31"},
        {"period": "2ο Τρίμηνο 2026", "status": "submitted", "period_end": "2026-06-30"},
        {"period": "3ο Τρίμηνο 2026", "status": "not_submitted", "period_end": "2026-09-30"}]}
    kept = easycheck._only_liable_fee_periods(conn, AFM, 2026, entry)            # noqa: SLF001
    assert [p["period"] for p in kept["periods"]] == ["2ο Τρίμηνο 2026", "3ο Τρίμηνο 2026"]
    none = easycheck._only_liable_fee_periods(conn, AFM, 2026, {**entry, "obligation": "ΔΙΑΜΟΝΗ"})  # noqa: SLF001
    assert [p["period"] for p in none["periods"]] == ["2ο Τρίμηνο 2026"] and none["status"] == "submitted"
