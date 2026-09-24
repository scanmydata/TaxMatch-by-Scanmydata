"""Ανάγνωση των PDF του Κεντρικού Μητρώου Πραγματικών Δικαιούχων (Κ.Μ.Π.Δ.) — «Εκτύπωση δικαιούχων» και
«Βεβαίωση υποβολής» — ώστε ο πίνακας δικαιούχων και τα στοιχεία εκπροσώπου του φακέλου να συμπληρώνονται αυτόματα.

Το PDF είναι ψηφιακά υπογεγραμμένο έγγραφο ΓΓΠΣ με πραγματικό κείμενο (όχι εικόνα)· διαβάζεται με `pypdf`
(`extraction_mode="layout"`: κρατά τις στήλες του πίνακα ΔΙΚΑΙΟΥΧΟΙ στις θέσεις της κεφαλίδας). Η δομή επαληθεύτηκε
σε πραγματικό PDF (2026-09-24): γραμμές «A/A» ιεραρχικές (1 = η ίδια η οντότητα, 1.1, 1.1.1 … = μέτοχοι/δικαιούχοι),
στήλες ΑΦΜ/VAT · ΦΠ/ΝΠ · ΟΝΟΜ/ΝΥΜΟ-ΕΠΩΝΥΜΙΑ · ΙΔΙΟΤΗΤΕΣ · ΤΙΤΛΟΣ · ΚΑΤΟΧΗ(%) · ΚΑΤΟΧΗ ΑΡΧΙΚΗΣ(%) · ΨΗΦΟΙ(%) · ΑΛΛΑ·
οι ιδιότητες μπορεί να σπάνε σε επόμενη γραμμή. Ανεκτικό: ό,τι δεν αναγνωρίζεται απλώς λείπει — ποτέ εφεύρεση.
"""
from __future__ import annotations

import io
import re
from typing import Any, Optional

_ROW = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+(\S+)\s+(Φ\.Π\.|Ν\.Π\.)\s")
_HEADER = re.compile(r"A/A\s+A?ΦΜ/VAT", re.I)
#: κεφαλίδες στηλών όπως τις γράφει το ΚΜΠΔ (λατινικό «A» στα «A/A», «AΦΜ»)
_COLUMNS = (("afm", r"A?ΦΜ/VAT"), ("type", r"ΦΠ/ΝΠ"), ("name", r"ΟΝΟΜ/ΝΥΜΟ-ΕΠΩΝΥΜΙΑ"), ("roles", r"ΙΔΙΟΤΗΤΕΣ"),
            ("title", r"ΤΙΤΛΟΣ"), ("percent", r"ΚΑΤΟΧΗ\(%\)"), ("percent_initial", r"ΚΑΤΟΧΗ(?!\()"),
            ("votes", r"ΨΗΦΟΙ\(%\)"), ("other", r"ΑΛΛΑ"))
_FOOTER = re.compile(r"Σελίδα\s*:|ΒΕΒΑΙΩΣΗ ΥΠΟΒΟΛΗΣ|ΚΕΝΤΡΙΚΟ ΜΗΤΡΩΟ|ΕΛΛΗΝΙΚΗ ΔΗΜΟΚΡΑΤΙΑ|ΥΠΟΥΡΓΕΙΟ")


class KmpdPdfError(ValueError):
    pass


def pdf_text(data: bytes, layout: bool = False) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:                                        # pragma: no cover — εξάρτηση του requirements.txt
        raise KmpdPdfError("λείπει η βιβλιοθήκη pypdf") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [p.extract_text(extraction_mode="layout") if layout else p.extract_text() for p in reader.pages]
    except Exception as exc:                                           # κατεστραμμένο/μη-PDF
        raise KmpdPdfError(f"το PDF δεν διαβάστηκε: {exc}") from exc
    return "\n".join(p or "" for p in pages)


def _field(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def _pct(value: str) -> str:
    m = re.search(r"\d+(?:[.,]\d+)?", value or "")
    if not m:
        return ""
    num = float(m.group(0).replace(",", "."))
    return f"{num:g}"


def parse_header(text: str) -> dict[str, Any]:
    """Κοινά πεδία «Δικαιούχων» και «Βεβαίωσης»: αριθμός καταχώρισης, ημερομηνίες, οντότητα, εκπρόσωπος."""
    entity = _field(text, r"ΣΤΟΙΧΕΙΑ ΝΟΜΙΚΗΣ ΟΝΤΟΤΗΤΑΣ(.*?)ΣΤΟΙΧΕΙΑ\s+ΕΚΠΡΟΣΩΠΟΥ") if "ΣΤΟΙΧΕΙΑ ΝΟΜΙΚΗΣ" in text else ""
    rep_block = _field(text, r"ΣΤΟΙΧΕΙΑ\s+ΕΚΠΡΟΣΩΠΟΥ(.*?)(?:ΔΙΚΑΙΟΥΧΟΙ|$)") if "ΕΚΠΡΟΣΩΠΟΥ" in text else ""
    out: dict[str, Any] = {
        "registration_no": _field(text, r"Αριθμός καταχώρισης\s*:\s*(\d+)"),
        "submitted": _field(text, r"Ημερομηνία αρχικής υποβολής\s*:[\s:]*(\d{2}/\d{2}/\d{4})"),
        "modified": _field(text, r"Ημερομηνία τελευταίας τροποποίησης\s*:\s*(\d{2}/\d{2}/\d{4})"),
        "printed": _field(text, r"Ημερομηνία εκτύπωσης\s*:\s*(\d{2}/\d{2}/\d{4})"),
        "entity_afm": _field(text, r"με ΑΦΜ\s*:\s*(\d{9})") or _field(entity, r"ΑΦΜ\s*:\s*(\d{9})"),
        "entity_name": _field(text, r"νομική οντότητα\s*:\s*(.+?)\s+με ΑΦΜ"),
        "rep": {},
    }
    if rep_block:
        address = " ".join(x for x in (_field(rep_block, r"Οδός\s*:\s*(.+?)\s+Αρ\.\s*:"),
                                        _field(rep_block, r"Αρ\.\s*:\s*(\S+)")) if x)
        tk, city = _field(rep_block, r"T\.?K\s*:\s*(\d{5})"), _field(rep_block, r"Πόλη\s*:\s*(.+?)\s+Περιοχή")
        if tk or city:
            address = ", ".join(x for x in (address, " ".join(x for x in (tk, city) if x)) if x)
        rep = {"afm": _field(rep_block, r"ΑΦΜ\s*:\s*(\d{9})"),
               "name": " ".join(x for x in (_field(rep_block, r"Επώνυμο\s*:\s*(.+?)\s+Όνομα"),
                                            _field(rep_block, r"Όνομα\s*:\s*(.+?)\s+Οδός")) if x),
               "address": address,
               "phone": _field(rep_block, r"[KΚ]ινητό\s*:\s*(\d{10})") or _field(rep_block, r"Τηλέφωνο\s*:\s*(\d{10})")}
        out["rep"] = {k: v for k, v in rep.items() if v}
    return out


def _column_starts(line: str) -> Optional[dict[str, int]]:
    starts = {}
    for key, pat in _COLUMNS:
        m = re.search(pat, line)
        if m:
            starts[key] = m.start()
    return starts if {"afm", "type", "name"} <= set(starts) else None


def _cut(line: str, pos: int) -> int:
    """Σημείο κοπής κοντά στη στήλη της κεφαλίδας — πάντα σε κενό, ώστε να μη σπάσει λέξη αν η στοίχιση απέχει 1–2 θέσεις."""
    if pos >= len(line):
        return len(line)
    for delta in range(0, 4):
        for p in (pos - delta, pos + delta):
            if 0 < p <= len(line) and line[p - 1] == " ":
                return p
    return pos


def _split(line: str, starts: dict[str, int]) -> dict[str, str]:
    keys = sorted(starts, key=starts.get)
    cuts = [_cut(line, starts[k]) for k in keys]
    out = {}
    for i, key in enumerate(keys):
        end = cuts[i + 1] if i + 1 < len(keys) else len(line)
        out[key] = line[cuts[i]:end].strip()
    return out


def parse_owners_text(layout_text: str) -> list[dict[str, str]]:
    """Γραμμές του πίνακα ΔΙΚΑΙΟΥΧΟΙ (κείμενο σε `layout` mode)."""
    rows: list[dict[str, str]] = []
    starts: Optional[dict[str, int]] = None
    in_table = False
    for line in layout_text.splitlines():
        if _HEADER.search(line):
            starts, in_table = _column_starts(line), True
            continue
        if not in_table or not starts or not line.strip():
            continue
        m = _ROW.match(line)
        if m:
            cells = _split(line, starts)
            name = re.sub(r"^[.\s…]+", "", cells.get("name", ""))           # «....» = εσοχή επιπέδου στο PDF
            rows.append({"aa": m.group(1), "afm": m.group(2), "type": "ΦΠ" if m.group(3) == "Φ.Π." else "ΝΠ",
                         "name": re.sub(r"\s+", " ", name), "roles": cells.get("roles", ""),
                         "title": cells.get("title", ""), "percent": _pct(cells.get("percent", "")),
                         "percent_initial": _pct(cells.get("percent_initial", "")), "votes": _pct(cells.get("votes", "")),
                         "other": cells.get("other", "")})
            continue
        if _FOOTER.search(line):
            in_table = False                                           # τέλος σελίδας· η επόμενη ξαναδίνει κεφαλίδα
            continue
        if rows and line.startswith(" " * 8):                           # συνέχεια κελιών της προηγούμενης γραμμής
            cells = _split(line, starts)
            for key in ("roles", "title", "name"):
                extra = cells.get(key, "")
                if extra and "(%)" not in extra:
                    rows[-1][key] = (rows[-1][key] + " " + extra).strip()
    for r in rows:
        for key in ("roles", "title"):
            r[key] = "" if r[key].strip("- ") == "" else re.sub(r"\s+", " ", r[key]).replace(" ,", ",")
    return rows


def parse_owners(data: bytes) -> dict[str, Any]:
    """«Εκτύπωση δικαιούχων» → {registration_no, submitted, modified, printed, entity_afm, entity_name, rep, owners}."""
    plain = pdf_text(data)
    if "ΔΙΚΑΙΟΥΧ" not in plain:
        raise KmpdPdfError("δεν μοιάζει με εκτύπωση του ΚΜΠΔ")
    out = parse_header(plain)
    out["owners"] = parse_owners_text(pdf_text(data, layout=True))
    return out


def parse_certificate(data: bytes) -> dict[str, Any]:
    """«Βεβαίωση υποβολής δήλωσης πραγματικών δικαιούχων» → αριθμός καταχώρισης/ημερομηνίες/οντότητα."""
    plain = pdf_text(data)
    if "ΠΡΑΓΜΑΤΙΚΩΝ ΔΙΚΑΙΟΥΧΩΝ" not in plain:
        raise KmpdPdfError("δεν μοιάζει με βεβαίωση του ΚΜΠΔ")
    out = parse_header(plain)
    out.pop("rep", None)
    return out


def to_ubos(parsed: dict[str, Any]) -> list[dict[str, str]]:
    """Δικαιούχοι του φακέλου = ΦΥΣΙΚΑ πρόσωπα του πίνακα. Τα ενδιάμεσα νομικά πρόσωπα (αλυσίδα) μπαίνουν ως «μέσω …»."""
    owners = parsed.get("owners") or []
    by_aa = {o["aa"]: o for o in owners}
    out = []
    for o in owners:
        if o["type"] != "ΦΠ":
            continue
        chain = []
        aa = o["aa"]
        while "." in aa:
            aa = aa.rsplit(".", 1)[0]
            parent = by_aa.get(aa)
            if parent and parent["type"] == "ΝΠ" and parent["afm"] != parsed.get("entity_afm"):
                chain.append(parent["name"])
        control = ", ".join(x for x in (o["roles"], o["title"]) if x)
        if o["votes"] and o["votes"] != o["percent"]:
            control += f" · ψήφοι {o['votes']}%"
        if chain:
            control += " · μέσω " + " → ".join(reversed(chain))
        out.append({"name": o["name"], "afm": o["afm"],
                    "percent": o["percent"], "control": control.strip(" ·")})
    return out


def merge_ubos(existing: list[dict[str, Any]], found: list[dict[str, str]]) -> tuple[list[dict[str, Any]], int, int]:
    """Συγχώνευση με ό,τι έχει ήδη ο φάκελος: ταίριασμα με ΑΦΜ (ή όνομα). Ενημερώνει ΜΟΝΟ ποσοστό/έλεγχο (αυτά που
    ξέρει το ΚΜΠΔ) — ΠΕΠ, ταυτότητα, χώρα κ.λπ. που έγραψε ο χρήστης δεν αγγίζονται. -> (λίστα, νέοι, ενημερωμένοι)."""
    rows = [dict(u) for u in existing]
    added = updated = 0
    for f in found:
        match = next((r for r in rows if f["afm"] and r.get("afm") == f["afm"]), None) or \
            next((r for r in rows if not r.get("afm") and r.get("name", "").strip().upper() == f["name"].upper()), None)
        if match is None:
            rows.append(dict(f))
            added += 1
            continue
        changed = False
        for key in ("afm", "name", "percent", "control"):
            if f.get(key) and match.get(key) != f[key] and (key in ("percent", "control") or not match.get(key)):
                match[key] = f[key]
                changed = True
        updated += changed
    return rows, added, updated
