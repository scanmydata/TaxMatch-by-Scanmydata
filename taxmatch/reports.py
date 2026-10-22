"""Έντυπα του γραφείου για οφειλές: δοσολόγια ΑΑΔΕ (σελίδα πίσω από την Ταυτότητα Οφειλής), καρτέλα οφειλέτη ΚΕΑΟ
ανά φορέα και έντυπο ανά ενεργή ρύθμιση — port της δομής `Report` του ScanMyData Tax Center (`doc/Report`,
`aade/DebtSchedule.kt`, `keao/KeaoCard.kt`): ταυτότητα υπόχρεου, ταυτότητα πληρωμής, σύνοψη, πίνακες με σύνολα, υποσέλιδο
που λέει ότι είναι ενημερωτικό έντυπο του γραφείου και ΟΧΙ έγγραφο της ΑΑΔΕ/ΚΕΑΟ.

PDF με QTextDocument + QPdfWriter (όπως τα έγγραφα της Δέουσας επιμέλειας) και προσάρτηση σελίδων με pypdf. Χρειάζεται
QGuiApplication — `ensure_qt()` το δημιουργεί και στον headless καθημερινό έλεγχο (ΠΡΟΣΟΧΗ: με `offscreen` τα ελληνικά
βγαίνουν «τετράγωνα»· εκτός tests χρησιμοποιείται η κανονική πλατφόρμα των Windows, χωρίς παράθυρο).
"""
from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MONEY = re.compile(r"-?[\d.]+,\d{2}")
_QT_APP = None


def ensure_qt() -> None:
    global _QT_APP
    from PySide6.QtCore import QCoreApplication
    if QCoreApplication.instance() is None:
        # QApplication (όχι σκέτο QGuiApplication): αν ανοίξει μετά παράθυρο στην ίδια διεργασία, χρειάζεται widgets.
        from PySide6.QtWidgets import QApplication
        _QT_APP = QApplication([])


def amount(raw: str) -> float:
    m = MONEY.search(str(raw or ""))
    return float(m.group(0).replace(".", "").replace(",", ".")) if m else 0.0


def money(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]]
    totals: list[str] = field(default_factory=list)
    numeric: list[int] = field(default_factory=list)


@dataclass
class Section:
    caption: str
    table: Table
    note: str = ""


@dataclass
class Report:
    title: str
    office: str = ""
    identity: list[tuple[str, str]] = field(default_factory=list)
    debtor_id: str = ""
    debtor_label: str = "Ταυτότητα Οφειλέτη"
    summary: list[tuple[str, str]] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    footer: list[str] = field(default_factory=list)


def table_from(headers: list[str], rows: list[list[str]]) -> Table:
    """Πίνακας της πύλης όπως ήρθε· ποσά = στήλες με «-1.234,56» κάπου (όχι ημερομηνίες/αριθμοί δόσεων), με σύνολα."""
    width = max([len(headers)] + [len(r) for r in rows]) if rows else len(headers)
    headers = headers + [""] * (width - len(headers))
    rows = [r + [""] * (width - len(r)) for r in rows]
    numeric = [c for c in range(width) if any(MONEY.search(r[c]) for r in rows)]
    totals = ["ΣΥΝΟΛΑ" if c == 0 else (money(sum(amount(r[c]) for r in rows)) if c in numeric else "")
              for c in range(width)] if numeric else []
    return Table(headers, rows, totals, numeric)


def render_html(r: Report) -> str:
    e = html.escape
    pairs = lambda items: "".join(  # noqa: E731
        f"<tr><td style='color:#41546b;padding:2px 14px 2px 0'>{e(k)}</td><td style='padding:2px 0'><b>{e(v)}</b></td></tr>"
        for k, v in items if v)
    parts = [f"<p style='color:#41546b;font-size:9pt'>{e(r.office)}</p>" if r.office else "",
             f"<h2 style='margin:4px 0 10px 0'>{e(r.title)}</h2>",
             f"<table>{pairs(r.identity)}</table>"]
    if r.debtor_id:
        parts.append(f"<p style='margin:10px 0;padding:8px;border:1px solid #2E7DE0;background:#F2F6FC'>"
                     f"{e(r.debtor_label)}: <b style='font-size:12pt'>{e(r.debtor_id)}</b></p>")
    if r.summary:
        parts.append(f"<table style='margin-top:6px'>{pairs(r.summary)}</table>")
    for s in r.sections:
        t = s.table
        head = "".join(f"<th align='{'right' if i in t.numeric else 'left'}' style='padding:3px 6px;border-bottom:1px "
                       f"solid #888'>{e(h)}</th>" for i, h in enumerate(t.headers))
        body = "".join("<tr>" + "".join(f"<td align='{'right' if i in t.numeric else 'left'}' style='padding:2px 6px'>"
                                        f"{e(c)}</td>" for i, c in enumerate(row)) + "</tr>" for row in t.rows)
        tot = ("<tr>" + "".join(f"<td align='{'right' if i in t.numeric else 'left'}' style='padding:3px 6px;border-top:"
                                f"1px solid #888'><b>{e(c)}</b></td>" for i, c in enumerate(t.totals)) + "</tr>") if t.totals else ""
        parts.append(f"<h3 style='margin:14px 0 4px 0'>{e(s.caption)}</h3><table width='100%' cellspacing='0'>"
                     f"<tr>{head}</tr>{body}{tot}</table>")
        if s.note:
            parts.append(f"<p style='color:#41546b;font-size:8pt'>{e(s.note)}</p>")
    if r.footer:
        parts.append("<hr><p style='color:#41546b;font-size:8pt'>" + "<br>".join(e(f) for f in r.footer) + "</p>")
    return "<div style='font-family:Segoe UI,Arial,sans-serif;font-size:9.5pt'>" + "".join(parts) + "</div>"


def write(report: Report, target: Path) -> Path:
    from .aml.documents import write_pdf
    ensure_qt()
    return write_pdf(render_html(report), Path(target), report.title)


def append_pages(base: Path, extra: Path) -> None:
    """Προσθέτει τις σελίδες του `extra` στο τέλος του `base` (το δοσολόγιο πίσω από την Ταυτότητα Οφειλής)."""
    from pypdf import PdfReader, PdfWriter
    out = PdfWriter()
    for path in (base, extra):
        for page in PdfReader(str(path)).pages:
            out.add_page(page)
    buf = io.BytesIO()
    out.write(buf)
    Path(base).write_bytes(buf.getvalue())


def office_name() -> str:
    try:
        from . import db, settings_store
        conn = db.connect()
        try:
            return settings_store.get(conn, "office_name")
        finally:
            conn.close()
    except Exception:                                   # noqa: BLE001
        return ""


def unpaid_schedule(headers: list[str], rows: list[list[str]]) -> tuple[list[tuple[str, str, str]], int]:
    """(απλήρωτες δόσεις [(α/α, λήξη, ποσό)], πλήθος όλων) — port του `Debts.schedule` του Tax Center: ημερομηνία = η
    πρώτη στήλη που μοιάζει με ημερομηνία, ποσό = η ΤΕΛΕΥΤΑΙΑ στήλη με ποσά (υπόλοιπο δόσης με προσαυξήσεις)·
    απλήρωτη = εκεί > 0. Οι στήλες κρίνονται από το περιεχόμενο, όχι από τα ονόματα."""
    if not rows:
        return [], 0
    width = max(len(r) for r in rows)
    date_col = next((c for c in range(width) if any(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", (r[c] if c < len(r) else "").strip())
                                                    for r in rows)), None)
    amount_col = next((c for c in reversed(range(width)) if any(MONEY.search(r[c] if c < len(r) else "") for r in rows)), None)
    if date_col is None or amount_col is None:
        return [], len(rows)
    unpaid = [((r[0] if r else ""), r[date_col], MONEY.search(r[amount_col]).group(0))
              for r in rows if amount_col < len(r) and amount(r[amount_col]) > 0.004]
    return unpaid, len(rows)


def next_due(unpaid: list[tuple[str, str, str]]) -> tuple[str, float]:
    """Η δόση που χρωστιέται ΤΩΡΑ: η παλαιότερη απλήρωτη (όχι «η επόμενη χρονικά» — βλ. Tax Center `Line.next`)."""
    if not unpaid:
        return "", 0.0
    return unpaid[0][1], amount(unpaid[0][2])


def safe(s: str, n: int = 60) -> str:
    return re.sub(r"[^0-9A-Za-zΑ-Ωα-ωάέήίόύώϊϋΐΰ.,_-]+", "_", str(s or "")).strip("_")[:n]


def fallback_path(folder: Path, name: str, used: Optional[set] = None) -> Path:
    path = folder / name
    if used is not None:
        n = 2
        while str(path) in used:
            path = folder / f"{Path(name).stem}_{n}.pdf"
            n += 1
        used.add(str(path))
    return path
