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
    # Λήψη myDATA (έσοδα + έξοδα, χωρίς PDF) στον καθημερινό έλεγχο — μόνο για πελάτες με κωδικούς myDATA
    SettingDef("daily_mydata", "Λήψη myDATA στον καθημερινό έλεγχο", default="1"),
    SettingDef("mydata_last_run", "Τελευταία προγραμματισμένη λήψη myDATA"),
    # Δέουσα επιμέλεια (taxmatch/aml): μία μεθοδολογία για ΟΛΟΥΣ τους πελάτες — γι' αυτό είναι ρύθμιση γραφείου
    SettingDef("aml_model", "Μοντέλο βαθμολόγησης (A|B)", default="A"),
    SettingDef("aml_review_low", "Επανεξέταση χαμηλού κινδύνου (μήνες)", default="24"),
    SettingDef("aml_review_medium", "Επανεξέταση μέτριου κινδύνου (μήνες)", default="12"),
    SettingDef("aml_review_high", "Επανεξέταση υψηλού κινδύνου (μήνες)", default="12"),
    SettingDef("aml_officer", "Υπεύθυνος συμμόρφωσης"),
    # Κεφαλίδα γραφείου στα έγγραφα (έως 4 γραμμές: επωνυμία, διεύθυνση, δραστηριότητα, ΑΦΜ/ΔΟΥ/τηλέφωνο)
    SettingDef("aml_office_info", "Στοιχεία γραφείου για τα έγγραφα"),
    SettingDef("aml_office_city", "Τόπος υπογραφής εγγράφων"),
    # Email προς πελάτες (taxmatch/mailer.py) — SMTP του γραφείου· ο κωδικός κρυπτογραφημένος
    SettingDef("smtp_host", "Διακομιστής SMTP"),
    SettingDef("smtp_port", "Θύρα SMTP", default="587"),
    SettingDef("smtp_security", "Ασφάλεια SMTP (starttls|ssl|none)", default="starttls"),
    SettingDef("smtp_user", "Χρήστης SMTP"),
    SettingDef("smtp_pass", "Κωδικός SMTP", secret=True, env="TAXMATCH_SMTP_PASS"),
    SettingDef("smtp_from", "Αποστολέας (email)"),
    SettingDef("email_bcc", "Κρυφή κοινοποίηση (Bcc) στο γραφείο"),
    SettingDef("email_signature", "Υπογραφή email"),
    # Αυτόματα email στον καθημερινό έλεγχο: οφειλές μόνο όταν αλλάξουν, νέα μόνο όσα δεν έχουν σταλεί
    SettingDef("auto_email_debts", "Αυτόματο email οφειλών", default="0"),
    SettingDef("auto_email_news", "Αυτόματο email νέων", default="0"),
    SettingDef("auto_email_news_freq", "Συχνότητα email νέων (daily|weekly)", default="weekly"),
    SettingDef("auto_email_weekday", "Ημέρα εβδομαδιαίου email (1=Δευτέρα)", default="1"),
    SettingDef("auto_email_news_days", "Νέα των τελευταίων N ημερών", default="14"),
    SettingDef("daily_debts", "Λήψη οφειλών ΑΑΔΕ στον καθημερινό έλεγχο", default="0"),
    # Μέλη/εκπρόσωποι εταιρειών από το Μητρώο ΑΑΔΕ με την ανάκτηση στοιχείων (members.py) — επιλογή του χρήστη
    SettingDef("fetch_members", "Άντληση μελών εταιρειών", default="1"),
    # «Αναζήτηση Βασικών Στοιχείων Μητρώου» (RgWsPublic2) — ειδικοί κωδικοί του γραφείου: ατομική επιχείρηση μελών
    SettingDef("rg_user", "Ειδικοί κωδικοί Μητρώου (RgWsPublic2) — χρήστης", secret=True),
    SettingDef("rg_pass", "Ειδικοί κωδικοί Μητρώου (RgWsPublic2) — κωδικός", secret=True),
    # «Τα Αιτήματά μου» της ΑΑΔΕ στον καθημερινό έλεγχο (requests_aade.py) — ειδοποίηση για απαντήσεις/αλλαγές
    SettingDef("daily_requests", "Έλεγχος αιτημάτων ΑΑΔΕ στον καθημερινό έλεγχο", default="0"),
    # Ειδοποίηση «μπορούν να επανέλθουν στο τρίμηνο» (notices.py): αναγνωσμένη για ΑΥΤΗ την προθεσμία/αυτούς τους
    # πελάτες· ο αστερίσκος της στήλης ΦΠΑ μένει ώς την επόμενη εκκίνηση, ή μόνιμα με το `vat_option_mark_keep`.
    SettingDef("notice_vat_quarter_read", "Αναγνωσμένη ειδοποίηση επιστροφής στο τρίμηνο"),
    SettingDef("vat_option_mark_keep", "Μόνιμος αστερίσκος ΦΠΑ για επιστροφή στο τρίμηνο", default="0"),
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
