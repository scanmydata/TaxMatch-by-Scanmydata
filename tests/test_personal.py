"""Τέλη κυκλοφορίας (myCAR) και ΕΝΦΙΑ (ETAK) φυσικών προσώπων — ψεύτικο HTTP με τη δομή των σελίδων του runner."""
from __future__ import annotations

from datetime import date

import pytest

from taxmatch import db as dbmod
from taxmatch import deadlines, easycheck, obligations, personal
from taxmatch.business_profiles import credentials, service

AFM = "094259216"
PDF = b"%PDF-1.4 " + b"x" * 600


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls, self.jar = routes, [], {"www1.aade.gr": {"JSESSIONID-ETAK": "abc"}}

    def follow(self, method, url, form=None, headers=None):
        self.calls.append((method, url, form))
        for key, value in self.routes:
            if key in url and (not callable(value) or True):
                body = value(form) if callable(value) else value
                if isinstance(body, bytes):
                    return {"status": 200, "text": "", "content": body, "is_pdf": True}
                return {"status": 200, "text": body, "content": body.encode(), "is_pdf": False}
        return {"status": 200, "text": "", "content": b"", "is_pdf": False}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TAXMATCH_DATA_DIR", str(tmp_path))
    c = dbmod.connect()
    yield c
    c.close()


def _car_routes(years=("2026", "2027"), vehicles=True, with_fee=("2027",)):
    def chk(_form, y=None):
        return ""
    routes = [("getuserdata", f"<x><afm>{AFM}</afm></x>"), ("getDownTime", "ok"),
              ("get5Years", ",".join(f'{{"validfiscalyear":"{y}"}}' for y in years)),
              ("getAllVehicles", "Επιλογή όλων" if vehicles else "[]")]
    for y in years:
        routes.append((f"getChkAfmArkyklPeriod/{AFM}/", None))
    routes = [r for r in routes if r[1] is not None]
    routes.append(("getPrintPDF3", PDF))
    return routes, with_fee


def test_traffic_fees_notice_and_no_vehicles(tmp_path):
    routes, with_fee = _car_routes()
    http = FakeHttp(routes)
    orig = http.follow

    def follow(method, url, form=None, headers=None):
        if "getChkAfmArkyklPeriod" in url:
            y = url.rsplit("/", 2)[-2]
            return {"status": 200, "text": f"<fiscalyear>{y}</fiscalyear>" if y in with_fee else "<x/>", "content": b""}
        return orig(method, url, form, headers)
    http.follow = follow
    res = personal.traffic_fees(http, [2027, 2026], lambda y: tmp_path / str(y - 1))
    assert res[2027]["status"] == "notice" and res[2027]["pdf"].endswith(f"ΤΕΛΗ_ΚΥΚΛΟΦΟΡΙΑΣ_{AFM}_2027.pdf")
    assert res[2026]["status"] == "not_found"
    e = personal.traffic_entry(2027, res[2027])
    assert e["status"] == "pending" and e["periods"][0]["period_end"] == "2026-12-31"
    assert e["periods"][0]["amounts"]["submission_pdf"]

    none = FakeHttp(_car_routes(vehicles=False)[0])
    res = personal.traffic_fees(none, [2027], lambda y: None)
    assert res[2027]["status"] == "no_vehicles"
    assert personal.traffic_entry(2027, res[2027])["status"] == "not_obligated"


_ETAK_ENTRY = "<script>x('_afrLoop', 'L00P'); y('_afrPage', '', 'W1');</script>"
_FORM = '<form id="f1" action="/etak/faces/main.jspx?_adf.ctrl-state=s1"><input name="javax.faces.ViewState" value="!1">'
_MAIN = (_FORM + '<select id="pt1:yearSelect::content"><option value="0" selected>2026</option>'
         '<option value="1">2025</option></select><a id="pt1:clPrintEkk2022">Εκτύπωση Εκκαθαριστικού</a>')


def test_enfia_notice_and_property(tmp_path):
    def post(form):
        if form and form.get("event", "").startswith("pt1:clPrintEkk"):
            return PDF
        return _MAIN
    http = FakeHttp([("/etak/faces", post), ("/etak/", _ETAK_ENTRY + _FORM + '"pt1:cbOpenOwnAFM"'),
                     ("getuserdata", '{"afm":"%s"}' % AFM), ("getPrintPeriousiaki", PDF), ("myPROPERTY", "")])
    res = personal.enfia(http, [2026, 2024], lambda y: tmp_path / str(y))
    assert res[2026]["status"] == "notice" and res[2026]["pdf"].endswith("ΕΝΦΙΑ_ΕΚΚΑΘΑΡΙΣΤΙΚΟ_2026.pdf")
    assert res[2026]["property_pdf"].endswith("ΠΕΡΙΟΥΣΙΑΚΗ_ΚΑΤΑΣΤΑΣΗ_2026.pdf")
    assert res[2024]["status"] == "not_obligated"                              # το έτος δεν υπάρχει στη λίστα
    e = personal.enfia_entry(2026, res[2026])
    assert len(e["periods"]) == 12 and e["periods"][-1]["period_end"].startswith("2027-02")


def test_payment_rules_liability_from_checks_and_debts(conn):
    service.add(conn, AFM, "ΠΑΠΑΣ ΓΙΑΝΝΗΣ")
    conn.execute("UPDATE businesses SET activity_state='none', legal_form='ΙΔΙΩΤΗΣ' WHERE afm=?", (AFM,))
    y = date.today().year
    easycheck.store_results(conn, AFM, y, [personal.enfia_entry(y, {"status": "notice"})])
    easycheck.store_results(conn, AFM, y, [personal.traffic_entry(y + 1, {"status": "no_vehicles"})])
    b = next(x for x in deadlines._businesses(conn) if x["afm"] == AFM)
    assert obligations.liability("enfia", b, f"{y}-05")[0] == "yes"
    assert obligations.liability("enfia", b, f"{y + 1}-02")[0] == "yes"          # 12η δόση του ίδιου εκκαθαριστικού
    assert obligations.liability("traffic_fees", b, f"{y}-12")[0] == "no"
    sub = obligations.submission("enfia", b, f"{y}-05")
    assert sub["status"] == "pending" and "Λήψη οφειλών" in sub["label"]      # χωρίς λήψη οφειλών: άγνωστη εξόφληση
    # με πετυχημένη λήψη οφειλών και καμία ανοιχτή οφειλή ΕΝΦΙΑ → εξοφλήθηκε (ρητό αίτημα 2026-11-10)
    conn.execute("INSERT INTO debt_sync(afm, synced_at, status) VALUES (?,?,?)", (AFM, "x", "ok"))
    b = next(x for x in deadlines._businesses(conn) if x["afm"] == AFM)
    assert obligations.submission("enfia", b, f"{y}-05")["status"] == "submitted"
    conn.execute("INSERT INTO aade_debts(afm, kind, ref, title, total, fetched_at) VALUES (?,?,?,?,?,?)",
                 (AFM, "debt", "e", "ΕΝΦΙΑ ΦΥΣΙΚΩΝ ΠΡΟΣΩΠΩΝ", 80.0, "x"))
    b = next(x for x in deadlines._businesses(conn) if x["afm"] == AFM)
    assert obligations.submission("enfia", b, f"{y}-05")["label"].startswith("Ανοιχτή οφειλή")
    conn.execute("DELETE FROM aade_debts WHERE afm=?", (AFM,))
    conn.execute("DELETE FROM debt_sync WHERE afm=?", (AFM,))
    b = next(x for x in deadlines._businesses(conn) if x["afm"] == AFM)
    assert obligations.liability("income_tax_instalment", b, f"{y}-09")[0] == "maybe"
    conn.execute("INSERT INTO aade_debts(afm, kind, ref, title, total, fetched_at) VALUES (?,?,?,?,?,?)",
                 (AFM, "out", "1", "ΦΟΡΟΣ ΕΙΣΟΔΗΜΑΤΟΣ ΦΥΣΙΚΩΝ ΠΡΟΣΩΠΩΝ", 120.0, "x"))
    b = next(x for x in deadlines._businesses(conn) if x["afm"] == AFM)
    assert obligations.liability("income_tax_instalment", b, f"{y}-09") == ("yes", "ανοιχτή οφειλή στις οφειλές ΑΑΔΕ")


def test_check_entries_without_credentials_does_nothing(conn):
    service.add(conn, AFM, "ΠΑΠΑΣ")
    assert personal.check_entries(conn, AFM, 2026) == {}
    credentials.set_(conn, AFM, "u", "p")
    assert personal.check_entries(conn, AFM, 2026, login_fn=lambda u, p: {"ok": False}) == {}
