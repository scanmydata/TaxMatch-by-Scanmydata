"""Μόνιμες ειδοποιήσεις μέσα στην εφαρμογή (πάνω-πάνω σε κάθε σελίδα) για προβλήματα που θέλουν ενέργεια του χρήστη:
λάθος κωδικοί TAXISnet, αποτυχία ανάλυσης LLM. Υπολογίζονται από την κατάσταση στη βάση — εξαφανίζονται μόνες τους
όταν λυθεί το πρόβλημα (π.χ. διορθωθούν οι κωδικοί και η επόμενη σύνδεση πετύχει)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from typing import Any, Optional

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
    _add_marks(conn, "vat", afms)


#: Αστερίσκοι ανά πελάτη μετά το «Αναγνώστηκε» (ρητό αίτημα χρήστη 2026-10-27): μένουν στη λίστα πελατών — στη στήλη
#: ΦΠΑ (επιστροφή στο τρίμηνο) ή στον ΚΑΔ (παρεπιδημούντες) — ώσπου ο χρήστης να ανοίξει την καρτέλα του πελάτη.
MARK_KEYS = {"vat": "notice_marks_vat", "parepid": "notice_marks_parepid"}


def marks(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {a for a in (settings_store.get(conn, MARK_KEYS[kind]) or "").split(",") if a}


def _add_marks(conn: sqlite3.Connection, kind: str, afms: list[str]) -> None:
    settings_store.set_value(conn, MARK_KEYS[kind], ",".join(sorted(marks(conn, kind) | set(afms))))


def clear_marks(conn: sqlite3.Connection, afm: str) -> None:
    """Η καρτέλα του πελάτη άνοιξε: οι αστερίσκοι του σβήνουν."""
    for kind, key in MARK_KEYS.items():
        current = marks(conn, kind)
        if afm in current:
            settings_store.set_value(conn, key, ",".join(sorted(current - {afm})))


def parepid_candidates(conn: sqlite3.Connection) -> list[str]:
    """Ενεργοί πελάτες με ΚΑΔ καταλυμάτων/εστίασης/διασκέδασης (`parepid.py`) που ΔΕΝ έχουν ήδη επιβεβαιωθεί από το
    dae.govapp.gr (έλεγχος «ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ») — πιθανοί υπόχρεοι τέλους παρεπιδημούντων/ακαθαρίστων."""
    from . import parepid
    kads: dict[str, list[str]] = {}
    for r in conn.execute("SELECT k.afm, k.code FROM business_kad k JOIN businesses b ON b.afm=k.afm "
                          "WHERE coalesce(b.activity_state,'')<>'ceased' AND coalesce(b.activity_state,'')<>'none' "
                          "AND b.parepid IS NULL"):
        kads.setdefault(r["afm"], []).append(r["code"])
    checked = {r["afm"] for r in conn.execute("SELECT DISTINCT afm FROM obligation_checks WHERE obligation='ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ'")}
    return sorted(a for a, codes in kads.items() if a not in checked and parepid.kad_matches(codes))


def parepid_read(conn: sqlite3.Connection) -> bool:
    afms = parepid_candidates(conn)
    return bool(afms) and settings_store.get(conn, "notice_parepid_read") == ",".join(afms)


def mark_parepid_read(conn: sqlite3.Connection) -> None:
    afms = parepid_candidates(conn)
    settings_store.set_value(conn, "notice_parepid_read", ",".join(afms))
    _add_marks(conn, "parepid", afms)


def openbusiness_candidates(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """ΑΦΜ → αιτίες, για ενεργούς πελάτες με ΚΑΔ που πιθανώς θέλουν αδειοδότηση/γνωστοποίηση στο OpenBusiness
    (`openbusiness.py` — λίστα ΚΑΔ του χρήστη + δραστηριότητες της πύλης). Ρητό αίτημα 2026-11-13."""
    from . import openbusiness
    kads: dict[str, list[str]] = {}
    for r in conn.execute("SELECT k.afm, k.code FROM business_kad k JOIN businesses b ON b.afm=k.afm "
                          "WHERE coalesce(b.activity_state,'') NOT IN ('ceased','none') ORDER BY k.is_main DESC"):
        kads.setdefault(r["afm"], []).append(r["code"])
    out = {}
    for afm, codes in kads.items():
        why = openbusiness.reasons(codes)
        if why:
            out[afm] = why
    return out


def _openbusiness_stamp(found: dict[str, list[str]]) -> str:
    return ",".join(sorted(found))


def mark_openbusiness_read(conn: sqlite3.Connection) -> None:
    settings_store.set_value(conn, "notice_openbusiness_read", _openbusiness_stamp(openbusiness_candidates(conn)))


def vat_mismatch_clients(conn: sqlite3.Connection, year: Optional[int] = None) -> dict[str, int]:
    """ΑΦΜ → πλήθος παραστατικών της χρήσης με ΦΠΑ (ή χαρακτηρισμό ΦΠΑ) σε πελάτη που ΔΕΝ είναι υπόχρεος ΦΠΑ — η ίδια
    προειδοποίηση με την καρτέλα myDATA, και στο κέντρο ειδοποιήσεων (ρητό αίτημα 2026-11-04)."""
    year = year or date.today().year
    return {r["afm"]: r["n"] for r in conn.execute(
        "SELECT d.afm, COUNT(*) AS n FROM mydata_docs d JOIN businesses b ON b.afm=d.afm WHERE b.vat_subject=0 "
        "AND d.vat_class=1 AND d.cancelled=0 AND substr(d.issue_date,1,4)=? GROUP BY d.afm", (str(year),))}


#: Εισφορές πλαστικών (ρητό αίτημα 2026-11-04 «και οι 2 που έχουμε»): ομάδα FEE_GROUPS → (έντυπο ελέγχου, ετικέτα)
PLASTIC_FEES = {"plastic_bag": ("ΠΕΡΙΒΑΛΛΟΝ", "τέλος πλαστικής σακούλας (TAXISnet H7)"),
                "plastics": ("ΠΛΑΣΤΙΚΑ", "εισφορά πλαστικών προϊόντων (ν. 4736/2020, εφαρμογή Plastic)")}


def plastic_clients(conn: sqlite3.Connection, year: Optional[int] = None) -> dict[str, dict[str, set[int]]]:
    """{'plastic_bag'|'plastics': {ΑΦΜ: τρίμηνα}} — πελάτες που εισπράττουν την εισφορά στα έσοδα της χρήσης (myDATA,
    κατηγορίες τελών 8 / 16). Η υποχρέωση προκύπτει ΜΟΝΟ από το myDATA (οι πύλες δείχνουν «ενεργός» για όλους)."""
    from .mydata.analysis import FEE_GROUPS
    year = year or date.today().year
    out: dict[str, dict[str, set[int]]] = {k: {} for k in PLASTIC_FEES}
    for r in conn.execute("SELECT afm, issue_date, taxes_json FROM mydata_docs WHERE direction='outgoing' AND cancelled=0 "
                          "AND substr(issue_date,1,4)=? AND taxes_json NOT IN ('', '{}')", (str(year),)):
        try:
            taxes = json.loads(r["taxes_json"])
        except ValueError:
            continue
        for key in PLASTIC_FEES:
            kind, cats = FEE_GROUPS[key]
            if any(c in (taxes.get(kind) or {}) for c in cats):
                out[key].setdefault(r["afm"], set()).add((int(r["issue_date"][5:7]) - 1) // 3 + 1)
    return out


def plastic_unsubmitted(conn: sqlite3.Connection, year: Optional[int] = None) -> dict[str, set[str]]:
    """{'plastic_bag'|'plastics': ΑΦΜ} με τουλάχιστον ένα τρίμηνο «δεν υποβλήθηκε» στον έλεγχο υποβολών."""
    year = year or date.today().year
    out: dict[str, set[str]] = {k: set() for k in PLASTIC_FEES}
    for key, (form, _label) in PLASTIC_FEES.items():
        out[key] = {r[0] for r in conn.execute("SELECT DISTINCT afm FROM obligation_checks WHERE year=? AND obligation=? "
                                               "AND status='not_submitted'", (year, form))}
    return out


def _plastic_stamp(found: dict[str, dict[str, set[int]]]) -> str:
    return ";".join(f"{k}:" + ",".join(f"{a}{''.join(map(str, sorted(q)))}" for a, q in sorted(v.items()))
                    for k, v in sorted(found.items()))


def mark_plastic_read(conn: sqlite3.Connection) -> None:
    settings_store.set_value(conn, "notice_plastic_read", _plastic_stamp(plastic_clients(conn)))


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
        limits = settings_store.get(conn, "llm_limits")
        if err and not _transient_llm(err):
            out.append({"level": "danger", "endpoint": "main.settings", "label": "Ρυθμίσεις", "dismiss": "llm",
                        "text": f"Η ανάλυση άρθρων με LLM δεν δουλεύει: {err} Ώσπου να λυθεί δεν δημιουργούνται matches."})
        elif limits or err:
            out.append({"level": "info", "endpoint": "main.settings", "label": "Ρυθμίσεις", "dismiss": "llm",
                        "text": "Ανάλυση νέων: τα δωρεάν όρια των μοντέλων LLM καλύφθηκαν προσωρινά — η εφαρμογή αλλάζει "
                                "μόνη της μοντέλο/πάροχο και συνεχίζει στον επόμενο έλεγχο. (Με κλειδί και για δεύτερο "
                                "πάροχο, Groq ή OpenRouter, η ανάλυση δεν σταματά.)"})

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
    from .requests_aade import CHANGE_LABEL, chat_line, unseen, waiting_on_client
    waiting_rows = waiting_on_client(conn)
    if waiting_rows:                                    # η ΑΑΔΕ περιμένει ενέργεια — μένει ώσπου να αλλάξει η κατάσταση
        afms = list(dict.fromkeys(r["afm"] for r in waiting_rows))
        out.append({"level": "warn", "endpoint": "main.clients_list", "label": "Δείτε τους πελάτες", "afms": afms,
                    "text": f"{len(waiting_rows)} {'αίτημα' if len(waiting_rows) == 1 else 'αιτήματα'} ΑΑΔΕ σε "
                            f"εκκρεμότητα από τον πελάτη (χρειάζεται ενέργεια):\n"
                            + "\n".join("• " + chat_line(r) for r in waiting_rows[:4])
                            + (f"\n… και άλλα {len(waiting_rows) - 4}" if len(waiting_rows) > 4 else "")})
    pending = [r for r in unseen(conn) if r["change"] != "waiting_client"
               or not any(w["id"] == r["id"] and w["afm"] == r["afm"] for w in waiting_rows)]
    if pending:
        afms = list(dict.fromkeys(r["afm"] for r in pending))
        answered = sum(1 for r in pending if r["change"] == "answered")
        waiting = sum(1 for r in pending if r["change"] == "waiting_client")
        parts = [f"{answered} απαντήθηκαν" if answered else "", f"{waiting} περιμένουν ενέργεια του πελάτη" if waiting else ""]
        lines = "\n".join("• " + chat_line(r) for r in pending[:4]) + (f"\n… και άλλα {len(pending) - 4}"
                                                                       if len(pending) > 4 else "")
        out.append({"level": "warn" if waiting else "info", "endpoint": "main.clients_list", "label": "Δείτε τους πελάτες",
                    "afms": afms,
                    "text": f"Αιτήματα ΑΑΔΕ με αλλαγές ({', '.join(p for p in parts if p) or CHANGE_LABEL['status']}):\n"
                            + lines})
    # ---- ΦΠΑ σε παραστατικά πελάτη που ΔΕΝ είναι υπόχρεος ΦΠΑ (καρτέλα myDATA → και εδώ)
    mismatch = vat_mismatch_clients(conn)
    stamp = ",".join(f"{a}:{n}" for a, n in sorted(mismatch.items()))
    if mismatch and settings_store.get(conn, "notice_vat_mismatch_read") != stamp:
        afms = sorted(mismatch)
        out.append({"level": "warn", "endpoint": "main.clients_list", "label": "Δείτε τους πελάτες", "afms": afms,
                    "dismiss": "vat_mismatch",
                    "text": f"{len(afms)} {'πελάτης' if len(afms) == 1 else 'πελάτες'} ΔΕΝ είναι υπόχρεοι ΦΠΑ, αλλά στο "
                            f"myDATA της χρήσης {date.today().year} υπάρχουν {sum(mismatch.values())} παραστατικά με ΦΠΑ "
                            f"ή χαρακτηρισμό ΦΠΑ: {_names(conn, afms)} — ελέγξτε την έκδοση/τον χαρακτηρισμό ή την "
                            "υπαγωγή σε ΦΠΑ (καρτέλα πελάτη → myDATA)."})
    # ---- εισφορές πλαστικών (σακούλα H7 / πλαστικά προϊόντα) από το myDATA — δήλωση ανά τρίμηνο
    plastic = plastic_clients(conn)
    late = plastic_unsubmitted(conn)
    if any(plastic.values()) and settings_store.get(conn, "notice_plastic_read") != _plastic_stamp(plastic):
        afms = sorted(set().union(*plastic.values()))
        parts = []
        for key, (_form, label) in PLASTIC_FEES.items():
            if plastic[key]:
                miss = sorted(set(plastic[key]) & late[key])
                parts.append(f"{label}: {_names(conn, sorted(plastic[key]))}"
                             + (f" — ΔΕΝ υποβλήθηκε τρίμηνο: {_names(conn, miss)}" if miss else ""))
        bad = any(set(plastic[k]) & late[k] for k in PLASTIC_FEES)
        out.append({"level": "warn" if bad else "info", "endpoint": "main.clients_list", "label": "Δείτε τους πελάτες",
                    "afms": afms, "dismiss": "plastic",
                    "text": f"Εισφορές πλαστικών στο myDATA της χρήσης {date.today().year} ({len(afms)} "
                            f"{'πελάτης' if len(afms) == 1 else 'πελάτες'}) — τριμηνιαία δήλωση: " + " · ".join(parts) + "."})
    # ---- πρόταση ΑΚΕΕΔ: 14.3 από Google στο myDATA χωρίς απόφαση για την ειδική εισφορά διαδικτύου (2026-11-02)
    from .advertising import google_candidates
    google = google_candidates(conn)
    if google and settings_store.get(conn, "notice_google_read") != ",".join(google):
        n = len(google)
        out.append({"level": "info", "endpoint": "main.clients_list", "label": "Δείτε τους πελάτες", "afms": google,
                    "dismiss": "google",
                    "text": f"{n} {'πελάτης' if n == 1 else 'πελάτες'} με 14.3 από Google / Meta στο myDATA — πιθανή "
                            f"ειδική εισφορά διαδικτύου 2% ΕΔΟΕΑΠ (ΑΚΕΕΔ): ενεργοποιήστε την στο Προφίλ αν ισχύει: "
                            f"{_names(conn, google)}."})
    # ---- πιθανοί υπόχρεοι τέλους παρεπιδημούντων/ακαθαρίστων από τον ΚΑΔ (ίδιο μοτίβο με το τρίμηνο ΦΠΑ)
    parepid_afms = parepid_candidates(conn)
    if parepid_afms and settings_store.get(conn, "notice_parepid_read") != ",".join(parepid_afms):
        n = len(parepid_afms)
        out.append({"level": "info", "endpoint": "main.clients_list", "label": "Δείτε τους πελάτες",
                    "afms": parepid_afms, "dismiss": "parepid",
                    "text": f"{n} {'πελάτης' if n == 1 else 'πελάτες'} με ΚΑΔ καταλυμάτων/εστίασης/διασκέδασης "
                            f"μπορεί να {'είναι υπόχρεος' if n == 1 else 'είναι υπόχρεοι'} τέλους παρεπιδημούντων / "
                            f"ακαθαρίστων εσόδων (αν το επιβάλλει ο δήμος — dae.govapp.gr): {_names(conn, parepid_afms)}."})
    # ---- πιθανοί υπόχρεοι αδειοδότησης / γνωστοποίησης στο OpenBusiness (ΚΑΔ)
    ob = openbusiness_candidates(conn)
    if ob and settings_store.get(conn, "notice_openbusiness_read") != _openbusiness_stamp(ob):
        afms = sorted(ob)
        n = len(afms)
        out.append({"level": "info", "endpoint": "main.clients_list", "label": "Δείτε τους πελάτες",
                    "afms": afms, "dismiss": "openbusiness",
                    "text": f"{n} {'πελάτης' if n == 1 else 'πελάτες'} με ΚΑΔ που μπορεί να απαιτ"
                            f"{'εί' if n == 1 else 'ούν'} γνωστοποίηση ή έγκριση στο OpenBusiness (ν. 4442/2016 — "
                            f"ανάλογα με τη δραστηριότητα/εγκατάσταση): {_names(conn, afms)}. Η αιτία φαίνεται στο "
                            "Προφίλ κάθε πελάτη."})
    if eligible:
        out.append({"level": "info", "endpoint": "main.clients_list", "label": "Δείτε τους πελάτες",
                    "afms": eligible, "dismiss": "vat_quarter",
                    "text": f"{len(eligible)} {'πελάτης' if len(eligible) == 1 else 'πελάτες'} Β΄ κατηγορίας σε μηνιαίο ΦΠΑ "
                            f"μπορ{'εί' if len(eligible) == 1 else 'ούν'} να επανέλθ{'ει' if len(eligible) == 1 else 'ουν'} "
                            f"στον τριμηνιαίο (24 μήνες από την έναρξη) — δήλωση επιλογής στο myAADE έως "
                            f"{deadline.strftime('%d/%m/%Y')}: {_names(conn, eligible)}."})
    return out


def _transient_llm(err: str) -> bool:
    """Μηνύματα που γράφτηκαν από παλιότερες εκδόσεις για όρια/συνωστισμό (όχι βλάβη) — δεν είναι κόκκινο σφάλμα."""
    t = (err or "").lower()
    return any(k in t for k in ("υπερφορτωμένο", "όριο", "quota", "δοκιμάζεται αυτόματα", "εκτός λειτουργίας",
                                "429", "rate"))


# ---- απόκρυψη ΟΠΟΙΑΣΔΗΠΟΤΕ ειδοποίησης από τον χρήστη (ρητό αίτημα 2026-11-11: «δεν μπορώ να τις κάνω dismiss»).
# Κρατιέται το αποτύπωμα του ΚΕΙΜΕΝΟΥ: αν αλλάξει η κατάσταση (άλλοι πελάτες, άλλο πλήθος) ξαναεμφανίζεται.
HIDDEN_KEY = "notice_hidden"
SEEN_DANGER_KEY = "notice_danger_seen"
_MAX_STAMPS = 300


def stamp(notice: dict[str, Any]) -> str:
    return hashlib.sha1(str(notice.get("text", "")).encode("utf-8")).hexdigest()[:16]


def _stamps(conn: sqlite3.Connection, key: str) -> list[str]:
    return [s for s in (settings_store.get(conn, key) or "").split(",") if s]


def _add_stamp(conn: sqlite3.Connection, key: str, value: str) -> None:
    current = [s for s in _stamps(conn, key) if s != value] + [value]
    settings_store.set_value(conn, key, ",".join(current[-_MAX_STAMPS:]))


def hide(conn: sqlite3.Connection, notice: dict[str, Any]) -> None:
    """Κρύβει μία ειδοποίηση. Για το LLM σβήνει και το ίδιο το σφάλμα: νέα αποτυχία = νέα ειδοποίηση."""
    if notice.get("dismiss") == "llm":
        settings_store.set_value(conn, "llm_last_error", "")
        settings_store.set_value(conn, "llm_limits", "")
        return
    _add_stamp(conn, HIDDEN_KEY, stamp(notice))


def visible(conn: sqlite3.Connection, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hidden = set(_stamps(conn, HIDDEN_KEY))
    return [n for n in found if stamp(n) not in hidden]


def unseen_danger(conn: sqlite3.Connection, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Σφάλματα που δεν έχει ξαναδεί ο χρήστης (ούτε σε προηγούμενη εκκίνηση) — μόνο αυτά ανοίγουν το πάνελ μόνα
    τους· πριν άνοιγε σε ΚΑΘΕ εκκίνηση για το ίδιο παλιό σφάλμα."""
    seen = set(_stamps(conn, SEEN_DANGER_KEY))
    fresh = [n for n in found if n["level"] == "danger" and stamp(n) not in seen]
    for n in fresh:
        _add_stamp(conn, SEEN_DANGER_KEY, stamp(n))
    return fresh
