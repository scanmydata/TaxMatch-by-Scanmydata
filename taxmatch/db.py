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
        -- (v3 προσθέτει: activity_state 'active'|'ceased'|'none'|'', cease_date, cease_reason, start_date)
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
    # v3 — κατάσταση επιχείρησης από το Μητρώο ΑΑΔΕ (ενεργή / διακοπή)
    """
    ALTER TABLE businesses ADD COLUMN activity_state TEXT NOT NULL DEFAULT '';
    ALTER TABLE businesses ADD COLUMN cease_date TEXT NOT NULL DEFAULT '';
    ALTER TABLE businesses ADD COLUMN cease_reason TEXT NOT NULL DEFAULT '';
    ALTER TABLE businesses ADD COLUMN start_date TEXT NOT NULL DEFAULT ''
    """,
    # v4 — ποιοι μήνες του πλήρους ημερολογίου taxheaven (σελίδα, όχι soft_dat.xml) έχουν ήδη κατέβει
    """
    CREATE TABLE calendar_sync (
        year_month  TEXT PRIMARY KEY,       -- 'YYYY-MM'
        synced_at   TEXT NOT NULL,
        event_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    # v5 — δέουσα επιμέλεια (ν. 4557/2018). ΣΚΟΠΙΜΑ χωρίς FOREIGN KEY/CASCADE προς `businesses`: η διαγραφή ενός
    # πελάτη ΔΕΝ πρέπει να σβήνει το ιστορικό δέουσας επιμέλειας (τήρηση 5 ετών μετά τη λήξη της σχέσης, άρθρο 30).
    """
    CREATE TABLE aml_profile (
        afm                TEXT PRIMARY KEY,
        pep_status         TEXT NOT NULL DEFAULT 'unknown',  -- no|domestic|foreign|family|associate|unknown
        relationship_start TEXT NOT NULL DEFAULT '',          -- ISO ημερομηνία έναρξης σχέσης
        relationship_end   TEXT NOT NULL DEFAULT '',          -- ISO ημερομηνία λήξης (αρχή της 5ετίας τήρησης)
        purpose            TEXT NOT NULL DEFAULT '',          -- σκοπός/φύση σχέσης + πηγές
        kyc_json           TEXT NOT NULL DEFAULT '{}',        -- {item: 'YYYY-MM-DD' ημερομηνία ολοκλήρωσης}
        notes              TEXT NOT NULL DEFAULT '',
        updated_at         TEXT NOT NULL
    );

    CREATE TABLE aml_assessments (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        afm             TEXT NOT NULL,
        client_name     TEXT NOT NULL DEFAULT '',             -- στιγμιότυπο: μένει και αν διαγραφεί ο πελάτης
        kind            TEXT NOT NULL DEFAULT 'initial',      -- initial|periodic|extraordinary
        assessed_on     TEXT NOT NULL,                        -- ISO ημερομηνία
        model           TEXT NOT NULL DEFAULT 'A',            -- A|B: ποιο μοντέλο αποφάσισε
        factors_json    TEXT NOT NULL DEFAULT '[]',
        overrides_json  TEXT NOT NULL DEFAULT '[]',
        sum_a           INTEGER NOT NULL DEFAULT 0,
        cat_a           TEXT NOT NULL DEFAULT '',
        total_b         INTEGER NOT NULL DEFAULT 0,
        cat_b           TEXT NOT NULL DEFAULT '',
        model_category  TEXT NOT NULL,
        final_category  TEXT NOT NULL,                        -- = model_category ή ΥΨΗΛΟΤΕΡΗ (ποτέ χαμηλότερη)
        escalation_note TEXT NOT NULL DEFAULT '',
        justification   TEXT NOT NULL DEFAULT '',
        assessor        TEXT NOT NULL DEFAULT '',
        approved_by     TEXT NOT NULL DEFAULT '',             -- έγκριση ανώτερου στελέχους (υψηλός κίνδυνος/ΠΕΠ)
        next_review     TEXT NOT NULL DEFAULT '',             -- ISO ημερομηνία επόμενης επανεξέτασης
        created_at      TEXT NOT NULL
    );
    CREATE INDEX idx_aml_assess_afm ON aml_assessments(afm, assessed_on);

    CREATE TABLE aml_office (
        item       TEXT PRIMARY KEY,                          -- q01..q23 | step01..step15 | doc_*
        state      TEXT NOT NULL DEFAULT '',                  -- yes|no|na|done|''
        note       TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );

    CREATE TABLE aml_register (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        kind       TEXT NOT NULL,                             -- training|report|rejection|whistle|audit
        event_date TEXT NOT NULL,
        afm        TEXT NOT NULL DEFAULT '',
        title      TEXT NOT NULL DEFAULT '',
        details    TEXT NOT NULL DEFAULT '',
        outcome    TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_aml_register_kind ON aml_register(kind, event_date)
    """,
    # v6 — δέουσα επιμέλεια: προφίλ συναλλαγών/προέλευσης κεφαλαίων, κατάσταση φακέλου, έγγραφα ανά νομική μορφή,
    # πραγματικοί δικαιούχοι, τρόπος εξόφλησης αμοιβής (βλ. aml/content.py)
    """
    ALTER TABLE aml_profile ADD COLUMN tx_json TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE aml_profile ADD COLUMN file_status TEXT NOT NULL DEFAULT '';
    ALTER TABLE aml_profile ADD COLUMN docs_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE aml_profile ADD COLUMN ubo_state TEXT NOT NULL DEFAULT '';
    ALTER TABLE aml_profile ADD COLUMN ubo_notes TEXT NOT NULL DEFAULT '';
    ALTER TABLE aml_profile ADD COLUMN fee_payment TEXT NOT NULL DEFAULT ''
    """,
    # v7 — δέουσα επιμέλεια: είδος πελάτη (διορθώσιμο) και υποθέσεις (εσωτερικές καταγγελίες ν. 4990/2022 /
    # εσωτερικές αναφορές ύποπτων συναλλαγών -> απόφαση για αναφορά στην Αρχή). Κι αυτές ΔΕΝ διαγράφονται με τον πελάτη.
    """
    ALTER TABLE aml_profile ADD COLUMN client_kind TEXT NOT NULL DEFAULT '';

    CREATE TABLE aml_cases (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        kind            TEXT NOT NULL,                        -- suspicion | whistle
        received_on     TEXT NOT NULL,                        -- ISO ημερομηνία παραλαβής/εντοπισμού
        channel         TEXT NOT NULL DEFAULT '',
        afm             TEXT NOT NULL DEFAULT '',             -- πελάτης (για suspicion· προαιρετικό για whistle)
        client_name     TEXT NOT NULL DEFAULT '',
        category        TEXT NOT NULL DEFAULT '',             -- κατηγορία παράβασης (whistle)
        description     TEXT NOT NULL DEFAULT '',
        reporter        TEXT NOT NULL DEFAULT '',             -- κενό αν ανώνυμη
        anonymous       INTEGER NOT NULL DEFAULT 0,
        handler         TEXT NOT NULL DEFAULT '',             -- υπεύθυνος παραλαβής/διερεύνησης
        status          TEXT NOT NULL DEFAULT 'received',
        ack_on          TEXT NOT NULL DEFAULT '',             -- βεβαίωση παραλαβής (whistle)
        feedback_on     TEXT NOT NULL DEFAULT '',             -- ενημέρωση καταγγέλλοντα (whistle)
        tx_description  TEXT NOT NULL DEFAULT '',             -- συναλλαγή(ές) (suspicion)
        tx_amount       TEXT NOT NULL DEFAULT '',
        tx_date         TEXT NOT NULL DEFAULT '',
        red_flags_json  TEXT NOT NULL DEFAULT '[]',
        decision        TEXT NOT NULL DEFAULT '',             -- report | no_report | exempt (suspicion) / outcome (whistle)
        decision_reason TEXT NOT NULL DEFAULT '',
        decided_on      TEXT NOT NULL DEFAULT '',
        authority_ref   TEXT NOT NULL DEFAULT '',             -- αριθμός πρωτοκόλλου αναφοράς στην Αρχή
        actions         TEXT NOT NULL DEFAULT '',
        closed_on       TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    CREATE INDEX idx_aml_cases_kind ON aml_cases(kind, received_on)
    """,
    # v8 — δέουσα επιμέλεια: νόμιμος εκπρόσωπος + πραγματικοί δικαιούχοι ανά πελάτη, και πρότυπα Word του γραφείου
    # (δήλωση/συμφωνητικό/αξιολόγηση × φυσικά/νομικά πρόσωπα — βλ. aml/templating.py)
    """
    ALTER TABLE aml_profile ADD COLUMN rep_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE aml_profile ADD COLUMN ubo_json TEXT NOT NULL DEFAULT '[]';

    CREATE TABLE aml_templates (
        slot        TEXT PRIMARY KEY,                     -- π.χ. 'declaration:legal'
        filename    TEXT NOT NULL DEFAULT '',
        content     BLOB NOT NULL,
        uploaded_at TEXT NOT NULL
    )
    """,
    # v9 — δέουσα επιμέλεια: έγγραφα φακέλου που ανακτήθηκαν αυτόματα (Μητρώο ΑΑΔΕ, Έντυπο Ν/Ε1/Ε3, ΚΜΠΔ) και
    # κωδικοί TAXISnet του ΝΟΜΙΜΟΥ ΕΚΠΡΟΣΩΠΟΥ (το ΚΜΠΔ δείχνει την εταιρεία μόνο σε αυτόν — βλ. aml/retrieval.py).
    # Χωρίς FK/CASCADE, όπως όλοι οι πίνακες aml_* (τήρηση 5ετίας).
    """
    CREATE TABLE aml_files (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        afm          TEXT NOT NULL,
        doc_key      TEXT NOT NULL,                        -- κλειδί της λίστας εγγράφων (aade, tax_return, ubo_registry…)
        kind         TEXT NOT NULL DEFAULT '',             -- registry | income_n | income_e1 | income_e3 | kmpd | kmpd_cert
        filename     TEXT NOT NULL,
        path         TEXT NOT NULL,
        bytes        INTEGER NOT NULL DEFAULT 0,
        source       TEXT NOT NULL DEFAULT '',             -- aade | kmpd | manual
        retrieved_at TEXT NOT NULL
    );
    CREATE INDEX idx_aml_files_afm ON aml_files(afm, doc_key);

    CREATE TABLE aml_rep_credentials (
        afm        TEXT PRIMARY KEY,                       -- ΑΦΜ του ΠΕΛΑΤΗ (νομικού προσώπου)
        taxis_user TEXT NOT NULL DEFAULT '',               -- enc:1:… του νόμιμου εκπροσώπου
        taxis_pass TEXT NOT NULL DEFAULT '',               -- enc:1:…
        updated_at TEXT NOT NULL
    )
    """,
    # v10 — δέουσα επιμέλεια: έλεγχοι σε λίστες κυρώσεων (τεκμήριο ελέγχου για τον φάκελο, άρθρο 30) και κωδικοί
    # Γ.Ε.ΜΗ. (businessportal) ανά πελάτη για τη σύνδεση στον τοπικό browser. Χωρίς FK/CASCADE (τήρηση 5ετίας).
    """
    CREATE TABLE aml_screenings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        afm         TEXT NOT NULL,
        screened_at TEXT NOT NULL,
        source      TEXT NOT NULL DEFAULT '',              -- π.χ. «EU FSF 22/09/2026»
        subjects_json TEXT NOT NULL DEFAULT '[]',          -- [{role, name, latin}]
        hits_json   TEXT NOT NULL DEFAULT '[]',            -- πιθανές ταυτίσεις (προς έλεγχο από τον λογιστή)
        result      TEXT NOT NULL DEFAULT 'clear',         -- clear | possible
        review_note TEXT NOT NULL DEFAULT ''               -- «ψευδώς θετικό επειδή…» / «επιβεβαιώθηκε…»
    );
    CREATE INDEX idx_aml_screenings_afm ON aml_screenings(afm, screened_at);

    CREATE TABLE aml_gemi_credentials (
        afm        TEXT PRIMARY KEY,
        gemi_user  TEXT NOT NULL DEFAULT '',               -- enc:1:…
        gemi_pass  TEXT NOT NULL DEFAULT '',               -- enc:1:…
        updated_at TEXT NOT NULL
    )
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
