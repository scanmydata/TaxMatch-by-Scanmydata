"""XML του myDATA → παραστατικά. Βάση: `timologio/mydata/parse.py` (μία εγγραφή ανά MARK), συν ό,τι χρειάζονται οι
υποχρεώσεις: **παρακρατούμενοι φόροι ανά κατηγορία** (γραμμές `withheldAmount`/`withheldPercentCategory` ή
`taxesTotals` με taxType=1) και **χώρα** εκδότη/λήπτη (ενδοκοινοτικές συναλλαγές)."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

NS = "{http://www.aade.gr/myDATA/invoice/v1.0}"

#: Κατηγορίες παρακρατούμενων φόρων myDATA (withheldPercentCategory) — πίνακας 8.4 της τεκμηρίωσης REST API
#: (ελέγχθηκε 2026-10-22 μέσω τρίτης πηγής που τον αναπαράγει). rate=None: ποσό, όχι ποσοστό.
WITHHELD_CATEGORIES: dict[str, tuple[str, float | None]] = {
    "1": ("Τόκοι (περ. β')", 15.0),
    "2": ("Δικαιώματα (περ. γ')", 20.0),
    "3": ("Αμοιβές συμβουλών διοίκησης / επιχειρηματικής δραστηριότητας (περ. δ')", 20.0),
    "4": ("Τεχνικά έργα — εργολάβοι (περ. δ')", 3.0),
    "5": ("Υγρά καύσιμα και προϊόντα καπνοβιομηχανίας", 1.0),
    "6": ("Λοιπά αγαθά", 4.0),
    "7": ("Παροχή υπηρεσιών", 8.0),
    "8": ("Προκαταβλητέος φόρος αρχιτεκτόνων/μηχανικών (μελέτες)", 4.0),
    "9": ("Προκαταβλητέος φόρος αρχιτεκτόνων/μηχανικών (λοιπά έργα)", 10.0),
    "10": ("Προκαταβλητέος φόρος αμοιβών δικηγόρων", 15.0),
    "11": ("Παρακράτηση φόρου μισθωτών υπηρεσιών", None),
    "12": ("Παρακράτηση φόρου αξιωματικών εμπορικού ναυτικού", 15.0),
    "13": ("Παρακράτηση φόρου κατώτερου πληρώματος εμπορικού ναυτικού", 10.0),
    "14": ("Παρακράτηση ειδικής εισφοράς αλληλεγγύης", None),
    "15": ("Αποζημίωση λόγω διακοπής σχέσης εργασίας", None),
    "16": ("Παρακρατήσεις συναλλαγών αλλοδαπής (ΣΑΔΦ)", None),
    "17": ("Λοιπές παρακρατήσεις φόρου", None),
    "18": ("Μερίσματα", None),
}


def _t(elem: ET.Element | None, name: str) -> str:
    if elem is None:
        return ""
    node = elem.find(NS + name)
    if node is None:                                # η ΑΑΔΕ κατά καιρούς επιστρέφει στοιχεία χωρίς namespace
        node = elem.find(name)
    return (node.text or "").strip() if node is not None and node.text else ""


def _f(value: str) -> float:
    try:
        return float((value or "0").replace(",", "."))
    except ValueError:
        return 0.0


def iso_date(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 10 and value[4] == "-":
        return value[:10]
    parts = value.split("/")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return value


def _party(inv: ET.Element, which: str) -> dict[str, str]:
    node = inv.find(NS + which)
    if node is None:
        node = inv.find(which)
    address = node.find(NS + "address") if node is not None else None
    return {"vat": _t(node, "vatNumber"), "name": _t(node, "name"), "country": (_t(node, "country") or "").upper(),
            "city": _t(address, "city") if address is not None else ""}


def withheld_lines(inv: ET.Element) -> dict[str, float]:
    """Κατηγορία → ποσό παρακράτησης. Πρώτα οι γραμμές (`invoiceDetails`), αλλιώς τα `taxesTotals` (taxType 1 =
    παρακρατούμενος). Ποτέ και τα δύο (θα διπλομετρούσε)."""
    out: dict[str, float] = {}
    for det in inv.iter(NS + "invoiceDetails"):
        cat, amt = _t(det, "withheldPercentCategory"), _f(_t(det, "withheldAmount"))
        if cat and amt:
            out[cat] = round(out.get(cat, 0.0) + amt, 2)
    if out:
        return out
    for tax in inv.iter(NS + "taxes"):
        if _t(tax, "taxType") == "1":
            cat, amt = _t(tax, "taxCategory"), _f(_t(tax, "taxAmount"))
            if cat and amt:
                out[cat] = round(out.get(cat, 0.0) + amt, 2)
    return out


#: Κατηγορίες ΦΠΑ myDATA (vatCategory) ΧΩΡΙΣ φόρο: 7 = 0%, 8 = εγγραφές χωρίς ΦΠΑ (π.χ. μισθοδοσία/αποσβέσεις).
_NO_VAT_CATEGORIES = {"7", "8", ""}


def carries_vat(inv: ET.Element, direction: str = "outgoing") -> bool:
    """Για την προειδοποίηση σε ΜΗ υπόχρεους ΦΠΑ. Εκδοθέντα: γραμμή με κατηγορία ΦΠΑ που έχει συντελεστή ή ποσό ΦΠΑ.
    Και στις δύο κατευθύνσεις: χαρακτηρισμός ΦΠΑ (`vatClassification` / `VAT_…`, π.χ. VAT_361 «με δικαίωμα έκπτωσης»).
    Τα ΛΗΦΘΕΝΤΑ από προμηθευτές φέρουν φυσικά ΦΠΑ — εκεί μετρά μόνο ο χαρακτηρισμός."""
    if direction == "outgoing":
        for det in inv.iter(NS + "invoiceDetails"):
            if _t(det, "vatCategory") not in _NO_VAT_CATEGORIES or _f(_t(det, "vatAmount")):
                return True
    for elem in inv.iter():
        name = elem.tag.split("}", 1)[-1]
        if name in ("vatClassification", "vatClassificationType") or (
                name == "classificationType" and (elem.text or "").strip().startswith("VAT_")):
            return True
    return False


#: Παραστατικό ΕΝΟΙΚΙΟΥ: τύπος 8.1 «Ενοίκια – Έσοδο», ή γραμμή με ψηφιακό τέλος κατηγορίας 3 (3,6% = ο συντελεστής των
#: μισθώσεων ακινήτων, άρθρο 6 ν. 5135/2024). Στα ενοίκια το ψηφιακό τέλος αποδίδεται ΕΤΗΣΙΩΣ με τη δήλωση φορολογίας
#: εισοδήματος — όχι με τη μηνιαία δήλωση (ρητό αίτημα χρήστη 2026-10-22· στα πραγματικά του δεδομένα ψηφιακό τέλος
#: στα έσοδα υπήρχε μόνο σε 8.1, 3,56% επί της καθαρής).
RENT_TYPES = ("8.1",)
RENT_STAMP_CATEGORY = "3"


def is_rent(inv: ET.Element, invoice_type: str) -> bool:
    if invoice_type in RENT_TYPES:
        return True
    return any(_t(det, "stampDutyPercentCategory") == RENT_STAMP_CATEGORY for det in inv.iter(NS + "invoiceDetails"))


def parse_invoice(inv: ET.Element, direction: str) -> dict[str, Any]:
    header = inv.find(NS + "invoiceHeader")
    summary = inv.find(NS + "invoiceSummary")
    issuer, counter = _party(inv, "issuer"), _party(inv, "counterpart")
    net, vat = _f(_t(summary, "totalNetValue")), _f(_t(summary, "totalVatAmount"))
    gross = _f(_t(summary, "totalGrossValue")) or round(net + vat, 2)
    url = _t(inv, "downloadingInvoiceUrl")
    return {
        "mark": _t(inv, "mark"), "direction": direction, "invoice_type": _t(header, "invoiceType"),
        "series": _t(header, "series"), "aa": _t(header, "aa"), "issue_date": iso_date(_t(header, "issueDate")),
        "issuer_vat": issuer["vat"], "issuer_name": issuer["name"], "issuer_country": issuer["country"],
        "counter_vat": counter["vat"], "counter_name": counter["name"], "counter_country": counter["country"],
        "net": round(net, 2), "vat": round(vat, 2), "total": round(gross, 2),
        "withheld": round(_f(_t(summary, "totalWithheldAmount")), 2), "withheld_lines": withheld_lines(inv),
        "url": url, "provider": urlsplit(url).netloc if url else "",
        "cancelled": bool(_t(inv, "cancelledByMark")),
        # Ψηφιακό τέλος συναλλαγής = το πρώην «χαρτόσημο» του myDATA (μετονομασία στο API v1.0.11)
        "stamp": round(_f(_t(summary, "totalStampDutyAmount")), 2), "vat_class": carries_vat(inv, direction),
        "rent": is_rent(inv, _t(header, "invoiceType")),
    }


def parse_page(payload: bytes, direction: str) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    return [parse_invoice(inv, direction) for inv in root.iter(NS + "invoice")]
