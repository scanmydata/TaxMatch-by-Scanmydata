"""Γύρος 2026-11-04: διασταύρωση δηλώσεων παρακράτησης με myDATA, τέλος παρεπιδημούντων στο Προφίλ, Meta στην ΑΚΕΕΔ,
ειδοποίηση ΦΠΑ σε μη υπόχρεο, αόρατη λήψη PDF «μόνο online». Ψεύτικα δεδομένα — καμία πραγματική δήλωση."""
from __future__ import annotations

import json
from datetime import date

import pytest

from taxmatch import db as dbmod
from taxmatch import deadlines, fee_portals, notices, obligations, withholding_check as wc
from taxmatch.business_profiles import service
from taxmatch.mydata import analysis, pdfs

AFM = "094259216"
SUPPLIER_A, SUPPLIER_B = "123456783", "010101016"

BUSINESS_TEXT = """Δήλωση Παρακρατούμενων και Προκαταβλητέων Φόρων σε αμοιβές από Επιχειρηματική Δραστηριότητα
ΣΥΝΤ/ΤΗΣ     ΕΝΔΕΙΞΗ       ΠΕΡΙΟΔΟΣ      ΑΚΑΘΑΡΙΣΤΕΣ         ΚΡΑΤΗΣΕΙΣ             ΚΑΘΑΡΕΣ                  ΠΟΣΟ
            ΠΕΡΙΟΔΟΥ       ΑΠΟΔΟΣΗΣ         ΑΜΟΙΒΕΣ                                ΑΜΟΙΒΕΣ                 ΦΟΡΟΥ
               Μήνας             Μάρτιος          1.150,00                 0,00               1.150,00          230,00    0,00
"""
CONTRACTOR_TEXT = f""" Αξία εκτελεσθέντος έργου, αντίτιμο ενοικίασης, προμήθεια 3%:            2.021,00
 Φόρος που αναλογεί 3%                                                         60,63
Α/Α       Ημερομηνία             Α.Φ.Μ.
1     14/3/2026            {SUPPLIER_A}           ΧΧ    3%- Από       781,00     23,43     0,00     23,43
2     17/3/2026            {SUPPLIER_B}           ΧΧ    3%- Από     1.240,00     37,20     0,00     37,20
"""


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TAXMATCH_DATA_DIR", str(tmp_path))
    c = dbmod.connect()
    yield c
    c.close()


def _doc(conn, mark, issuer, cat, tax, net, ym="2026-03", direction="incoming", itype="1.1", vat_class=0):
    conn.execute("INSERT INTO mydata_docs(afm, mark, direction, invoice_type, issue_date, issuer_vat, issuer_name, "
                 "counter_vat, net, withheld_json, vat_class, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (AFM, mark, direction, itype, f"{ym}-10", issuer, "Χ", AFM, net,
                  json.dumps({cat: tax} if cat else {}), vat_class, "x"))


def test_parsers_business_and_contractors():
    b = wc.parse("ΦΜΥ_ΕΠΙΧ", BUSINESS_TEXT)
    assert (b["gross"], b["tax"]) == (1150.0, 230.0)
    c = wc.parse("ΕΡΓΟΛΑΒΩΝ", CONTRACTOR_TEXT)
    assert (c["gross"], c["tax"]) == (2021.0, 60.63)
    assert [(r["afm"], r["value"], r["tax"]) for r in c["rows"]] == [(SUPPLIER_A, 781.0, 23.43), (SUPPLIER_B, 1240.0, 37.2)]


def test_reconcile_contractors_per_supplier(conn, monkeypatch, tmp_path):
    service.add(conn, AFM, "ΑΛΦΑ")
    _doc(conn, "1", SUPPLIER_A, "4", 23.43, 781.0)
    _doc(conn, "2", "020202024", "4", 9.0, 300.0)                   # στο myDATA αλλά όχι στη δήλωση
    _doc(conn, "3", SUPPLIER_A, "3", 50.0, 250.0)                   # άλλη κατηγορία (20%) — άλλη δήλωση
    monkeypatch.setattr(wc, "pdf_text", lambda _p: CONTRACTOR_TEXT)
    res = wc.reconcile(conn, AFM, "2026-03", "ΕΡΓΟΛΑΒΩΝ", tmp_path / "x.pdf")
    assert res["ok"] is False and res["declared"] == 60.63 and res["mydata"] == 32.43
    assert res["missing_in_pdf"] == ["020202024"] and res["missing_in_mydata"] == [SUPPLIER_B]
    assert "όχι στη δήλωση" in wc.summary_text(res)
    monkeypatch.setattr(wc, "pdf_text", lambda _p: BUSINESS_TEXT.replace("230,00", "50,00"))
    ok = wc.reconcile(conn, AFM, "2026-03", "ΦΜΥ_ΕΠΙΧ", tmp_path / "x.pdf")
    assert ok["ok"] is True and "✓" in wc.summary_text(ok)


def test_parepid_profile_flag_overrides_detection(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    conn.execute("UPDATE businesses SET activity_state='active' WHERE afm=?", (AFM,))
    assert "ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ" not in fee_portals.wanted(conn, AFM, 2026)
    conn.execute("UPDATE businesses SET parepid=1 WHERE afm=?", (AFM,))
    assert "ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ" in fee_portals.wanted(conn, AFM, 2026)
    b = next(x for x in deadlines._businesses(conn) if x["afm"] == AFM)
    assert obligations.liability("parepid_fee", b, "2026-03")[0] == "yes"
    conn.execute("UPDATE businesses SET parepid=0 WHERE afm=?", (AFM,))
    b = next(x for x in deadlines._businesses(conn) if x["afm"] == AFM)
    assert obligations.liability("parepid_fee", b, "2026-03")[0] == "no"


def test_meta_counts_as_ad_platform_and_vat_mismatch_notice(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    _doc(conn, "m1", "IE9692928F", None, 0, 100.0, itype="14.3")
    assert analysis.google_ads_docs(conn, AFM, "2026-03") == 1
    conn.execute("UPDATE businesses SET vat_subject=0 WHERE afm=?", (AFM,))
    y = date.today().year
    _doc(conn, "v1", AFM, None, 0, 100.0, ym=f"{y}-01", direction="outgoing", vat_class=1)
    assert notices.vat_mismatch_clients(conn) == {AFM: 1}
    assert any(n.get("dismiss") == "vat_mismatch" for n in notices.collect(conn))


def test_viewer_docs_get_one_render_pass(conn, tmp_path):
    service.add(conn, AFM, "ΑΛΦΑ")
    conn.execute("INSERT INTO mydata_docs(afm, mark, direction, invoice_type, issue_date, issuer_vat, issuer_name, "
                 "counter_vat, net, total, series, aa, url, pdf_status, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (AFM, "9", "incoming", "1.1", "2026-03-02", SUPPLIER_A, "Χ", AFM, 10, 12.4, "Α", "1",
                  "https://www.eskap.gr/invoice.php?authentication_code=abc", "viewer", "x"))
    calls = []

    def render(urls):
        calls.append(urls)
        return {urls[0]: b"%PDF-1.4 fake"}
    out = pdfs.download_month(conn, AFM, "2026-03", fetch=lambda u: b"", render=render)
    assert calls == [["https://www.eskap.gr/invoice_print.php?authentication_code=abc"]] and out["ok"] == 1
    row = conn.execute("SELECT pdf_status, render_tried FROM mydata_docs WHERE mark='9'").fetchone()
    assert (row["pdf_status"], row["render_tried"]) == ("ok", 1)


def test_plastic_fees_in_notices_and_filters(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    y = date.today().year
    for mark, cat, month in (("p1", "8", "02"), ("p2", "16", "05")):
        conn.execute("INSERT INTO mydata_docs(afm, mark, direction, invoice_type, issue_date, issuer_vat, counter_vat, net, "
                     "taxes_json, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (AFM, mark, "outgoing", "11.1", f"{y}-{month}-03", AFM, "", 10.0,
                      json.dumps({"fees": {cat: 0.09}}), "x"))
    found = notices.plastic_clients(conn)
    assert found == {"plastic_bag": {AFM: {1}}, "plastics": {AFM: {2}}}
    conn.execute("INSERT INTO obligation_checks(afm, year, obligation, period, status, checked_at) VALUES (?,?,?,?,?,?)",
                 (AFM, y, "ΠΛΑΣΤΙΚΑ", "2ο Τρίμηνο", "not_submitted", "x"))
    n = next(n for n in notices.collect(conn) if n.get("dismiss") == "plastic")
    assert n["level"] == "warn" and n["afms"] == [AFM] and "ΔΕΝ υποβλήθηκε" in n["text"]
    notices.mark_plastic_read(conn)
    assert not any(n.get("dismiss") == "plastic" for n in notices.collect(conn))


# ---------------------------------------------------------------- νέα: αντιστοίχιση, σειρά ανάλυσης, taxheaven
from taxmatch import scope as scope_mod, settings_store  # noqa: E402
from taxmatch.extraction import llm_extract  # noqa: E402
from taxmatch.ingestion import taxheaven_backfill  # noqa: E402
from taxmatch.matching.match import ALL_SCOPE_REASON, match_business  # noqa: E402

_BIZ = {"kads": ["56101101"], "books_category": "Β", "vat_subject": 1, "legal_form": "ΑΤΟΜΙΚΗ"}


def test_all_scope_skips_private_and_ceased_clients():
    ex = scope_mod.normalize({"relevant": True, "scope": {"type": "all"}})
    assert match_business(ex, {**_BIZ, "kind": "sole"}).reason == ALL_SCOPE_REASON
    assert match_business(ex, {**_BIZ, "kind": "private"}) is None
    assert match_business(ex, {**_BIZ, "kind": "sole", "ceased": True}) is None
    assert match_business(ex, {**_BIZ, "kind": None}) is not None          # άγνωστο είδος: δεν χάνεται


def test_entity_types_and_employers_only():
    persons = scope_mod.normalize({"scope": {"type": "targeted", "entity_types": ["ιδιώτες", "Ατομικές"]}})
    assert persons["scope"]["entity_types"] == ["ΙΔΙΩΤΕΣ", "ΑΤΟΜΙΚΕΣ"]
    assert match_business(persons, {**_BIZ, "kind": "private"}).confidence == 1.0
    assert match_business(persons, {**_BIZ, "kind": "company"}) is None
    payroll = scope_mod.normalize({"scope": {"type": "targeted", "employers_only": True}})
    assert match_business(payroll, {**_BIZ, "kind": "sole", "employer": 1}).reason == "Εργοδότης"
    assert match_business(payroll, {**_BIZ, "kind": "sole", "employer": 0}) is None
    assert match_business(payroll, {**_BIZ, "kind": "sole", "employer": None}) is None    # κανένα βέβαιο κριτήριο


def test_for_matching_kind_and_employer_from_ame(conn):
    service.add(conn, AFM, "ΑΛΦΑ ΙΚΕ")
    conn.execute("UPDATE businesses SET legal_form='ΙΚΕ', activity_state='active' WHERE afm=?", (AFM,))
    conn.execute("INSERT INTO efka_ames(afm, ame, updated_at) VALUES (?, ?, ?)", (AFM, "1234567890", "x"))
    b = next(x for x in service.for_matching(conn) if x["afm"] == AFM)
    assert (b["kind"], b["employer"], b["ceased"]) == ("company", 1, False)


def test_extraction_order_primary_first_courts_last():
    rows = [{"title": "Κάτι στο capital", "source": "capital_all"}, {"title": "ΣτΕ 714/2025 - x", "source": "taxheaven_law"},
            {"title": "Α.1233/2026 - x", "source": "taxheaven_law"}]
    assert [r["title"][:5] for r in sorted(rows, key=llm_extract.priority)] == ["Α.123", "Κάτι ", "ΣτΕ 7"]


def test_prompt_version_reanalyzes_recent_all_articles_once(conn):
    now = dbmod.utcnow()
    for i, typ in enumerate(("all", "targeted")):
        conn.execute("INSERT INTO articles(source, title, url, url_hash, fetched_at, published_at, extraction_status, "
                     "extracted_json) VALUES (?,?,?,?,?,?,?,?)", ("taxheaven_new", f"t{i}", f"u{i}", f"h{i}", now, now,
                                                                 "done", json.dumps({"scope": {"type": typ}})))
    assert llm_extract.reanalyze_for_prompt(conn) == 1
    assert settings_store.get(conn, "llm_prompt_version") == str(llm_extract.PROMPT_VERSION)
    assert llm_extract.reanalyze_for_prompt(conn) == 0


class _Resp:
    def __init__(self, status, text="", url=""):
        self.status_code, self.text, self.url = status, text, url


def test_taxheaven_backfill_fills_numbering_gaps(conn):
    now = dbmod.utcnow()
    for i in (100, 104):
        conn.execute("INSERT INTO articles(source, title, url, url_hash, fetched_at, published_at, extraction_status) "
                     "VALUES (?,?,?,?,?,?,?)", ("taxheaven_new", f"Τίτλος {i}", f"https://www.taxheaven.gr/news/{i}/a",
                                                f"h{i}", now, now, "pending"))
    assert taxheaven_backfill.missing_ids(conn) == [103, 102, 101]
    page = ('<meta property="og:title" content="Δηλώσεις βραχυχρόνιας διαμονής - Έως 13/11">'
            '<meta property="og:url" content="https://www.taxheaven.gr/news/103/dhlwseis">'
            '<meta property="article:published_time" content="2026-11-02T15:17:00+02:00">')

    class S:
        def get(self, url, **_kw):
            n = int(url.split("/news/")[1].split("/")[0])
            return _Resp(200, page, "https://www.taxheaven.gr/news/103/dhlwseis") if n == 103 else _Resp(404)
    out = taxheaven_backfill.backfill(conn, S(), sleep=lambda _s: None)
    assert out == {"missing": 3, "fetched": 1, "new": 1}
    row = conn.execute("SELECT title, published_at FROM articles WHERE url LIKE '%/news/103/%'").fetchone()
    assert row["title"].startswith("Δηλώσεις") and row["published_at"] == "2026-11-02T13:17:00Z"
    assert taxheaven_backfill.missing_ids(conn) == []                      # τα 404 δεν ξαναζητούνται
