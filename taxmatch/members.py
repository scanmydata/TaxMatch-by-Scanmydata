"""Μέλη και εκπρόσωποι εταιρειών (όχι ατομικών επιχειρήσεων) — ρητό αίτημα χρήστη 2026-10-22: «αν θέλει ο χρήστης να
αντλεί και τα μέλη, που μπορεί να προέλθουν και από το Excel, και να αντλούνται και γι' αυτά οφειλές και έντυπα».

Πηγές:
* **Μητρώο ΑΑΔΕ** (ίδιο login/εφαρμογή με την ανάκτηση στοιχείων — comregistry): `getMelh/{afm}` (μέλη: επωνυμία,
  ΑΦΜ, είδος συμμετοχής, ποσοστό) και `getSxeseisEpixeirhshs/{afm}` (εκπρόσωποι/διαχειριστές) — ΜΟΝΟ ενεργά (χωρίς
  `hmdiakophs`), όπως το `registryDetails` του ScanMyData Tax Center. Χρειάζονται οι κωδικοί TAXISnet της εταιρείας.
* **Excel**: στη γραμμή του μέλους «ΑΦΜ εταιρείας», ή στη γραμμή της εταιρείας «Μέλη» / «ΑΦΜ μελών» (λίστα).

Οφειλές/έντυπα ενός μέλους αντλούνται με τους ΔΙΚΟΥΣ ΤΟΥ κωδικούς — γι' αυτό το μέλος γίνεται πελάτης («Προσθήκη ως
πελάτη», μόνο ρητά) και οι κωδικοί του έρχονται από το Excel ή τη δική του καρτέλα. Τρίτοι που δεν είναι πελάτες
δεν αποθηκεύονται πέρα από όνομα/ΑΦΜ/ιδιότητα (ελαχιστοποίηση).
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urljoin

from . import db
from .business_profiles import credentials, lookup_aade
from .identifiers import normalize_afm

ROLE_FIELDS = ("eidossymmetoxhs", "eidossxeshs")


def _rows(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def parse_members(melh: str, sxeseis: str) -> list[dict[str, str]]:
    """Ενεργά μέλη + σχέσεις → [{afm, name, role, share, since}] (ένα ανά ΑΦΜ· οι ιδιότητες ενώνονται)."""
    out: dict[str, dict[str, str]] = {}
    for r, afm_key, role_key in [(r, "afmmelous", "eidossymmetoxhs") for r in _rows(melh)] + \
                                [(r, "afmsxeshs", "eidossxeshs") for r in _rows(sxeseis)]:
        if _s(r.get("hmdiakophs")):
            continue
        afm = normalize_afm(_s(r.get(afm_key)))
        if not afm:
            continue
        m = out.setdefault(afm, {"afm": afm, "name": _s(r.get("epwnymia")), "role": "", "share": "", "since": ""})
        role = _s(r.get(role_key))
        if role and role not in m["role"]:
            m["role"] = " · ".join(x for x in (m["role"], role) if x)
        if _s(r.get("percentage")) and not m["share"]:
            m["share"] = _s(r.get("percentage")).replace(".", ",") + "%"
        m["since"] = m["since"] or _s(r.get("hmenarxhs"))
        m["name"] = m["name"] or _s(r.get("epwnymia"))
    return list(out.values())


def fetch_from_registry(http, afm: str) -> list[dict[str, str]]:
    """Με ήδη συνδεδεμένο http (aade_login της εταιρείας)."""
    reg = urljoin(lookup_aade.AADE, "/saadeapps3/comregistry")
    w = reg + "/webresources/infomytaxisnet"
    http.follow("GET", reg + "/")
    return parse_members(http.follow("GET", f"{w}/getMelh/{afm}")["text"],
                         http.follow("GET", f"{w}/getSxeseisEpixeirhshs/{afm}")["text"])


def store(conn: sqlite3.Connection, company: str, members: Iterable[dict[str, str]], source: str) -> int:
    """Αποθηκεύει (ή ενημερώνει) μέλη. Από ΑΑΔΕ: όσα δεν εμφανίζονται πια (αποχώρησαν) σβήνονται — εκτός αν ήρθαν από
    Excel/χειροκίνητα. Επιστρέφει πλήθος."""
    members = [m for m in members if m.get("afm") and m["afm"] != company]
    now = db.utcnow()
    if source == "aade":
        marks = ",".join("?" * len(members)) or "''"
        conn.execute(f"DELETE FROM company_members WHERE company_afm=? AND source='aade' AND member_afm NOT IN ({marks})",
                     (company, *[m["afm"] for m in members]))
    for m in members:
        conn.execute("INSERT INTO company_members(company_afm, member_afm, name, role, share, since, source, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(company_afm, member_afm) DO UPDATE SET "
                     "name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE company_members.name END, "
                     "role=CASE WHEN excluded.role<>'' THEN excluded.role ELSE company_members.role END, "
                     "share=CASE WHEN excluded.share<>'' THEN excluded.share ELSE company_members.share END, "
                     "since=CASE WHEN excluded.since<>'' THEN excluded.since ELSE company_members.since END, "
                     "source=CASE WHEN company_members.source IN ('excel','manual') THEN company_members.source "
                     "ELSE excluded.source END, updated_at=excluded.updated_at",
                     (company, m["afm"], m.get("name", ""), m.get("role", ""), m.get("share", ""), m.get("since", ""),
                      source, now))
    return len(members)


def fetch_for_company(conn: sqlite3.Connection, afm: str,
                      login_fn: Callable[[str, str], dict] = lookup_aade.aade_login) -> dict[str, Any]:
    creds = credentials.get(conn, afm)
    if not creds:
        return {"ok": False, "reason": "Δεν υπάρχουν κωδικοί TAXISnet της εταιρείας.", "n": 0}
    login = login_fn(*creds)
    if not login.get("ok"):
        return {"ok": False, "reason": lookup_aade.REASONS_EL.get(login.get("reason") or "", "Αποτυχία σύνδεσης."), "n": 0}
    found = fetch_from_registry(login["http"], afm)
    return {"ok": True, "reason": "", "n": store(conn, afm, found, "aade")}


def company_members(conn: sqlite3.Connection, company: str) -> list[dict[str, Any]]:
    """Μέλη με ό,τι χρειάζεται η οθόνη: αν είναι πελάτης, αν έχει κωδικούς, οφειλές (σύνολα ανά πύλη)."""
    out = []
    for r in conn.execute("SELECT m.*, b.afm AS client_afm, b.email FROM company_members m LEFT JOIN businesses b "
                          "ON b.afm=m.member_afm WHERE m.company_afm=? ORDER BY m.role DESC, m.name", (company,)):
        d = dict(r)
        d["is_client"] = bool(r["client_afm"])
        d["has_creds"] = credentials.get(conn, r["member_afm"]) is not None if d["is_client"] else False
        d["debts"] = {x["source"]: x for x in (dict(y) for y in conn.execute(
            "SELECT source, total, status, synced_at FROM debt_source_sync WHERE afm=?", (r["member_afm"],)))}
        out.append(d)
    return out


def companies_of(conn: sqlite3.Connection, member: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT m.company_afm, m.role, b.name FROM company_members m LEFT JOIN "
                                          "businesses b ON b.afm=m.company_afm WHERE m.member_afm=?", (member,))]


def add_as_client(conn: sqlite3.Connection, company: str, member_afm: str) -> bool:
    """Το μέλος γίνεται πελάτης (για να έχει δικούς του κωδικούς/οφειλές/έντυπα). False αν ήταν ήδη."""
    from .business_profiles import service
    row = conn.execute("SELECT name FROM company_members WHERE company_afm=? AND member_afm=?",
                       (company, member_afm)).fetchone()
    return service.add(conn, member_afm, row["name"] if row else "", source="member")


def sync_member_clients(conn: sqlite3.Connection, company: str,
                        rg_lookup: Optional[Callable[[str], dict[str, Any]]] = None, check_business: bool = True) -> list[str]:
    """Κάθε μέλος της εταιρείας γίνεται ΚΑΙ εγγραφή στους πελάτες (εμφανίζεται κάτω από την εταιρεία στη λίστα,
    ανοίγει κανονικά — ρητό αίτημα χρήστη 2026-10-23) και εξετάζεται αν έχει ατομική επιχείρηση. Επιστρέφει τους νέους.
    Ατομική επιχείρηση μέλους χωρίς δικούς του κωδικούς: «Αναζήτηση Βασικών Στοιχείων Μητρώου» (RgWsPublic2) με τους
    ειδικούς κωδικούς του γραφείου — το Μητρώο TAXISnet δείχνει μόνο τον ΑΦΜ του λογαριασμού. Χωρίς αυτούς: άγνωστο."""
    from .business_profiles import service
    added = []
    rows = conn.execute("SELECT member_afm FROM company_members WHERE company_afm=?", (company,)).fetchall()
    for r in rows:
        if add_as_client(conn, company, r["member_afm"]):
            added.append(r["member_afm"])
    lookup = (rg_lookup or _rg_lookup(conn)) if check_business else None
    for r in rows:
        afm = r["member_afm"]
        if lookup is None or credentials.get(conn, afm):
            continue                                # με δικούς του κωδικούς: η κανονική ανάκτηση ξέρει περισσότερα
        res = lookup(afm)
        if not res.get("ok"):
            continue
        from .business_profiles.rgwspublic import business_state
        state = business_state(res)
        conn.execute("UPDATE businesses SET activity_state=CASE WHEN ?<>'' THEN ? ELSE activity_state END, "
                     "cease_date=?, start_date=CASE WHEN ?<>'' THEN ? ELSE start_date END, "
                     "trade_name=CASE WHEN ?<>'' THEN ? ELSE trade_name END, "
                     "name=CASE WHEN name='' OR name=afm THEN ? ELSE name END, updated_at=? WHERE afm=?",
                     (state, state, res.get("stop_date") or "", res.get("start_date") or "", res.get("start_date") or "",
                      res.get("trade_name") or "", res.get("trade_name") or "", res.get("name") or "", db.utcnow(), afm))
        if state == "active" and res.get("kads"):
            service.set_kads(conn, afm, res["kads"])
    return added


def _rg_lookup(conn: sqlite3.Connection) -> Optional[Callable[[str], dict[str, Any]]]:
    from . import settings_store
    from .business_profiles import rgwspublic
    user, password = settings_store.get(conn, "rg_user"), settings_store.get(conn, "rg_pass")
    if not (user and password):
        return None
    return lambda afm: rgwspublic.lookup(afm, user, password)


def parent_company(conn: sqlite3.Connection) -> dict[str, str]:
    """ΑΦΜ μέλους → ΑΦΜ εταιρείας στην οποία εμφανίζεται (λίστα πελατών). Μέλος σε πολλές: η πρώτη αλφαβητικά."""
    out: dict[str, str] = {}
    for r in conn.execute("SELECT m.member_afm, m.company_afm FROM company_members m JOIN businesses b ON "
                          "b.afm=m.member_afm JOIN businesses c ON c.afm=m.company_afm ORDER BY m.company_afm"):
        if r["member_afm"] != r["company_afm"]:
            out.setdefault(r["member_afm"], r["company_afm"])
    return out


def remove(conn: sqlite3.Connection, company: str, member_afm: str) -> None:
    conn.execute("DELETE FROM company_members WHERE company_afm=? AND member_afm=?", (company, member_afm))


def parse_afm_list(value: Any) -> list[str]:
    """«123456789, 987654321» / «123456789;987654321» / ένας ΑΦΜ ανά γραμμή → έγκυροι ΑΦΜ."""
    out = []
    for part in re.split(r"[,;/\n\s]+", str(value or "")):
        afm = normalize_afm(part)
        if afm and afm not in out:
            out.append(afm)
    return out


def from_import(conn: sqlite3.Connection, rows: Iterable[Any]) -> int:
    """Σύνδεση μελών από το Excel: `member_of` (ΑΦΜ εταιρειών στη γραμμή του μέλους) και `member_afms` (ΑΦΜ μελών
    στη γραμμή της εταιρείας). Μόνο για εταιρείες που είναι πελάτες."""
    names = {r.afm: r.name for r in rows}
    n = 0
    clients = {r["afm"] for r in conn.execute("SELECT afm FROM businesses")}
    for r in rows:
        pairs = [(c, r.afm) for c in getattr(r, "member_of", [])] + [(r.afm, m) for m in getattr(r, "member_afms", [])]
        for company, member in pairs:
            if company in clients and company != member:
                n += store(conn, company, [{"afm": member, "name": names.get(member, "")}], "excel")
                # και εγγραφή στους πελάτες (χωρίς δίκτυο εδώ — η ατομική επιχείρηση ελέγχεται με την ανανέωση στοιχείων)
                sync_member_clients(conn, company, check_business=False)
    return n


def legal_entity(conn: sqlite3.Connection, afm: str) -> Optional[bool]:
    from .obligations import is_legal_entity
    row = conn.execute("SELECT legal_form, activity_state FROM businesses WHERE afm=?", (afm,)).fetchone()
    if not row:
        return None
    return is_legal_entity(row["legal_form"] or "")
