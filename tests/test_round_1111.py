"""Γύρος 2026-11-11: απόκρυψη ειδοποιήσεων, μέλη ανά χρήση από την σελίδα Μητρώου (port ScanMyData
`members_registry`), PDF «Σε αναμονή» στο myDATA. Ψεύτικα ΑΦΜ/ονόματα — καμία πραγματική σελίδα."""
from __future__ import annotations

import json

import pytest

from taxmatch import db as dbmod
from taxmatch import members, notices, settings_store
from taxmatch.extraction import llm_extract as L
from taxmatch.mydata import pdfs

CO = "094259216"
A, B, C = "111111114", "121212129", "131313133"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TAXMATCH_DATA_DIR", str(tmp_path))
    c = dbmod.connect()
    yield c
    c.close()


# ------------------------------------------------------------------ ειδοποιήσεις
def test_any_notice_can_be_hidden_and_reappears_when_text_changes(conn):
    n1 = {"level": "warn", "text": "Κάτι για 2 πελάτες"}
    n2 = {"level": "warn", "text": "Κάτι για 3 πελάτες"}
    notices.hide(conn, n1)
    assert notices.visible(conn, [n1, n2]) == [n2]


def test_hiding_the_llm_notice_clears_the_error(conn):
    settings_store.set_value(conn, "openrouter_api_key", "x")
    settings_store.set_value(conn, "llm_provider", "openrouter")
    settings_store.set_value(conn, "llm_last_error", "Όλα τα άρθρα απέτυχαν στην ανάλυση (δείτε το Αρχείο καταγραφής).")
    found = [n for n in notices.collect(conn) if n.get("dismiss") == "llm"]
    assert found and found[0]["level"] == "danger"
    notices.hide(conn, found[0])
    assert settings_store.get(conn, "llm_last_error") == ""
    assert not [n for n in notices.collect(conn) if n.get("dismiss") == "llm"]


def test_danger_opens_the_panel_only_once_across_restarts(conn):
    n = {"level": "danger", "text": "Λάθος κωδικοί"}
    assert notices.unseen_danger(conn, [n]) == [n]
    assert notices.unseen_danger(conn, [n]) == []          # επόμενη εκκίνηση: ίδιο σφάλμα, δεν ανοίγει ξανά


def test_requeue_own_failures_clears_the_stale_red_error(conn):
    conn.execute("INSERT INTO articles(url_hash, url, title, source, extraction_status, extraction_tries, extraction_error, "
                 "fetched_at) VALUES ('h1','https://x/1','t','s','failed',3,"
                 "'HTTP 400: ''models'' array must have 3 items or fewer', '2026-11-10T00:00:00Z')")
    settings_store.set_value(conn, "llm_last_error", "Όλα τα άρθρα απέτυχαν στην ανάλυση (δείτε το Αρχείο καταγραφής).")
    assert L.requeue_own_failures(conn) == 1
    assert conn.execute("SELECT extraction_status FROM articles").fetchone()[0] == "pending"
    assert settings_store.get(conn, "llm_last_error") == ""


# ------------------------------------------------------------------ μέλη (σελίδα Μητρώου ΑΑΔΕ)
def _row(*cells: str) -> str:
    return '<tr class="tblRow1">' + "".join(f'<td align="left">{c}</td>' for c in cells) + "</tr>"


REGISTRY = f"""<html><body>
<table><tr class="tblHeader"><td><div>Σχετιζόμενος ΑΦΜ</div></td><td><div>Επώνυμο Όνομα Πατρώνυμο</div></td>
<td><div>Είδος σχέσης</div></td><td><div>Ημ/νία έναρξης σχέσης</div></td><td><div>Ημ/νία διακοπής σχέσης</div></td></tr>
{_row(A, "ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ", "ΔΙΑΧΕΙΡΙΣΤΗΣ", "05/07/2021", "")}
{_row(B, "ΓΕΩΡΓΙΟΥ ΜΑΡΙΑ", "ΔΙΑΧΕΙΡΙΣΤΗΣ", "24/05/2019", "05/07/2021")}
{_row(C, "ΛΟΓΙΣΤΗΣ ΤΡΙΤΟΣ", "ΛΟΓΙΣΤΗΣ", "24/05/2019", "")}
</table>
<table><tr class="tblHeader table-column-style"><td><div>ΑΦΜ μέλους</div></td><td><div>Επώνυμο Όνομα Πατρώνυμο</div></td>
<td><div>Ημ/νία έναρξης μέλους</div></td><td><div>Ημ/νία διακοπής μέλους</div></td><td><div>Είδος συμμετοχής</div></td>
<td><div>Ποσοστό συμμετοχής</div></td><td><div>Χώρα</div></td></tr>
{_row(A, "ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ", "24/05/2019", "", "Ε.Μ.", "99", "/")}
{_row(B, "ΓΕΩΡΓΙΟΥ ΜΑΡΙΑ", "24/05/2019", "", "Ε.Μ.", "1", "/")}
{_row(CO, "Η ΙΔΙΑ Η ΕΤΑΙΡΕΙΑ", "24/05/2019", "", "Ε.Μ.", "0", "/")}
</table></body></html>"""


def test_registry_page_gives_the_full_history_per_member():
    found = {m["afm"]: m for m in members.parse_registry_html(REGISTRY, CO)}
    assert set(found) == {A, B}                     # ο λογιστής (άσχετη σχέση) και η ίδια η εταιρεία όχι
    assert found[A]["role"] == "Ετερόρρυθμο Μέλος · ΔΙΑΧΕΙΡΙΣΤΗΣ" and found[A]["share"] == "99%"
    assert found[A]["since"] == "24/05/2019" and found[A]["until"] == ""
    # η Β έμεινε μέλος — μόνο η διαχείριση έληξε
    assert found[B]["role"] == "Ετερόρρυθμο Μέλος" and found[B]["until"] == ""
    assert ("ΔΙΑΧΕΙΡΙΣΤΗΣ", "05/07/2021") in [(s["role"], s["until"]) for s in found[B]["spans"]]


def test_members_per_tax_year_with_role_changes(conn):
    conn.execute("INSERT INTO businesses(afm, name, legal_form, created_at, updated_at) "
                 "VALUES (?, 'ΔΟΚΙΜΑΣΤΙΚΗ ΙΚΕ', 'ΙΚΕ', 'now', 'now')", (CO,))
    members.store(conn, CO, members.parse_registry_html(REGISTRY, CO), "aade")
    by_year = {y: {m["member_afm"]: m for m in members.company_members(conn, CO, y)} for y in (2019, 2020, 2021, 2022)}
    assert by_year[2019][A]["role"] == "Ετερόρρυθμο Μέλος" and by_year[2019][A]["movement"] == "Εισήλθε 24/05/2019"
    assert by_year[2020][B]["role"] == "Ετερόρρυθμο Μέλος · ΔΙΑΧΕΙΡΙΣΤΗΣ"
    assert "ΔΙΑΧΕΙΡΙΣΤΗΣ από 05/07/2021" in by_year[2021][A]["movement"]
    assert "ΔΙΑΧΕΙΡΙΣΤΗΣ έως 05/07/2021" in by_year[2021][B]["movement"]
    assert by_year[2022][B]["role"] == "Ετερόρρυθμο Μέλος" and by_year[2022][B]["movement"] == ""


def test_registry_falls_back_to_json_when_the_page_has_no_tables():
    class Http:
        def __init__(self):
            self.urls = []

        def follow(self, method, url, **_k):
            self.urls.append(url)
            if url.endswith("getMelh/" + CO):
                return {"text": json.dumps([{"afmmelous": A, "epwnymia": "Α", "eidossymmetoxhs": "ΕΤΑΙΡΟΣ",
                                             "hmenarxhs": "01/01/2020", "percentage": "100"}])}
            if "getSxeseis" in url:
                return {"text": "[]"}
            return {"text": "<html>login</html>"}
    http = Http()
    found = members.fetch_from_registry(http, CO)
    assert [m["afm"] for m in found] == [A]
    assert any(members.REGISTRY_PAGE in u for u in http.urls)


# ------------------------------------------------------------------ myDATA: «Σε αναμονή»
def _doc(conn, mark: str, classified: bool, url: str = "https://provider.example/inv") -> None:
    detail = {"summary_classifications": [{"type": "E3_561_001"}]} if classified else {}
    conn.execute("INSERT INTO mydata_docs(afm, mark, direction, issue_date, invoice_type, url, detail_json, fetched_at) "
                 "VALUES (?,?,?,?,?,?,?,'now')", (CO, mark, "incoming", "2026-10-05", "1.1", url, json.dumps(detail)))


def test_unclassified_only_puts_the_rest_on_pending_and_marks_download_them(conn):
    _doc(conn, "1", classified=True)
    _doc(conn, "2", classified=False)
    counts = pdfs.download_month(conn, CO, "2026-10", fetch=lambda u: b"%PDF-1.4 x", render=lambda u: {},
                                 unclassified_only=True)
    assert counts["pending"] == 1 and counts["ok"] == 1
    status = dict(conn.execute("SELECT mark, pdf_status FROM mydata_docs").fetchall())
    assert status == {"1": "pending", "2": "ok"}
    assert pdfs.STATUS_LABEL["pending"] == "Σε αναμονή"
    # διπλό κλικ = λήψη του συγκεκριμένου, ακόμη και με το φίλτρο
    pdfs.download_month(conn, CO, "2026-10", fetch=lambda u: b"%PDF-1.4 y", render=lambda u: {}, marks=["1"],
                        unclassified_only=True)
    assert conn.execute("SELECT pdf_status FROM mydata_docs WHERE mark='1'").fetchone()[0] == "ok"


def test_never_downloaded_with_link_shows_pending():
    from taxmatch.gui.mydata_tab import _pdf_state
    assert _pdf_state({"pdf_status": "", "url": "https://x"}) == "pending"
    assert _pdf_state({"pdf_status": "", "url": ""}) == ""
    assert _pdf_state({"pdf_status": "ok", "url": "https://x"}) == "ok"
