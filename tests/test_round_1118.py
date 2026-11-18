"""Γύρος 2026-11-18: ρυθμίσεις/εξωδικαστικός ΑΑΔΕ (TaxSystem aade-payment-match), ανάγκη ΑΠΔ ΤΕΚΑ από την ΕΡΓΑΝΗ
(TaxSystem efka-teka-apd-need), Gemini API, γρήγορο κλείσιμο ειδοποιήσεων. Ψεύτικα δεδομένα — καμία πραγματική σελίδα."""
from __future__ import annotations

import io
import time

import pytest

from taxmatch import db as dbmod
from taxmatch import debts, notices, obligations, settings_store, teka_need
from taxmatch.extraction import llm_extract as L

AFM = "094259216"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TAXMATCH_DATA_DIR", str(tmp_path))
    c = dbmod.connect()
    yield c
    c.close()


# ------------------------------------------------------------------ ρύθμιση / εξωδικαστικός
def test_arrangement_summary_counts_only_leading_zero_instalments_as_paid():
    fields = {"ΔΟΥ": "ΚΕΦΟΔΕ", "Τύπος Ρύθμισης": "ΕΞΩΔΙΚΑΣΤΙΚΟΣ ΜΗΧΑΝΙΣΜΟΣ Ν.4738/2020", "Α/Α": "12209",
              "Ημ/νία Ρύθμισης": "01/03/2026", "Ρυθμιζόμενο Ποσό Βασικής Οφειλής": "1.000,00",
              "Ρυθμιζόμενες Προσαυξήσεις,Τόκοι,Τέλη": "0,00", "Συνολικό Ρυθμιζόμενο Ποσό": "1.000,00",
              "Συνολικό Υπόλοιπο": "600,00"}
    inst = {"headers": ["Α/Α δόσης", "Ημ/νία λήξης", "Υπόλοιπο Ποσό Δόσης", "Ποσό Επιβάρυνσης", "Συνολικό Υπόλοιπο Δόσης"],
            "rows": [["1", "30/09/2026", "0,00", "0,00", "0,00"], ["2", "31/10/2026", "0,00", "0,00", "0,00"],
                     ["3", "30/11/2026", "200,00", "1,50", "201,50"], ["4", "31/12/2026", "200,00", "0,00", "200,00"],
                     ["5", "31/01/2027", "0,00", "0,00", "0,00"]]}
    a = debts.arrangement_summary(fields, inst)
    assert a["typos"].startswith("ΕΞΩΔΙΚΑΣΤΙΚΟΣ") and a["arr_no"] == "12209" and a["basic"] == "1.000,00"
    assert (a["doseis_paid"], a["doseis_pending"], a["doseis_total"]) == (2, 3, 5)   # η τελική 0€ ΔΕΝ είναι πληρωμένη
    assert a["next"] == {"aa": "3", "date": "30/11/2026", "amount": "201,50"} and a["surcharge_total"] == 1.5


# ------------------------------------------------------------------ ΑΠΔ ΤΕΚΑ από την ΕΡΓΑΝΗ
def _p(afm, f, t=None):
    return {"afm": afm, "surname": "Ε" + afm[-2:], "name": "", "from": f, "to": t}


TEKA = {"teka": True, "auxiliary": "ΤΑΜΕΙΟ ΕΠΙΚΟΥΡΙΚΗΣ ΚΕΦΑΛΑΙΟΠΟΙΗΤΙΚΗΣ ΑΣΦΑΛΙΣΗΣ (ΤΕΚΑ)", "amka": "1"}
EFKA = {"teka": False, "auxiliary": "ΚΛΑΔΟΣ ΕΠΙΚΟΥΡΙΚΗΣ ΑΣΦΑΛΙΣΗΣ e-ΕΦΚΑ", "amka": "2"}


def test_teka_need_required_possibly_and_not():
    pers = [_p("111111114", "2026-03-05", "2026-04-28"), _p("121212129", "2026-04-23"), _p("131313133", "2020-01-01")]
    decl = [{"type": "ΨΗΦΙΑΚΗ ΑΝΑΓΓΕΛΙΑ ΛΗΞΗΣ ΕΡΓΑΣΙΑΣ - Καταγγελία Σύμβασης Χωρίς Προειδοποίηση", "status": "",
             "afm": "111111114", "submitted": "2026-05-04"}]
    cards = {"111111114": TEKA, "121212129": EFKA, "131313133": {"teka": False, "auxiliary": "", "amka": "3"}}
    apr = teka_need.assess("2026-04", pers, decl, cards)
    assert apr["status"] == "REQUIRED" and apr["teka_count"] == 1
    assert ("ΑΠΟΛΥΣΗ (καταγγελία σύμβασης)", "2026-04-28") in [(m["type"], m["date"]) for m in apr["movements"]]
    may = teka_need.assess("2026-05", pers, decl, cards)          # αποζημίωση απόλυσης εργαζομένου του ΤΕΚΑ
    assert may["status"] == "POSSIBLY_REQUIRED" and may["severance"]
    jun = teka_need.assess("2026-06", pers, decl, cards)
    assert jun["status"] == "NOT_REQUIRED"                        # πριν το 2022 = όχι ΤΕΚΑ, κενός επικουρικός δεν μετρά
    assert teka_need.assess("2026-06", [], [], {})["status"] == "NO_EMPLOYEES"


def test_teka_need_drives_the_obligation(conn):
    b = {"afm": AFM, "employer": 1, "teka": 0, "teka_need": {"2026-04": "REQUIRED", "2026-06": "NOT_REQUIRED",
                                                              "2026-05": "POSSIBLY_REQUIRED"}}
    assert obligations.liability("apd_teka", b, "2026-04")[0] == "yes"
    assert obligations.liability("apd_teka", b, "2026-05")[0] == "maybe"
    assert obligations.liability("apd_teka", b, "2026-06")[0] == "no"
    assert obligations.liability("apd_teka", b, "2026-07")[0] == "no"          # χωρίς ΕΡΓΑΝΗ: η επιλογή του Προφίλ


def test_ergani_excel_with_wrong_dimension_is_read():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ΑΦΜ", "Επώνυμο", "Ημ/νία Από", "Ημ/νία Έως"])
    ws.append(["111111114", "Α", "05/03/2026", ""])
    buf = io.BytesIO()
    wb.save(buf)
    import zipfile
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:                          # όπως η ΕΡΓΑΝΗ: <dimension ref="A1"/>
        for n in src.namelist():
            data = src.read(n)
            if n.endswith("sheet1.xml"):
                data = data.replace(b'<dimension ref="A1:D2"/>', b'<dimension ref="A1"/>')
            z.writestr(n, data)
    rows = teka_need.xlsx_rows(out.getvalue())
    assert rows and rows[0]["ΑΦΜ"] == "111111114" and teka_need.dmy(rows[0]["Ημ/νία Από"]) == "2026-03-05"


def test_teka_need_stored_and_profile_flag(conn, monkeypatch):
    conn.execute("INSERT INTO businesses(afm, name, created_at, updated_at) VALUES (?, 'ΔΟΚΙΜΗ ΙΚΕ', 'now', 'now')", (AFM,))
    monkeypatch.setattr(teka_need, "_credentials", lambda c, a: [("u", "p")])

    class FakeErgani:
        def __init__(self, _s):
            self.ergodoti_id = "1"

        def login(self, u, p):
            return {"ok": True}

        def personnel(self):
            return [_p("111111114", "2026-01-10")]

        def declarations(self):
            return []

        def employee_card(self, afm):
            return TEKA

        def logout(self):
            pass
    monkeypatch.setattr(teka_need, "Ergani", FakeErgani)
    r = teka_need.check_client(conn, AFM, months=["2026-10", "2026-11"])
    assert r["ok"] and teka_need.stored(conn, AFM)[AFM] == {"2026-10": "REQUIRED", "2026-11": "REQUIRED"}
    assert conn.execute("SELECT teka FROM businesses WHERE afm=?", (AFM,)).fetchone()[0] == 1


# ------------------------------------------------------------------ Gemini
def test_gemini_free_models_and_per_minute_limit():
    assert L.is_free_model("gemini", "gemini-3.5-flash-lite") and L.is_free_model("gemini", "gemma-4-31b-it")
    assert not L.is_free_model("gemini", "gemini-4-argon") and not L.is_free_model("gemini", "gemini-3.1-pro-preview")
    assert L.pick_model("gemini", ["models/x", "gemini-3.8-flash", "gemini-4-argon"], free_only=True) == "gemini-3.8-flash"

    class S:
        def __init__(self, status, text):
            self.status, self.text = status, text

        def post(self, url, headers=None, json=None, timeout=None):
            outer = self

            class R:
                status_code = outer.status
                text = outer.text
                headers: dict = {}
            return R()
    url = L.PROVIDERS["gemini"]["url"]
    with pytest.raises(L.LLMError) as e:
        L.LLMClient("gemini", "k", "gemini-3.5-flash-lite", url, S(
            429, '{"error":{"status":"RESOURCE_EXHAUSTED","message":"Quota exceeded for metric '
                 'generate_content_free_tier_requests, limit: 15 PerMinute","details":[{"retryDelay":"27s"}]}}'
        )).complete_json("s", "u")
    assert e.value.kind == "rate_limit" and e.value.retry_after == 27.0
    with pytest.raises(L.LLMError) as e:
        L.LLMClient("gemini", "k", "gemini-3.5-flash-lite", url, S(
            429, '{"error":{"status":"RESOURCE_EXHAUSTED","message":"Quota exceeded: requests per day"}}')).complete_json("s", "u")
    assert e.value.kind == "credits"
    with pytest.raises(L.LLMError) as e:
        L.LLMClient("gemini", "k", "m", url, S(400, '{"error":{"message":"API key not valid. Please pass a valid API key."}}')
                    ).complete_json("s", "u")
    assert e.value.kind == "auth"


def test_gemini_key_counts_as_configured_llm(conn):
    settings_store.set_value(conn, "llm_provider", "gemini")
    assert any("API key για gemini" in n["text"] for n in notices.collect(conn))
    settings_store.set_value(conn, "gemini_api_key", "k")
    assert not any("API key" in n["text"] for n in notices.collect(conn))


# ------------------------------------------------------------------ ταχύτητα ειδοποιήσεων
def test_ui_writes_do_not_recompute_mydata_flags(conn, monkeypatch):
    from taxmatch.mydata import analysis
    calls = []
    real = analysis._month_flags
    monkeypatch.setattr(analysis, "_month_flags", lambda c: calls.append(1) or real(c))
    analysis._FLAGS_CACHE._conn = None
    analysis.month_flags(conn)
    settings_store.set_value(conn, "notice_hidden", "abc")          # όπως το «✕»
    analysis.month_flags(conn)
    assert len(calls) == 1
    conn.execute("INSERT INTO mydata_docs(afm, mark, direction, issue_date, invoice_type, fetched_at) "
                 "VALUES (?, '1', 'incoming', '2026-10-01', '1.1', 'now')", (AFM,))
    analysis.month_flags(conn)
    assert len(calls) == 2                                           # νέο παραστατικό = νέος υπολογισμός
