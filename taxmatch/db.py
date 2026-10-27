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
    # v11 — (α) τιμοκατάλογος υπηρεσιών του γραφείου και αμοιβές ανά πελάτη· (β) έλεγχος ολοκλήρωσης υποχρεώσεων
    # απευθείας από την ΑΑΔΕ (EasyCheck: Φ2/Φ4/ΦΜΥ/Ε1/Ν… «έχει υποβληθεί;»)· (γ) «εργοδότης» / «Intrastat» από την
    # εισαγωγή Excel (στήλη «Μισθοδοσία», κωδικοί Intrastat) — λύνουν τα «υπό προϋποθέσεις» των ΑΠΔ/Ε4/Intrastat·
    # (δ) βαθμός σχετικότητας ML ανά άρθρο (paperless-ngx-style ταξινομητής, matching/ml.py).
    """
    CREATE TABLE office_services (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        name           TEXT NOT NULL,
        default_amount REAL NOT NULL DEFAULT 0,             -- προ ΦΠΑ
        period         TEXT NOT NULL DEFAULT 'monthly',     -- monthly | quarterly | yearly | once
        active         INTEGER NOT NULL DEFAULT 1,
        sort           INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE client_fees (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        afm         TEXT NOT NULL,
        service_id  INTEGER,                                 -- από τον τιμοκατάλογο (προαιρετικό)
        description TEXT NOT NULL DEFAULT '',
        amount      REAL NOT NULL DEFAULT 0,                 -- προ ΦΠΑ
        period      TEXT NOT NULL DEFAULT 'monthly',
        start_date  TEXT NOT NULL DEFAULT '',
        end_date    TEXT NOT NULL DEFAULT '',
        notes       TEXT NOT NULL DEFAULT '',
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX idx_client_fees_afm ON client_fees(afm);
    CREATE TABLE obligation_checks (
        afm         TEXT NOT NULL,
        year        INTEGER NOT NULL,
        obligation  TEXT NOT NULL,                           -- κλειδί του easycheck.OBLIGATIONS (Φ2, ΦΜΥ, E1…)
        period      TEXT NOT NULL DEFAULT '',                -- «1ο Τρίμηνο 2026», «Μάρτιος 2026», «» για ετήσιες
        period_end  TEXT NOT NULL DEFAULT '',                -- ISO, για ταξινόμηση/αντιστοίχιση με προθεσμίες
        status      TEXT NOT NULL,                           -- submitted | pending | not_submitted | not_obligated
        detail      TEXT NOT NULL DEFAULT '',
        checked_at  TEXT NOT NULL,
        PRIMARY KEY (afm, year, obligation, period)
    );
    -- ΠΡΟΣΟΧΗ: σχόλιο ΜΕΣΑ σε ALTER … ADD COLUMN γράφεται στο σχήμα του πίνακα και το χαλάει («incomplete input»).
    -- employer: NULL = άγνωστο · 1/0 (στήλη «Μισθοδοσία» του Excel) · intrastat: NULL = άγνωστο · 1 = κωδικοί Intrastat
    -- ml_score: P(σχετικό) από matching/ml.py — σειρά ανάλυσης LLM
    -- myDATA ανά πελάτη (port του Timologio Downloader): κωδικοί API (enc:1:), παραστατικά (ένα ανά MARK), ποιοι μήνες
    -- έχουν κατέβει (ώστε «κανένα ενδοκοινοτικό» να σημαίνει «όχι» και όχι «άγνωστο»).
    CREATE TABLE mydata_credentials (
        afm         TEXT PRIMARY KEY,
        mydata_user TEXT NOT NULL DEFAULT '',
        mydata_key  TEXT NOT NULL DEFAULT '',
        check_status TEXT NOT NULL DEFAULT '',
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE mydata_docs (
        afm           TEXT NOT NULL,
        mark          TEXT NOT NULL,
        direction     TEXT NOT NULL,
        invoice_type  TEXT NOT NULL DEFAULT '',
        series        TEXT NOT NULL DEFAULT '',
        aa            TEXT NOT NULL DEFAULT '',
        issue_date    TEXT NOT NULL DEFAULT '',
        issuer_vat    TEXT NOT NULL DEFAULT '',
        issuer_name   TEXT NOT NULL DEFAULT '',
        issuer_country TEXT NOT NULL DEFAULT '',
        counter_vat   TEXT NOT NULL DEFAULT '',
        counter_name  TEXT NOT NULL DEFAULT '',
        counter_country TEXT NOT NULL DEFAULT '',
        net           REAL NOT NULL DEFAULT 0,
        vat           REAL NOT NULL DEFAULT 0,
        total         REAL NOT NULL DEFAULT 0,
        withheld      REAL NOT NULL DEFAULT 0,
        withheld_json TEXT NOT NULL DEFAULT '{}',
        cancelled     INTEGER NOT NULL DEFAULT 0,
        url           TEXT NOT NULL DEFAULT '',
        provider      TEXT NOT NULL DEFAULT '',
        pdf_path      TEXT NOT NULL DEFAULT '',
        pdf_status    TEXT NOT NULL DEFAULT '',
        fetched_at    TEXT NOT NULL,
        PRIMARY KEY (afm, mark)
    );
    CREATE INDEX idx_mydata_docs_month ON mydata_docs(afm, issue_date);
    CREATE TABLE mydata_sync (
        afm       TEXT NOT NULL,
        ym        TEXT NOT NULL,
        synced_at TEXT NOT NULL,
        n_docs    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (afm, ym)
    );
    ALTER TABLE businesses ADD COLUMN employer INTEGER;
    ALTER TABLE businesses ADD COLUMN intrastat INTEGER;
    ALTER TABLE articles ADD COLUMN ml_score REAL
    """,
    # v12 — (α) ποσά των υποβληθεισών δηλώσεων (Φ2: αποτέλεσμα/οφειλή/ταυτότητα οφειλής — port του runner
    # lib/aade-vat-http.js) και είδος περιόδου (μήνας/τρίμηνο) ανά γραμμή ελέγχου· (β) περίοδος ΦΠΑ από τις ΠΡΑΓΜΑΤΙΚΕΣ
    # υποχρεώσεις Φ2 (`vat_period_source`='easycheck' — η ανάκτηση από το Μητρώο δεν την πατά)· (γ) επωνυμίες
    # αντισυμβαλλομένων myDATA (από παραστατικά / πελάτες / VIES, όπως το Timologio Downloader).
    """
    -- party_names: name='' με source='vies_miss' = ρωτήθηκε στο VIES χωρίς απάντηση (να μην ξαναρωτηθεί)
    CREATE TABLE party_names (
        vat        TEXT PRIMARY KEY,
        name       TEXT NOT NULL DEFAULT '',
        source     TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );
    -- obligation_done: ο χρήστης σημείωσε χειροκίνητα «ολοκληρώθηκε» μια προθεσμία πελάτη (π.χ. ψηφιακό τέλος
    -- συναλλαγής — δεν υπάρχει ακόμη αυτόματος έλεγχος υποβολής)
    CREATE TABLE obligation_done (
        afm       TEXT NOT NULL,
        rule_id   TEXT NOT NULL,
        ref_month TEXT NOT NULL,
        done_at   TEXT NOT NULL,
        note      TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (afm, rule_id, ref_month)
    );
    -- mydata_docs.stamp: ψηφιακό τέλος συναλλαγής (πρώην χαρτόσημο, totalStampDutyAmount)· vat_class: 1 = το
    -- παραστατικό φέρει ΦΠΑ (κατηγορία ΦΠΑ με συντελεστή ή χαρακτηρισμό VAT_…) — για προειδοποίηση σε μη υπόχρεους
    ALTER TABLE mydata_docs ADD COLUMN stamp REAL NOT NULL DEFAULT 0;
    ALTER TABLE mydata_docs ADD COLUMN vat_class INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE obligation_checks ADD COLUMN amounts_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE obligation_checks ADD COLUMN period_type TEXT NOT NULL DEFAULT '';
    ALTER TABLE businesses ADD COLUMN vat_period_source TEXT NOT NULL DEFAULT ''
    """,
    # v13 — e-ΕΦΚΑ εργοδότη: ΑΜΕ (Αριθμός Μητρώου Εργοδότη — από Excel ή ανάκτηση ePistErg) και κωδικοί ΙΚΑ εργοδότη
    # (enc:1:) για τον έλεγχο υποβολής ΑΠΔ από την οικονομική καρτέλα εργοδότη. Χωριστά από το businesses, όπως οι
    # κωδικοί TAXISnet, ώστε να μη διαρρέουν σε ερωτήματα λίστας.
    """
    CREATE TABLE efka_credentials (
        afm           TEXT PRIMARY KEY,
        ame           TEXT NOT NULL DEFAULT '',
        ame_source    TEXT NOT NULL DEFAULT '',
        ika_user      TEXT NOT NULL DEFAULT '',
        ika_pass      TEXT NOT NULL DEFAULT '',
        check_status  TEXT NOT NULL DEFAULT '',
        check_message TEXT NOT NULL DEFAULT '',
        checked_at    TEXT,
        updated_at    TEXT NOT NULL
    )
    """,
    # v14 — (α) mydata_docs.rent: παραστατικό ενοικίου (8.1 ή ψηφιακό τέλος 3,6%) — το ψηφιακό τέλος των μισθώσεων
    # αποδίδεται με τη δήλωση φορολογίας εισοδήματος, όχι μηνιαία· (β) στοιχεία επικοινωνίας από το Μητρώο ΑΑΔΕ
    # (getLdapInfo) — contact_source 'manual' = τα άλλαξε ο χρήστης, η ανάκτηση δεν τα πατά· (γ) οφειλές ΑΑΔΕ
    # (εκτός/σε ρύθμιση, με ταυτότητα οφειλής και PDF — port του Tax Center aade-debts.js)· (δ) ιστορικό email.
    """
    ALTER TABLE mydata_docs ADD COLUMN rent INTEGER NOT NULL DEFAULT 0;
    UPDATE mydata_docs SET rent=1 WHERE invoice_type='8.1' OR (stamp>0 AND net>0 AND abs(stamp/net-0.036)<0.0015);
    ALTER TABLE businesses ADD COLUMN email TEXT NOT NULL DEFAULT '';
    ALTER TABLE businesses ADD COLUMN mobile TEXT NOT NULL DEFAULT '';
    ALTER TABLE businesses ADD COLUMN phone TEXT NOT NULL DEFAULT '';
    ALTER TABLE businesses ADD COLUMN contact_source TEXT NOT NULL DEFAULT '';
    ALTER TABLE businesses ADD COLUMN email_auto INTEGER NOT NULL DEFAULT 1;
    CREATE TABLE aade_debts (
        afm          TEXT NOT NULL,
        kind         TEXT NOT NULL,
        ref          TEXT NOT NULL,
        title        TEXT NOT NULL DEFAULT '',
        overdue      REAL NOT NULL DEFAULT 0,
        non_overdue  REAL NOT NULL DEFAULT 0,
        total        REAL NOT NULL DEFAULT 0,
        code         TEXT NOT NULL DEFAULT '',
        pdf_path     TEXT NOT NULL DEFAULT '',
        details_json TEXT NOT NULL DEFAULT '{}',
        fetched_at   TEXT NOT NULL,
        PRIMARY KEY (afm, kind, ref)
    );
    CREATE TABLE debt_sync (
        afm         TEXT PRIMARY KEY,
        synced_at   TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT '',
        message     TEXT NOT NULL DEFAULT '',
        overdue     REAL NOT NULL DEFAULT 0,
        non_overdue REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE email_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        afm         TEXT NOT NULL,
        kind        TEXT NOT NULL,
        recipient   TEXT NOT NULL,
        subject     TEXT NOT NULL,
        fingerprint TEXT NOT NULL DEFAULT '',
        auto        INTEGER NOT NULL DEFAULT 0,
        status      TEXT NOT NULL,
        error       TEXT NOT NULL DEFAULT '',
        sent_at     TEXT NOT NULL
    )
    """,
    # v15 — όλη η λογική οφειλών/αιτημάτων του ScanMyData Tax Center (ρητό αίτημα χρήστη 2026-10-22): οφειλές ανά πύλη
    # (ΑΑΔΕ / ΚΕΑΟ / τρέχουσες εισφορές ΕΦΚΑ) με επόμενη δόση· ΑΜΚΑ (enc:1:) για ΚΕΑΟ/ΕΦΚΑ φυσικών προσώπων· από ποιο
    # πεδίο του Μητρώου ήρθε το email (mail2 = του υπόχρεου, mailemep = εκπροσώπου/λογιστή, mail = παλιό TAXISnet) και
    # πότε· μέλη/εκπρόσωποι εταιρειών (ΑΑΔΕ getMelh/getSxeseisEpixeirhshs ή Excel)· «Τα Αιτήματά μου» της ΑΑΔΕ.
    """
    ALTER TABLE aade_debts ADD COLUMN source TEXT NOT NULL DEFAULT 'aade';
    ALTER TABLE aade_debts ADD COLUMN next_due TEXT NOT NULL DEFAULT '';
    ALTER TABLE aade_debts ADD COLUMN next_amount REAL NOT NULL DEFAULT 0;
    ALTER TABLE aade_debts ADD COLUMN instalments INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE aade_debts ADD COLUMN unpaid INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE debt_source_sync (
        afm       TEXT NOT NULL,
        source    TEXT NOT NULL,
        synced_at TEXT NOT NULL,
        status    TEXT NOT NULL DEFAULT '',
        message   TEXT NOT NULL DEFAULT '',
        total     REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (afm, source)
    );
    ALTER TABLE efka_credentials ADD COLUMN amka TEXT NOT NULL DEFAULT '';
    ALTER TABLE businesses ADD COLUMN email_origin TEXT NOT NULL DEFAULT '';
    ALTER TABLE businesses ADD COLUMN contact_at TEXT NOT NULL DEFAULT '';
    CREATE TABLE company_members (
        company_afm TEXT NOT NULL,
        member_afm  TEXT NOT NULL,
        name        TEXT NOT NULL DEFAULT '',
        role        TEXT NOT NULL DEFAULT '',
        share       TEXT NOT NULL DEFAULT '',
        since       TEXT NOT NULL DEFAULT '',
        source      TEXT NOT NULL DEFAULT '',
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (company_afm, member_afm)
    );
    CREATE TABLE aade_requests (
        afm         TEXT NOT NULL,
        id          TEXT NOT NULL,
        case_no     TEXT NOT NULL DEFAULT '',
        submitted   TEXT NOT NULL DEFAULT '',
        updated     TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT '',
        topic       TEXT NOT NULL DEFAULT '',
        process     TEXT NOT NULL DEFAULT '',
        service     TEXT NOT NULL DEFAULT '',
        text        TEXT NOT NULL DEFAULT '',
        answer      TEXT NOT NULL DEFAULT '',
        rejected    INTEGER NOT NULL DEFAULT 0,
        stamp       TEXT NOT NULL DEFAULT '',
        change      TEXT NOT NULL DEFAULT '',
        seen        INTEGER NOT NULL DEFAULT 1,
        files_json  TEXT NOT NULL DEFAULT '[]',
        fetched_at  TEXT NOT NULL,
        PRIMARY KEY (afm, id)
    );
    CREATE TABLE request_sync (
        afm       TEXT PRIMARY KEY,
        synced_at TEXT NOT NULL,
        status    TEXT NOT NULL DEFAULT '',
        message   TEXT NOT NULL DEFAULT ''
    )
    """,
    # v16 (2026-10-23): διακριτικός τίτλος (Μητρώο `commercialtitle`, και για αναζήτηση)· email βοηθού/εκπροσώπου
    # χωριστά από το προσωπικό (`getLdapInfo`: κύριο = `mail2` που δηλώνει ο ίδιος ο υπόχρεος· `mail` = email εγγραφής
    # TAXISnet, συχνά του βοηθού/λογιστή· `mailemep` = εκπροσώπου). Όσα είχαν έρθει από `mail`/`mailemep` μετακινούνται.
    """
    ALTER TABLE businesses ADD COLUMN trade_name TEXT NOT NULL DEFAULT '';
    ALTER TABLE businesses ADD COLUMN email_assistant TEXT NOT NULL DEFAULT '';
    UPDATE businesses SET email_assistant=email, email='', email_origin=''
        WHERE contact_source='aade' AND email_origin IN ('mail', 'mailemep') AND email<>''
    """,
    # v17: επιπλέον ΑΜΕ οικοδομοτεχνικών έργων και επιλογή αναζήτησής του ανά εταιρεία.
    """
    ALTER TABLE efka_credentials ADD COLUMN ame_oikodomotexnikon TEXT NOT NULL DEFAULT '';
    ALTER TABLE efka_credentials ADD COLUMN search_oikodomotexnika INTEGER NOT NULL DEFAULT 0
    """,
    # v18: ιστορικό μελών ανά φορολογική χρήση και διόρθωση της αντιστοίχισης email του Μητρώου ΑΑΔΕ.
    """
    ALTER TABLE businesses ADD COLUMN email_assistant_origin TEXT NOT NULL DEFAULT '';
    UPDATE businesses SET email=email_assistant, email_assistant=email,
        email_origin='', email_assistant_origin=email_origin
        WHERE contact_source='aade';
    CREATE TABLE company_member_history (
        company_afm TEXT NOT NULL,
        tax_year    INTEGER NOT NULL,
        member_afm  TEXT NOT NULL,
        name        TEXT NOT NULL DEFAULT '',
        role        TEXT NOT NULL DEFAULT '',
        share       TEXT NOT NULL DEFAULT '',
        since       TEXT NOT NULL DEFAULT '',
        source      TEXT NOT NULL DEFAULT '',
        snapshot_at TEXT NOT NULL,
        PRIMARY KEY (company_afm, tax_year, member_afm)
    );
    INSERT INTO company_member_history(company_afm, tax_year, member_afm, name, role, share, since, source, snapshot_at)
        SELECT company_afm, CAST(strftime('%Y','now') AS INTEGER), member_afm, name, role, share, since, source, updated_at
        FROM company_members
    """,
    # v19 (2026-10-27): τέλη/λοιποί φόροι ανά κατηγορία myDATA (σακούλα, πλαστικά, παρεπιδημούντες, διαμονή) και
    # ΑΜΕ ανά εργοδότη — πολλά ΑΜΕ (ιδίως οικοδομοτεχνικά), το καθένα με ΔΙΚΟΥΣ του κωδικούς ΙΚΑ, για έλεγχο ΑΠΔ ΑΝΑ ΑΜΕ.
    """
    ALTER TABLE mydata_docs ADD COLUMN taxes_json TEXT NOT NULL DEFAULT '{}';
    CREATE TABLE efka_ames (
        afm        TEXT NOT NULL,
        ame        TEXT NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'koinon',
        name       TEXT NOT NULL DEFAULT '',
        apd_code   TEXT NOT NULL DEFAULT '',
        ika_user   TEXT NOT NULL DEFAULT '',
        ika_pass   TEXT NOT NULL DEFAULT '',
        active     INTEGER NOT NULL DEFAULT 1,
        source     TEXT NOT NULL DEFAULT '',
        check_status  TEXT NOT NULL DEFAULT '',
        check_message TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (afm, ame)
    );
    INSERT OR IGNORE INTO efka_ames(afm, ame, kind, source, updated_at)
        SELECT afm, ame, 'koinon', ame_source, updated_at FROM efka_credentials WHERE ame<>'';
    INSERT OR IGNORE INTO efka_ames(afm, ame, kind, source, updated_at)
        SELECT afm, ame_oikodomotexnikon, 'oikodom', 'efka', updated_at FROM efka_credentials
        WHERE ame_oikodomotexnikon<>''
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
