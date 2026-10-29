"""«Τα Αιτήματά μου» της ΑΑΔΕ ανά πελάτη: λίστα, τι άλλαξε από την τελευταία φορά, πλήρες αίτημα με τα συνημμένα του.

Πιστό port του ScanMyData Tax Center (ρητό αίτημα χρήστη 2026-10-22):
* `configs/easynotify.js` (which=REQUESTS == Hyper.Server `GetAadeRequests`): `GET saadekef/eticketaade/api/amsmsg/getUser`
  (στήνει τη συνεδρία) → `POST …/filterMessages` (JSON, pageIndex από 0, 10/σελίδα, ταξινόμηση submittedDate desc).
* `configs/aade-request.js`: `POST …/fetch` (σώμα = ο messageId ως γυμνός αριθμός) → πλήρες αίτημα + `messageFiles`·
  `GET …/getMessageComm/<id>` (αλληλογραφία)· `GET …/downloadFile/<fileId>` (attachedFile = ό,τι υπέβαλε ο πολίτης,
  certificateFile = εκπροσώπηση, internalFile = τα ΑΠΑΝΤΗΤΙΚΑ της υπηρεσίας).
* `requests/AadeRequests.kt`: «απαντήθηκε» από την κατάσταση Ή το κείμενο απάντησης· αλλαγές = νέα απάντηση / σε
  επεξεργασία / σε εκκρεμότητα από τον συναλλασσόμενο / αλλαγή κατάστασης — και ΤΙΠΟΤΑ «νέο» στην πρώτη λήψη ενός
  πελάτη (δέκα παλιά απαντημένα αιτήματα δεν είναι δέκα ειδοποιήσεις).

**ΜΟΝΟ ανάγνωση. Δεν έχει δοκιμαστεί ζωντανά από το TaxMatch** (tests με ψεύτικο API της δομής του Tax Center).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, db
from .business_profiles import credentials, lookup_aade
from .textutil import strip_accents

log = logging.getLogger(__name__)
API = lookup_aade.AADE + "/saadekef/eticketaade/api/amsmsg"
CHANGE_LABEL = {"answered": "Απαντήθηκε", "processing": "Σε επεξεργασία", "waiting_client": "Σε εκκρεμότητα από τον "
                "συναλλασσόμενο — χρειάζεται ενέργεια", "status": "Άλλαξε κατάσταση"}
ORIGIN_LABEL = {"attachedFile": "Υποβλήθηκε", "certificateFile": "Εκπροσώπηση", "internalFile": "Απάντηση ΑΑΔΕ",
                "commFile": "Αλληλογραφία"}


def fold(s: str) -> str:
    return strip_accents(str(s or "")).strip().lower()


def _json(text: str) -> Any:
    try:
        return json.loads(text or "")
    except ValueError:
        return None


def _post_json(http, url: str, body: str) -> dict[str, Any]:
    """POST με σώμα JSON (το `http.api` του runner) — `_HyperHttp.post_raw`."""
    return http.post_raw(url, body)


def list_requests(http, vat: str = "") -> list[dict[str, Any]]:
    """Όλα τα αιτήματα (όλες οι σελίδες) — εγγραφές του filterMessages αυτούσιες."""
    user = _json(http.follow("GET", API + "/getUser")["text"]) or {}
    vat = vat or str((user.get("userData") or {}).get("userVat") or "")
    out, seen, page, pages = [], set(), 1, 1
    while page <= pages and page <= 100:
        body = json.dumps({"pageIndex": page - 1, "pageSize": 10, "startDate": "", "endDate": "", "representativeRole": 0,
                           "transactorVat": vat, "taxeeVat": "", "caseNumber": "", "messageStatus": 0,
                           "sortBy": "submittedDate", "sortByDesc": True}, separators=(",", ":"))
        data = _json(_post_json(http, API + "/filterMessages", body)["text"]) or {}
        pages = int(data.get("pageCount") or pages)
        for m in data.get("entityModels") or []:
            if m.get("messageId") not in seen:
                seen.add(m.get("messageId"))
                out.append(m)
        page += 1
    return out


def normalize(m: dict[str, Any]) -> dict[str, Any]:
    t = lambda k: "" if m.get(k) is None else str(m.get(k)).strip()  # noqa: E731
    r = {"id": t("messageId"), "case_no": t("caseNumber"), "submitted": t("submittedDate"), "updated": t("updatedDate"),
         "status": t("messageStatusText") or t("messageStatus"),
         "topic": t("thematicalGroupText") or t("processGroupText"), "process": t("diadikasiaText"),
         "service": " · ".join(dict.fromkeys(x for x in (t("orgGroupText"), t("protipoTmimaText")) if x)),
         "text": t("messageText"), "answer": t("answerText"), "rejected": bool(m.get("rejected"))}
    r["stamp"] = stamp(r)
    return r


def answered(r: dict[str, Any]) -> bool:
    return bool(r["answer"]) or fold(r["status"]).startswith("απαντ")


def stage(status: str) -> str:
    f = fold(status)
    if "εκκρεμ" in f and "συναλλασσ" in f:
        return "waiting_client"
    if "επεξεργασ" in f:
        return "processing"
    return ""


def stamp(r: dict[str, Any]) -> str:
    """Κατάσταση, απόρριψη και ΑΠΟΤΥΠΩΜΑ της απάντησης (όχι το κείμενο) — όπως `Request.stamp` του Tax Center."""
    ans = hashlib.sha256(r["answer"].strip().encode()).hexdigest()[:12] if r["answer"].strip() else ""
    return "|".join((fold(r["status"]), "R" if r["rejected"] else "", ans))


def _answer_of(s: str) -> str:
    parts = s.split("|")
    if len(parts) > 2 and parts[2]:
        return parts[2]
    return "A" if parts and parts[0].startswith("απαντ") else ""


def diff(known: Optional[dict[str, str]], now: list[dict[str, Any]]) -> dict[str, str]:
    """id → είδος αλλαγής (port του `AadeRequests.diff`). `known` None = πρώτη λήψη ⇒ τίποτα νέο."""
    if known is None:
        return {}
    out = {}
    for r in now:
        before = known.get(r["id"])
        if before == r["stamp"]:
            continue
        if answered(r) and (before is None or _answer_of(before) != _answer_of(r["stamp"])):
            out[r["id"]] = "answered"
        elif stage(r["status"]) and (before is None or before.split("|")[0] != fold(r["status"])):
            out[r["id"]] = stage(r["status"])
        elif before is not None:
            out[r["id"]] = "status"
    return out


def refresh(conn: sqlite3.Connection, afm: str,
            login_fn: Callable[[str, str], dict] = lookup_aade.aade_login) -> dict[str, Any]:
    """Λίστα αιτημάτων του πελάτη → `aade_requests` με σήμανση αλλαγών (αθέατες μέχρι να τις δει ο χρήστης).
    {ok, reason, n, changes: {id: είδος}}."""
    creds = credentials.get(conn, afm)
    if not creds:
        return {"ok": False, "reason": "Δεν υπάρχουν κωδικοί TAXISnet του πελάτη.", "n": 0, "changes": {}}
    login = login_fn(*creds)
    if not login.get("ok"):
        reason = lookup_aade.REASONS_EL.get(login.get("reason") or "", "Αποτυχία σύνδεσης TAXISnet.")
        _sync(conn, afm, "error", reason)
        return {"ok": False, "reason": reason, "n": 0, "changes": {}}
    try:
        items = [normalize(m) for m in list_requests(login["http"], afm)]
    except Exception as exc:                            # noqa: BLE001 — «δεν διαβάστηκε» ≠ «κανένα αίτημα»
        _sync(conn, afm, "error", str(exc)[:160])
        return {"ok": False, "reason": f"Τα αιτήματα δεν διαβάστηκαν ({str(exc)[:80]}).", "n": 0, "changes": {}}
    first = conn.execute("SELECT 1 FROM request_sync WHERE afm=? AND status='ok'", (afm,)).fetchone() is None
    known = None if first else {r["id"]: r["stamp"] for r in conn.execute(
        "SELECT id, stamp FROM aade_requests WHERE afm=?", (afm,))}
    changes = diff(known, items)
    now = db.utcnow()
    for r in items:
        ch = changes.get(r["id"], "")
        conn.execute(
            "INSERT INTO aade_requests(afm, id, case_no, submitted, updated, status, topic, process, service, text, "
            "answer, rejected, stamp, change, seen, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(afm, id) DO UPDATE SET case_no=excluded.case_no, submitted=excluded.submitted, "
            "updated=excluded.updated, status=excluded.status, topic=excluded.topic, process=excluded.process, "
            "service=excluded.service, text=excluded.text, answer=excluded.answer, rejected=excluded.rejected, "
            "stamp=excluded.stamp, fetched_at=excluded.fetched_at, "
            "change=CASE WHEN excluded.change<>'' THEN excluded.change ELSE aade_requests.change END, "
            "seen=CASE WHEN excluded.change<>'' THEN 0 ELSE aade_requests.seen END",
            (afm, r["id"], r["case_no"], r["submitted"], r["updated"], r["status"], r["topic"], r["process"],
             r["service"], r["text"], r["answer"], int(r["rejected"]), r["stamp"], ch, 0 if ch else 1, now))
    _sync(conn, afm, "ok", "")
    return {"ok": True, "reason": "", "n": len(items), "changes": changes}


def _sync(conn: sqlite3.Connection, afm: str, status: str, message: str) -> None:
    conn.execute("INSERT INTO request_sync(afm, synced_at, status, message) VALUES (?,?,?,?) ON CONFLICT(afm) DO UPDATE "
                 "SET synced_at=excluded.synced_at, status=excluded.status, message=excluded.message",
                 (afm, db.utcnow(), status, message))


def files_of(node: Any, origin: str = "", out: Optional[list] = None, depth: int = 0) -> list[dict[str, Any]]:
    """Κάθε αντικείμενο με fileId + fileName, σε οποιοδήποτε βάθος (όπως `filesOf` του aade-request.js)."""
    out = [] if out is None else out
    if depth > 6 or node is None:
        return out
    if isinstance(node, list):
        for x in node:
            files_of(x, origin, out, depth + 1)
    elif isinstance(node, dict):
        if node.get("fileId") is not None and node.get("fileName") is not None:
            if not any(str(f["fileId"]) == str(node["fileId"]) for f in out):
                out.append({"fileId": node["fileId"], "fileName": str(node["fileName"]),
                            "fileOrigin": node.get("fileOrigin") or origin, "fileSize": node.get("fileSize")})
        else:
            for v in node.values():
                files_of(v, origin, out, depth + 1)
    return out


def safe_name(name: str, ctype: str = "") -> str:
    s = re.sub(r"\s+", "_", re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(name or "").strip()))
    s = re.sub(r"_+", "_", s).strip("_.")
    m = re.search(r"\.([A-Za-z0-9]{1,5})$", s)
    ext = m.group(1).lower() if m else ("pdf" if "pdf" in ctype else "jpg" if "jpeg" in ctype or "jpg" in ctype
                                        else "png" if "png" in ctype else "")
    if m:
        s = s[:m.start()]
    s = s[:60].strip("_.") or "arxeio"
    return f"{s}.{ext}" if ext else s


def requests_dir(afm: str, request_id: str) -> Path:
    return config.data_dir() / "requests" / afm / str(request_id)


def open_request(conn: sqlite3.Connection, afm: str, request_id: str, with_files: bool = True,
                 login_fn: Callable[[str, str], dict] = lookup_aade.aade_login) -> dict[str, Any]:
    """Πλήρες αίτημα + αλληλογραφία + συνημμένα (υποβολής ΚΑΙ απάντησης) σε `<data>/requests/<ΑΦΜ>/<id>/`."""
    creds = credentials.get(conn, afm)
    if not creds:
        return {"ok": False, "reason": "Δεν υπάρχουν κωδικοί TAXISnet του πελάτη."}
    login = login_fn(*creds)
    if not login.get("ok"):
        return {"ok": False, "reason": lookup_aade.REASONS_EL.get(login.get("reason") or "", "Αποτυχία σύνδεσης TAXISnet.")}
    http = login["http"]
    http.follow("GET", API + "/getUser")
    r = _post_json(http, API + "/fetch", str(int(request_id)))
    detail = _json(r["text"])
    if r["status"] != 200 or not isinstance(detail, dict):
        return {"ok": False, "reason": "Το αίτημα δεν βρέθηκε στην ΑΑΔΕ."}
    comm = _json(http.follow("GET", f"{API}/getMessageComm/{request_id}")["text"]) or []
    files = files_of(detail.get("messageFiles")) + [f for f in files_of(comm, "commFile")
                                                    if not any(str(f["fileId"]) == str(x["fileId"])
                                                               for x in files_of(detail.get("messageFiles")))]
    folder = requests_dir(afm, request_id)
    for f in files:
        if not with_files:
            continue
        have = next(iter(folder.glob(f"{f['fileId']}_*")), None) if folder.exists() else None
        if have is not None:                            # κατέβηκε ήδη — τα συνημμένα δεν αλλάζουν
            f["path"] = str(have)
            continue
        got = http.follow("GET", f"{API}/downloadFile/{f['fileId']}")
        content = got.get("content") or b""
        if got.get("status", 200) != 200 or not content:
            f["error"] = f"HTTP {got.get('status')}"
            continue
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{f['fileId']}_{safe_name(f['fileName'], got.get('ct', ''))}"
        path.write_bytes(content)
        f["path"] = str(path)
    conn.execute("UPDATE aade_requests SET files_json=?, seen=1 WHERE afm=? AND id=?",
                 (json.dumps(files, ensure_ascii=False), afm, str(request_id)))
    return {"ok": True, "reason": "", "detail": detail, "comm": comm, "files": files}


def client_requests(conn: sqlite3.Connection, afm: str) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM aade_requests WHERE afm=? ORDER BY coalesce(nullif(updated,''), "
                                          "submitted) DESC", (afm,))]
    for r in rows:
        r["opened"] = bool(r["files_json"])             # '' = δεν άνοιξε ποτέ· '[]' = άνοιξε, χωρίς συνημμένα
        r["files"] = json.loads(r["files_json"] or "[]")
    return rows


def mark_seen(conn: sqlite3.Connection, afm: str, request_id: Optional[str] = None) -> None:
    if request_id:
        conn.execute("UPDATE aade_requests SET seen=1 WHERE afm=? AND id=?", (afm, request_id))
    else:
        conn.execute("UPDATE aade_requests SET seen=1 WHERE afm=?", (afm,))


def unseen(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Αλλαγές που δεν έχει δει ο χρήστης — για την ειδοποίηση στην κορυφή της εφαρμογής."""
    return [dict(r) for r in conn.execute("SELECT r.afm, r.id, r.change, r.process, r.topic, b.name FROM aade_requests r "
                                          "LEFT JOIN businesses b ON b.afm=r.afm WHERE r.seen=0 ORDER BY r.afm")]


def waiting_on_client(conn: sqlite3.Connection, afms: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Αιτήματα «Σε εκκρεμότητα από τον συναλλασσόμενο» (η ΑΑΔΕ περιμένει ενέργεια του πελάτη) — είτε τα έχει δει ο
    χρήστης είτε όχι: μένουν στις ειδοποιήσεις ώσπου να αλλάξει η κατάσταση στην ΑΑΔΕ (ρητό αίτημα 2026-10-29)."""
    rows = [dict(r) for r in conn.execute(
        "SELECT r.afm, r.id, r.case_no, r.status, r.process, r.topic, r.updated, b.name FROM aade_requests r "
        "LEFT JOIN businesses b ON b.afm=r.afm ORDER BY r.afm, r.updated DESC")]
    return [r for r in rows if stage(r["status"]) == "waiting_client" and (afms is None or r["afm"] in afms)]


def date_text(raw: str) -> str:
    """«2026-09-30 12:34:56» → «30/09/2026 12:34»."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?", raw or "")
    if not m:
        return raw or ""
    y, mo, d, h, mi = m.groups()
    return f"{d}/{mo}/{y}" + (f" {h}:{mi}" if h else "")
