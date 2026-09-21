import os
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Κάθε test παίρνει δικό του φάκελο δεδομένων και κλειδί — τίποτα δεν αγγίζει τα πραγματικά δεδομένα."""
    monkeypatch.setenv("TAXMATCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TAXMATCH_ENC_KEY", Fernet.generate_key().decode())
    for var in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "BUSINESS_PORTAL_KEY", "AADE_USER", "AADE_PASS"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def conn():
    from taxmatch import db
    c = db.connect()
    yield c
    c.close()
