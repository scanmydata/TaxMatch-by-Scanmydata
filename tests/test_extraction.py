import json

import pytest

from taxmatch import db, settings_store
from taxmatch.extraction import llm_extract
from taxmatch.extraction.llm_extract import LLMClient, LLMError, parse_json_loose
from tests.fakes import FakeResponse, FakeSession


def llm_reply(payload) -> FakeResponse:
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return FakeResponse(json_data={"choices": [{"message": {"content": content}}]})


def make_client(session) -> LLMClient:
    return LLMClient("groq", "k", "m", "https://api.groq.com/openai/v1/chat/completions", session)


def add_article(conn, title="Τίτλος", url="https://x.gr/1", published="2026-09-20T08:00:00Z"):
    conn.execute("INSERT INTO articles(source,title,url,url_hash,published_at,fetched_at,raw_summary) VALUES (?,?,?,?,?,?,?)",
                 ("t", title, url, url, published, db.utcnow(), "περίληψη"))
    return conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()


def test_parse_json_loose_variants():
    assert parse_json_loose('{"a": 1}') == {"a": 1}
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('Ορίστε: {"a": {"b": 2}} τέλος') == {"a": {"b": 2}}
    for bad in ("", "καθόλου json", "[1,2]"):
        with pytest.raises(ValueError):
            parse_json_loose(bad)


def test_prompt_loads_and_has_date():
    text = llm_extract.system_prompt()
    assert "{today}" not in text and "kad_prefixes" in text


def test_extract_stores_normalized_scope(conn):
    s = FakeSession().route("groq.com", llm_reply({
        "relevant": True, "summary": "Παράταση", "scope": {"type": "targeted", "kad_prefixes": ["56"]},
        "deadline": "2026-10-15", "action_required": "Υποβολή", "topic": "ΦΠΑ"}))
    art = add_article(conn)
    status = llm_extract.extract_article(conn, art, make_client(s), "sys", session=None, use_full_text=False)
    assert status == "done"
    row = conn.execute("SELECT * FROM articles WHERE id=?", (art["id"],)).fetchone()
    assert row["deadline"] == "2026-10-15" and row["extraction_status"] == "done"
    assert json.loads(row["extracted_json"])["scope"]["kad_prefixes"] == ["56"]
    sent = s.calls[0][2]["json"]
    assert sent["response_format"] == {"type": "json_object"} and "Τίτλος" in sent["messages"][1]["content"]
    assert s.calls[0][2]["headers"]["Authorization"] == "Bearer k"


def test_irrelevant_article_status(conn):
    s = FakeSession().route("groq.com", llm_reply({"relevant": False, "scope": {"type": "all"}}))
    art = add_article(conn)
    assert llm_extract.extract_article(conn, art, make_client(s), "sys", None, False) == "irrelevant"


def test_auth_error_stops_batch_without_burning_tries(conn):
    s = FakeSession().route("groq.com", FakeResponse(b"unauthorized", 401))
    for i in range(3):
        add_article(conn, url=f"https://x.gr/{i}")
    out = llm_extract.extract_pending(conn, make_client(s), 10, 30, sleep=lambda _s: None)
    assert "πιστοποίησης" in out["stopped"] and out["processed"] == 0
    assert conn.execute("SELECT MAX(extraction_tries) FROM articles").fetchone()[0] == 0
    assert len(s.calls) == 1


def test_rate_limit_waits_then_gives_up_leaving_articles_pending(conn):
    s = FakeSession().route("groq.com", FakeResponse(b"", 429, {"retry-after": "7"}))
    add_article(conn)
    sleeps = []
    out = llm_extract.extract_pending(conn, make_client(s), 10, 30, sleep=sleeps.append)
    assert "Όριο αιτημάτων" in out["stopped"] and out["remaining"] == 1
    assert 7.0 in sleeps                                  # σεβάστηκε το Retry-After
    assert conn.execute("SELECT extraction_status FROM articles").fetchone()[0] == "pending"


def test_bad_json_marks_failed_and_retries_up_to_max(conn):
    s = FakeSession().route("groq.com", llm_reply("δεν είναι json"))
    add_article(conn)
    for _ in range(5):
        llm_extract.extract_pending(conn, make_client(s), 10, 30, sleep=lambda _s: None)
    row = conn.execute("SELECT extraction_status, extraction_tries FROM articles").fetchone()
    assert row["extraction_status"] == "failed" and row["extraction_tries"] == llm_extract.MAX_TRIES
    assert len(s.calls) == llm_extract.MAX_TRIES


def test_old_articles_outside_lookback_are_skipped(conn):
    s = FakeSession().route("groq.com", llm_reply({"relevant": True, "scope": {"type": "all"}}))
    add_article(conn, url="https://x.gr/old", published="2020-01-01T00:00:00Z")
    out = llm_extract.extract_pending(conn, make_client(s), 10, 10, sleep=lambda _s: None)
    assert out["processed"] == 0 and not s.calls


def test_full_text_fetched_and_stored(conn):
    html = "<html><body><nav>μενού</nav><article>" + ("Το κείμενο του άρθρου. " * 30) + "</article></body></html>"
    s = FakeSession()
    s.route("x.gr", FakeResponse(html, headers={"content-type": "text/html"}))
    s.route("groq.com", llm_reply({"relevant": True, "scope": {"type": "all"}}))
    art = add_article(conn)
    llm_extract.extract_article(conn, art, make_client(s), "sys", session=s, use_full_text=True)
    stored = conn.execute("SELECT full_text FROM articles").fetchone()[0]
    assert "Το κείμενο του άρθρου" in stored and "μενού" not in stored
    assert "Το κείμενο του άρθρου" in s.calls[-1][2]["json"]["messages"][1]["content"]


def test_very_long_article_is_summarized_by_llm_instead_of_character_truncated(conn):
    long_text = "Άρθρο 1: παράταση προθεσμίας ΦΠΑ. " * 400        # πολύ πάνω από FULL_TEXT_MAX
    assert len(long_text) > llm_extract.FULL_TEXT_MAX

    def route(url, **kw):
        system = kw["json"]["messages"][0]["content"]
        if "Συνοψίζεις" in system:
            assert str(len(long_text)) in kw["json"]["messages"][1]["content"]   # πήρε ΟΛΟΚΛΗΡΟ το κείμενο, όχι κομμένο
            return llm_reply({"summary": "Παράταση προθεσμίας ΦΠΑ έως 31/12."})
        assert "Παράταση προθεσμίας ΦΠΑ έως 31/12" in kw["json"]["messages"][1]["content"]
        assert "Άρθρο 1: παράταση προθεσμίας ΦΠΑ. Άρθρο 1" not in kw["json"]["messages"][1]["content"]
        return llm_reply({"relevant": True, "scope": {"type": "all"}, "deadline": "2026-12-31"})

    s = FakeSession().route("groq.com", route)
    art = add_article(conn)
    conn.execute("UPDATE articles SET full_text=? WHERE id=?", (long_text, art["id"]))
    art = conn.execute("SELECT * FROM articles WHERE id=?", (art["id"],)).fetchone()
    status = llm_extract.extract_article(conn, art, make_client(s), "sys", session=None, use_full_text=False,
                                         sleep=lambda _s: None)
    assert status == "done"
    assert len(s.calls) == 2                                  # 1 περίληψη + 1 κύρια εξαγωγή
    assert conn.execute("SELECT full_text FROM articles WHERE id=?", (art["id"],)).fetchone()[0] == long_text


def test_summarize_falls_back_to_smart_truncate_on_invalid_json(conn):
    long_text = "Κείμενο άρθρου. " * 500
    calls = {"n": 0}

    def route(url, **kw):
        calls["n"] += 1
        system = kw["json"]["messages"][0]["content"]
        if "Συνοψίζεις" in system:
            return llm_reply("δεν είναι json")               # άκυρη απάντηση περίληψης -> fallback
        return llm_reply({"relevant": True, "scope": {"type": "all"}})

    s = FakeSession().route("groq.com", route)
    art = add_article(conn)
    conn.execute("UPDATE articles SET full_text=? WHERE id=?", (long_text, art["id"]))
    art = conn.execute("SELECT * FROM articles WHERE id=?", (art["id"],)).fetchone()
    status = llm_extract.extract_article(conn, art, make_client(s), "sys", session=None, use_full_text=False,
                                         sleep=lambda _s: None)
    assert status == "done"
    final_msg = s.calls[-1][2]["json"]["messages"][1]["content"]
    assert llm_extract._TRUNCATION_MARK.strip() in final_msg    # έπεσε πίσω στο smart truncate, όχι στο ωμό κείμενο


def test_summarize_error_propagates_like_a_normal_llm_error(conn):
    long_text = "Κείμενο άρθρου. " * 500

    def route(url, **kw):
        system = kw["json"]["messages"][0]["content"]
        if "Συνοψίζεις" in system:
            return FakeResponse(b"", 429, {"retry-after": "3"})
        raise AssertionError("δεν έπρεπε να φτάσει ποτέ στην κύρια εξαγωγή")

    s = FakeSession().route("groq.com", route)
    art = add_article(conn)
    conn.execute("UPDATE articles SET full_text=? WHERE id=?", (long_text, art["id"]))
    art = conn.execute("SELECT * FROM articles WHERE id=?", (art["id"],)).fetchone()
    with pytest.raises(llm_extract.LLMError) as exc_info:
        llm_extract.extract_article(conn, art, make_client(s), "sys", session=None, use_full_text=False,
                                    sleep=lambda _s: None)
    assert exc_info.value.kind == "rate_limit"


def test_short_article_is_not_summarized(conn):
    s = FakeSession().route("groq.com", llm_reply({"relevant": True, "scope": {"type": "all"}}))
    art = add_article(conn)
    llm_extract.extract_article(conn, art, make_client(s), "sys", session=None, use_full_text=False,
                                sleep=lambda _s: None)
    assert len(s.calls) == 1                                  # καμία κλήση περίληψης για κοντό κείμενο


def test_full_text_strips_subscription_upsell_from_paywalled_circulars(conn):
    html = ("<html><body><article>"
            "Περίληψη: Οι υποσταθμοί διανομής δεν υπάγονται στο τέλος καθαριότητας."
            "Συνδρομητικό περιεχόμενο Το πλήρες κείμενο είναι διαθέσιμο στα μέλη. "
            "Αποκτήστε πρόσβαση — 100,00€/έτος (πλέον Φ.Π.Α.)"
            "</article></body></html>")
    s = FakeSession().route("x.gr", FakeResponse(html, headers={"content-type": "text/html"}))
    text = llm_extract.fetch_full_text("https://x.gr/circulars/1", s)
    assert "υποσταθμοί διανομής" in text
    assert "100,00" not in text and "Συνδρομητικό" not in text


def test_full_text_refuses_private_hosts():
    s = FakeSession()
    assert llm_extract.fetch_full_text("http://127.0.0.1:8000/x", s) == ""
    assert llm_extract.fetch_full_text("http://192.168.1.5/x", s) == ""
    assert llm_extract.fetch_full_text("file:///etc/passwd", s) == ""
    assert not s.calls


def test_client_from_settings_requires_key(conn):
    with pytest.raises(llm_extract.NotConfigured):
        LLMClient.from_settings(conn)
    settings_store.set_value(conn, "llm_provider", "openrouter")
    settings_store.set_value(conn, "openrouter_api_key", "sk-or-1")
    c = LLMClient.from_settings(conn)
    assert c.provider == "openrouter" and "openrouter.ai" in c.url and c.api_key == "sk-or-1"
