"""Κωδικοί TAXISnet ανά πελάτη, VIES, εισαγωγή με κωδικούς, νέες πηγές/φίλτρα, κανόνες ημερολογίου."""
import io
import sqlite3
from datetime import date

import pytest
from openpyxl import Workbook

from taxmatch import db, obligations, pipeline, settings_store
from taxmatch.business_profiles import credentials, import_excel, service, vies
from taxmatch.deadlines import events_between
from taxmatch.ingestion import filters, rss_fetch, sources
from taxmatch.web import create_app
from tests.fakes import FakeResponse, FakeSession

AFM = "094259216"
TOKEN = "tok"
SECRET_USER, SECRET_PASS = "user_SECRET_1", "pass_SECRET_2"


def xlsx(rows):
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def client():
    c = create_app(TOKEN, testing=True).test_client()
    c.get(f"/?t={TOKEN}")
    return c


# ---------------------------------------------------------------- migration
def test_upgrade_from_v1_database_keeps_data(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    for stmt in db._split(db.MIGRATIONS[0]):
        raw.execute(stmt)
    raw.execute("INSERT INTO businesses(afm,name,created_at,updated_at) VALUES ('094259216','ΠΑΛΙΟΣ','x','x')")
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()
    c = db.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert c.execute("SELECT name FROM businesses").fetchone()[0] == "ΠΑΛΙΟΣ"
    assert c.execute("SELECT COUNT(*) FROM client_credentials").fetchone()[0] == 0
    assert "title_key" in [r[1] for r in c.execute("PRAGMA table_info(articles)")]


# ---------------------------------------------------------------- VIES
def vies_session(payload, status=200):
    return FakeSession().route("vies", FakeResponse(json_data=payload, status=status))


def test_vies_cleans_name_and_address():
    s = vies_session({"isValid": True, "name": "ΑΛΦΑ ΑΕ||ALPHA SA", "address": "ΟΔΟΣ 1\n  11111 - ΑΘΗΝΑ"})
    r = vies.lookup_live(AFM, s)
    assert r.valid and r.name == "ΑΛΦΑ ΑΕ" and r.address == "ΟΔΟΣ 1 11111 - ΑΘΗΝΑ"
    assert "/EL/vat/094259216" in s.calls[0][1]


def test_vies_dashes_invalid_and_errors():
    assert vies.lookup_live(AFM, vies_session({"isValid": True, "name": "---", "address": "---"})).name == ""
    assert vies.lookup_live(AFM, vies_session({"isValid": False})).valid is False
    r = vies.lookup_live(AFM, vies_session({}, status=503))
    assert r.error and not r.valid                                  # σφάλμα ≠ «άκυρο ΑΦΜ»
    assert vies.lookup_live("123", FakeSession()).error


def test_lookup_afm_api_local_then_vies_then_error(client, conn, monkeypatch):
    assert client.get("/api/lookup-afm?afm=12").status_code == 400
    monkeypatch.setattr(vies, "lookup", lambda afm, session=None: vies.ViesResult(valid=True, name="ΑΠΟ VIES", address="ΔΙΕΥΘΥΝΣΗ"))
    d = client.get(f"/api/lookup-afm?afm={AFM}").get_json()
    assert d["ok"] and d["source"] == "vies" and d["name"] == "ΑΠΟ VIES" and not d["exists"]
    service.add(conn, AFM, "ΗΔΗ ΣΤΗ ΒΑΣΗ")
    d = client.get(f"/api/lookup-afm?afm={AFM}").get_json()
    assert d["source"] == "local" and d["exists"] and d["name"] == "ΗΔΗ ΣΤΗ ΒΑΣΗ"
    monkeypatch.setattr(vies, "lookup", lambda afm, session=None: vies.ViesResult(error="Το VIES δεν απάντησε"))
    d = client.get("/api/lookup-afm?afm=123456783").get_json()
    assert d["ok"] is False and "χειροκίνητα" in d["error"]


def test_lookup_fills_name_from_vies_when_no_other_source(conn, monkeypatch):
    service.add(conn, AFM)
    monkeypatch.setattr(vies, "lookup", lambda afm, session=None: vies.ViesResult(valid=True, name="ΟΝΟΜΑ VIES", address="ΟΔΟΣ 5"))
    out = service.lookup_and_store(conn, AFM, FakeSession())
    b = service.get(conn, AFM)
    assert out["sources"] == ["VIES"] and out["status"] == "partial"        # χωρίς ΚΑΔ = μερικά
    assert (b["name"], b["address"]) == ("ΟΝΟΜΑ VIES", "ΟΔΟΣ 5")


def test_vies_network_error_keeps_client_pending_not_failed(conn):
    service.add(conn, AFM)
    out = service.lookup_and_store(conn, AFM, FakeSession())               # το stub του conftest επιστρέφει error
    assert out["status"] == "pending" and service.get(conn, AFM)["lookup_status"] == "pending"


def test_vies_not_registered_marks_failed(conn, monkeypatch):
    service.add(conn, AFM)
    monkeypatch.setattr(vies, "lookup", lambda afm, session=None: vies.ViesResult(valid=False))
    assert service.lookup_and_store(conn, AFM, FakeSession())["status"] == "failed"


# ---------------------------------------------------------------- κωδικοί ανά πελάτη
def test_credentials_encrypted_roundtrip_and_blank_password_keeps(conn):
    service.add(conn, AFM)
    assert credentials.set_(conn, AFM, SECRET_USER, SECRET_PASS)
    raw = conn.execute("SELECT taxis_user, taxis_pass FROM client_credentials").fetchone()
    assert raw[0].startswith("enc:1:") and raw[1].startswith("enc:1:")
    assert SECRET_USER not in raw[0] and SECRET_PASS not in raw[1]
    assert credentials.get(conn, AFM) == (SECRET_USER, SECRET_PASS)
    credentials.set_(conn, AFM, "νέος_χρήστης", "")                       # κενός κωδικός = κρατά τον παλιό
    assert credentials.get(conn, AFM) == ("νέος_χρήστης", SECRET_PASS)
    assert not credentials.set_(conn, AFM, "", "")
    credentials.clear(conn, AFM)
    assert credentials.get(conn, AFM) is None


def test_status_map_never_exposes_secrets(conn):
    service.add(conn, AFM)
    credentials.set_(conn, AFM, SECRET_USER, SECRET_PASS)
    m = credentials.status_map(conn)
    assert m[AFM]["complete"] is True and SECRET_USER not in str(m) and SECRET_PASS not in str(m)


def test_credentials_test_records_status(conn):
    service.add(conn, AFM)
    credentials.set_(conn, AFM, "u", "p")
    ok, msg = credentials.test(conn, AFM, login=lambda u, p: {"ok": True})
    assert ok and conn.execute("SELECT check_status FROM client_credentials").fetchone()[0] == "ok"
    ok, msg = credentials.test(conn, AFM, login=lambda u, p: {"ok": False, "reason": "InvalidCredentials"})
    assert not ok and "Λάθος" in msg and conn.execute("SELECT check_status FROM client_credentials").fetchone()[0] == "invalid"
    def boom(u, p):
        raise RuntimeError("δίκτυο")
    assert credentials.test(conn, AFM, login=boom)[0] is False
    credentials.clear(conn, AFM)
    assert not credentials.test(conn, AFM, login=lambda u, p: {"ok": True})[0]


def test_changing_credentials_resets_check_status(conn):
    service.add(conn, AFM)
    credentials.set_(conn, AFM, "u", "p")
    credentials.test(conn, AFM, login=lambda u, p: {"ok": True})
    credentials.set_(conn, AFM, "u", "άλλος")
    assert conn.execute("SELECT check_status FROM client_credentials").fetchone()[0] == ""


def test_bulk_set_skips_unknown_clients(conn):
    service.add(conn, AFM)
    out = credentials.bulk_set(conn, [(AFM, "u", "p"), ("123456783", "u", "p")])
    assert out == {"saved": 1, "skipped_unknown": 1}


def test_client_credentials_win_over_office_account(conn):
    settings_store.set_value(conn, "aade_user", "office_user")
    settings_store.set_value(conn, "aade_pass", "office_pass")
    service.add(conn, AFM)
    service.add(conn, "123456783")
    credentials.set_(conn, AFM, "client_user", "client_pass")
    used = {}

    def fake(user, pwd, afm):
        used[afm] = (user, pwd)
        return {"ok": True, "doy": "Δ", "name": "", "all_tags": {}}
    service.lookup_and_store(conn, AFM, FakeSession(), fake)
    service.lookup_and_store(conn, "123456783", FakeSession(), fake)
    assert used[AFM] == ("client_user", "client_pass")
    assert used["123456783"] == ("office_user", "office_pass")            # fallback


# ---------------------------------------------------------------- Excel με κωδικούς
def test_table_with_taxis_columns():
    res = import_excel.parse_file("x.xlsx", xlsx([
        ["Επωνυμία", "Α.Φ.Μ.", "Όνομα χρήστη TAXISnet", "Κωδικός TAXISnet", "Κλειδί myDATA"],
        ["ΑΛΦΑ", AFM, "alpha_user", "alpha_pass", "0123456789abcdef0123456789abcdef"],
        ["ΒΗΤΑ", "123456783", None, None, None]]))
    a, b = res.rows
    assert (a.taxis_user, a.taxis_pass, a.has_credentials) == ("alpha_user", "alpha_pass", True)
    assert not b.has_credentials and res.credential_count == 1 and res.detected == "table"
    assert "0123456789abcdef" not in repr(res)                            # το κλειδί myDATA δεν διαβάστηκε ποτέ


def test_generic_user_password_columns_only_in_simple_sheet():
    simple = import_excel.parse_rows([("ΑΦΜ", "Επωνυμία", "Όνομα χρήστη", "Κωδικός"), (AFM, "Α", "u1", "p1")])
    assert simple.rows[0].has_credentials
    wide_header = ["ΑΦΜ", "Επωνυμία", "Όνομα χρήστη", "Κωδικός"] + [f"Στήλη {i}" for i in range(9)]
    wide = import_excel.parse_rows([tuple(wide_header), (AFM, "Α", "efka_user", "efka_pass") + (None,) * 9])
    assert not wide.rows[0].has_credentials                               # πλατύς πίνακας: ασαφές, δεν μαντεύουμε


def test_block_format_reads_only_taxis_net_row():
    rows = [
        (None, "00250 ΠΑΡΑΔΕΙΓΜΑ Ο Ε 123456783"),
        (None, "Taxis Net", None, None, "taxis_u", None, "taxis_p"),
        (None, "Ηλεκτρονικά Βιβλία Α.Α.Δ.Ε. (myDATA)", None, None, "mydata_u", None, "mydata_KEY"),
        (None, "00251 ΑΛΛΗ ΕΠΙΧΕΙΡΗΣΗ ΑΕ 094259216"),
        (None, "Taxis Net", None, None, "u2", None, "p2"),
    ]
    res = import_excel.parse_file("codes.xlsx", xlsx(rows))
    assert res.detected == "blocks" and [r.afm for r in res.rows] == ["123456783", "094259216"]
    assert res.rows[0].name == "ΠΑΡΑΔΕΙΓΜΑ Ο Ε" and (res.rows[0].taxis_user, res.rows[0].taxis_pass) == ("taxis_u", "taxis_p")
    assert res.rows[1].taxis_user == "u2"
    assert "mydata_KEY" not in repr(res) and "mydata_u" not in repr(res)


def test_half_credentials_dropped_with_warning_without_leaking():
    res = import_excel.parse_rows([("ΑΦΜ", "Χρήστης TAXISnet", "Κωδικός TAXISnet"), (AFM, "μόνο_χρήστης", None)])
    assert not res.rows[0].taxis_user and any("μόνο χρήστη" in w for w in res.warnings)
    assert all("μόνο_χρήστης" not in w for w in res.warnings)


def test_template_roundtrip():
    res = import_excel.parse_file("t.xlsx", import_excel.template_xlsx())
    assert res.rows == [] and not res.invalid                            # κενό πρότυπο, αναγνωρίζει επικεφαλίδες
    filled = xlsx([import_excel.TEMPLATE_HEADERS, [AFM, "Δοκιμή", "47.11", "u", "p"]])
    r = import_excel.parse_file("t.xlsx", filled).rows[0]
    assert (r.afm, r.kads, r.taxis_user, r.taxis_pass) == (AFM, ["47.11"], "u", "p")


def test_import_result_saves_credentials_also_for_existing_clients(conn):
    service.add(conn, AFM, "Υπάρχων")
    res = import_excel.parse_rows([("ΑΦΜ", "Χρήστης TAXISnet", "Κωδικός TAXISnet"), (AFM, "u", "p"), ("123456783", "u2", "p2")])
    out = service.import_result(conn, res)
    assert out["added"] == 1 and out["credentials"] == 2
    assert credentials.get(conn, AFM) == ("u", "p") and credentials.get(conn, "123456783") == ("u2", "p2")


# ---------------------------------------------------------------- web: κωδικοί, bulk, export
def test_client_credentials_endpoints_and_no_leak(client, conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    client.post(f"/clients/{AFM}/credentials", data={"action": "save", "taxis_user": SECRET_USER, "taxis_pass": SECRET_PASS})
    assert credentials.get(conn, AFM) == (SECRET_USER, SECRET_PASS)
    for path in (f"/clients/{AFM}", "/clients", "/", "/settings", "/calendar", "/logs"):
        html = client.get(path).get_data(as_text=True)
        assert SECRET_PASS not in html, path
    assert SECRET_USER in client.get(f"/clients/{AFM}").get_data(as_text=True)      # το όνομα χρήστη φαίνεται, ο κωδικός ΟΧΙ
    client.post(f"/clients/{AFM}/credentials", data={"action": "clear"})
    assert credentials.get(conn, AFM) is None
    assert client.post("/clients/000000000/credentials", data={}).status_code == 404


def test_new_client_form_saves_credentials_and_rejects_half(client, conn):
    client.post("/clients/new", data={"afm": AFM, "name": "Α", "taxis_user": "u", "taxis_pass": "p"})
    assert credentials.get(conn, AFM) == ("u", "p")
    r = client.post("/clients/new", data={"afm": "123456783", "taxis_user": "μόνο", "taxis_pass": ""}, follow_redirects=True)
    assert credentials.get(conn, "123456783") is None and "δεν αποθηκεύτηκαν" in r.get_data(as_text=True)


def test_bulk_actions(client, conn):
    for a in (AFM, "123456783", "111111118"):
        service.add(conn, a, a)
    client.post("/clients/bulk", data={"action": "set_creds", "afms": [AFM, "123456783"], "taxis_user": "shared", "taxis_pass": "pw"})
    assert credentials.get(conn, AFM) == ("shared", "pw") and credentials.get(conn, "111111118") is None
    client.post("/clients/bulk", data={"action": "clear_creds", "afms": [AFM]})
    assert credentials.get(conn, AFM) is None and credentials.get(conn, "123456783")
    client.post("/clients/bulk", data={"action": "delete", "afms": ["123456783", "111111118"]})
    assert [b["afm"] for b in service.list_all(conn)] == [AFM]
    assert client.post("/clients/bulk", data={"action": "boom", "afms": [AFM]}).status_code == 400
    r = client.post("/clients/bulk", data={"action": "delete"}, follow_redirects=True)
    assert "Δεν επιλέχθηκε" in r.get_data(as_text=True)


def test_export_csv_has_no_credentials(client, conn):
    service.add(conn, AFM, "ΑΛΦΑ ΑΕ")
    credentials.set_(conn, AFM, SECRET_USER, SECRET_PASS)
    r = client.get("/clients/export.csv")
    text = r.get_data(as_text=True)
    assert r.mimetype == "text/csv" and text.startswith("﻿") and AFM in text and "ΑΛΦΑ ΑΕ" in text
    assert SECRET_USER not in text and SECRET_PASS not in text


def test_import_preview_counts_credentials_without_showing_them(client):
    data = xlsx([["ΑΦΜ", "Χρήστης TAXISnet", "Κωδικός TAXISnet"], [AFM, SECRET_USER, SECRET_PASS]])
    html = client.post("/clients/import", data={"file": (io.BytesIO(data), "k.xlsx")}, content_type="multipart/form-data").get_data(as_text=True)
    assert "1 με κωδικούς TAXISnet" in html and SECRET_PASS not in html and SECRET_USER not in html


def test_template_download_and_logs_and_dialog_everywhere(client):
    r = client.get("/clients/import/template.xlsx")
    assert r.status_code == 200 and r.data[:2] == b"PK"
    assert client.get("/logs").status_code == 200
    for path in ("/", "/clients", "/calendar", "/news", "/settings"):
        assert 'id="clientDialog"' in client.get(path).get_data(as_text=True)


# ---------------------------------------------------------------- φίλτρα πηγών
def item(title, url, summary=""):
    return rss_fetch.FeedItem(title, url, "2026-09-20T08:00:00Z", summary, "", url)


def test_keyword_prefilter():
    assert filters.is_tax_relevant("Παράταση προθεσμίας για δηλώσεις ΦΠΑ")
    assert filters.is_tax_relevant("Νέα εγκύκλιος", "αφορά το myDATA")
    assert not filters.is_tax_relevant("Ρωσία: Επίθεση με drone στο Μπριάνσκ")
    assert not filters.is_tax_relevant("Ο Ολυμπιακός νίκησε στο Καραϊσκάκη")


def test_general_portal_articles_are_skipped_not_lost(conn):
    n = rss_fetch.store_articles(conn, "capital_all", [item("Ρωσία: Επίθεση με drone", "https://c.gr/1"),
                                                       item("Νέα ρύθμιση για ΦΠΑ επιχειρήσεων", "https://c.gr/2")], keywords=True)
    assert n == 2
    st = {r["title"]: r["extraction_status"] for r in conn.execute("SELECT title, extraction_status FROM articles")}
    assert st["Ρωσία: Επίθεση με drone"] == "skipped" and st["Νέα ρύθμιση για ΦΠΑ επιχειρήσεων"] == "pending"


def test_duplicate_topics_across_sources_are_linked_not_reanalyzed(conn):
    rss_fetch.store_articles(conn, "taxheaven_law", [item("Α.1187/2026 - Μετάθεση της έναρξης ισχύος της παρ. 1 του άρθρου 129 του ν. 5264/2025", "https://t.gr/1")])
    rss_fetch.store_articles(conn, "eforologia_7", [item("Μετάθεση της έναρξης ισχύος της παρ. 1 του άρθρου 129 του ν. 5264/2025", "https://e.gr/9")])
    rows = conn.execute("SELECT source, extraction_status, duplicate_of FROM articles ORDER BY id").fetchall()
    assert rows[0]["extraction_status"] == "pending" and rows[1]["extraction_status"] == "duplicate" and rows[1]["duplicate_of"] == 1


def test_short_or_different_titles_are_not_duplicates(conn):
    rss_fetch.store_articles(conn, "a", [item("ΦΠΑ", "https://x/1"), item("Νέα υποχρέωση για εστίαση από 1η Οκτωβρίου", "https://x/2")])
    rss_fetch.store_articles(conn, "b", [item("ΦΠΑ", "https://y/1"), item("Νέα υποχρέωση για λιανεμπόριο από 1η Νοεμβρίου", "https://y/2")])
    statuses = [r[0] for r in conn.execute("SELECT extraction_status FROM articles ORDER BY id")]
    assert statuses == ["pending", "pending", "duplicate", "pending"]     # μόνο ο ίδιος σκέτος τίτλος «ΦΠΑ» (ίδιο key)


def test_max_items_keeps_newest():
    feed = "<rss version='2.0'><channel>" + "".join(
        f"<item><title>Άρθρο {i}</title><link>https://s.gr/{i}</link><pubDate>Mon, {i:02d} Sep 2026 07:00:00 GMT</pubDate></item>"
        for i in range(1, 11)) + "</channel></rss>"
    src = sources.Source("t", "T", "https://s.gr/feed", max_items=3)
    items = rss_fetch.fetch_feed(src, FakeSession().route("s.gr", FakeResponse(feed)))
    assert [i.title for i in items] == ["Άρθρο 10", "Άρθρο 9", "Άρθρο 8"]


def test_unavailable_source_is_never_fetched(conn):
    forin = sources.BY_ID["forin"]
    assert not forin.available and forin not in pipeline.enabled_sources(conn)
    settings_store.set_source_enabled(conn, "forin", True)              # ακόμη και αν «ενεργοποιηθεί» χειροκίνητα
    assert forin not in pipeline.enabled_sources(conn)


def test_new_sources_registered_with_valid_urls():
    ids = {s.id for s in sources.SOURCES}
    assert {"ot_forologia", "forologikanea", "naftemporiki_tax", "capital_all", "eforiakoi", "forin"} <= ids
    assert all(s.url.startswith("https://") for s in sources.SOURCES)


# ---------------------------------------------------------------- κανόνες ημερολογίου
def test_orthodox_easter_known_dates():
    assert obligations.orthodox_easter(2024) == date(2024, 5, 5)
    assert obligations.orthodox_easter(2025) == date(2025, 4, 20)
    assert obligations.orthodox_easter(2026) == date(2026, 4, 12)


def test_business_days_and_greek_holidays():
    assert not obligations.is_business_day(date(2026, 4, 10))            # Μεγάλη Παρασκευή 2026
    assert not obligations.is_business_day(date(2026, 4, 13))            # Δευτέρα του Πάσχα
    assert not obligations.is_business_day(date(2026, 2, 23))            # Καθαρά Δευτέρα 2026
    assert not obligations.is_business_day(date(2026, 10, 28)) and obligations.is_business_day(date(2026, 10, 29))
    assert obligations.last_business_day(2026, 5) == date(2026, 5, 29)   # 31/5 Κυριακή
    assert obligations.last_business_day(2026, 10) == date(2026, 10, 30)  # 31/10 Σάββατο


def test_vies_is_26th_moved_to_next_business_day():
    got = {o.date.isoformat(): o.title for o in obligations.occurrences(date(2026, 9, 1), date(2026, 10, 31)) if o.rule_id == "vies"}
    assert set(got) == {"2026-09-28", "2026-10-26"}                      # 26/9 = Σάββατο -> Δευτέρα 28/9
    assert "Αύγουστος 2026" in got["2026-09-28"]


def test_vat_rules_follow_client_period_type():
    def ids(biz):
        return {o.rule_id for o in obligations.occurrences(date(2026, 10, 1), date(2026, 10, 31), biz) if o.rule_id.startswith("vat")}
    assert ids({"vat_subject": 1, "vat_period_type": "monthly"}) == {"vat_monthly"}
    assert ids({"vat_subject": 1, "vat_period_type": "quarterly"}) == {"vat_quarterly"}
    assert ids({"vat_subject": 0, "vat_period_type": "monthly"}) == set()      # μη υπόχρεος
    assert ids(None) == {"vat_monthly", "vat_quarterly"}                        # γενικό
    unknown = [o for o in obligations.occurrences(date(2026, 10, 1), date(2026, 10, 31), {"vat_subject": None, "vat_period_type": ""})
               if o.rule_id.startswith("vat")]
    assert len(unknown) == 2 and all(o.conditional for o in unknown)            # άγνωστο: και τα δύο, υπό προϋποθέσεις


def test_quarterly_vat_only_in_quarter_end_months():
    months = {o.date.month for o in obligations.occurrences(date(2026, 1, 1), date(2026, 12, 31), None) if o.rule_id == "vat_quarterly"}
    assert months == {1, 4, 7, 10}


def test_events_merge_rules_feed_and_hide_duplicates(conn):
    now = db.utcnow()
    conn.execute("INSERT INTO obligations_general(guid,title,due_date,source_url,fetched_at) VALUES ('g','Υποβολή δήλωσης ΦΠΑ για τον μήνα Αύγουστο','2026-09-30','u',?)", (now,))
    evs = events_between(conn, date(2026, 9, 28), date(2026, 9, 30))
    assert any(e["kind"] == "general" and e["title"].startswith("Υποβολή δήλωσης ΦΠΑ") for e in evs)
    assert not any(e["kind"] == "rule" and e["id"] in ("vat_monthly", "vat_quarterly") for e in evs)   # κρύφτηκε: το feed έχει ήδη το γεγονός
    assert any(e["kind"] == "rule" and e["id"] == "vies" for e in evs)
    no_cond = events_between(conn, date(2026, 9, 28), date(2026, 9, 30), include_conditional=False)
    assert not any(e["conditional"] for e in no_cond)
    assert not any(e["kind"] == "rule" for e in events_between(conn, date(2026, 9, 28), date(2026, 9, 30), include_rules=False))


def test_client_calendar_uses_client_vat_period(conn):
    service.add(conn, AFM, "Τριμηνιαίος")
    service.update_fields(conn, AFM, {"vat_period_type": "quarterly"}, vat_subject=True)
    ids = {e["id"] for e in events_between(conn, date(2026, 10, 1), date(2026, 10, 31), afm=AFM) if e["kind"] == "rule"}
    assert "vat_quarterly" in ids and "vat_monthly" not in ids


def test_calendar_page_shows_rule_events_and_toggles(client):
    html = client.get("/calendar?month=2026-10").get_data(as_text=True)
    assert "Πίνακας VIES" in html and "Δήλωση ΦΠΑ (μηνιαία)" in html
    assert "Πίνακας VIES" not in client.get("/calendar?month=2026-10&rules=0").get_data(as_text=True)
    cond_off = client.get("/calendar?month=2026-10&cond=0").get_data(as_text=True)
    assert "Πίνακας VIES" not in cond_off and "Δήλωση ΦΠΑ (μηνιαία)" in cond_off
