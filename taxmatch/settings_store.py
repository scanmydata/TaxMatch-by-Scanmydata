"""Ρυθμίσεις εφαρμογής (πίνακας `settings`). Τα μυστικά αποθηκεύονται κρυπτογραφημένα.

Προτεραιότητα τιμής: Ρυθμίσεις εφαρμογής (βάση) > μεταβλητή περιβάλλοντος/.env > default.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import crypto


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    secret: bool = False
    env: Optional[str] = None
    default: str = ""


DEFINITIONS: dict[str, SettingDef] = {d.key: d for d in [
    # OpenRouter προεπιλογή (όχι Groq): έχει πραγματικά δωρεάν μοντέλα (':free') χωρίς να χρειάζεται το Groq να
    # είναι απαραίτητο για να δουλέψει η ανάλυση άρθρων — ο χρήστης μπορεί να αλλάξει πάροχο ελεύθερα στις Ρυθμίσεις.
    SettingDef("llm_provider", "Πάροχος LLM", default="openrouter"),
    SettingDef("llm_model_groq", "Μοντέλο Groq", default="llama-3.3-70b-versatile"),
    # 2026-09-23: το παλιό default (meta-llama/llama-3.3-70b-instruct:free) επαληθεύτηκε ζωντανά ότι είναι πλέον
    # 404 «unavailable for free» — ο κατάλογος δωρεάν μοντέλων του OpenRouter αλλάζει συχνά (βλ. llm_extract.py:
    # PREFERRED_MODELS). Αυτό δούλεψε σε ζωντανή δοκιμή JSON-mode την ίδια μέρα.
    SettingDef("llm_model_openrouter", "Μοντέλο OpenRouter", default="liquid/lfm-2.5-2.6b:free"),
    SettingDef("groq_api_key", "Groq API key", secret=True, env="GROQ_API_KEY"),
    SettingDef("openrouter_api_key", "OpenRouter API key", secret=True, env="OPENROUTER_API_KEY"),
    SettingDef("business_portal_key", "Business Portal (ΓΕΜΗ) API key", secret=True, env="BUSINESS_PORTAL_KEY"),
    SettingDef("aade_user", "TAXISnet χρήστης (γραφείου)", secret=True, env="AADE_USER"),
    SettingDef("aade_pass", "TAXISnet κωδικός (γραφείου)", secret=True, env="AADE_PASS"),
    # Κατάσταση (όχι ρυθμίσεις χρήστη): αποτέλεσμα της τελευταίας σύνδεσης/ανάλυσης, για τις ειδοποιήσεις της εφαρμογής
    SettingDef("aade_office_status", "Κατάσταση κωδικών γραφείου (ok|invalid|'')"),
    SettingDef("aade_office_message", "Μήνυμα σύνδεσης γραφείου"),
    SettingDef("llm_last_error", "Τελευταίο σφάλμα ανάλυσης LLM"),
    SettingDef("lookback_days", "Ημέρες αναδρομής άρθρων", default="10"),
    SettingDef("max_extractions_per_run", "Μέγιστες εξαγωγές LLM ανά έλεγχο", default="60"),
    SettingDef("fetch_full_text", "Λήψη πλήρους κειμένου άρθρου", default="1"),
    SettingDef("daily_time", "Ώρα καθημερινού ελέγχου", default="08:00"),
]}


def _row(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT value, encrypted FROM settings WHERE key = ?", (key,)).fetchone()


def get(conn: sqlite3.Connection, key: str) -> str:
    d = DEFINITIONS.get(key)
    row = _row(conn, key)
    if row is not None and row["value"] != "":
        return crypto.dec(row["value"]) if row["encrypted"] else row["value"]
    if d and d.env and os.getenv(d.env):
        return os.environ[d.env]
    return d.default if d else ""


def is_set(conn: sqlite3.Connection, key: str) -> bool:
    """Έχει τιμή (από βάση ή env) — χωρίς να ξεκλειδώσει το μυστικό."""
    d = DEFINITIONS.get(key)
    row = _row(conn, key)
    if row is not None and row["value"] != "":
        return True
    return bool(d and d.env and os.getenv(d.env))


def set_value(conn: sqlite3.Connection, key: str, value: str) -> None:
    d = DEFINITIONS.get(key)
    secret = bool(d and d.secret)
    stored = crypto.enc(value) if (secret and value) else value
    conn.execute(
        "INSERT INTO settings(key, value, encrypted) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, encrypted=excluded.encrypted",
        (key, stored, 1 if secret and value else 0),
    )


def get_int(conn: sqlite3.Connection, key: str, fallback: int) -> int:
    try:
        return int(get(conn, key))
    except (TypeError, ValueError):
        return fallback


def source_enabled(conn: sqlite3.Connection, source_id: str, default: bool) -> bool:
    row = _row(conn, f"source_enabled:{source_id}")
    if row is None:
        return default
    return row["value"] == "1"


def set_source_enabled(conn: sqlite3.Connection, source_id: str, enabled: bool) -> None:
    conn.execute(
        "INSERT INTO settings(key, value, encrypted) VALUES (?,?,0) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"source_enabled:{source_id}", "1" if enabled else "0"),
    )
