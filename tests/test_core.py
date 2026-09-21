import os

import pytest

from taxmatch import crypto, db, settings_store


def test_schema_created_and_idempotent(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"businesses", "business_kad", "articles", "matches", "obligations_general", "settings", "runs"} <= tables
    db.connect().close()  # δεύτερο άνοιγμα: καμία αποτυχία migration


def test_foreign_keys_cascade(conn):
    now = db.utcnow()
    conn.execute("INSERT INTO businesses(afm,created_at,updated_at) VALUES ('094259216',?,?)", (now, now))
    conn.execute("INSERT INTO business_kad(afm,code) VALUES ('094259216','47.11')")
    conn.execute("DELETE FROM businesses WHERE afm='094259216'")
    assert conn.execute("SELECT COUNT(*) FROM business_kad").fetchone()[0] == 0


def test_crypto_roundtrip_and_passthrough():
    token = crypto.enc("μυστικό-123")
    assert token.startswith("enc:1:") and "μυστικό" not in token
    assert crypto.dec(token) == "μυστικό-123"
    assert crypto.dec("plain") == "plain"
    assert crypto.enc("") == ""


def test_secrets_are_encrypted_in_db(conn):
    settings_store.set_value(conn, "groq_api_key", "gsk_test_123")
    raw = conn.execute("SELECT value, encrypted FROM settings WHERE key='groq_api_key'").fetchone()
    assert raw["encrypted"] == 1 and "gsk_test_123" not in raw["value"]
    assert settings_store.get(conn, "groq_api_key") == "gsk_test_123"


def test_setting_priority_db_over_env(conn, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert settings_store.get(conn, "groq_api_key") == "from-env"
    settings_store.set_value(conn, "groq_api_key", "from-db")
    assert settings_store.get(conn, "groq_api_key") == "from-db"
    assert settings_store.get(conn, "llm_provider") == "groq"      # default


def test_dpapi_keyfile_roundtrip(tmp_path, monkeypatch):
    """Στα Windows: πραγματικό DPAPI wrap/unwrap του .enckey (χωρίς το env override)."""
    if os.name != "nt":
        pytest.skip("DPAPI μόνο σε Windows")
    monkeypatch.delenv("TAXMATCH_ENC_KEY")
    crypto._fernet_cache.clear()
    token = crypto.enc("secret")
    assert (tmp_path / "data" / ".enckey").read_text().startswith("taxmatch-key: dpapi1")
    crypto._fernet_cache.clear()
    assert crypto.dec(token) == "secret"
