import io

import pytest
from openpyxl import Workbook

from taxmatch import settings_store
from taxmatch.business_profiles import import_excel, lookup_business_portal as portal, service
from taxmatch.business_profiles.vat_profile import books_category_letter, interpret_vat_profile
from tests.fakes import FakeResponse, FakeSession

AFM = "094259216"


# ---------------------------------------------------------------- VAT profile (port από ScanMyData app.py)

def test_books_category_letter():
    assert books_category_letter("Γ-ΔΙΠΛΟΓΡΑΦΙΚΑ") == "Γ"
    assert books_category_letter("Β-ΑΠΛΟΓΡΑΦΙΚΑ ΜΕ ΜΗΝΙΑΙΑ ΠΕΡΙΟΔΟ ΦΠΑ") == "Β"
    assert books_category_letter("g") == "Γ" and books_category_letter("B") == "Β"
    assert books_category_letter("") == "" and books_category_letter("Α") == ""


def test_vat_profile_period_text_beats_letter_default():
    p = interpret_vat_profile({"ypagwghfpa": "NAI", "kathgoriabibliwn": "Β-ΑΠΛΟΓΡΑΦΙΚΑ ΜΕ ΜΗΝΙΑΙΑ ΠΕΡΙΟΔΟ ΦΠΑ"})
    assert p["books_category"] == "Β" and p["vat_period_type"] == "monthly" and p["vat_subject"] is True
    q = interpret_vat_profile({"kathgoriabibliwn": "Β-ΑΠΛΟΓΡΑΦΙΚΑ ΜΕ ΤΡΙΜΗΝΙΑΙΑ ΠΕΡΙΟΔΟ ΦΠΑ"})
    assert q["vat_period_type"] == "quarterly"                       # ΤΡΙΜΗΝ πριν από ΜΗΝΙΑ
    assert interpret_vat_profile({"kathgoriabibliwn": "Γ-ΔΙΠΛΟΓΡΑΦΙΚΑ"})["vat_period_type"] == "monthly"


def test_vat_exempt_regime_overrides_yes_flag():
    p = interpret_vat_profile({"ypagwghfpa": "NAI", "kathgoriasfpa": "ΕΙΔΙΚΟ ΕΓΧΩΡΙΟ ΚΑΘΕΣΤΩΣ ΜΙΚΡΩΝ ΕΠΙΧΕΙΡΗΣΕΩΝ"})
    assert p["vat_subject"] is False
    assert interpret_vat_profile({"ypagwghfpa": "OXI"})["vat_subject"] is False
    assert interpret_vat_profile({})["vat_subject"] is None


# ---------------------------------------------------------------- Business Portal parsing

@pytest.mark.parametrize("payload,expected", [
    # A: activities με φωλιασμένο activity
    ({"arGemi": "1", "coNameEl": "ΤΟ ΒΑΨΙΜΟ Ε.Ε.", "legalType": {"id": "5", "descr": "Ετερόρρυθμη"},
      "activities": [{"activity": {"id": "47111001", "descr": "Λιανικό εμπόριο"}, "type": "ΚΥΡΙΑ"},
                     {"activity": {"id": "56101101", "descr": "Εστίαση"}, "type": "ΔΕΥΤΕΡΕΥΟΥΣΑ"}]},
     [("47111001", True), ("56101101", False)]),
    # B: επίπεδα αντικείμενα
    ({"arGemi": "2", "coNameEl": "X", "activities": [{"code": "47.11.10.01", "descr": "α", "isMain": True},
                                                      {"code": "56.10.11.01", "descr": "β"}]},
     [("47.11.10.01", True), ("56.10.11.01", False)]),
    # C: κείμενο «κωδικός - περιγραφή», χωρίς ένδειξη κύριου -> το πρώτο κύριο
    ({"arGemi": "3", "coNameEl": "Y", "kadList": ["62.01.11.00 - Προγραμματισμός", "63.11.11.00 - Επεξεργασία"]},
     [("62.01.11.00", True), ("63.11.11.00", False)]),
    # D: μέσα σε searchResults
    ({"searchResults": [{"arGemi": "4", "coNameEl": "Z", "activities": [{"code": "43.21.10.00", "descr": "γ"}]}]},
     [("43.21.10.00", True)]),
])
def test_parse_company_kad_shapes(payload, expected):
    kads = portal.parse_company(payload)["kads"]
    assert [(k["code"], k["is_main"]) for k in kads] == expected


def test_parse_company_fields_and_garbage():
    c = portal.parse_company({"arGemi": 7, "coNameEl": "ΤΟ ΒΑΨΙΜΟ Ε.Ε.", "afm": AFM,
                              "legalType": {"descr": "Ετερόρρυθμη Εταιρεία"}, "status": {"descr": "Ενεργή"},
                              "address": {"street": "Ερμού", "streetNumber": "5", "city": "Αθήνα", "zipCode": "10563"}})
    assert (c["name"], c["legal_form"], c["status"]) == ("ΤΟ ΒΑΨΙΜΟ Ε.Ε.", "Ετερόρρυθμη Εταιρεία", "Ενεργή")
    assert c["address"] == "Ερμού 5 Αθήνα 10563" and c["ar_gemi"] == "7"
    assert portal.parse_company(None) == {} and portal.parse_company([]) == {}
    assert portal.parse_company({"foo": "bar"})["kads"] == []


def test_rate_limiter_blocks_after_max_rpm(monkeypatch):
    monkeypatch.setattr(portal, "_MAX_RPM", 3)
    portal._calls.clear()
    t = {"now": 1000.0}
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        t["now"] += 61            # ο χρόνος «περνά» ώστε το παλιότερο αίτημα να βγει από το παράθυρο

    for _ in range(4):
        portal._acquire(sleep=fake_sleep, now=lambda: t["now"])
    assert len(sleeps) == 1                                      # μόνο το 4ο περίμενε


def portal_session2(search_result, detail=None):
    s = FakeSession()

    def handler(url, **kw):
        if kw.get("params"):                                   # search endpoint
            return FakeResponse(json_data={"searchResults": [search_result] if search_result else []})
        return FakeResponse(json_data=detail or {})
    s.route("businessportal.gr", handler)
    return s


def test_portal_lookup_uses_detail_when_search_lacks_kads():
    portal.clear_cache()
    s = portal_session2({"arGemi": "9", "coNameEl": "Α"},
                        {"arGemi": "9", "coNameEl": "Α", "activities": [{"code": "47.11", "descr": "δ"}]})
    out = portal.lookup(AFM, "key", s)
    assert out["company"]["kads"][0]["code"] == "47.11"
    assert len(s.calls) == 2
    portal.lookup(AFM, "key", s)                                # cache
    assert len(s.calls) == 2


def test_portal_not_found_and_bad_key():
    portal.clear_cache()
    with pytest.raises(portal.NotFound):
        portal.lookup("111111111", "key", portal_session2(None))
    s = FakeSession().route("businessportal.gr", FakeResponse(b"", 401))
    with pytest.raises(PermissionError):
        portal.lookup("222222222", "key", s)


# ---------------------------------------------------------------- Excel/CSV import

def make_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_import_with_header_and_messy_values():
    data = make_xlsx([["Πελάτες γραφείου", None], ["Επωνυμία", "ΑΦΜ", "ΚΑΔ"],
                      ["ΔΕΗ ΑΕ", 94259216, "35.11; 35.13"], ["Άλλος", "EL 123 456 783", None],
                      ["Λάθος", "abc", None], ["Διπλό", "094259216", None]])
    res = import_excel.parse_file("x.xlsx", data)
    assert [r.afm for r in res.rows] == ["094259216", "123456783"]
    assert res.rows[0].name == "ΔΕΗ ΑΕ" and res.rows[0].kads == ["35.11", "35.13"]
    assert res.invalid == [(5, "abc")] and res.duplicates == 1


def test_import_without_header_uses_first_column_and_warns():
    res = import_excel.parse_file("x.xlsx", make_xlsx([["094259216"], ["999999999"]]))
    assert len(res.rows) == 2 and any("επικεφαλίδα" in w for w in res.warnings)
    assert any("ψηφίου ελέγχου" in w for w in res.warnings)      # 999999999 δεν περνά το checksum


def test_import_csv_semicolon_greek_encoding():
    csv_bytes = "ΑΦΜ;Επωνυμία\n094259216;ΔΕΗ\n".encode("cp1253")
    res = import_excel.parse_file("x.csv", csv_bytes)
    assert res.rows[0].afm == "094259216" and res.rows[0].name == "ΔΕΗ"


def test_import_rejects_unknown_type():
    with pytest.raises(ValueError):
        import_excel.parse_file("x.pdf", b"")


# ---------------------------------------------------------------- service

def test_import_result_does_not_overwrite_existing(conn):
    service.add(conn, AFM, "Χειροκίνητο όνομα")
    res = import_excel.parse_rows([("ΑΦΜ", "Επωνυμία"), (AFM, "Άλλο όνομα"), ("123456783", "Νέος")])
    out = service.import_result(conn, res)
    assert out == {"added": 1, "updated": 0, "invalid": 0, "duplicates": 0}
    assert service.get(conn, AFM)["name"] == "Χειροκίνητο όνομα"


def test_set_kads_formats_dedups_and_sets_main(conn):
    service.add(conn, AFM)
    service.set_kads(conn, AFM, [{"code": "47111001"}, {"code": "47.11.10.01"}, {"code": "56101101", "descr": "x"}])
    b = service.get(conn, AFM)
    assert [k["code"] for k in b["kads"]] == ["47.11.10.01", "56.10.11.01"]
    assert b["kad_main_code"] == "47.11.10.01"


def test_lookup_without_credentials_keeps_client_pending(conn):
    service.add(conn, AFM)
    out = service.lookup_and_store(conn, AFM, FakeSession())
    assert out["status"] == "pending" and len(out["errors"]) == 2
    assert service.get(conn, AFM)["lookup_status"] == "pending"


def test_lookup_merges_portal_and_aade(conn):
    portal.clear_cache()
    settings_store.set_value(conn, "business_portal_key", "k")
    settings_store.set_value(conn, "aade_user", "u")
    settings_store.set_value(conn, "aade_pass", "p")
    service.add(conn, AFM)
    s = portal_session2({"arGemi": "9", "coNameEl": "ΤΟ ΒΑΨΙΜΟ Ε.Ε.", "legalType": {"descr": "ΕΕ"},
                         "activities": [{"code": "47.11.10.01", "descr": "Λιανικό"}]})
    aade = lambda u, p, afm: {"ok": True, "doy": "Α' ΑΘΗΝΩΝ", "name": "ΑΛΛΟ", "address": "Ερμού 5",
                              "all_tags": {"ypagwghfpa": "NAI", "kathgoriabibliwn": "Γ-ΔΙΠΛΟΓΡΑΦΙΚΑ"}}
    out = service.lookup_and_store(conn, AFM, s, aade)
    b = service.get(conn, AFM)
    assert out["status"] == "ok" and out["sources"] == ["ΓΕΜΗ", "ΑΑΔΕ"]
    assert (b["name"], b["doy"], b["books_category"], b["vat_subject"], b["vat_period_type"]) == \
           ("ΤΟ ΒΑΨΙΜΟ Ε.Ε.", "Α' ΑΘΗΝΩΝ", "Γ", 1, "monthly")        # όνομα από ΓΕΜΗ, όχι από ΑΑΔΕ
    assert b["kads"][0]["code"] == "47.11.10.01" and b["lookup_raw"]


def test_aade_failure_is_reported_but_portal_data_kept(conn):
    portal.clear_cache()
    settings_store.set_value(conn, "business_portal_key", "k")
    settings_store.set_value(conn, "aade_user", "u")
    settings_store.set_value(conn, "aade_pass", "p")
    service.add(conn, AFM)
    s = portal_session2({"arGemi": "9", "coNameEl": "Α", "activities": [{"code": "47.11", "descr": ""}]})
    out = service.lookup_and_store(conn, AFM, s, lambda *a: {"ok": False, "reason": "InvalidCredentials"})
    assert out["status"] == "ok" and any("λανθασμένο" in e or "Λάθος" in e for e in out["errors"])

    def boom(*a):
        raise RuntimeError("άλλαξε το HTML της ΑΑΔΕ")
    out = service.lookup_and_store(conn, AFM, s, boom)          # δεν ρίχνει exception
    assert any("μη αναμενόμενο" in e for e in out["errors"])


def test_for_matching_and_filters(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    service.add(conn, "123456783", "ΒΗΤΑ")
    service.set_kads(conn, AFM, [{"code": "47.11.10.01"}])
    assert [b["afm"] for b in service.list_all(conn, kad="47.11")] == [AFM]
    assert [b["afm"] for b in service.list_all(conn, q="βητα")] == ["123456783"]
    fm = {b["afm"]: b for b in service.for_matching(conn)}
    assert fm[AFM]["kads"] == ["47.11.10.01"] and fm["123456783"]["kads"] == []
