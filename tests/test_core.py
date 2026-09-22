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


def test_unprotected_keyfile_roundtrip(tmp_path, monkeypatch):
    """Χωρίς κύριο κωδικό: σκέτο κλειδί Fernet στο δίσκο (ίδιο σχήμα με το timologio downloader)."""
    monkeypatch.delenv("TAXMATCH_ENC_KEY")
    crypto._fernet_cache.clear()
    token = crypto.enc("secret")
    content = (tmp_path / "data" / ".enckey").read_text()
    assert not content.startswith("taxmatch-key: 2") and crypto._parse(content.encode()) is None
    crypto._fernet_cache.clear()
    assert crypto.dec(token) == "secret"


def test_master_password_protects_and_unlocks_keyfile(tmp_path, monkeypatch):
    monkeypatch.delenv("TAXMATCH_ENC_KEY")
    crypto._fernet_cache.clear()
    path = tmp_path / "data" / ".enckey"
    token = crypto.enc("secret")                              # δημιουργεί ακάλυπτο κλειδί
    assert not crypto.is_protected(path)
    crypto.set_password(path, "hunter2-orange-bus")
    assert crypto.is_protected(path)
    crypto._fernet_cache.clear()
    crypto.forget()
    with pytest.raises(crypto.KeyfileLocked):
        crypto.dec(token)                                     # ξεκλείδωτο -> χρειάζεται κωδικό
    with pytest.raises(crypto.WrongPassword):
        crypto.unlock(path, "λάθος")
    crypto.unlock(path, "hunter2-orange-bus")
    assert crypto.dec(token) == "secret"                       # ίδιο κλειδί δεδομένων: ό,τι κρυπτογραφήθηκε πριν διαβάζεται


def test_master_password_can_be_removed(tmp_path, monkeypatch):
    monkeypatch.delenv("TAXMATCH_ENC_KEY")
    crypto._fernet_cache.clear()
    path = tmp_path / "data" / ".enckey"
    token = crypto.enc("secret")
    crypto.set_password(path, "hunter2-orange-bus")
    crypto.remove_password(path, "hunter2-orange-bus")
    assert not crypto.is_protected(path)
    crypto._fernet_cache.clear()
    crypto.forget()
    assert crypto.dec(token) == "secret"                       # ξανά προσβάσιμο χωρίς κωδικό


def test_legacy_plain_keyfile_still_readable(tmp_path, monkeypatch):
    """Παλιά μορφή .enckey του TaxMatch (πριν τον κύριο κωδικό) διαβάζεται κανονικά — τα δεδομένα δεν χάνονται."""
    monkeypatch.delenv("TAXMATCH_ENC_KEY")
    path = tmp_path / "data" / ".enckey"
    path.parent.mkdir(parents=True)
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    path.write_text(f"taxmatch-key: plain1\n{key.decode()}\n", encoding="utf-8")
    crypto._fernet_cache.clear()
    assert crypto.load_or_create_key(path) == key


@pytest.mark.skipif(os.name != "nt", reason="DPAPI μόνο σε Windows")
def test_legacy_dpapi_keyfile_still_readable(tmp_path, monkeypatch):
    """Παλιά DPAPI-προστατευμένη .enckey (η προεπιλογή πριν τον κύριο κωδικό) διαβάζεται κανονικά."""
    import base64
    monkeypatch.delenv("TAXMATCH_ENC_KEY")
    path = tmp_path / "data" / ".enckey"
    path.parent.mkdir(parents=True)
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    wrapped = base64.b64encode(_dpapi_protect(key))
    path.write_text(f"taxmatch-key: dpapi1\n{wrapped.decode()}\n", encoding="ascii")
    crypto._fernet_cache.clear()
    assert crypto.load_or_create_key(path) == key


def _dpapi_protect(data: bytes) -> bytes:
    import ctypes
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    src = crypto._DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = crypto._DataBlob()
    assert crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)
