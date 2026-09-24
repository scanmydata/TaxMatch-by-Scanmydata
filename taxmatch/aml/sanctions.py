"""Έλεγχος σε λίστες οικονομικών κυρώσεων (δέουσα επιμέλεια — άρθρα 13, 16, 18 ν. 4557/2018· κυρώσεις ΕΕ/ΟΗΕ).

Πηγή: **Ενοποιημένη λίστα οικονομικών κυρώσεων της ΕΕ** (Financial Sanctions Files — FSF, CSV v1.1). Είναι δημόσια·
το `token` στο URL είναι το ΔΗΜΟΣΙΟ που δημοσιεύει η ίδια η Επιτροπή στη σελίδα λήψης (όχι μυστικό/λογαριασμός). Η λίστα
της ΕΕ ενσωματώνει και τις κυρώσεις του Συμβουλίου Ασφαλείας του ΟΗΕ. Επαληθεύτηκε 2026-09-24: ~25 MB, ~44.000 γραμμές
(μία ανά ψευδώνυμο/διεύθυνση/…), στήλες «Entity_LogicalId;…;NameAlias_WholeName;…» (`;`, UTF-8 με BOM).

Σχεδιασμός:
* κατεβαίνει ΜΙΑ φορά και ανανεώνεται αν είναι παλαιότερη από `MAX_AGE_DAYS` — κρατάμε μόνο ένα συμπαγές ευρετήριο JSON
  (`<data>/sanctions/eu.json`), όχι το CSV.
* τα ελληνικά ονόματα λατινοποιούνται (ΕΛΟΤ 743) και το ταίριασμα γίνεται σε «σκελετό» ονόματος (CH/KH/H, MP/B, OU/U, Y/I…)
  με ανοχή σε μικρές διαφορές — επειδή η λίστα γράφει «Charalambos» ενώ η ΑΑΔΕ «ΧΑΡΑΛΑΜΠΟΣ».
* αποτέλεσμα = **πιθανές** ταυτίσεις για έλεγχο από τον λογιστή (ημερομηνία γέννησης, ιθαγένεια), ΠΟΤΕ αυτόματη κατάταξη:
  η «ΥΠΕΡΙΣΧΥΕΙ — κυρώσεις» τη βάζει ο χρήστης αφού επιβεβαιώσει.
* κάθε έλεγχος καταγράφεται (`aml_screenings`) ως τεκμήριο για τον φάκελο (τήρηση 5ετίας, άρθρο 30).

Δεν καλύπτονται (ανοιχτό): OpenSanctions / OFAC (το taxis τα χρησιμοποιεί — OpenSanctions θέλει εμπορική άδεια για
επαγγελματική χρήση), λίστες ΠΕΠ (δεν υπάρχει επίσημη δημόσια λίστα — ο έλεγχος ΠΕΠ μένει δήλωση + αναζήτηση Google).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .. import config, db

log = logging.getLogger(__name__)

EU_CSV_URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList_1_1/content"
              "?token=dG9rZW4tMjAxNw")
EU_INFO_URL = "https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions"
MAX_AGE_DAYS = 7
FUZZY = 0.85

# ------------------------------------------------------------------ λατινοποίηση (ΕΛΟΤ 743, απλοποιημένη)
_DIGRAPHS = (("ΟΥ", "OU"), ("ΑΙ", "AI"), ("ΕΙ", "EI"), ("ΟΙ", "OI"), ("ΑΥ", "AV"), ("ΕΥ", "EV"), ("ΗΥ", "IV"),
             ("ΜΠ", "MP"), ("ΝΤ", "NT"), ("ΓΚ", "GK"), ("ΓΓ", "NG"), ("ΓΞ", "NX"), ("ΓΧ", "NCH"))
_SINGLE = dict(zip("ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ",
                   ["A", "V", "G", "D", "E", "Z", "I", "TH", "I", "K", "L", "M", "N", "X", "O", "P", "R", "S", "T", "Y",
                    "F", "CH", "PS", "O"]))


def _plain_upper(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.upper().replace("ς", "Σ")


def transliterate(text: str) -> str:
    """Ελληνικά → λατινικά (ΕΛΟΤ 743 χωρίς τις λεπτομέρειες του ΑΥ/ΕΥ πριν από άηχο)· τα λατινικά μένουν ως έχουν."""
    t = _plain_upper(text)
    for gr, lat in _DIGRAPHS:
        t = t.replace(gr, lat)
    return "".join(_SINGLE.get(c, c) for c in t)


#: τύποι εταιρείας/μόρια που δεν χαρακτηρίζουν πρόσωπο
_STOP = {"IKE", "AE", "EPE", "OE", "EE", "MONOPROSOPI", "MONOPROSOPH", "LTD", "LIMITED", "SA", "LLC", "INC", "CO",
         "COMPANY", "CORP", "CORPORATION", "GMBH", "AG", "BV", "PLC", "THE", "OF", "AND", "KAI", "TOU", "TIS", "TON",
         "AL", "EL", "BIN", "IBN", "BINT", "ABU", "OOO", "ZAO", "OAO", "PAO", "JSC", "LLP"}
_SKELETON = (("KH", "H"), ("CH", "H"), ("PH", "F"), ("TH", "T"), ("OU", "U"), ("MB", "B"), ("MP", "B"), ("NT", "D"),
             ("GK", "G"), ("NG", "G"), ("KS", "X"), ("EI", "I"), ("OI", "I"), ("AI", "E"), ("Y", "I"), ("W", "V"),
             ("B", "V"), ("F", "V"), ("J", "I"), ("C", "K"), ("Q", "K"), ("Z", "S"))


def skeleton(token: str) -> str:
    s = token
    for a, b in _SKELETON:
        s = s.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", s)


def name_tokens(name: str) -> list[str]:
    """Σκελετοί των λέξεων ενός ονόματος (χωρίς εταιρικούς τύπους και μονογράμματα)."""
    latin = transliterate(name)
    words = re.findall(r"[A-Z0-9]+", latin)
    return [skeleton(w) for w in words if len(w) > 1 and w not in _STOP]


# ------------------------------------------------------------------ ευρετήριο
def index_path() -> Path:
    return config.data_dir() / "sanctions" / "eu.json"


def build_index(csv_text: str) -> dict[str, Any]:
    """CSV της ΕΕ → {generated, entities:[{id, ref, un, type, names, programme, designated, remark, url, birth,
    countries}]} (μία εγγραφή ανά οντότητα, όλα τα ψευδώνυμα μαζί)."""
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")), delimiter=";")
    ents: dict[str, dict[str, Any]] = {}
    generated = ""
    for row in reader:
        eid = (row.get("Entity_LogicalId") or "").strip()
        if not eid:
            continue
        generated = generated or (row.get("fileGenerationDate") or "").strip()
        e = ents.setdefault(eid, {"id": eid, "ref": (row.get("Entity_EU_ReferenceNumber") or "").strip(),
                                  "un": (row.get("Entity_UnitedNationId") or "").strip(),
                                  "type": (row.get("Entity_SubjectType") or "").strip(),
                                  "programme": (row.get("Entity_Regulation_Programme") or "").strip(),
                                  "designated": (row.get("Entity_DesignationDate") or "").strip(),
                                  "remark": (row.get("Entity_Remark") or "").strip()[:300],
                                  "url": (row.get("Entity_Regulation_PublicationUrl") or "").strip(),
                                  "names": [], "birth": [], "countries": []})
        for key, field in (("names", "NameAlias_WholeName"), ("birth", "BirthDate_BirthDate"),
                           ("countries", "Citizenship_CountryDescription"), ("countries", "Address_CountryDescription")):
            value = (row.get(field) or "").strip()
            if value and value not in e[key]:
                e[key].append(value)
    return {"generated": generated, "entities": [e for e in ents.values() if e["names"]]}


def load_index(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    p = path or index_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def index_age_days(idx: Optional[dict[str, Any]]) -> Optional[float]:
    if not idx or not idx.get("downloaded_at"):
        return None
    try:
        when = datetime.fromisoformat(idx["downloaded_at"].replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400


def refresh_index(fetch: Optional[Callable[[str], str]] = None, force: bool = False,
                  progress: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Κατεβάζει τη λίστα αν λείπει ή είναι παλιά. Αν η λήψη αποτύχει αλλά υπάρχει παλιό ευρετήριο, κρατά αυτό
    (με `stale=True`) — καλύτερα έλεγχος σε λίστα λίγων ημερών παρά κανένας· το αποτέλεσμα γράφει την ημερομηνία της."""
    idx = load_index()
    age = index_age_days(idx)
    if idx and not force and age is not None and age < MAX_AGE_DAYS:
        return idx
    progress("Λήψη της ενοποιημένης λίστας κυρώσεων της ΕΕ (~25 MB)…")
    try:
        text = fetch(EU_CSV_URL) if fetch else _download(EU_CSV_URL)
        new = build_index(text)
        if len(new["entities"]) < 100:
            raise ValueError("η λίστα φαίνεται ελλιπής")
    except Exception as exc:                                           # δίκτυο / αλλαγή μορφής
        if idx:
            log.warning("[sanctions] ανανέωση απέτυχε, χρησιμοποιείται η αποθηκευμένη λίστα: %s", exc)
            return {**idx, "stale": True}
        raise
    new["downloaded_at"] = db.utcnow()
    p = index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    log.info("[sanctions] λίστα ΕΕ: %d οντότητες (αρχείο %s)", len(new["entities"]), new["generated"])
    return new


def _download(url: str) -> str:
    from ..http import make_session
    r = make_session().get(url, timeout=120)
    r.raise_for_status()
    return r.content.decode("utf-8-sig", errors="replace")


# ------------------------------------------------------------------ ταίριασμα
def _similar(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if a[:1] != b[:1] or abs(len(a) - len(b)) > 3:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def match_score(query: list[str], alias: list[str]) -> float:
    """0 = καμία σχέση. Θετικό μόνο αν ΟΛΕΣ οι λέξεις του ΜΙΚΡΟΤΕΡΟΥ ονόματος βρίσκονται (ακριβώς ή σχεδόν) στο άλλο,
    και τουλάχιστον δύο λέξεις (ή ταυτόσημο ονομα μιας λέξης). Επιστρέφει κάλυψη × μέση ομοιότητα."""
    if not query or not alias:
        return 0.0
    short, long_ = (query, alias) if len(query) <= len(alias) else (alias, query)
    used: set[int] = set()
    sims = []
    for t in short:
        best, best_i = 0.0, -1
        for i, u in enumerate(long_):
            if i in used:
                continue
            s = _similar(t, u)
            if s > best:
                best, best_i = s, i
        if best < FUZZY:
            return 0.0
        used.add(best_i)
        sims.append(best)
    if len(short) < 2 and len(long_) > 1:
        # μία λέξη (π.χ. «ROSNEFT»): μόνο ακριβής και όχι κοινή/σύντομη λέξη — αλλιώς κάθε επώνυμο θα «ταίριαζε»
        return 0.5 if sims[0] == 1.0 and len(short[0]) >= 5 else 0.0
    return round(len(short) / len(long_) * (sum(sims) / len(sims)), 3)


def screen(subjects: Iterable[tuple[str, str]], idx: dict[str, Any], limit: int = 5,
           threshold: float = 0.5) -> list[dict[str, Any]]:
    """subjects: (ρόλος, όνομα). -> [{role, name, latin, hits:[…]}] — hits ταξινομημένα κατά βαθμό."""
    prepared = []
    for e in idx.get("entities", []):
        prepared.append((e, [name_tokens(n) for n in e["names"]]))
    out = []
    for role, name in subjects:
        q = name_tokens(name)
        hits = []
        if q:
            for e, aliases in prepared:
                best, best_name = 0.0, ""
                for n, toks in zip(e["names"], aliases):
                    s = match_score(q, toks)
                    if s > best:
                        best, best_name = s, n
                if best >= threshold:
                    hits.append({"id": e["id"], "ref": e["ref"], "un": e["un"], "type": e["type"],
                                 "matched_name": best_name, "score": best, "programme": e["programme"],
                                 "designated": e["designated"], "birth": e["birth"][:3], "countries": e["countries"][:3],
                                 "remark": e["remark"], "url": e["url"]})
        hits.sort(key=lambda h: -h["score"])
        out.append({"role": role, "name": name, "latin": transliterate(name), "hits": hits[:limit]})
    return out


# ------------------------------------------------------------------ πελάτης
def subjects_for_client(conn: sqlite3.Connection, afm: str) -> list[tuple[str, str]]:
    """Ποιους ελέγχουμε: τον πελάτη, τον νόμιμο εκπρόσωπο και τους πραγματικούς δικαιούχους του φακέλου."""
    from . import store
    b = conn.execute("SELECT name FROM businesses WHERE afm=?", (afm,)).fetchone()
    p = store.get_profile(conn, afm)
    out: list[tuple[str, str]] = []
    if b and b["name"]:
        out.append(("Πελάτης", b["name"]))
    if p["rep"].get("name"):
        out.append(("Νόμιμος εκπρόσωπος", p["rep"]["name"]))
    for u in p["ubos"]:
        if u.get("name"):
            out.append(("Πραγματικός δικαιούχος", u["name"]))
    seen, uniq = set(), []
    for role, name in out:
        key = " ".join(sorted(name_tokens(name)))
        if key and key not in seen:
            seen.add(key)
            uniq.append((role, name))
    return uniq


def source_label(idx: dict[str, Any]) -> str:
    label = f"Ενοποιημένη λίστα κυρώσεων ΕΕ (αρχείο {idx.get('generated') or '—'})"
    return label + " — ΠΑΛΙΑ ΕΚΔΟΣΗ (η ανανέωση απέτυχε)" if idx.get("stale") else label


def screen_client(conn: sqlite3.Connection, afm: str, idx: dict[str, Any]) -> dict[str, Any]:
    subjects = subjects_for_client(conn, afm)
    results = screen(subjects, idx)
    hits = [dict(h, subject=r["name"], role=r["role"]) for r in results for h in r["hits"]]
    result = "possible" if hits else "clear"
    cur = conn.execute(
        "INSERT INTO aml_screenings(afm, screened_at, source, subjects_json, hits_json, result) VALUES (?,?,?,?,?,?)",
        (afm, db.utcnow(), source_label(idx),
         json.dumps([{"role": r["role"], "name": r["name"], "latin": r["latin"]} for r in results], ensure_ascii=False),
         json.dumps(hits, ensure_ascii=False), result))
    return {"id": cur.lastrowid, "result": result, "subjects": results, "hits": hits, "source": source_label(idx)}


def list_screenings(conn: sqlite3.Connection, afm: str) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT * FROM aml_screenings WHERE afm=? ORDER BY screened_at DESC, id DESC", (afm,)):
        d = dict(r)
        d["subjects"] = json.loads(d.pop("subjects_json") or "[]")
        d["hits"] = json.loads(d.pop("hits_json") or "[]")
        out.append(d)
    return out


def latest_screening(conn: sqlite3.Connection, afm: str) -> Optional[dict[str, Any]]:
    rows = list_screenings(conn, afm)
    return rows[0] if rows else None


def set_review_note(conn: sqlite3.Connection, screening_id: int, note: str) -> None:
    conn.execute("UPDATE aml_screenings SET review_note=? WHERE id=?", ((note or "").strip()[:1000], screening_id))


def google_query_url(name: str) -> str:
    """Αναζήτηση Google για ΠΕΠ/δυσμενή δημοσιεύματα — ανοίγει ΜΟΝΟ με ρητό κλικ του χρήστη (όπως το «Έλεγχος σε Google»
    του taxis). Δεν στέλνεται ΑΦΜ, μόνο το όνομα."""
    from urllib.parse import quote_plus
    terms = ('"' + (name or "").strip() + '" (βουλευτής OR δήμαρχος OR υπουργός OR "γενικός γραμματέας" OR '
             'απάτη OR ξέπλυμα OR καταδίκη OR σύλληψη OR κυρώσεις)')
    return "https://www.google.com/search?q=" + quote_plus(terms)


def screening_html(conn: sqlite3.Connection, afm: str, screening: dict[str, Any]) -> str:
    """Έκθεση ελέγχου κυρώσεων για τον φάκελο (PDF μέσω documents.write_pdf)."""
    from html import escape
    b = conn.execute("SELECT name FROM businesses WHERE afm=?", (afm,)).fetchone()
    when = screening["screened_at"][:16].replace("T", " ")
    rows = "".join(f"<tr><td>{escape(s['role'])}</td><td>{escape(s['name'])}</td><td>{escape(s['latin'])}</td></tr>"
                   for s in screening["subjects"])
    if screening["hits"]:
        hits = "".join(
            f"<tr><td>{escape(h['subject'])}</td><td>{escape(h['matched_name'])}</td><td>{h['score']:.2f}</td>"
            f"<td>{escape(h['ref'] or h['id'])} · {escape(h['programme'])}</td>"
            f"<td>{escape(', '.join(h['birth']))}</td><td>{escape(', '.join(h['countries']))}</td></tr>"
            for h in screening["hits"])
        hits_html = ("<h3>Πιθανές ταυτίσεις — ΑΠΑΙΤΕΙΤΑΙ ΕΛΕΓΧΟΣ</h3><table border='1' cellpadding='4' cellspacing='0'>"
                     "<tr><th>Πρόσωπο</th><th>Όνομα στη λίστα</th><th>Βαθμός</th><th>Αναφορά/Πρόγραμμα</th>"
                     "<th>Γέννηση</th><th>Χώρες</th></tr>" + hits + "</table>"
                     "<p>Ταυτοποίηση μόνο με σύγκριση ημερομηνίας γέννησης, ιθαγένειας και αριθμού εγγράφου. Σε "
                     "επιβεβαίωση: δέσμευση/μη εκτέλεση συναλλαγής και ενημέρωση της Αρχής (άρθρο 22 ν. 4557/2018)· "
                     "απαγόρευση γνωστοποίησης στον πελάτη (άρθρο 27).</p>")
    else:
        hits_html = "<p><b>Αποτέλεσμα: καμία ταύτιση.</b></p>"
    note = f"<p><b>Σημείωση ελέγχου:</b> {escape(screening['review_note'])}</p>" if screening.get("review_note") else ""
    return (f"<h1>Έκθεση ελέγχου σε λίστες κυρώσεων</h1><p>Πελάτης: <b>{escape(b['name'] if b else '')}</b> "
            f"(ΑΦΜ {escape(afm)})<br>Ημερομηνία ελέγχου: {escape(when)} UTC<br>Πηγή: {escape(screening['source'])}</p>"
            "<h3>Πρόσωπα που ελέγχθηκαν</h3><table border='1' cellpadding='4' cellspacing='0'>"
            "<tr><th>Ρόλος</th><th>Όνομα</th><th>Λατινικά (ΕΛΟΤ 743)</th></tr>" + rows + "</table>" + hits_html + note +
            "<p style='font-size:8pt;color:#666'>Αυτόματη σύγκριση ονομάτων με ανοχή σε παραλλαγές λατινοποίησης· "
            "δεν αντικαθιστά την κρίση του υπόχρεου. Η λίστα της ΕΕ περιλαμβάνει και τις κυρώσεις του ΣΑ του ΟΗΕ.</p>")
