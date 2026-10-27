"""Πελάτες: CRUD στη βάση + ενοποίηση lookups (VIES + ΓΕΜΗ + ΑΑΔΕ). Το UI και το scheduler περνούν από εδώ."""
from __future__ import annotations

import json
import inspect
import logging
import sqlite3
import re
from typing import Any, Callable, Optional

import requests

from .. import db, settings_store
from ..identifiers import format_kad, kad_digits, normalize_afm
from . import credentials, legal_form, lookup_aade, lookup_business_portal as portal, vies
from ..textutil import strip_accents
from .import_excel import ImportResult
from .vat_profile import interpret_vat_profile

log = logging.getLogger(__name__)

EDITABLE = ("name", "legal_form", "status", "address", "doy", "books_category", "vat_period_type", "notes", "trade_name")


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
        sql.append("AND (b.afm LIKE ? OR UPPER(b.name) LIKE ? OR UPPER(b.trade_name) LIKE ?)")
        args += [f"%{q.strip()}%", f"%{q.strip().upper()}%", f"%{q.strip().upper()}%"]
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


def _is_better_name(current: str, new: str) -> bool:
    """Κενή επωνυμία, ή η νέα ΕΠΕΚΤΕΙΝΕΙ την υπάρχουσα (π.χ. «ΑΓΓΕΛΙΚΑΚΗΣ» -> «ΑΓΓΕΛΙΚΑΚΗΣ ΙΩΑΝΝΗΣ») — έτσι μια νέα
    εισαγωγή του ίδιου Excel συμπληρώνει τα μικρά ονόματα που έλειπαν από παλιότερη εισαγωγή, χωρίς ποτέ να
    αντικαταστήσει επωνυμία που διόρθωσε ο χρήστης (αυτή δεν είναι πρόθεμα της νέας)."""
    current = " ".join((current or "").split())
    if not current:
        return True
    a, b = strip_accents(current).upper(), strip_accents(new).upper()
    return len(b) > len(a) and b.startswith(a + " ")


def _is_company(conn: sqlite3.Connection, afm: str, name: str = "") -> bool:
    """Εταιρεία (νομικό πρόσωπο) από τη νομική μορφή — ή, αν δεν είναι ακόμη γνωστή, από την κατάληξη της επωνυμίας."""
    from ..obligations import is_legal_entity
    row = conn.execute("SELECT legal_form, name FROM businesses WHERE afm=?", (afm,)).fetchone()
    form = (row["legal_form"] if row else "") or legal_form.from_name(name or (row["name"] if row else ""))
    return is_legal_entity(form or "")


def preview_import(conn: sqlite3.Connection, res: ImportResult) -> list[dict[str, Any]]:
    """Τι θα κάνει η εισαγωγή για κάθε γραμμή, ΠΡΙΝ γραφτεί οτιδήποτε (όπως το Timologio Downloader — ρητό αίτημα χρήστη
    2026-10-22): {afm, name, action: new|update|same, existing_name, changes: [...]}. Οι κωδικοί συγκρίνονται
    αποκρυπτογραφημένοι ΜΟΝΟ εδώ, για το ναι/όχι — δεν επιστρέφονται ποτέ."""
    from .. import efka
    from ..aml import gemi_browser
    from ..mydata import credentials as mydata_creds
    out = []
    for r in res.rows:
        row = conn.execute("SELECT name, employer, intrastat FROM businesses WHERE afm=?", (r.afm,)).fetchone()
        if row is None:
            out.append({"afm": r.afm, "name": r.name, "action": "new", "existing_name": "", "changes": []})
            continue
        changes = []
        if r.name and _is_better_name(row["name"], r.name):
            changes.append("επωνυμία")
        for label, has, new, old in (
                ("κωδικοί TAXISnet", r.has_credentials, (r.taxis_user, r.taxis_pass), credentials.get(conn, r.afm)),
                ("κωδικοί Γ.Ε.ΜΗ.", r.has_gemi, (r.gemi_user, r.gemi_pass), gemi_browser.get_credentials(conn, r.afm)),
                ("κωδικοί myDATA", r.has_mydata, (r.mydata_user, r.mydata_key), mydata_creds.get(conn, r.afm)),
                ("κωδικοί ΙΚΑ εργοδότη", r.has_ika, (r.ika_user, r.ika_pass), efka.ika_credentials(conn, r.afm))):
            if has and tuple(old or ()) != new:
                changes.append(label + (" (νέοι)" if not old else " (αλλαγή)"))
        if r.ame and efka.normalize_ame(r.ame) != efka.get_ame(conn, r.afm):
            changes.append("ΑΜΕ")
        if r.amka and not _is_company(conn, r.afm, r.name) and r.amka.split(".")[0].strip().zfill(11) != efka.get_amka(conn, r.afm):
            changes.append("ΑΜΚΑ")
        if r.member_of or r.member_afms:
            changes.append("μέλη εταιρείας")
        if r.employer is not None and row["employer"] != int(r.employer):
            changes.append("μισθοδοσία")
        if r.intrastat and not row["intrastat"]:
            changes.append("Intrastat")
        out.append({"afm": r.afm, "name": r.name or row["name"], "action": "update" if changes else "same",
                    "existing_name": row["name"] or "", "changes": changes})
    return out


def import_result(conn: sqlite3.Connection, res: ImportResult) -> dict[str, int]:
    """Αποθηκεύει τις γραμμές του import. Υπάρχοντες πελάτες δεν αντικαθίστανται (συμπληρώνεται μόνο κενή επωνυμία)·
    οι κωδικοί TAXISnet (και Γ.Ε.ΜΗ.) όμως ΕΝΗΜΕΡΩΝΟΝΤΑΙ και σε υπάρχοντες (η μαζική ρύθμιση κωδικών είναι βασική χρήση)."""
    from ..aml import gemi_browser              # τοπικά: το aml/ εξαρτάται ήδη από το business_profiles, όχι το αντίστροφο
    from ..mydata import credentials as mydata_creds
    from .. import efka
    added = updated = creds = gemi = mydata = ika = 0
    for r in res.rows:
        if add(conn, r.afm, r.name, source="excel", kads=r.kads):
            added += 1
        else:
            existing = conn.execute("SELECT name FROM businesses WHERE afm=?", (r.afm,)).fetchone()
            if r.name and _is_better_name(existing["name"], r.name):
                conn.execute("UPDATE businesses SET name=?, updated_at=? WHERE afm=?", (r.name, db.utcnow(), r.afm))
                updated += 1
        if r.has_credentials and credentials.set_(conn, r.afm, r.taxis_user, r.taxis_pass):
            creds += 1
        if r.has_gemi and gemi_browser.set_credentials(conn, r.afm, r.gemi_user, r.gemi_pass):
            gemi += 1
        if r.has_mydata and mydata_creds.set_(conn, r.afm, r.mydata_user, r.mydata_key):
            mydata += 1
        if (r.has_ika or r.ame) and efka.set_(conn, r.afm, ame=r.ame or None, user=r.ika_user, password=r.ika_pass,
                                              source="excel"):
            ika += 1
        if r.amka and not _is_company(conn, r.afm, r.name):
            efka.set_amka(conn, r.afm, r.amka)            # οι εταιρείες δεν έχουν ΑΜΚΑ
        if r.employer is not None:
            conn.execute("UPDATE businesses SET employer=? WHERE afm=?", (int(r.employer), r.afm))
        if r.intrastat:
            conn.execute("UPDATE businesses SET intrastat=1 WHERE afm=?", (r.afm,))
    from .. import members as members_mod
    linked = members_mod.from_import(conn, res.rows)
    return {"added": added, "updated": updated, "invalid": len(res.invalid), "duplicates": res.duplicates,
            "members": linked,
            "credentials": creds, "gemi": gemi, "mydata": mydata, "ika": ika}


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


#: Παράλληλες ανακτήσεις. Κάθε πελάτης έχει δικό του login TAXISnet (άλλοι κωδικοί), άρα δεν μοιράζονται τίποτα·
#: 6 ταυτόχρονες συνδέσεις είναι συντηρητικό για την ΑΑΔΕ και κόβουν τον χρόνο ~5x για 100+ πελάτες.
LOOKUP_WORKERS = 6


def lookup_many(afms: list[str], on_each: Optional[Callable[[str, int, dict[str, Any]], None]] = None,
                workers: int = LOOKUP_WORKERS, fetch: Optional[Callable[[sqlite3.Connection, str], dict]] = None,
                should_cancel: Optional[Callable[[], bool]] = None) -> int:
    """Ανάκτηση για πολλούς πελάτες ΠΑΡΑΛΛΗΛΑ. `should_cancel()` = True: όσοι πελάτες δεν έχουν ξεκινήσει παραλείπονται,
    όσοι τρέχουν ολοκληρώνονται («διακοπή μετά τον τρέχοντα πελάτη»). Κάθε thread ανοίγει ΔΙΚΗ του σύνδεση βάσης (το sqlite3 δεν επιτρέπει
    κοινή χρήση ανάμεσα σε threads· WAL + busy_timeout σειριοποιούν τις λίγες γραφές). `on_each(afm, done, result)`
    καλείται από το thread του worker μετά από κάθε πελάτη. Επιστρέφει πόσοι ολοκληρώθηκαν."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    fetch = fetch or lookup_and_store
    lock = threading.Lock()
    done = 0

    def one(afm: str) -> None:
        nonlocal done
        if should_cancel and should_cancel():
            return
        conn = db.connect()                               # ανά πελάτη: ένα sqlite3 object δεν περνά σε άλλο thread
        try:
            result = fetch(conn, afm)
        except KeyError:                                  # σβήστηκε στο μεταξύ
            result = {"status": "missing", "errors": [], "sources": [], "issues": []}
        except Exception as exc:                          # ένας πελάτης δεν ρίχνει όλο το batch
            log.exception("lookup απέτυχε για %s", afm)
            result = {"status": "failed", "errors": [str(exc)[:200]], "sources": [], "issues": []}
        finally:
            conn.close()
        with lock:
            done += 1
            n = done
        if on_each:
            on_each(afm, n, result)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(afms) or 1)), thread_name_prefix="lookup") as pool:
        list(pool.map(one, afms))
    return done


def lookup_and_store(conn: sqlite3.Connection, afm: str, session: Optional[requests.Session] = None,
                     aade_fetch: Callable[..., dict] = lookup_aade.fetch_company_profile,
                     amka_fetch: Optional[Callable[[sqlite3.Connection, str], dict]] = None) -> dict[str, Any]:
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
            # Μέλη εταιρείας (ρύθμιση «Άντληση μελών εταιρειών») — μόνο με τους κωδικούς της ίδιας της εταιρείας
            with_members = (who == "client" and settings_store.get(conn, "fetch_members") == "1"
                            and "with_members" in inspect.signature(aade_fetch).parameters)
            a = aade_fetch(user, pwd, afm, with_members=True) if with_members else aade_fetch(user, pwd, afm)
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
    gemi_state: dict[str, str] = {}
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
            gemi_state = {"state": c.get("state") or "", "date": c.get("status_date") or "",
                          "status": c.get("status") or "", "start": c.get("start_date") or ""}
            if not kads and gemi_state["state"] != "ceased":
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

    if not aade and gemi_state.get("state") == "ceased" and \
            is_individual_form(fields.get("legal_form") or current["legal_form"]):
        _as_private_person(conn, afm, fields)

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
    elif gemi_state.get("state"):
        # Χωρίς Μητρώο ΑΑΔΕ (π.χ. μέλος εταιρείας χωρίς δικούς του κωδικούς): η κατάσταση του ΓΕΜΗ — «Διαγραφή» =
        # διακοπή με ημερομηνία την τελευταία αλλαγή κατάστασης (αναφορά χρήστη 2026-10-27).
        sets += ["activity_state=?", "cease_date=?", "cease_reason=?"]
        args += [gemi_state["state"], gemi_state.get("date", "") if gemi_state["state"] == "ceased" else "",
                 f"ΓΕΜΗ: {gemi_state.get('status', '')}" if gemi_state["state"] == "ceased" else ""]
        if gemi_state.get("start"):
            sets.append("start_date=CASE WHEN start_date='' THEN ? ELSE start_date END")
            args.append(gemi_state["start"])
    if vat:
        sets += ["vat_subject=?", "books_category=?", "books_category_raw=?"]
        args += [None if vat["vat_subject"] is None else int(vat["vat_subject"]), vat["books_category"],
                 vat["books_category_raw"]]
        # Η περίοδος από τις ΠΡΑΓΜΑΤΙΚΕΣ υποχρεώσεις Φ2 (easycheck, vat_period_source='easycheck') υπερισχύει της
        # ερμηνείας της κατηγορίας βιβλίων — δεν την πατάμε.
        sets.append("vat_period_type=CASE WHEN vat_period_source IN ('easycheck','manual') THEN vat_period_type ELSE ? END")
        args.append(vat["vat_period_type"])
    contact = (aade or {}).get("contact") or {}
    if any(contact.values()):
        # Στοιχεία επικοινωνίας του Μητρώου — ΠΟΤΕ πάνω σε όσα άλλαξε ο χρήστης (contact_source='manual')
        sets.append("email=CASE WHEN contact_source='manual' THEN email ELSE ? END")
        sets.append("mobile=CASE WHEN contact_source='manual' THEN mobile ELSE ? END")
        sets.append("phone=CASE WHEN contact_source='manual' THEN phone ELSE ? END")
        sets.append("contact_source=CASE WHEN contact_source='manual' THEN 'manual' ELSE 'aade' END")
        sets.append("email_origin=CASE WHEN contact_source='manual' THEN email_origin ELSE ? END")
        sets.append("email_assistant=CASE WHEN contact_source='manual' THEN email_assistant ELSE ? END")
        sets.append("email_assistant_origin=CASE WHEN contact_source='manual' THEN email_assistant_origin ELSE ? END")
        sets.append("contact_at=?")
        args += [contact.get("email", ""), contact.get("mobile", ""), contact.get("phone", ""), contact.get("origin", ""),
             contact.get("email_assistant", ""), contact.get("assistant_origin", ""), now]
    if aade and aade.get("trade_name") is not None and "trade_name" in aade:
        sets.append("trade_name=?")
        args.append(aade.get("trade_name") or "")
    if aade and aade.get("members") is not None:
        from .. import members as members_mod
        members_mod.store(conn, afm, aade["members"], "aade")
        try:
            # τα μέλη γίνονται και εγγραφές πελατών + έλεγχος ατομικής επιχείρησης (RgWsPublic2, αν υπάρχουν κωδικοί)
            members_mod.sync_member_clients(conn, afm)
        except Exception as exc:                       # noqa: BLE001 — τα μέλη είναι επιπλέον· ποτέ αποτυχία ανάκτησης
            log.warning("μέλη %s: %s", afm, exc)
    if amka_fetch is None and aade_fetch is lookup_aade.fetch_company_profile:     # πραγματική ανάκτηση (όχι tests)
        from .. import efka
        amka_fetch = efka.retrieve_amka
    if amka_fetch is not None and aade and aade.get("kind") != "ΝΟΜΙΚΟ ΠΡΟΣΩΠΟ" and credentials.get(conn, afm):
        # Φυσικό πρόσωπο (ιδιώτης, μέλος, ατομική) με δικούς του κωδικούς: και ο ΑΜΚΑ από το myAMKA, αν λείπει — τον
        # θέλουν ΚΕΑΟ / εισφορές e-ΕΦΚΑ (ρητό αίτημα 2026-10-27). Ποτέ αποτυχία της ανάκτησης στοιχείων εξαιτίας του.
        from .. import efka
        try:
            amka = amka_fetch(conn, afm)
            if amka.get("ok") and not amka.get("existing"):
                sources.append("myAMKA")
            elif not amka.get("ok"):
                errors.append("ΑΜΚΑ: " + efka.REASONS_EL.get(amka.get("reason", ""), amka.get("reason", "")))
        except Exception as exc:                       # noqa: BLE001
            log.warning("ΑΜΚΑ %s: %s", afm, exc)
    if kads:
        set_kads(conn, afm, kads)
    elif aade and aade.get("business_state") in ("ceased", "none"):
        set_kads(conn, afm, [])                    # κλειστή/χωρίς επιχείρηση: δεν υπάρχουν ενεργά ΚΑΔ
    have_kads = bool(kads) or bool(conn.execute("SELECT 1 FROM business_kad WHERE afm=?", (afm,)).fetchone())
    kad_optional = (bool(aade) and aade.get("business_state") in ("ceased", "none")) or \
        fields.get("legal_form") == "ΙΔΙΩΤΗΣ"
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


def is_individual_form(form: Optional[str]) -> bool:
    return "ΑΤΟΜΙΚ" in strip_accents(form or "").upper()


def clean_gemi_name(name: str) -> str:
    """«(ΔΙΑΓΡΑΦΗΚΕ)ΕΛΕΥΘΕΡΙΟΣ …» → «ΕΛΕΥΘΕΡΙΟΣ …» — το ΓΕΜΗ βάζει το πρόθεμα στις διαγραμμένες εγγραφές."""
    return re.sub(r"^\s*\(\s*ΔΙΑΓΡΑΦ[^)]*\)\s*", "", name or "", flags=re.IGNORECASE).strip()


def _as_private_person(conn: sqlite3.Connection, afm: str, fields: dict[str, Any]) -> None:
    """Φυσικό πρόσωπο με ατομική επιχείρηση διαγραμμένη στο ΓΕΜΗ = ΙΔΙΩΤΗΣ (όπως το Μητρώο ΑΑΔΕ: «φυσικό πρόσωπο με
    κλειστή επιχείρηση είναι ΙΔΙΩΤΗΣ»), χωρίς ενεργούς ΚΑΔ. Ως όνομα: του Μητρώου μελών ΑΑΔΕ (αν είναι μέλος), αλλιώς
    του ΓΕΜΗ χωρίς το «(ΔΙΑΓΡΑΦΗΚΕ)» (αναφορά χρήστη 2026-10-27)."""
    fields["legal_form"] = "ΙΔΙΩΤΗΣ"
    member = conn.execute("SELECT name FROM company_members WHERE member_afm=? AND name<>'' LIMIT 1", (afm,)).fetchone()
    name = member["name"] if member else clean_gemi_name(fields.get("name") or "")
    if name:
        fields["name"] = name
    set_kads(conn, afm, [])


def reapply_cached_gemi(conn: sqlite3.Connection) -> int:
    """Εφαρμόζει τον κανόνα «διαγραμμένη ατομική = ιδιώτης» σε όσους έχουν ήδη απάντηση ΓΕΜΗ στη βάση χωρίς Μητρώο ΑΑΔΕ
    — ώστε να διορθωθούν χωρίς νέα «Ανανέωση στοιχείων». Καλείται στην εκκίνηση· φθηνό (μόνο τοπικά δεδομένα)."""
    n = 0
    for r in conn.execute("SELECT afm, name, legal_form, lookup_raw FROM businesses "
                          "WHERE lookup_raw LIKE '%business_portal%' AND legal_form<>'ΙΔΙΩΤΗΣ'").fetchall():
        try:
            raw = json.loads(r["lookup_raw"] or "{}")
        except ValueError:
            continue
        if raw.get("aade") or not raw.get("business_portal"):
            continue
        bp = raw["business_portal"]
        c = portal.parse_company(bp.get("search") or bp) if isinstance(bp, dict) else {}
        if c.get("state") != "ceased" or not is_individual_form(c.get("legal_form") or r["legal_form"]):
            continue
        fields: dict[str, Any] = {"name": c.get("name") or r["name"]}
        _as_private_person(conn, r["afm"], fields)
        conn.execute("UPDATE businesses SET name=?, legal_form=?, activity_state='ceased', cease_date=?, cease_reason=?, "
                     "updated_at=? WHERE afm=?",
                     (fields["name"], fields["legal_form"], c.get("status_date") or "",
                      f"ΓΕΜΗ: {c.get('status') or ''}", db.utcnow(), r["afm"]))
        n += 1
    return n


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


def missing_basics(b: dict[str, Any]) -> list[str]:
    """Βασικά στοιχεία που λείπουν και χρειάζεται η εφαρμογή, ανάλογα με το είδος του πελάτη — ιδιώτης / επιχειρηματίας
    (ατομική) / επιχείρηση (νομικό πρόσωπο). Κενό = πλήρης. Για τον διάλογο «Έλεγχοι & ενημέρωση»: αν λείπει κάτι,
    προτείνεται «Ανανέωση στοιχείων» (ρητό αίτημα χρήστη 2026-10-23)."""
    from ..obligations import is_legal_entity, vat_period
    out = []
    if not (b.get("name") or "").strip() or b.get("name") == b.get("afm"):
        out.append("επωνυμία")
    state = b.get("activity_state") or ""
    if not state:
        return out + ["κατάσταση (ιδιώτης / επιχείρηση)"]
    if not (b.get("doy") or "").strip():
        out.append("ΔΟΥ")
    if state == "none":                                    # ιδιώτης: δεν χρειάζεται τίποτε επιχειρηματικό
        return out
    if state == "active" and not (b.get("kad_main_code") or "").strip():
        out.append("κύριος ΚΑΔ")
    if not (b.get("books_category") or "").strip() and not (b.get("books_category_raw") or "").strip():
        out.append("κατηγορία βιβλίων")
    if b.get("vat_subject") is None:
        out.append("καθεστώς ΦΠΑ")
    elif b.get("vat_subject") == 1 and state == "active" and not vat_period(b):
        out.append("περίοδος ΦΠΑ")
    if is_legal_entity(b.get("legal_form") or "") or b.get("kind") == "ΝΟΜΙΚΟ ΠΡΟΣΩΠΟ":
        if not (b.get("address") or "").strip():
            out.append("έδρα")
    elif not (b.get("legal_form") or "").strip() and state == "active":
        out.append("νομική μορφή")
    return out
