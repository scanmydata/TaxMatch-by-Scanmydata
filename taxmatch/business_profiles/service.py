"""Πελάτες: CRUD στη βάση + ενοποίηση lookups (VIES + ΓΕΜΗ + ΑΑΔΕ). Το UI και το scheduler περνούν από εδώ."""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, Optional

import requests

from .. import db, settings_store
from ..identifiers import format_kad, kad_digits, normalize_afm
from . import credentials, legal_form, lookup_aade, lookup_business_portal as portal, vies
from .import_excel import ImportResult
from .vat_profile import interpret_vat_profile

log = logging.getLogger(__name__)

EDITABLE = ("name", "legal_form", "status", "address", "doy", "books_category", "vat_period_type", "notes")


# ------------------------------------------------------------------ CRUD

def get(conn: sqlite3.Connection, afm: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM businesses WHERE afm=?", (afm,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["kads"] = [dict(r) for r in conn.execute(
        "SELECT code, descr, is_main FROM business_kad WHERE afm=? ORDER BY is_main DESC, code", (afm,))]
    return d


def list_all(conn: sqlite3.Connection, q: str = "", kad: str = "", books: str = "", status: str = "") -> list[dict[str, Any]]:
    sql = ["SELECT b.*, (SELECT COUNT(*) FROM business_kad k WHERE k.afm=b.afm) AS kad_count FROM businesses b WHERE 1=1"]
    args: list[Any] = []
    if q:
        sql.append("AND (b.afm LIKE ? OR UPPER(b.name) LIKE ?)")
        args += [f"%{q.strip()}%", f"%{q.strip().upper()}%"]
    if books:
        sql.append("AND b.books_category = ?")
        args.append(books)
    if status:
        sql.append("AND b.lookup_status = ?")
        args.append(status)
    if kad:
        sql.append("AND EXISTS (SELECT 1 FROM business_kad k WHERE k.afm=b.afm AND REPLACE(k.code,'.','') LIKE ?)")
        args.append(kad_digits(kad) + "%")
    sql.append("ORDER BY b.name COLLATE NOCASE, b.afm")
    return [dict(r) for r in conn.execute(" ".join(sql), args)]


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]


def add(conn: sqlite3.Connection, afm: str, name: str = "", source: str = "manual",
        kads: Optional[list[str]] = None) -> bool:
    """Δημιουργεί πελάτη (status 'pending' ώστε να εμπλουτιστεί). False αν υπήρχε ήδη."""
    now = db.utcnow()
    cur = conn.execute("INSERT OR IGNORE INTO businesses(afm,name,source,lookup_status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?)", (afm, name.strip(), source, "pending", now, now))
    if cur.rowcount == 0:
        return False
    if kads:
        set_kads(conn, afm, [{"code": k, "descr": ""} for k in kads])
    return True


def update_fields(conn: sqlite3.Connection, afm: str, fields: dict[str, Any], vat_subject: Any = "keep") -> None:
    sets, args = [], []
    for k in EDITABLE:
        if k in fields:
            sets.append(f"{k}=?")
            args.append(str(fields[k]).strip())
    if vat_subject != "keep":
        sets.append("vat_subject=?")
        args.append(None if vat_subject in (None, "") else int(bool(vat_subject)))
    if not sets:
        return
    sets.append("updated_at=?")
    args += [db.utcnow(), afm]
    conn.execute(f"UPDATE businesses SET {', '.join(sets)} WHERE afm=?", args)


def set_kads(conn: sqlite3.Connection, afm: str, kads: list[dict[str, Any]]) -> None:
    """Αντικαθιστά τα ΚΑΔ. Το πρώτο με is_main (ή απλώς το πρώτο) γίνεται κύριο και ανανεώνεται το denormalized πεδίο."""
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for k in kads:
        code = format_kad(k.get("code")) if kad_digits(k.get("code")) else ""
        if not code or kad_digits(code) in seen:
            continue
        seen.add(kad_digits(code))
        clean.append({"code": code, "descr": str(k.get("descr") or "").strip(), "is_main": bool(k.get("is_main"))})
    if clean and not any(k["is_main"] for k in clean):
        clean[0]["is_main"] = True
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM business_kad WHERE afm=?", (afm,))
        for k in clean:
            conn.execute("INSERT INTO business_kad(afm,code,descr,is_main) VALUES (?,?,?,?)",
                         (afm, k["code"], k["descr"], int(k["is_main"])))
        main = next((k for k in clean if k["is_main"]), None)
        conn.execute("UPDATE businesses SET kad_main_code=?, kad_main_desc=?, updated_at=? WHERE afm=?",
                     (main["code"] if main else "", main["descr"] if main else "", db.utcnow(), afm))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def delete(conn: sqlite3.Connection, afm: str) -> None:
    conn.execute("DELETE FROM businesses WHERE afm=?", (afm,))


def for_matching(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Ελάχιστη μορφή που χρειάζεται η matching engine (όλοι οι πελάτες, με ΚΑΔ)."""
    kads: dict[str, list[str]] = {}
    for r in conn.execute("SELECT afm, code FROM business_kad"):
        kads.setdefault(r["afm"], []).append(r["code"])
    return [{"afm": r["afm"], "name": r["name"], "legal_form": r["legal_form"], "books_category": r["books_category"],
             "vat_subject": r["vat_subject"], "kads": kads.get(r["afm"], [])}
            for r in conn.execute("SELECT afm,name,legal_form,books_category,vat_subject FROM businesses")]


def import_result(conn: sqlite3.Connection, res: ImportResult) -> dict[str, int]:
    """Αποθηκεύει τις γραμμές του import. Υπάρχοντες πελάτες δεν αντικαθίστανται (συμπληρώνεται μόνο κενή επωνυμία)·
    οι κωδικοί TAXISnet όμως ΕΝΗΜΕΡΩΝΟΝΤΑΙ και σε υπάρχοντες (η μαζική ρύθμιση κωδικών είναι βασική χρήση)."""
    added = updated = creds = 0
    for r in res.rows:
        if add(conn, r.afm, r.name, source="excel", kads=r.kads):
            added += 1
        else:
            existing = conn.execute("SELECT name FROM businesses WHERE afm=?", (r.afm,)).fetchone()
            if r.name and not existing["name"]:
                conn.execute("UPDATE businesses SET name=?, updated_at=? WHERE afm=?", (r.name, db.utcnow(), r.afm))
                updated += 1
        if r.has_credentials and credentials.set_(conn, r.afm, r.taxis_user, r.taxis_pass):
            creds += 1
    return {"added": added, "updated": updated, "invalid": len(res.invalid), "duplicates": res.duplicates,
            "credentials": creds}


# ------------------------------------------------------------------ lookups

def _aade_attempts(conn: sqlite3.Connection, afm: str) -> list[tuple[str, str, str]]:
    """Σειρά προσπαθειών σύνδεσης TAXISnet: πρώτα οι κωδικοί του ίδιου του πελάτη, μετά του γραφείου (Ρυθμίσεις)."""
    out: list[tuple[str, str, str]] = []
    own = credentials.get(conn, afm)
    if own:
        out.append(("client", own[0], own[1]))
    office = (settings_store.get(conn, "aade_user"), settings_store.get(conn, "aade_pass"))
    if office[0] and office[1]:
        out.append(("office", office[0], office[1]))
    return out


def _aade_status_label(a: dict[str, Any]) -> str:
    state = a.get("business_state")
    if state == "ceased":
        return "ΔΙΑΚΟΠΗ ΕΡΓΑΣΙΩΝ" + (f" {a['cease_date']}" if a.get("cease_date") else "") \
               + (f" ({a['cease_reason']})" if a.get("cease_reason") else "")
    if state == "none":
        return "ΙΔΙΩΤΗΣ (χωρίς επιχείρηση)"
    return "ΕΝΕΡΓΗ"


def lookup_and_store(conn: sqlite3.Connection, afm: str, session: Optional[requests.Session] = None,
                     aade_fetch: Callable[..., dict] = lookup_aade.fetch_company_profile) -> dict[str, Any]:
    """Ανάκτηση στοιχείων πελάτη. Σειρά: Μητρώο ΑΑΔΕ (ΚΑΔ, κατάσταση/διακοπή, ΔΟΥ, ΦΠΑ, βιβλία, είδος) → ΓΕΜΗ ως fallback
    (ΚΑΔ όταν λείπουν, νομική μορφή νομικών προσώπων) → VIES (επωνυμία). Κάθε πηγή είναι προαιρετική.
    Επιστρέφει {'status', 'errors', 'sources', 'issues'} και ενημερώνει τη γραμμή. `issues`: [{'code', 'message'}] με
    code ∈ bad_creds_client | bad_creds_office (για τις ειδοποιήσεις της εφαρμογής)."""
    if not get(conn, afm):
        raise KeyError(afm)
    errors: list[str] = []
    sources: list[str] = []
    issues: list[dict[str, str]] = []
    raw: dict[str, Any] = {}

    attempted = False
    definitive = False        # κάποια πηγή απάντησε οριστικά (όχι απλώς σφάλμα δικτύου)

    # ---- 1. Μητρώο ΑΑΔΕ
    aade: Optional[dict[str, Any]] = None
    attempts = _aade_attempts(conn, afm)
    for who, user, pwd in attempts:
        attempted = True
        try:
            a = aade_fetch(user, pwd, afm)
        except requests.RequestException as exc:
            errors.append(f"ΑΑΔΕ: σφάλμα δικτύου ({str(exc)[:120]})")
            break                                  # το δίκτυο θα αποτύχει και για τους επόμενους κωδικούς
        except Exception as exc:                   # το σχήμα/HTML της ΑΑΔΕ αλλάζει· μην ρίχνεις όλο το batch
            log.exception("aade lookup απέτυχε για %s", afm)
            errors.append(f"ΑΑΔΕ: μη αναμενόμενο σφάλμα ({str(exc)[:120]})")
            break
        reason = a.get("reason") or ""
        login_ok = a.get("ok") or reason in ("NoRegistry", "NoAfm")
        if who == "client":
            credentials.record_check(conn, afm, "ok" if login_ok else ("invalid" if reason == "InvalidCredentials" else "error"),
                                     "Η σύνδεση στο TAXISnet πέτυχε." if login_ok
                                     else lookup_aade.REASONS_EL.get(reason, reason or "Αποτυχία σύνδεσης"))
        else:
            settings_store.set_value(conn, "aade_office_status", "ok" if login_ok else ("invalid" if reason == "InvalidCredentials" else ""))
            settings_store.set_value(conn, "aade_office_message", "" if login_ok else lookup_aade.REASONS_EL.get(reason, reason))
        if a.get("ok"):
            aade = a
            break
        definitive = definitive or reason in ("InvalidCredentials", "NoRegistry")
        if reason == "InvalidCredentials":
            who_el = "του πελάτη" if who == "client" else "γραφείου"
            issues.append({"code": f"bad_creds_{who}", "message": f"Λάθος ή κλειδωμένοι κωδικοί TAXISnet {who_el}."})
            errors.append(f"ΑΑΔΕ: λάθος ή κλειδωμένοι κωδικοί TAXISnet {who_el}")
        elif reason == "NoRegistry":
            errors.append("ΑΑΔΕ: " + (lookup_aade.REASON_OFFICE_NOREGISTRY if who == "office"
                                       else "οι κωδικοί TAXISnet δεν ανήκουν σε αυτό το ΑΦΜ (το Μητρώο δείχνει μόνο το δικό του ΑΦΜ)"))
        else:
            errors.append("ΑΑΔΕ: " + lookup_aade.REASONS_EL.get(reason, str(reason)))
    if not attempts:
        errors.append("ΑΑΔΕ: δεν έχουν οριστεί κωδικοί TAXISnet (στον πελάτη ή στις Ρυθμίσεις)")

    aade_fields: dict[str, Any] = {}
    kads: list[dict] = []
    vat: Optional[dict] = None
    if aade:
        raw["aade"] = aade.get("raw") or {"kind": aade.get("kind")}
        vat = interpret_vat_profile(aade.get("all_tags") or {})
        kads = aade.get("kads") or []
        aade_fields = {"name": aade.get("name") or "", "doy": aade.get("doy") or "", "address": aade.get("address") or "",
                       "legal_form": aade.get("legal_form") or "", "status": _aade_status_label(aade)}
        sources.append("ΑΑΔΕ")

    # ---- 2. ΓΕΜΗ: fallback (ΚΑΔ που λείπουν, νομική μορφή νομικών προσώπων, όταν η ΑΑΔΕ δεν απάντησε)
    gemi_fields: dict[str, Any] = {}
    key = settings_store.get(conn, "business_portal_key")
    needs_gemi = (not aade or aade.get("kind") == "ΝΟΜΙΚΟ ΠΡΟΣΩΠΟ"
                  or (aade.get("business_state", "active") == "active" and not kads))
    if key and needs_gemi:
        attempted = True
        try:
            res = portal.lookup(afm, key, session)
            c = res["company"]
            raw["business_portal"] = res["raw"]
            for src, dst in (("name", "name"), ("legal_form", "legal_form"), ("status", "status"),
                             ("address", "address"), ("ar_gemi", "ar_gemi")):
                if c.get(src):
                    gemi_fields[dst] = c[src]
            if not kads:
                kads = c.get("kads") or []
            sources.append("ΓΕΜΗ")
        except (portal.NotFound, PermissionError) as exc:
            definitive = True
            errors.append(f"ΓΕΜΗ: {exc}")
        except requests.RequestException as exc:
            errors.append(f"ΓΕΜΗ: σφάλμα δικτύου ({str(exc)[:120]})")
    elif not key and needs_gemi:
        errors.append("ΓΕΜΗ: δεν έχει οριστεί API key (Ρυθμίσεις)")

    # Συγχώνευση: η ΑΑΔΕ υπερισχύει· το ΓΕΜΗ συμπληρώνει κενά και δίνει την πλήρη νομική μορφή (ΙΚΕ/ΑΕ…)
    # Επωνυμία/διεύθυνση/νομική μορφή: ΓΕΜΗ (επίσημη εγγραφή)· κατάσταση (ενεργή/διακοπή), ΔΟΥ, ΚΑΔ, ΦΠΑ: ΑΑΔΕ.
    fields: dict[str, Any] = {**{k: v for k, v in aade_fields.items() if v}, **gemi_fields}
    if aade_fields.get("status"):
        fields["status"] = aade_fields["status"]

    # ---- 3. VIES: επωνυμία/διεύθυνση χωρίς key — μόνο αν καμία άλλη πηγή δεν έδωσε όνομα (και δεν υπάρχει ήδη)
    current = conn.execute("SELECT name, legal_form FROM businesses WHERE afm=?", (afm,)).fetchone()
    if not fields.get("name") and not current["name"]:
        attempted = True
        v = vies.lookup(afm, session)
        if v.valid and v.name:
            fields["name"] = v.name
            if v.address:
                fields.setdefault("address", v.address)
            sources.append("VIES")
        elif v.error:
            errors.append(f"VIES: {v.error}")
        elif not v.valid:
            definitive = True
            errors.append("VIES: το ΑΦΜ δεν βρέθηκε στο μητρώο ΦΠΑ")

    # ---- 4. Νομική μορφή από την επωνυμία, μόνο ως τελευταίο fallback
    if not fields.get("legal_form") and not current["legal_form"]:
        derived = legal_form.from_name(fields.get("name") or current["name"])
        if derived:
            fields["legal_form"] = derived
            raw["legal_form_source"] = "επωνυμία"

    if not attempted or (not sources and not definitive):
        # Καμία πηγή ρυθμισμένη, ή μόνο παροδικά σφάλματα δικτύου: ο πελάτης μένει 'pending' και ξαναδοκιμάζεται.
        return {"status": "pending", "errors": errors, "sources": [], "issues": issues}

    now = db.utcnow()
    sets, args = [], []
    for k, v in fields.items():
        if v:
            sets.append(f"{k}=?")
            args.append(v)
    if aade:                                       # πάντα, ακόμη κι αν είναι κενά (π.χ. καθάρισμα παλιάς διακοπής)
        sets += ["activity_state=?", "cease_date=?", "cease_reason=?", "start_date=?"]
        args += [aade.get("business_state") or "", aade.get("cease_date") or "", aade.get("cease_reason") or "",
                 aade.get("business_start") or ""]
    if vat:
        sets += ["vat_subject=?", "books_category=?", "books_category_raw=?", "vat_period_type=?"]
        args += [None if vat["vat_subject"] is None else int(vat["vat_subject"]), vat["books_category"],
                 vat["books_category_raw"], vat["vat_period_type"]]
    if kads:
        set_kads(conn, afm, kads)
    elif aade and aade.get("business_state") in ("ceased", "none"):
        set_kads(conn, afm, [])                    # κλειστή/χωρίς επιχείρηση: δεν υπάρχουν ενεργά ΚΑΔ
    have_kads = bool(kads) or bool(conn.execute("SELECT 1 FROM business_kad WHERE afm=?", (afm,)).fetchone())
    kad_optional = bool(aade) and aade.get("business_state") in ("ceased", "none")
    row = conn.execute("SELECT name FROM businesses WHERE afm=?", (afm,)).fetchone()
    complete = (have_kads or kad_optional) and bool(fields.get("name") or row["name"])
    status = "failed" if not sources else ("ok" if complete else "partial")
    prev = conn.execute("SELECT lookup_status FROM businesses WHERE afm=?", (afm,)).fetchone()["lookup_status"]
    if not sources and prev in ("ok", "partial", "manual"):
        status = prev                    # αποτυχία τώρα (π.χ. λάθος κωδικοί) δεν υποβαθμίζει στοιχεία που ήδη έχουμε
    sets += ["lookup_status=?", "lookup_error=?", "lookup_raw=?", "lookup_at=?", "updated_at=?"]
    args += [status, "; ".join(errors), json.dumps(raw, ensure_ascii=False)[:200_000], now, now, afm]
    conn.execute(f"UPDATE businesses SET {', '.join(sets)} WHERE afm=?", args)
    return {"status": status, "errors": errors, "sources": sources, "issues": issues}


def enrich_pending(conn: sqlite3.Connection, limit: int = 20, session: Optional[requests.Session] = None,
                   on_progress: Optional[Callable[[str], None]] = None,
                   aade_fetch: Callable[..., dict] = lookup_aade.fetch_company_profile,
                   afms: Optional[list[str]] = None, include_partial: bool = False) -> dict[str, int]:
    """Lookup για πελάτες σε κατάσταση 'pending' (νέοι από import) — ή για συγκεκριμένους `afms`. Το ΓΕΜΗ επιτρέπει
    8 αιτήματα/λεπτό και το VIES 1/δευτ., γι' αυτό το batch είναι φραγμένο και συνεχίζει στον επόμενο έλεγχο.
    `include_partial`: ξαναδοκιμάζει και όσους έχουν μερικά στοιχεία (π.χ. μετά την προσθήκη key/κωδικών)."""
    if afms is not None:
        rows = [{"afm": a} for a in afms]
    else:
        statuses = "('pending','partial')" if include_partial else "('pending')"
        rows = conn.execute(f"SELECT afm FROM businesses WHERE lookup_status IN {statuses} ORDER BY created_at LIMIT ?",
                            (limit,)).fetchall()
    stats = {"processed": 0, "ok": 0, "partial": 0, "failed": 0, "pending": 0, "bad_creds": 0, "bad_office_creds": 0}
    for i, r in enumerate(rows, 1):
        if on_progress:
            on_progress(f"Εμπλουτισμός πελατών {i}/{len(rows)}")
        out = lookup_and_store(conn, r["afm"], session, aade_fetch)
        stats["processed"] += 1
        stats[out["status"]] += 1
        for issue in out.get("issues", []):
            if issue["code"] == "bad_creds_client":
                stats["bad_creds"] += 1
            elif issue["code"] == "bad_creds_office":
                stats["bad_office_creds"] += 1
    return stats
