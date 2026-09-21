"""Μαζική εισαγωγή πελατών από Excel/CSV (στήλη ΑΦΜ + προαιρετικά επωνυμία/ΚΑΔ)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..identifiers import is_valid_afm, normalize_afm
from ..textutil import strip_accents

_AFM_HEADERS = {"ΑΦΜ", "Α.Φ.Μ.", "Α.Φ.Μ", "AFM", "VAT", "VATNUMBER", "VAT NUMBER", "TAXID", "TAX ID"}
_NAME_HEADERS = {"ΕΠΩΝΥΜΙΑ", "ΟΝΟΜΑΣΙΑ", "ΟΝΟΜΑ", "ΕΠΩΝΥΜΟ", "NAME", "COMPANY", "ΠΕΛΑΤΗΣ", "ΕΠΙΧΕΙΡΗΣΗ"}
_KAD_HEADERS = {"ΚΑΔ", "Κ.Α.Δ.", "Κ.Α.Δ", "KAD", "ΚΑΔ ΚΥΡΙΟ", "ΚΥΡΙΟΣ ΚΑΔ"}


@dataclass
class ImportRow:
    afm: str
    name: str = ""
    kads: list[str] = field(default_factory=list)
    checksum_ok: bool = True


@dataclass
class ImportResult:
    rows: list[ImportRow] = field(default_factory=list)
    invalid: list[tuple[int, str]] = field(default_factory=list)      # (γραμμή αρχείου, τιμή)
    duplicates: int = 0
    warnings: list[str] = field(default_factory=list)


def _header(v: object) -> str:
    return re.sub(r"\s+", " ", strip_accents(str(v or "")).strip().upper())


def _split_kads(v: object) -> list[str]:
    return [p.strip() for p in re.split(r"[;,/\n]+", str(v or "")) if re.search(r"\d{2}", p)]


def parse_rows(table: Iterable[tuple]) -> ImportResult:
    """`table`: iterable γραμμών (tuples). Η πρώτη γραμμή που περιέχει επικεφαλίδα ΑΦΜ ορίζει τις στήλες·
    αν δεν βρεθεί επικεφαλίδα, η στήλη Α θεωρείται ΑΦΜ (και η Β επωνυμία)."""
    result = ImportResult()
    rows = [tuple(r) for r in table]
    afm_col, name_col, kad_col, start = 0, 1, None, 0
    for i, row in enumerate(rows[:15]):
        heads = [_header(c) for c in row]
        if any(h in _AFM_HEADERS for h in heads):
            afm_col = next(j for j, h in enumerate(heads) if h in _AFM_HEADERS)
            name_col = next((j for j, h in enumerate(heads) if h in _NAME_HEADERS), None)
            kad_col = next((j for j, h in enumerate(heads) if h in _KAD_HEADERS), None)
            start = i + 1
            break
    else:
        result.warnings.append("Δεν βρέθηκε επικεφαλίδα «ΑΦΜ» — χρησιμοποιήθηκε η πρώτη στήλη.")

    seen: set[str] = set()
    for offset, row in enumerate(rows[start:], start=start + 1):
        if not row or all(c in (None, "") for c in row):
            continue
        cell = row[afm_col] if afm_col < len(row) else None
        afm = normalize_afm(cell)
        if not afm:
            result.invalid.append((offset, str(cell)))
            continue
        if afm in seen:
            result.duplicates += 1
            continue
        seen.add(afm)
        name = str(row[name_col]).strip() if name_col is not None and name_col < len(row) and row[name_col] else ""
        kads = _split_kads(row[kad_col]) if kad_col is not None and kad_col < len(row) else []
        result.rows.append(ImportRow(afm=afm, name=name, kads=kads, checksum_ok=is_valid_afm(afm)))
    bad = sum(1 for r in result.rows if not r.checksum_ok)
    if bad:
        result.warnings.append(f"{bad} ΑΦΜ δεν περνούν τον έλεγχο ψηφίου ελέγχου — εισάγονται αλλά ελέγξτε τα.")
    return result


def parse_file(filename: str, content: bytes) -> ImportResult:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            ws = wb.worksheets[0]
            return parse_rows(ws.iter_rows(values_only=True))
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
