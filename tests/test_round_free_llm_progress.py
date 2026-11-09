"""Tests for free LLM management, multi-model fallback, and news progress tracking."""
import json
import sqlite3
import pytest
from taxmatch import db, settings_store
from taxmatch.extraction import llm_extract
from taxmatch.extraction.llm_extract import LLMClient, LLMError
from taxmatch.matching import engine
from tests.fakes import FakeResponse, FakeSession


def _add_test_article(conn, id=1, title="Νέες φορολογικές ρυθμίσεις 2026"):
    conn.execute(
        "INSERT INTO articles(id, source, url, url_hash, title, raw_summary, full_text, extraction_status, published_at, fetched_at) "
        "VALUES (?, 'taxheaven_new', 'https://example.com/art', ?, ?, 'Σύνοψη', 'Πλήρες κείμενο', 'pending', "
        "'2026-11-09T08:00:00Z', ?)",
        (id, f"hash_{id}", title, db.utcnow()),
    )


def test_is_free_model_recognizes_openrouter_free_and_free_suffix():
    assert llm_extract.is_free_model("openrouter", "openrouter/free") is True
    assert llm_extract.is_free_model("openrouter", "liquid/lfm-2.5-2.6b:free") is True
    assert llm_extract.is_free_model("openrouter", "google/gemma-4-31b-it:free") is True
    assert llm_extract.is_free_model("openrouter", "meta-llama/llama-3.3-70b-instruct") is False
    assert llm_extract.is_free_model("groq", "any-groq-model") is True


def test_openrouter_complete_json_adds_models_fallback_array():
    session = FakeSession()
    # Mocking completion response
    session.route("chat/completions", FakeResponse(
        b'{"choices":[{"message":{"content":"{\\"relevant\\":true,\\"deadline\\":\\"2026-12-31\\",\\"scope\\":{\\"type\\":\\"all\\"}}"}}]}'
    ))
    client = LLMClient("openrouter", "k", "liquid/lfm-2.5-2.6b:free",
                       "https://openrouter.ai/api/v1/chat/completions", session)
    res = client.complete_json("system", "user")
    assert "relevant" in res
    # Inspect the posted body in the fake session
    assert len(session.calls) >= 1
    method, url, kw = session.calls[-1]
    posted_json = kw["json"]
    assert "models" in posted_json
    assert posted_json["models"][0] == "liquid/lfm-2.5-2.6b:free"
    assert "openrouter/free" in posted_json["models"]
    assert "google/gemma-4-31b-it:free" in posted_json["models"]


def test_extract_pending_multi_model_rotation_on_upstream_congestion(conn):
    _add_test_article(conn, 1)
    
    congestion_resp = FakeResponse(
        b'{"error":{"code":429,"message":"liquid/lfm-2.5-2.6b:free is temporarily rate-limited upstream","metadata":{"limit_source":"upstream_provider"}}}',
        429,
    )
    success_resp = FakeResponse(
        b'{"choices":[{"message":{"content":"{\\"relevant\\":true,\\"deadline\\":null,\\"scope\\":{\\"type\\":\\"all\\"}}"}}]}'
    )

    models_list_resp = FakeResponse(json_data={"data": [
        {"id": "liquid/lfm-2.5-2.6b:free"},
        {"id": "openrouter/free"},
        {"id": "google/gemma-4-31b-it:free"},
        {"id": "nvidia/nemotron-3.5-lightning:free"},
    ]})

    call_count = 0
    def custom_chat_handler(url, **kw):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return congestion_resp
        return success_resp

    session = FakeSession()
    session.route("/models", models_list_resp)
    session.route("chat/completions", custom_chat_handler)

    client = LLMClient("openrouter", "k", "liquid/lfm-2.5-2.6b:free",
                       "https://openrouter.ai/api/v1/chat/completions", session)
    progress_messages = []
    out = llm_extract.extract_pending(conn, client, 10, 30, on_progress=progress_messages.append, sleep=lambda _s: None)

    # Article succeeded!
    assert out["done"] == 1
    assert out["failed"] == 0
    assert out["stopped"] == ""
    # Model switched past liquid/lfm and openrouter/free to google/gemma-4-31b-it:free
    assert out["model"] == "google/gemma-4-31b-it:free"
    assert any("Αλλαγή σε διαθέσιμο δωρεάν μοντέλο" in msg for msg in progress_messages)


def test_rematch_emits_progress(conn):
    _add_test_article(conn, 1)
    conn.execute(
        "UPDATE articles SET extracted_json='{\"relevant\": true, \"scope\": {\"type\": \"all\"}}', extraction_status='done' WHERE id=1"
    )
    # Add a business
    conn.execute(
        "INSERT INTO businesses(afm, name, legal_form, doy, activity_state, created_at, updated_at) "
        "VALUES ('094014201', 'ΤΕΣΤ ΑΕ', 'ΑΕ', 'ΑΘΗΝΩΝ', 'ΕΝΕΡΓΗ', ?, ?)",
        (db.utcnow(), db.utcnow())
    )

    progress_messages = []
    res = engine.rematch(conn, on_progress=progress_messages.append)
    assert res["added"] == 1
    assert any("Αντιστοίχιση" in msg for msg in progress_messages)
