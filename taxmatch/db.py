"""SQLite: σύνδεση + schema (PRODUCT_SPEC §5) με απλό versioned migration μέσω PRAGMA user_version.

Η βάση μοιράζεται ανάμεσα στο UI και στο headless daily task, γι' αυτό WAL + busy_timeout.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config

MIGRATIONS: list[str] = [
    # v1 — αρχικό schema
    """
    CREATE TABLE businesses (
        afm               TEXT PRIMARY KEY,
        ar_gemi           TEXT,
        name              TEXT NOT NULL DEFAULT '',
        legal_form        TEXT NOT NULL DEFAULT '',
        status            TEXT NOT NULL DEFAULT '',
        address           TEXT NOT NULL DEFAULT '',
        doy               TEXT NOT NULL DEFAULT '',
        books_category    TEXT NOT NULL DEFAULT '',      -- 'Β' | 'Γ' | ''
        books_category_raw TEXT NOT NULL DEFAULT '',
        vat_subject       INTEGER,                       -- 1 / 0 / NULL (άγνωστο)
        vat_period_type   TEXT NOT NULL DEFAULT '',      -- 'monthly' | 'quarterly' | ''
        kad_main_code     TEXT NOT NULL DEFAULT '',
        kad_main_desc     TEXT NOT NULL DEFAULT '',
        notes             TEXT NOT NULL DEFAULT '',
        lookup_status     TEXT NOT NULL DEFAULT 'pending', -- pending | ok | partial | failed | manual
        lookup_error      TEXT NOT NULL DEFAULT '',
        lookup_raw        TEXT NOT NULL DEFAULT '',        -- JSON: ωμά δεδομένα πηγών (για διόρθωση parsing)
        lookup_at         TEXT,
        source            TEXT NOT NULL DEFAULT 'manual',  -- manual | excel | lookup
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    );

    CREATE TABLE business_kad (
        afm     TEXT NOT NULL REFERENCES businesses(afm) ON DELETE CASCADE,
        code    TEXT NOT NULL,
        descr   TEXT NOT NULL DEFAULT '',
        is_main INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (afm, code)
    );

    CREATE TABLE articles (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        source            TEXT NOT NULL,                 -- id πηγής (sources.py)
        title             TEXT NOT NULL,
        url               TEXT NOT NULL,
        url_hash          TEXT NOT NULL UNIQUE,
        published_at      TEXT,
        fetched_at        TEXT NOT NULL,
        category          TEXT NOT NULL DEFAULT '',
        raw_summary       TEXT NOT NULL DEFAULT '',
        full_text         TEXT NOT NULL DEFAULT '',
        extracted_json    TEXT,                          -- scope JSON
        extraction_status TEXT NOT NULL DEFAULT 'pending', -- pending | done | failed | irrelevant
        extraction_error  TEXT NOT NULL DEFAULT '',
        extraction_model  TEXT NOT NULL DEFAULT '',
        extraction_tries  INTEGER NOT NULL DEFAULT 0,
        deadline          TEXT                           -- ISO ημερομηνία, denormalized από extracted_json
    );
    CREATE INDEX idx_articles_status ON articles(extraction_status);
    CREATE INDEX idx_articles_published ON articles(published_at);

    CREATE TABLE matches (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id      INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        afm             TEXT NOT NULL REFERENCES businesses(afm) ON DELETE CASCADE,
        matched_reason  TEXT NOT NULL,
        confidence      REAL NOT NULL DEFAULT 1.0,
        created_at      TEXT NOT NULL,
        user_feedback   INTEGER,                        -- 1 (👍) | -1 (👎) | NULL
        feedback_at     TEXT,
        UNIQUE (article_id, afm)
    );
    CREATE INDEX idx_matches_afm ON matches(afm);

    CREATE TABLE obligations_general (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        guid        TEXT NOT NULL UNIQUE,
        title       TEXT NOT NULL,
        due_date    TEXT NOT NULL,                      -- ISO ημερομηνία
        source_url  TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        fetched_at  TEXT NOT NULL
    );
    CREATE INDEX idx_obligations_due ON obligations_general(due_date);

    CREATE TABLE settings (
        key       TEXT PRIMARY KEY,
        value     TEXT NOT NULL DEFAULT '',
        encrypted INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger     TEXT NOT NULL,                      -- scheduled | manual
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        status      TEXT NOT NULL DEFAULT 'running',    -- running | ok | error
        stats_json  TEXT NOT NULL DEFAULT '{}',
        error       TEXT NOT NULL DEFAULT ''
    );
    """,
    # v2 — κωδικοί TAXISnet ανά πελάτη (προαιρετικοί, κρυπτογραφημένοι), dedup άρθρων
    """
    CREATE TABLE client_credentials (
        afm           TEXT PRIMARY KEY REFERENCES businesses(afm) ON DELETE CASCADE,
        taxis_user    TEXT NOT NULL DEFAULT '',      -- enc:1:…
        taxis_pass    TEXT NOT NULL DEFAULT '',      -- enc:1:…
        check_status  TEXT NOT NULL DEFAULT '',      -- '' | ok | invalid | error
        check_message TEXT NOT NULL DEFAULT '',
        checked_at    TEXT,
        updated_at    TEXT NOT NULL
    );

    ALTER TABLE articles ADD COLUMN duplicate_of INTEGER;
    ALTER TABLE articles ADD COLUMN title_key TEXT NOT NULL DEFAULT '';
    CREATE INDEX idx_articles_titlekey ON articles(title_key)
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """Νέα σύνδεση (μία ανά thread/request). Δημιουργεί/αναβαθμίζει το schema αν χρειάζεται."""
    p = Path(path) if path else config.db_path()
    conn = sqlite3.connect(str(p), timeout=15, isolation_level=None)  # autocommit· transactions ρητά
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(MIGRATIONS):
        return
    # Δύο processes (UI + task) μπορεί να ξεκινήσουν μαζί: ο BEGIN IMMEDIATE τα σειριοποιεί.
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i in range(version, len(MIGRATIONS)):
            for stmt in _split(MIGRATIONS[i]):
                conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _split(script: str) -> list[str]:
    # Τα scripts δεν περιέχουν ';' μέσα σε strings/triggers, οπότε ο απλός διαχωρισμός αρκεί.
    return [s.strip() for s in script.split(";") if s.strip()]
