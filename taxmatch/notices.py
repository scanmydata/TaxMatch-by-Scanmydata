"""Μόνιμες ειδοποιήσεις μέσα στην εφαρμογή (πάνω-πάνω σε κάθε σελίδα) για προβλήματα που θέλουν ενέργεια του χρήστη:
λάθος κωδικοί TAXISnet, αποτυχία ανάλυσης LLM. Υπολογίζονται από την κατάσταση στη βάση — εξαφανίζονται μόνες τους
όταν λυθεί το πρόβλημα (π.χ. διορθωθούν οι κωδικοί και η επόμενη σύνδεση πετύχει)."""
from __future__ import annotations

import sqlite3
from typing import Any

from . import settings_store

MAX_NAMES = 4


def _names(conn: sqlite3.Connection, afms: list[str]) -> str:
    out = []
    for afm in afms[:MAX_NAMES]:
        row = conn.execute("SELECT name FROM businesses WHERE afm=?", (afm,)).fetchone()
        out.append(f"{row['name']} ({afm})" if row and row["name"] else afm)
    more = len(afms) - MAX_NAMES
    return ", ".join(out) + (f" και άλλοι {more}" if more > 0 else "")


def collect(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """[{level: danger|warn|info, text, endpoint, args, label}] — το template φτιάχνει το link από το endpoint."""
    out: list[dict[str, Any]] = []

    # ---- LLM (η ανάλυση άρθρων είναι ό,τι παράγει τα matches)
    provider = settings_store.get(conn, "llm_provider")
    key = "openrouter_api_key" if provider == "openrouter" else "groq_api_key"
    if not settings_store.is_set(conn, key):
        out.append({"level": "warn", "endpoint": "main.settings", "label": "Ρυθμίσεις",
                    "text": f"Δεν έχει οριστεί API key για {provider} — τα άρθρα δεν αναλύονται, άρα δεν υπάρχουν matches."})
    else:
        err = settings_store.get(conn, "llm_last_error")
        if err:
            out.append({"level": "danger", "endpoint": "main.settings", "label": "Ρυθμίσεις",
                        "text": f"Η ανάλυση άρθρων με LLM δεν δουλεύει: {err} Ώσπου να λυθεί δεν δημιουργούνται matches."})

    # ---- κωδικοί TAXISnet γραφείου
    if settings_store.get(conn, "aade_office_status") == "invalid":
        out.append({"level": "danger", "endpoint": "main.settings", "label": "Ρυθμίσεις",
                    "text": "Οι κωδικοί TAXISnet του γραφείου (Ρυθμίσεις) είναι λάθος ή ο λογαριασμός είναι κλειδωμένος."})

    # ---- κωδικοί TAXISnet πελατών
    bad = [r["afm"] for r in conn.execute("SELECT afm FROM client_credentials WHERE check_status='invalid' ORDER BY afm")]
    if bad:
        out.append({"level": "danger", "endpoint": "main.clients_list", "args": {"_anchor": "badcreds"}, "label": "Δείτε τους πελάτες",
                    "text": f"Λάθος ή κλειδωμένοι κωδικοί TAXISnet σε {len(bad)} "
                            f"{'πελάτη' if len(bad) == 1 else 'πελάτες'}: {_names(conn, bad)}."})
    return out
