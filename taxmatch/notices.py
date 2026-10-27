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


def vat_quarter_eligible(conn: sqlite3.Connection) -> tuple[list[str], Any]:
    """Β΄ κατηγορίας σε υποχρεωτικά μηνιαίο ΦΠΑ που συμπλήρωσαν 24 μήνες (Α.1049/2025) — μπορούν να επανέλθουν στο
    τρίμηνο. ([ΑΦΜ], προθεσμία δήλωσης επιλογής | None)."""
    from .obligations import quarter_option
    eligible, deadline = [], None
    for r in conn.execute("SELECT * FROM businesses WHERE books_category='Β' AND coalesce(vat_subject,1)<>0 "
                          "AND activity_state='active' ORDER BY name"):
        opt = quarter_option(dict(r))
        if opt and opt["by_law"] and opt["eligible_now"]:
            eligible.append(r["afm"])
            deadline = opt["deadline"]
    return eligible, deadline


def _vat_quarter_stamp(afms: list[str], deadline: Any) -> str:
    return f"{deadline.isoformat() if deadline else ''}|{','.join(sorted(afms))}"


def vat_quarter_read(conn: sqlite3.Connection) -> bool:
    """Έχει «αναγνωστεί» η ειδοποίηση για τους ΤΩΡΙΝΟΥΣ πελάτες και την ΤΩΡΙΝΗ προθεσμία; (νέος πελάτης ή νέα
    προθεσμία = ξαναεμφανίζεται)."""
    afms, deadline = vat_quarter_eligible(conn)
    return bool(afms) and settings_store.get(conn, "notice_vat_quarter_read") == _vat_quarter_stamp(afms, deadline)


def mark_vat_quarter_read(conn: sqlite3.Connection) -> None:
    afms, deadline = vat_quarter_eligible(conn)
    settings_store.set_value(conn, "notice_vat_quarter_read", _vat_quarter_stamp(afms, deadline))


def parepid_candidates(conn: sqlite3.Connection) -> list[str]:
    """Ενεργοί πελάτες με ΚΑΔ καταλυμάτων/εστίασης/διασκέδασης (`parepid.py`) που ΔΕΝ έχουν ήδη επιβεβαιωθεί από το
    dae.govapp.gr (έλεγχος «ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ») — πιθανοί υπόχρεοι τέλους παρεπιδημούντων/ακαθαρίστων."""
    from . import parepid
    kads: dict[str, list[str]] = {}
    for r in conn.execute("SELECT k.afm, k.code FROM business_kad k JOIN businesses b ON b.afm=k.afm "
                          "WHERE coalesce(b.activity_state,'')<>'ceased' AND coalesce(b.activity_state,'')<>'none'"):
        kads.setdefault(r["afm"], []).append(r["code"])
    checked = {r["afm"] for r in conn.execute("SELECT DISTINCT afm FROM obligation_checks WHERE obligation='ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ'")}
    return sorted(a for a, codes in kads.items() if a not in checked and parepid.kad_matches(codes))


def parepid_read(conn: sqlite3.Connection) -> bool:
    afms = parepid_candidates(conn)
    return bool(afms) and settings_store.get(conn, "notice_parepid_read") == ",".join(afms)


def mark_parepid_read(conn: sqlite3.Connection) -> None:
    settings_store.set_value(conn, "notice_parepid_read", ",".join(parepid_candidates(conn)))


def collect(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """[{level: danger|warn|info, text, endpoint, args, label, afms?, dismiss?}] — το template φτιάχνει το link από το
    endpoint· `afms` = οι πελάτες της ειδοποίησης (το gui φιλτράρει τη λίστα σε αυτούς)· `dismiss` = κλειδί για
    «Αναγνώστηκε»."""
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
                    "afms": bad,
                    "text": f"Λάθος ή κλειδωμένοι κωδικοί TAXISnet σε {len(bad)} "
                            f"{'πελάτη' if len(bad) == 1 else 'πελάτες'}: {_names(conn, bad)}."})

    # ---- Β΄ κατηγορίας σε υποχρεωτικά μηνιαίο ΦΠΑ που συμπλήρωσαν 24 μήνες (Α.1049/2025) — μπορούν να επανέλθουν στο τρίμηνο
    eligible, deadline = vat_quarter_eligible(conn)
    if eligible and settings_store.get(conn, "notice_vat_quarter_read") == _vat_quarter_stamp(eligible, deadline):
        eligible = []                                   # «Αναγνώστηκε» για αυτούς τους πελάτες / αυτή την προθεσμία
    # ---- «Τα Αιτήματά μου» ΑΑΔΕ: απαντήσεις/αλλαγές που δεν έχει δει ο χρήστης (requests_aade.py)
    from .requests_aade import CHANGE_LABEL, unseen
    pending = unseen(conn)
    if pending:
        afms = list(dict.fromkeys(r["afm"] for r in pending))
        answered = sum(1 for r in pending if r["change"] == "answered")
        waiting = sum(1 for r in pending if r["change"] == "waiting_client")
        parts = [f"{answered} απαντήθηκαν" if answered else "", f"{waiting} περιμένουν ενέργεια του πελάτη" if waiting else ""]
        out.append({"level": "warn" if waiting else "info", "endpoint": "main.clients_list", "label": "Δείτε τους πελάτες",
                    "afms": afms,
                    "text": f"Αιτήματα ΑΑΔΕ με αλλαγές ({', '.join(p for p in parts if p) or CHANGE_LABEL['status']}): "
                            f"{_names(conn, afms)} — καρτέλα πελάτη → «Αιτήματα ΑΑΔΕ»."})
    # ---- πιθανοί υπόχρεοι τέλους παρεπιδημούντων/ακαθαρίστων από τον ΚΑΔ (ίδιο μοτίβο με το τρίμηνο ΦΠΑ)
    parepid_afms = parepid_candidates(conn)
    if parepid_afms and settings_store.get(conn, "notice_parepid_read") != ",".join(parepid_afms):
        n = len(parepid_afms)
        out.append({"level": "info", "endpoint": "main.clients_list", "label": "Δείτε τους πελάτες",
                    "afms": parepid_afms, "dismiss": "parepid",
                    "text": f"{n} {'πελάτης' if n == 1 else 'πελάτες'} με ΚΑΔ καταλυμάτων/εστίασης/διασκέδασης "
                            f"μπορεί να {'είναι υπόχρεος' if n == 1 else 'είναι υπόχρεοι'} τέλους παρεπιδημούντων / "
                            f"ακαθαρίστων εσόδων (αν το επιβάλλει ο δήμος — dae.govapp.gr): {_names(conn, parepid_afms)}."})
    if eligible:
        out.append({"level": "info", "endpoint": "main.clients_list", "label": "Δείτε τους πελάτες",
                    "afms": eligible, "dismiss": "vat_quarter",
                    "text": f"{len(eligible)} {'πελάτης' if len(eligible) == 1 else 'πελάτες'} Β΄ κατηγορίας σε μηνιαίο ΦΠΑ "
                            f"μπορ{'εί' if len(eligible) == 1 else 'ούν'} να επανέλθ{'ει' if len(eligible) == 1 else 'ουν'} "
                            f"στον τριμηνιαίο (24 μήνες από την έναρξη) — δήλωση επιλογής στο myAADE έως "
                            f"{deadline.strftime('%d/%m/%Y')}: {_names(conn, eligible)}."})
    return out
