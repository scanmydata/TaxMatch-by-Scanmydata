"""Έλεγχος κυρώσεων (aml/sanctions.py) και σύνδεση Γ.Ε.ΜΗ. στον τοπικό browser (aml/gemi_browser.py) — χωρίς δίκτυο."""
import json

from taxmatch.aml import gemi_browser, sanctions, store
from taxmatch.business_profiles import service

AFM = "094259216"
HEADER = ("fileGenerationDate;Entity_LogicalId;Entity_EU_ReferenceNumber;Entity_UnitedNationId;Entity_DesignationDate;"
          "Entity_Remark;Entity_SubjectType;Entity_Regulation_Programme;Entity_Regulation_PublicationUrl;"
          "NameAlias_WholeName;BirthDate_BirthDate;Citizenship_CountryDescription;Address_CountryDescription")


def _row(eid, name, birth="", country="", typ="P"):
    return f"22/09/2026;{eid};EU.{eid};;2022-02-25;;{typ};RUS;https://eur-lex.europa.eu/x;{name};{birth};{country};"


CSV = "﻿" + "\n".join([HEADER, _row(1, "Vladimir Vladimirovich PUTIN", "1952-10-07", "Russia"),
                            _row(1, "Владимир Путин"), _row(2, "Sergey Viktorovich LAVROV", "1950-03-21"),
                            _row(3, "ROSNEFT AERO", typ="E"), _row(4, "Ali"), _row(5, "Charalambos TESTOPOULOS")])


def test_transliteration_and_skeleton():
    assert sanctions.transliterate("Χαράλαμπος Γεωργίου") == "CHARALAMPOS GEORGIOU"
    # διαφορετικές λατινοποιήσεις του ίδιου ονόματος → ίδιος σκελετός
    assert sanctions.name_tokens("ΧΑΡΑΛΑΜΠΟΣ") == sanctions.name_tokens("Haralambos")
    assert sanctions.name_tokens("ΔΟΚΙΜΑΣΤΙΚΗ ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε") == ["DOKIMASTIKI"]    # τύπος εταιρείας/μονογράμματα έξω


def test_index_groups_aliases_per_entity():
    idx = sanctions.build_index(CSV)
    assert idx["generated"] == "22/09/2026"
    putin = next(e for e in idx["entities"] if e["id"] == "1")
    assert putin["names"] == ["Vladimir Vladimirovich PUTIN", "Владимир Путин"] and putin["birth"] == ["1952-10-07"]


def test_screen_finds_greek_spelling_and_avoids_false_positives():
    idx = sanctions.build_index(CSV)
    res = {r["name"]: r["hits"] for r in sanctions.screen(
        [("x", "ΒΛΑΝΤΙΜΙΡ ΠΟΥΤΙΝ"), ("x", "ΣΕΡΓΚΕΪ ΛΑΒΡΟΦ"), ("x", "ROSNEFT"), ("x", "ΑΛΗΣ ΠΑΠΑΔΟΠΟΥΛΟΣ"),
         ("x", "ΧΑΡΑΛΑΜΠΟΣ ΤΕΣΤΟΠΟΥΛΟΣ"), ("x", "ΓΙΩΡΓΟΣ ΤΕΣΤΟΠΟΥΛΟΣ"), ("x", "ΒΛΑΝΤΙΜΙΡ ΠΑΠΑΔΟΠΟΥΛΟΣ")], idx)}
    assert res["ΒΛΑΝΤΙΜΙΡ ΠΟΥΤΙΝ"][0]["id"] == "1"
    assert res["ΣΕΡΓΚΕΪ ΛΑΒΡΟΦ"][0]["id"] == "2"
    assert res["ROSNEFT"][0]["id"] == "3"                      # μία λέξη: μόνο ακριβής, μακριά λέξη
    assert res["ΧΑΡΑΛΑΜΠΟΣ ΤΕΣΤΟΠΟΥΛΟΣ"][0]["id"] == "5"       # Χαράλαμπος ~ Charalambos
    assert res["ΑΛΗΣ ΠΑΠΑΔΟΠΟΥΛΟΣ"] == []                      # «Ali» (σύντομο ψευδώνυμο) δεν πιάνει κάθε Αλή
    assert res["ΓΙΩΡΓΟΣ ΤΕΣΤΟΠΟΥΛΟΣ"] == []                    # κοινό επώνυμο δεν αρκεί
    assert res["ΒΛΑΝΤΙΜΙΡ ΠΑΠΑΔΟΠΟΥΛΟΣ"] == []


def test_screen_client_records_evidence(conn):
    service.add(conn, AFM, "ΔΟΚΙΜΗ ΙΚΕ")
    store.save_profile(conn, AFM, pep_status="no", rep={"name": "ΒΛΑΝΤΙΜΙΡ ΠΟΥΤΙΝ"},
                       ubos=[{"name": "ΓΙΩΡΓΟΣ ΝΙΚΟΛΑΟΥ", "afm": "111111111"}])
    idx = sanctions.build_index(CSV)
    res = sanctions.screen_client(conn, AFM, idx)
    assert res["result"] == "possible" and [h["role"] for h in res["hits"]] == ["Νόμιμος εκπρόσωπος"]
    assert [s["role"] for s in res["subjects"]] == ["Πελάτης", "Νόμιμος εκπρόσωπος", "Πραγματικός δικαιούχος"]
    last = sanctions.latest_screening(conn, AFM)
    assert last["result"] == "possible" and "22/09/2026" in last["source"]
    sanctions.set_review_note(conn, last["id"], "ψευδώς θετικό — άλλη ημ. γέννησης")
    html = sanctions.screening_html(conn, AFM, sanctions.latest_screening(conn, AFM))
    assert "Vladimir Vladimirovich PUTIN" in html and "ψευδώς θετικό" in html and "άρθρο 27" in html


def test_refresh_keeps_old_list_when_download_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(sanctions, "index_path", lambda: tmp_path / "eu.json")
    big = "﻿" + "\n".join([HEADER] + [_row(i, f"Person Number{i} TEST") for i in range(150)])
    idx = sanctions.refresh_index(fetch=lambda url: big)
    assert len(idx["entities"]) == 150 and idx["downloaded_at"]
    assert sanctions.refresh_index(fetch=lambda url: 1 / 0) == idx          # φρέσκια: καμία λήψη

    def boom(url):
        raise OSError("offline")
    stale = sanctions.refresh_index(fetch=boom, force=True)
    assert stale["stale"] is True and len(stale["entities"]) == 150
    assert "ΠΑΛΙΑ ΕΚΔΟΣΗ" in sanctions.source_label(stale)


def test_google_url_has_name_only():
    url = sanctions.google_query_url("ΔΟΚΙΜΗ ΙΚΕ")
    assert url.startswith("https://www.google.com/search?q=") and AFM not in url


# ------------------------------------------------------------------ Γ.Ε.ΜΗ.
def test_gemi_credentials_encrypted(conn):
    assert gemi_browser.set_credentials(conn, AFM, "gemiuser", "G3mi!") is True
    row = conn.execute("SELECT gemi_pass FROM aml_gemi_credentials WHERE afm=?", (AFM,)).fetchone()
    assert "G3mi" not in row["gemi_pass"]
    gemi_browser.set_credentials(conn, AFM, "gemiuser2", "")
    assert gemi_browser.get_credentials(conn, AFM) == ("gemiuser2", "G3mi!")
    assert gemi_browser.user_of(conn, AFM) == "gemiuser2"


class FakeCdp:
    def __init__(self, result):
        self.result, self.calls, self.expr = result, [], ""

    def evaluate(self, expression, await_promise=False):
        self.expr = expression
        return json.dumps(self.result)

    def call(self, method, params=None):
        self.calls.append(method)
        return {}


def test_gemi_login_in_page_success_and_failure():
    ok = FakeCdp({"status": 200, "message": "", "username": "user1"})
    assert gemi_browser.login_in_page(ok, "user1", 'p"w') == {"ok": True, "reason": ""}
    assert ok.calls == ["Page.reload"]
    assert '"p\\"w"' in ok.expr and "/api/welcome/login?lang=el" in ok.expr     # JSON literal — όχι σπασμένο JS
    bad = FakeCdp({"status": 400, "message": "Ελέγξτε πάλι τα στοιχεία πρόσβασης", "username": ""})
    r = gemi_browser.login_in_page(bad, "user1", "x")
    assert r["reason"] == "InvalidCredentials" and "Ελέγξτε" in r["message"] and bad.calls == []
    assert gemi_browser.login_in_page(FakeCdp({"reason": "PageError"}), "u", "p")["reason"] == "PageError"


def test_gemi_open_without_browser(monkeypatch):
    monkeypatch.setattr(gemi_browser, "find_browser", lambda: None)
    assert gemi_browser.open_logged_in("u", "p")["reason"] == "NoBrowser"
