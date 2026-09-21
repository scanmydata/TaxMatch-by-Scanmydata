"""Μαζική εισαγωγή πελατών από Excel/CSV: ΑΦΜ + προαιρετικά επωνυμία, ΚΑΔ και **κωδικούς TAXISnet**.

Αναγνωρίζονται δύο μορφές (όπως στο timologio downloader του ScanMyData):
* πίνακας με επικεφαλίδες (μία γραμμή ανά πελάτη)·
* μπλοκ ανά υπόχρεο — η εκτύπωση «Κωδικοί Υπηρεσιών μέσω Internet» των Hyper/Extra/TaxSystem, με γραμμή «Taxis Net»
  (χρήστης, συνθηματικό)· οι τιμές διαβάζονται ΘΕΣΙΑΚΑ ανάμεσα στα μη κενά κελιά, όχι από σταθερές στήλες.

Το ταίριασμα επικεφαλίδων είναι whole-string (ποτέ substring): μια στήλη «Κλειδί myDATA» δεν πρέπει ποτέ να
διαβαστεί ως κωδικός TAXISnet. Κωδικοί άλλων υπηρεσιών (myDATA, ΕΦΚΑ, ΓΕΜΗ) δεν διαβάζονται ποτέ.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..identifiers import is_valid_afm, normalize_afm
from ..textutil import strip_accents

FIELD_ALIASES: dict[str, set[str]] = {
    "afm": {"αφμ", "α φ μ", "afm", "vat", "vat number", "vatnumber", "tax id", "taxid", "αφμ υποχρεου"},
    "name": {"επωνυμια επωνυμο", "επωνυμια", "επωνυμο", "ονομασια", "name", "company", "πελατης", "επιχειρηση",
             "ονομα επιχειρησης", "onomasia"},
    "kad": {"καδ", "κ α δ", "kad", "καδ κυριο", "κυριος καδ", "καδ κυριος"},
    "taxis_user": {"ονομα χρηστη taxisnet", "χρηστης taxisnet", "taxisnet user", "taxisnet username", "taxis user",
                   "ονομα χρηστη taxis", "username taxisnet", "taxis net user", "ονομα χρηστη taxis net",
                   "taxisnet ονομα χρηστη", "χρηστης taxis"},
    "taxis_pass": {"συνθηματικο taxisnet", "κωδικος taxisnet", "taxisnet password", "taxis password",
                   "κωδικος προσβασης taxisnet", "συνθηματικο taxis", "κωδικος taxis", "taxis net password",
                   "κωδικος προσβασης taxis net", "taxisnet κωδικος"},
}
#: Γενικές στήλες: δεκτές ΜΟΝΟ σε απλό αρχείο χωρίς ρητές στήλες TAXISnet (ώστε να δουλεύει το πρότυπό μας
#: «ΑΦΜ | Επωνυμία | Όνομα χρήστη | Κωδικός»), όχι μέσα σε πλατύ πίνακα με δεκάδες κωδικούς διαφορετικών υπηρεσιών.
GENERIC_USER = {"ονομα χρηστη", "χρηστης", "username", "user"}
GENERIC_PASS = {"κωδικος", "κωδικος προσβασης", "συνθηματικο", "password"}
SIMPLE_SHEET_MAX_COLS = 8

BLOCK = re.compile(r"^(\d{3,6})\s+(.+?)\s+(\d{9})$")
_TAXIS_LABELS = ("taxis net", "taxisnet", "taxis")
_BLOCK_SPAN = 10
_HEADER_SCAN = 15

TEMPLATE_HEADERS = ["ΑΦΜ", "Επωνυμία", "ΚΑΔ", "Όνομα χρήστη TAXISnet", "Κωδικός TAXISnet"]


def norm_header(v: object) -> str:
    """Ελληνικά/λατινικά, χωρίς τόνους/σημεία στίξης, μικρά, μονά κενά: 'Α.Φ.Μ.' -> 'α φ μ'."""
    s = strip_accents(str(v or "")).lower()
    s = re.sub(r"[^0-9a-zα-ω]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_LOOKUP = {alias: f for f, aliases in FIELD_ALIASES.items() for alias in aliases}


@dataclass
class ImportRow:
    afm: str
    name: str = ""
    kads: list[str] = field(default_factory=list)
    checksum_ok: bool = True
    taxis_user: str = ""
    taxis_pass: str = ""

    @property
    def has_credentials(self) -> bool:
        return bool(self.taxis_user and self.taxis_pass)


@dataclass
class ImportResult:
    rows: list[ImportRow] = field(default_factory=list)
    invalid: list[tuple[int, str]] = field(default_factory=list)      # (γραμμή αρχείου, τιμή)
    duplicates: int = 0
    warnings: list[str] = field(default_factory=list)
    detected: str = ""                                                # 'table' | 'blocks'

    @property
    def credential_count(self) -> int:
        return sum(1 for r in self.rows if r.has_credentials)


def _split_kads(v: object) -> list[str]:
    return [p.strip() for p in re.split(r"[;,/\n]+", str(v or "")) if re.search(r"\d{2}", p)]


def _cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _find_header(rows: list[tuple]) -> Optional[tuple[int, dict[str, int]]]:
    for i, row in enumerate(rows[:_HEADER_SCAN]):
        heads = [norm_header(c) for c in row]
        mapping: dict[str, int] = {}
        for j, h in enumerate(heads):
            f = _LOOKUP.get(h)
            if f and f not in mapping:
                mapping[f] = j
        if "afm" not in mapping:
            continue
        if not ("taxis_user" in mapping or "taxis_pass" in mapping) and len([h for h in heads if h]) <= SIMPLE_SHEET_MAX_COLS:
            for j, h in enumerate(heads):
                if h in GENERIC_USER and "taxis_user" not in mapping:
                    mapping["taxis_user"] = j
                elif h in GENERIC_PASS and "taxis_pass" not in mapping:
                    mapping["taxis_pass"] = j
        return i, mapping
    return None


def _parse_blocks(rows: list[tuple]) -> Optional[ImportResult]:
    """Μπλοκ «κωδικός επωνυμία ΑΦΜ» + γραμμή «Taxis Net» με χρήστη/συνθηματικό."""
    texts = [" ".join(_cell(c) for c in row if _cell(c)) for row in rows]
    if not any(BLOCK.match(t) for t in texts[:120]):
        return None
    res = ImportResult(detected="blocks")
    seen: set[str] = set()
    i = 0
    while i < len(rows):
        m = BLOCK.match(texts[i])
        if not m:
            i += 1
            continue
        _code, name, raw_afm = m.groups()
        afm = normalize_afm(raw_afm)
        user = pwd = ""
        j = i + 1
        while j < min(i + _BLOCK_SPAN, len(rows)):
            if j > i + 1 and BLOCK.match(texts[j]):
                break
            low = strip_accents(texts[j]).lower()
            if any(lbl in low for lbl in _TAXIS_LABELS) and "mydata" not in low and "βιβλια" not in low:
                vals = [_cell(c) for c in rows[j] if _cell(c)]
                if len(vals) >= 3:
                    user, pwd = vals[1], vals[2]
            j += 1
        i = j
        if not afm:
            res.invalid.append((i, raw_afm))
            continue
        if afm in seen:
            res.duplicates += 1
            continue
        seen.add(afm)
        res.rows.append(ImportRow(afm=afm, name=" ".join(name.split()), checksum_ok=is_valid_afm(afm),
                                  taxis_user=user, taxis_pass=pwd))
    return res


def parse_rows(table: Iterable[tuple]) -> ImportResult:
    rows = [tuple(r) for r in table]
    found = _find_header(rows)
    if found is None:
        blocks = _parse_blocks(rows)
        if blocks is not None:
            _finish(blocks)
            return blocks
    result = ImportResult(detected="table")
    if found:
        start, mapping = found[0] + 1, found[1]
    else:
        start, mapping = 0, {"afm": 0, "name": 1}
        result.warnings.append("Δεν βρέθηκε επικεφαλίδα «ΑΦΜ» — χρησιμοποιήθηκε η πρώτη στήλη.")

    def get(row: tuple, key: str) -> str:
        idx = mapping.get(key)
        return _cell(row[idx]) if idx is not None and idx < len(row) else ""

    seen: set[str] = set()
    for offset, row in enumerate(rows[start:], start=start + 1):
        if not row or all(_cell(c) == "" for c in row):
            continue
        afm = normalize_afm(row[mapping["afm"]] if mapping["afm"] < len(row) else None)
        if not afm:
            result.invalid.append((offset, get(row, "afm")))
            continue
        if afm in seen:
            result.duplicates += 1
            continue
        seen.add(afm)
        result.rows.append(ImportRow(afm=afm, name=get(row, "name"), kads=_split_kads(get(row, "kad")),
                                     checksum_ok=is_valid_afm(afm),
                                     taxis_user=get(row, "taxis_user"), taxis_pass=get(row, "taxis_pass")))
    _finish(result)
    return result


def _finish(result: ImportResult) -> None:
    bad = sum(1 for r in result.rows if not r.checksum_ok)
    if bad:
        result.warnings.append(f"{bad} ΑΦΜ δεν περνούν τον έλεγχο ψηφίου ελέγχου — εισάγονται αλλά ελέγξτε τα.")
    half = [r.afm for r in result.rows if bool(r.taxis_user) != bool(r.taxis_pass)]
    if half:
        result.warnings.append(f"{len(half)} πελάτες έχουν μόνο χρήστη ή μόνο κωδικό TAXISnet — οι κωδικοί τους αγνοούνται.")
        for r in result.rows:
            if r.afm in half:
                r.taxis_user = r.taxis_pass = ""


def parse_file(filename: str, content: bytes) -> ImportResult:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            # Μόνο το πρώτο φύλλο: τα επόμενα (π.χ. «Sheet (2)») συχνά έχουν τους ίδιους ΑΦΜ χωρίς κωδικούς.
            return parse_rows(wb.worksheets[0].iter_rows(values_only=True))
        finally:
            wb.close()
    if name.endswith((".csv", ".txt")):
        text = None
        for enc in ("utf-8-sig", "cp1253", "latin-1"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        text = text or ""
        try:
            dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        return parse_rows(csv.reader(io.StringIO(text), dialect))
    raise ValueError("Υποστηρίζονται αρχεία .xlsx και .csv")


def template_xlsx() -> bytes:
    """Κενό πρότυπο με τις στήλες που αναγνωρίζονται, για όσους δεν έχουν εξαγωγή από πρόγραμμα λογιστικής."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Πελάτες"
    ws.append(TEMPLATE_HEADERS)
    for col, width in zip("ABCDE", (14, 38, 18, 26, 26)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
