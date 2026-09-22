import json

import pytest

from taxmatch import db, pipeline, settings_store
from taxmatch.business_profiles import service
from taxmatch.ingestion import sources
from taxmatch.matching import engine
from tests.fakes import FakeResponse, FakeSession, calendar_page

AFM_SHOP, AFM_CAFE, AFM_IT = "094259216", "123456783", "999999999"

NEWS_FEED = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>t</title>
<item><title>Παράταση υποβολής δηλώσεων ΦΠΑ για όλους</title><link>https://x.gr/a1</link><guid>https://x.gr/a1</guid>
<description>Παράταση</description><pubDate>Sun, 20 Sep 2026 07:00:00 GMT</pubDate></item>
<item><title>Νέα υποχρέωση για επιχειρήσεις εστίασης (ΚΑΔ 56)</title><link>https://x.gr/a2</link><guid>https://x.gr/a2</guid>
<description>Εστίαση</description><pubDate>Sun, 20 Sep 2026 08:00:00 GMT</pubDate></item>
<item><title>Κόμιστρο ΤΑΞΙ</title><link>https://x.gr/a3</link><guid>https://x.gr/a3</guid>
<description>Ταξί</description><pubDate>Sun, 20 Sep 2026 09:00:00 GMT</pubDate></item>
<item><title>Αλλαγή για βιβλία κατηγορίας Γ</title><link>https://x.gr/a4</link><guid>https://x.gr/a4</guid>
<description>Γ</description><pubDate>Sun, 20 Sep 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""

CAL_PAGE = calendar_page("09", "2026", [{"Title": "Υποβολή ΦΠΑ", "Date": "09/30/2026",
                                        "url": "https://www.taxheaven.gr/calendar/event/1"}])

SCOPES = {
    "ΦΠΑ για όλους": {"relevant": True, "summary": "Παράταση ΦΠΑ", "scope": {"type": "all"}, "deadline": "2026-10-15",
                      "action_required": "Υποβολή ΦΠΑ", "topic": "ΦΠΑ"},
    "εστίασης": {"relevant": True, "summary": "Εστίαση", "scope": {"type": "targeted", "kad_prefixes": ["56"]}},
    "ΤΑΞΙ": {"relevant": False, "scope": {"type": "all"}},
    "κατηγορίας Γ": {"relevant": True, "summary": "Γ", "scope": {"type": "targeted", "books_categories": ["Γ"]}},
}


def llm_handler(url, **kw):
    user = kw["json"]["messages"][1]["content"]
    for needle, payload in SCOPES.items():
        if needle in user:
            return FakeResponse(json_data={"choices": [{"message": {"content": json.dumps(payload)}}]})
    return FakeResponse(b"", 500)


@pytest.fixture
def world(conn, monkeypatch):
    settings_store.set_value(conn, "llm_provider", "groq")  # το default άλλαξε σε openrouter· εδώ δρομολογούμε ρητά σε groq.com
    settings_store.set_value(conn, "groq_api_key", "k")
    settings_store.set_value(conn, "fetch_full_text", "0")
    monkeypatch.setattr(pipeline, "enabled_sources",
                        lambda c: [sources.BY_ID["taxheaven_new"], sources.BY_ID["taxheaven_dat"]])
    monkeypatch.setattr("taxmatch.extraction.llm_extract.MIN_SECONDS_BETWEEN_CALLS", 0)
    monkeypatch.setattr("taxmatch.extraction.llm_extract.time.sleep", lambda s: None)
    # οι ημερομηνίες των feeds είναι σταθερές (Σεπ 2026)· η αναδρομή πρέπει να τις καλύπτει ανεξαρτήτως «σήμερα»
    settings_store.set_value(conn, "lookback_days", "3650")
    monkeypatch.setattr(engine, "MATCH_WINDOW_DAYS", 3650)
    for afm, name, kads, books in [(AFM_SHOP, "ΜΠΑΚΑΛΙΚΟ", ["47.11.10.01"], "Β"),
                                   (AFM_CAFE, "ΚΑΦΕΤΕΡΙΑ", ["56.30.10.01"], "Γ"),
                                   (AFM_IT, "ΠΛΗΡΟΦΟΡΙΚΗ", ["62.01.11.00"], "Γ")]:
        service.add(conn, afm, name)
        service.set_kads(conn, afm, [{"code": k} for k in kads])
        service.update_fields(conn, afm, {"books_category": books})
    s = FakeSession()
    s.route("soft_new", FakeResponse(NEWS_FEED))
    s.route("taxheaven.gr/calendar", FakeResponse(CAL_PAGE, headers={"content-type": "text/html"}))
    s.route("groq.com", llm_handler)
    return s


def test_full_pipeline_end_to_end(conn, world):
    stats = pipeline.run_pipeline("manual", session=world, conn=conn)
    assert stats["ingest"]["taxheaven_new"]["new"] == 4
    assert stats["ingest"]["taxheaven_dat"] == {"months": 5, "refreshed": 5, "errors": 0}
    assert conn.execute("SELECT COUNT(*) FROM obligations_general").fetchone()[0] == 1  # ίδιο guid σε κάθε μήνα -> 1 γραμμή
    assert stats["extract"]["done"] == 3 and stats["extract"]["irrelevant"] == 1
    assert not stats["errors"]

    by_title = {}
    for g in engine.digest_articles(conn, days=3650):
        by_title[g["title"]] = {m["afm"] for m in g["matches"]}
    assert by_title["Παράταση υποβολής δηλώσεων ΦΠΑ για όλους"] == {AFM_SHOP, AFM_CAFE, AFM_IT}
    assert by_title["Νέα υποχρέωση για επιχειρήσεις εστίασης (ΚΑΔ 56)"] == {AFM_CAFE}
    assert by_title["Αλλαγή για βιβλία κατηγορίας Γ"] == {AFM_CAFE, AFM_IT}
    assert "Κόμιστρο ΤΑΞΙ" not in by_title

    run = pipeline.last_run(conn)
    assert run["status"] == "ok" and run["trigger"] == "manual" and run["finished_at"]


def test_pipeline_is_idempotent_and_skips_processed(conn, world):
    pipeline.run_pipeline("manual", session=world, conn=conn)
    llm_calls = sum(1 for m, u, _ in world.calls if "groq" in u)
    stats = pipeline.run_pipeline("manual", session=world, conn=conn)
    assert sum(1 for m, u, _ in world.calls if "groq" in u) == llm_calls           # καμία νέα κλήση LLM
    assert stats["match"] == {"added": 0, "updated": 0, "removed": 0}
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 6


def test_deadline_sorting_puts_dated_articles_first(conn, world):
    pipeline.run_pipeline("manual", session=world, conn=conn)
    groups = engine.digest_articles(conn, days=3650)
    assert groups[0]["deadline"] == "2026-10-15" or groups[0]["deadline"] is None
    assert engine._urgency("2099-01-01", 1.0) < engine._urgency(None, 1.0) < engine._urgency("2000-01-01", 1.0)
    assert engine._urgency("2099-01-01", 1.0) < engine._urgency("2099-06-01", 1.0)


def test_editing_client_updates_matches_and_keeps_feedback(conn, world):
    pipeline.run_pipeline("manual", session=world, conn=conn)
    m = conn.execute("SELECT m.id FROM matches m JOIN articles a ON a.id=m.article_id "
                     "WHERE m.afm=? AND a.title LIKE '%βιβλία κατηγορίας Γ%'", (AFM_IT,)).fetchone()
    assert engine.set_feedback(conn, m["id"], -1)
    service.update_fields(conn, AFM_IT, {"books_category": "Β"})           # δεν είναι πια Γ
    service.update_fields(conn, AFM_CAFE, {"books_category": "Β"})
    out = engine.rematch(conn)
    assert out["removed"] == 1                                             # το CAFE έχασε το match (χωρίς feedback)
    kept = conn.execute("SELECT user_feedback FROM matches WHERE id=?", (m["id"],)).fetchone()
    assert kept["user_feedback"] == -1                                     # το 👎 του IT διατηρήθηκε


def test_new_client_matches_existing_articles_after_rematch(conn, world):
    pipeline.run_pipeline("manual", session=world, conn=conn)
    service.add(conn, "111111111", "ΝΕΟΣ")
    service.set_kads(conn, "111111111", [{"code": "56.10.11.01"}])
    engine.rematch(conn)
    titles = {g["title"] for g in engine.digest_articles(conn, 3650) if any(m["afm"] == "111111111" for m in g["matches"])}
    assert "Νέα υποχρέωση για επιχειρήσεις εστίασης (ΚΑΔ 56)" in titles


def test_no_llm_key_still_ingests_and_reports(conn, world):
    settings_store.set_value(conn, "groq_api_key", "")
    stats = pipeline.run_pipeline("manual", session=world, conn=conn)
    assert stats["ingest"]["taxheaven_new"]["new"] == 4
    assert "skipped" in stats["extract"] and any("API key" in e for e in stats["errors"])
    assert conn.execute("SELECT COUNT(*) FROM obligations_general").fetchone()[0] == 1


def test_lock_prevents_concurrent_runs(conn, world):
    with pipeline.run_lock():
        with pytest.raises(pipeline.AlreadyRunning):
            with pipeline.run_lock():
                pass
    with pipeline.run_lock():          # ελευθερώθηκε
        pass


def test_feedback_validation(conn, world):
    pipeline.run_pipeline("manual", session=world, conn=conn)
    mid = conn.execute("SELECT id FROM matches LIMIT 1").fetchone()[0]
    assert engine.set_feedback(conn, mid, 1) and engine.set_feedback(conn, mid, None)
    with pytest.raises(ValueError):
        engine.set_feedback(conn, mid, 5)
    assert not engine.set_feedback(conn, 99999, 1)
