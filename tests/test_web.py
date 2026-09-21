import io
import json
import time
from datetime import date, timedelta

import pytest
from openpyxl import Workbook

from taxmatch import db, settings_store
from taxmatch.business_profiles import service
from taxmatch.web import create_app

TOKEN = "test-token-123"
AFM = "094259216"


@pytest.fixture
def client():
    app = create_app(TOKEN, testing=True)
    c = app.test_client()
    c.get(f"/?t={TOKEN}")                       # ορίζει το cookie συνεδρίας
    return c


def seed(conn):
    service.add(conn, AFM, "ΔΕΗ ΑΕ")
    service.set_kads(conn, AFM, [{"code": "35.11.10.01", "descr": "Παραγωγή ρεύματος"}])
    now = db.utcnow()
    conn.execute("INSERT INTO articles(source,title,url,url_hash,published_at,fetched_at,extraction_status,extracted_json,deadline) "
                 "VALUES ('taxheaven_new','Παράταση ΦΠΑ','https://x.gr/1','h1',?,?,'done',?,?)",
                 (now, now, json.dumps({"summary": "Σύνοψη", "action_required": "Υποβολή", "topic": "ΦΠΑ",
                                        "scope": {"type": "all"}, "relevant": True}),
                  (date.today() + timedelta(days=3)).isoformat()))
    conn.execute("INSERT INTO matches(article_id,afm,matched_reason,confidence,created_at) VALUES (1,?,?,1.0,?)",
                 (AFM, "Αφορά όλες τις επιχειρήσεις", now))
    conn.execute("INSERT INTO obligations_general(guid,title,due_date,source_url,fetched_at) VALUES ('g1','Δήλωση ΦΠΑ',?,?,?)",
                 (date.today().isoformat(), "https://www.taxheaven.gr/calendar/event/1", now))


# ---------------------------------------------------------------- ασφάλεια

def test_requests_without_token_are_rejected():
    c = create_app(TOKEN).test_client()
    assert c.get("/").status_code == 403
    assert c.get("/clients?t=wrong").status_code == 403
    assert c.post("/api/feedback", json={}).status_code == 403


def test_token_sets_cookie_and_redirects_clean_url():
    c = create_app(TOKEN).test_client()
    r = c.get(f"/clients?t={TOKEN}")
    assert r.status_code == 302 and r.headers["Location"] == "/clients"
    assert "tm_auth" in r.headers["Set-Cookie"] and "SameSite=Strict" in r.headers["Set-Cookie"]
    assert c.get("/clients").status_code == 200


def test_dns_rebinding_host_is_rejected(client):
    assert client.get("/", headers={"Host": "evil.example.com"}).status_code == 403
    # το cookie είναι ανά host: στο 127.0.0.1 χρειάζεται δικό του token handshake
    ok = client.get(f"/?t={TOKEN}", headers={"Host": "127.0.0.1:5000"}, follow_redirects=True)
    assert ok.status_code == 200
    assert client.get(f"/?t={TOKEN}", headers={"Host": "evil.example.com"}).status_code == 403   # ακόμη και με σωστό token


def test_cross_origin_post_is_rejected(client):
    r = client.post("/api/feedback", json={"match_id": 1, "value": 1}, headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403
    r = client.post("/api/feedback", json={"match_id": 1, "value": 1}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_open_external_only_allows_http(client, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
    assert client.post("/api/open-external", json={"url": "file:///C:/Windows/system32/calc.exe"}).status_code == 400
    assert client.post("/api/open-external", json={"url": "javascript:alert(1)"}).status_code == 400
    assert client.post("/api/open-external", json={"url": "https://www.taxheaven.gr/x"}).status_code == 200
    assert opened == ["https://www.taxheaven.gr/x"]


# ---------------------------------------------------------------- σελίδες

@pytest.mark.parametrize("path", ["/", "/?view=clients", "/clients", "/clients/new", "/clients/import", "/calendar",
                                  "/calendar?month=2026-09&news=0", "/calendar?month=garbage", "/news", "/news?only=all",
                                  "/settings"])
def test_all_pages_render_empty_state(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", ["/", "/?view=clients&days=14", f"/clients/{AFM}", "/clients", "/calendar", "/news?only=all", "/settings"])
def test_all_pages_render_with_data(client, path, conn):
    seed(conn)
    r = client.get(path)
    assert r.status_code == 200
    assert "Traceback" not in r.get_data(as_text=True)


def test_dashboard_shows_match_deadline_and_alerts(client, conn):
    seed(conn)
    html = client.get("/").get_data(as_text=True)
    assert "Παράταση ΦΠΑ" in html and "Υποβολή" in html and "ΔΕΗ ΑΕ" in html
    assert "σε 3 ημ." in html                                    # badge προθεσμίας
    assert "API key" in html                                     # ειδοποίηση: δεν υπάρχει LLM key


def test_calendar_shows_general_and_news_deadlines(client, conn):
    seed(conn)
    html = client.get("/calendar").get_data(as_text=True)
    assert "Δήλωση ΦΠΑ" in html and "Παράταση ΦΠΑ" in html
    html2 = client.get("/calendar?news=0").get_data(as_text=True)
    assert "Δήλωση ΦΠΑ" in html2 and "Παράταση ΦΠΑ" not in html2
    assert "Παράταση ΦΠΑ" in client.get(f"/calendar?afm={AFM}").get_data(as_text=True)


def test_unknown_client_404(client):
    assert client.get("/clients/000000000").status_code == 404
    assert client.post("/clients/000000000/edit", data={}).status_code == 404


# ---------------------------------------------------------------- πελάτες

def test_add_client_manually_validates_afm(client, conn):
    r = client.post("/clients/new", data={"afm": "abc"})
    assert r.status_code == 400 and "9ψήφιος" in r.get_data(as_text=True)
    r = client.post("/clients/new", data={"afm": AFM, "name": "ΔΕΗ"})
    assert r.status_code == 302 and service.get(conn, AFM)["name"] == "ΔΕΗ"
    time.sleep(0.3)                                              # background lookup (χωρίς keys: no-op)
    assert service.get(conn, AFM)["lookup_status"] == "pending"


def test_edit_client_saves_profile_and_kads(client, conn):
    service.add(conn, AFM, "Α")
    r = client.post(f"/clients/{AFM}/edit", data={
        "name": "ΝΕΟ ΟΝΟΜΑ", "legal_form": "ΙΚΕ", "books_category": "Γ", "vat_subject": "1", "vat_period_type": "monthly",
        "kads": "47.11.10.01 Λιανικό\n56.10.11.01 Εστίαση\n", "notes": "σημείωση"})
    assert r.status_code == 302
    b = service.get(conn, AFM)
    assert (b["name"], b["books_category"], b["vat_subject"], b["notes"]) == ("ΝΕΟ ΟΝΟΜΑ", "Γ", 1, "σημείωση")
    assert [k["code"] for k in b["kads"]] == ["47.11.10.01", "56.10.11.01"] and b["kad_main_code"] == "47.11.10.01"
    client.post(f"/clients/{AFM}/edit", data={"name": "ΝΕΟ ΟΝΟΜΑ", "vat_subject": "", "kads": ""})
    assert service.get(conn, AFM)["vat_subject"] is None


def xlsx_bytes(rows):
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_excel_import_preview_then_confirm(client, conn):
    data = xlsx_bytes([["ΑΦΜ", "Επωνυμία"], [AFM, "ΔΕΗ"], ["123456783", "ΑΛΛΟ"], ["xyz", "λάθος"]])
    r = client.post("/clients/import", data={"file": (io.BytesIO(data), "pelates.xlsx")}, content_type="multipart/form-data")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "2 έγκυροι ΑΦΜ" in html and "αγνοήθηκαν" in html
    assert service.count(conn) == 0                              # η προεπισκόπηση δεν αποθηκεύει
    token = html.split('name="token" value="')[1].split('"')[0]
    r = client.post("/clients/import/confirm", data={"token": token})
    assert r.status_code == 302 and service.count(conn) == 2
    r = client.post("/clients/import/confirm", data={"token": token})       # ίδιο token δεν ξαναχρησιμοποιείται
    assert service.count(conn) == 2


def test_import_rejects_bad_file(client):
    r = client.post("/clients/import", data={"file": (io.BytesIO(b"x"), "a.pdf")}, content_type="multipart/form-data",
                    follow_redirects=True)
    assert "Υποστηρίζονται" in r.get_data(as_text=True)
    r = client.post("/clients/import", data={"file": (io.BytesIO(b"not really excel"), "a.xlsx")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "Δεν ήταν δυνατή" in r.get_data(as_text=True)


def test_delete_client_cascades(client, conn):
    seed(conn)
    client.post(f"/clients/{AFM}/delete")
    assert service.count(conn) == 0 and conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0


# ---------------------------------------------------------------- feedback / ρυθμίσεις / jobs

def test_feedback_api_roundtrip(client, conn):
    seed(conn)
    r = client.post("/api/feedback", json={"match_id": 1, "value": -1})
    assert r.get_json() == {"ok": True, "value": -1}
    assert conn.execute("SELECT user_feedback FROM matches").fetchone()[0] == -1
    client.post("/api/feedback", json={"match_id": 1, "value": 0})
    assert conn.execute("SELECT user_feedback FROM matches").fetchone()[0] is None
    assert client.post("/api/feedback", json={"match_id": 1, "value": 7}).status_code == 400
    assert client.post("/api/feedback", json={"match_id": 999, "value": 1}).status_code == 404
    assert client.post("/api/feedback", json={"match_id": "x"}).status_code == 400


def test_settings_secrets_saved_encrypted_and_never_echoed(client, conn):
    client.post("/settings", data={"section": "keys", "groq_api_key": "gsk_SECRET_VALUE", "aade_pass": "pw123"})
    raw = conn.execute("SELECT value FROM settings WHERE key='groq_api_key'").fetchone()[0]
    assert raw.startswith("enc:1:") and "SECRET" not in raw
    html = client.get("/settings").get_data(as_text=True)
    assert "gsk_SECRET_VALUE" not in html and "pw123" not in html and "ορισμένο" in html
    client.post("/settings", data={"section": "keys", "groq_api_key": ""})              # κενό = αμετάβλητο
    assert settings_store.get(conn, "groq_api_key") == "gsk_SECRET_VALUE"
    client.post("/settings", data={"section": "keys", "clear_groq_api_key": "1"})
    assert settings_store.get(conn, "groq_api_key") == ""


def test_settings_sources_and_llm(client, conn):
    client.post("/settings", data={"section": "sources", "src_taxheaven_new": "1"})
    assert settings_store.source_enabled(conn, "taxheaven_new", False)
    assert not settings_store.source_enabled(conn, "eforologia_7", True)
    client.post("/settings", data={"section": "llm", "llm_provider": "openrouter", "lookback_days": "9999",
                                   "max_extractions_per_run": "abc"})
    assert settings_store.get(conn, "llm_provider") == "openrouter"
    assert settings_store.get(conn, "lookback_days") == "60"                 # clamp
    assert settings_store.get(conn, "max_extractions_per_run") == "60"       # άκυρο -> αμετάβλητο (default)


def test_schedule_time_validation(client, monkeypatch):
    from taxmatch import scheduler_win
    monkeypatch.setattr(scheduler_win, "install", lambda t: (True, f"ok {t}"))
    r = client.post("/settings", data={"section": "schedule", "daily_time": "07:30", "action": "install"}, follow_redirects=True)
    assert "ok 07:30" in r.get_data(as_text=True)
    with pytest.raises(ValueError):
        scheduler_win.task_xml("x.exe", "--daily", "25:99")


def test_run_api_starts_job_and_reports_progress(client, conn, monkeypatch):
    from taxmatch import pipeline
    monkeypatch.setattr(pipeline, "run_pipeline", lambda trigger, on_progress=None, **k: (on_progress("βήμα"), {"errors": []})[1])
    r = client.post("/api/run")
    assert r.status_code == 202
    for _ in range(50):
        snap = client.get("/api/job").get_json()
        if not snap["running"]:
            break
        time.sleep(0.05)
    assert snap["running"] is False and snap["error"] == "" and snap["result"] == {"errors": []}


def test_job_failure_is_reported_not_raised(client, monkeypatch):
    from taxmatch import pipeline

    def boom(*a, **k):
        raise pipeline.AlreadyRunning()
    monkeypatch.setattr(pipeline, "run_pipeline", boom)
    client.post("/api/run")
    for _ in range(50):
        snap = client.get("/api/job").get_json()
        if not snap["running"]:
            break
        time.sleep(0.05)
    assert "τρέχει ήδη" in snap["error"]
