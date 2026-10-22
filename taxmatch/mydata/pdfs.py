"""PDF τιμολογίων από τον πάροχο (downloadingInvoiceUrl) — port του `timologio/download/provider.py` + `storage.py`.

ΑΣΦΑΛΕΙΑ: Session ΧΩΡΙΣ auth headers — τα downloadingInvoiceUrl είναι capability URLs και δεν θέλουν το κλειδί
myDATA· διαρροή του σε πάροχο είναι αδύνατη γιατί αυτό το Session δεν το γνωρίζει.

Γνωστές ιδιαιτερότητες (μετρημένες στο timologio):
* χωρίς `/pdf` στο τέλος, Epsilon/Impact/Megasoft δίνουν σελίδα προβολής (HTML), όχι PDF·
* Epsilon: άμεσο `/filedocument/getfile?fileType=2&documentId=<uuid>`· 404 = ο tenant δεν έχει server PDF·
* eskap.gr: ΚΑΝΕΝΑ αρχείο PDF — μόνο εκτυπώσιμη σελίδα («μόνο online»).
Όσα δεν δίνουν PDF μένουν «μόνο online» με κουμπί ανοίγματος στον πάροχο (το timologio τα αποδίδει σε αόρατο
browser — δεν μεταφέρθηκε εδώ).
"""
from __future__ import annotations

import io
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

from .. import config

PDF_MAGIC = b"%PDF"
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

STATUS_LABEL = {"ok": "PDF", "viewer": "Μόνο online", "no_url": "Χωρίς σύνδεσμο", "failed": "Αποτυχία", "": "—"}


def _host(netloc: str, host: str) -> bool:
    netloc = (netloc or "").lower()
    return netloc == host or netloc.endswith("." + host)


def pdf_url(url: str) -> str:
    trimmed = url.rstrip("/")
    return trimmed if trimmed.endswith(("/pdf", "/myDATA", "/EN16931")) else trimmed + "/pdf"


def epsilon_pdf_url(url: str) -> Optional[str]:
    p = urlparse(url)
    if not _host(p.netloc, "epsilonnet.gr"):
        return None
    docid = None
    if "/DocViewer/" in p.path:
        docid = p.path.split("/DocViewer/")[-1].strip("/") or None
    else:
        m = re.search(r"/fd/([^/?#]+)", p.path, re.I)
        if m:
            hx = re.sub(r"[^0-9a-fA-F]", "", m.group(1).split(":")[0])
            if len(hx) == 32:
                docid = f"{hx[0:8]}-{hx[8:12]}-{hx[12:16]}-{hx[16:20]}-{hx[20:32]}"
    return f"{p.scheme}://{p.netloc}/filedocument/getfile?fileType=2&documentId={docid}" if docid else None


class NotAPdf(Exception):
    """Ο πάροχος δίνει μόνο σελίδα προβολής — δεν είναι σφάλμα."""


def fetch_pdf(url: str, session: Optional[requests.Session] = None, timeout: float = 45.0) -> bytes:
    if _host(urlparse(url).netloc, "eskap.gr"):
        raise NotAPdf("eskap: μόνο εκτυπώσιμη σελίδα")
    eps = epsilon_pdf_url(url)
    target = eps or pdf_url(url)
    sess = session or requests.Session()
    resp = sess.get(target, timeout=timeout, allow_redirects=True,
                    headers={"Accept": "application/pdf,*/*", "User-Agent": _BROWSER_UA})
    if resp.status_code in (404, 410) and eps:
        raise NotAPdf("Epsilon: χωρίς server PDF")
    resp.raise_for_status()
    if not resp.content.startswith(PDF_MAGIC):        # τα magic bytes δεν λένε ψέματα, το Content-Type ναι
        raise NotAPdf("ο πάροχος επέστρεψε σελίδα, όχι PDF")
    return resp.content


def sanitize(part: str, fallback: str = "_") -> str:
    cleaned = _INVALID.sub("_", (part or "").strip()).rstrip(". ")
    return cleaned[:40] or fallback


def month_dir(afm: str, ym: str) -> Path:
    return config.data_dir() / "invoices" / afm / ym


def other_party(doc: dict, afm: str = "") -> tuple[str, str]:
    """(επωνυμία, ΑΦΜ) του «άλλου». Όχι κατά κατεύθυνση: στα 14.x (ενδοκοινοτικές λήψεις) τα διαβιβάζει ο ίδιος ο
    πελάτης ως λήπτης, άρα ο άλλος είναι ο ΕΚΔΟΤΗΣ παρότι είναι «εκδοθέντα/διαβιβασμένα»."""
    if doc["issuer_vat"] and doc["issuer_vat"] != afm and (afm or doc["direction"] == "incoming"):
        return doc["issuer_name"], doc["issuer_vat"]
    if doc["counter_vat"] and doc["counter_vat"] != afm:
        return doc["counter_name"], doc["counter_vat"]
    return doc["issuer_name"] or doc["counter_name"], doc["issuer_vat"] or doc["counter_vat"]


def file_name(doc: dict) -> str:
    other_name, other_vat = other_party(doc, doc.get("afm", ""))
    value = f"{doc['total']:.2f}".replace(".", ",")
    parts = [sanitize(other_name, "ΧΩΡΙΣ ΕΠΩΝΥΜΙΑ"), other_vat or "-", doc["issue_date"], sanitize(doc["series"], "-"),
             sanitize(doc["aa"], "-"), value]
    return "_".join(parts) + ".pdf"


def download_month(conn: sqlite3.Connection, afm: str, ym: str, on_each: Optional[Callable[[int, int], None]] = None,
                   fetch: Callable[[str], bytes] = None,
                   should_cancel: Optional[Callable[[], bool]] = None) -> dict[str, int]:
    """PDF όλων των παραστατικών του μήνα ('2026-03') — ή όλης της χρήσης ('2026') — που δεν έχουν ήδη κατέβει.
    Επιστρέφει πλήθη ανά κατάσταση. Τα «μόνο online» (ο πάροχος δεν δίνει αρχείο) δεν ξαναζητούνται."""
    session = requests.Session()
    fetch = fetch or (lambda u: fetch_pdf(u, session))
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM mydata_docs WHERE afm=? AND substr(issue_date,1,?)=? ORDER BY issue_date, mark", (afm, len(ym), ym))]
    counts = {"ok": 0, "viewer": 0, "no_url": 0, "failed": 0}
    try:
        for i, doc in enumerate(rows, 1):
            if should_cancel and should_cancel():
                break
            folder = month_dir(afm, doc["issue_date"][:7])
            status, path = doc["pdf_status"], doc["pdf_path"]
            if status == "ok" and path and Path(path).exists():
                counts["ok"] += 1
            elif status == "viewer" and doc["url"]:
                counts["viewer"] += 1
            elif not doc["url"]:
                status, path = "no_url", ""
                counts["no_url"] += 1
            else:
                try:
                    payload = fetch(doc["url"])
                    folder.mkdir(parents=True, exist_ok=True)
                    target = folder / file_name(doc)
                    if target.exists() and target.read_bytes() != payload:
                        target = target.with_name(target.stem + f"_{doc['mark']}.pdf")   # ποτέ αντικατάσταση άλλου MARK
                    target.write_bytes(payload)
                    status, path = "ok", str(target)
                    counts["ok"] += 1
                except NotAPdf:
                    status, path = "viewer", ""
                    counts["viewer"] += 1
                except (requests.RequestException, OSError):
                    status, path = "failed", ""
                    counts["failed"] += 1
            conn.execute("UPDATE mydata_docs SET pdf_status=?, pdf_path=? WHERE afm=? AND mark=?",
                         (status, path, afm, doc["mark"]))
            if on_each:
                on_each(i, len(rows))
    finally:
        session.close()
    return counts


def zip_month(conn: sqlite3.Connection, afm: str, ym: str, marks: Optional[list[str]] = None) -> bytes:
    """ZIP με τα PDF του μήνα ή της χρήσης (ή μόνο των επιλεγμένων MARK), σε φακέλους «Λήψεις» / «Εκδοθέντα»."""
    sql = "SELECT direction, pdf_path FROM mydata_docs WHERE afm=? AND substr(issue_date,1,?)=? AND pdf_status='ok'"
    args: list = [afm, len(ym), ym]
    if marks:
        sql += f" AND mark IN ({','.join('?' * len(marks))})"
        args += marks
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in conn.execute(sql, args):
            p = Path(r["pdf_path"])
            if p.exists():
                z.write(p, f"{'Λήψεις' if r['direction'] == 'incoming' else 'Εκδοθέντα'}/{p.name}")
    return buf.getvalue()
