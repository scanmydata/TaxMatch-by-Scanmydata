"""Επαναλαμβανόμενες φορολογικές/ασφαλιστικές υποχρεώσεις, παραγόμενες από κανόνες (ΦΠΑ, VIES, Intrastat, OSS/IOSS, ΑΠΔ…).

Συμπληρώνουν το ημερολόγιο του taxheaven, που δείχνει μόνο ~30 ημέρες μπροστά: εδώ οι προθεσμίες υπολογίζονται για
οποιοδήποτε μήνα και προσαρμόζονται ανά πελάτη (π.χ. μηνιαία/τριμηνιαία ΦΠΑ από το καθεστώς του πελάτη).

ΠΡΟΣΟΧΗ — οι ημερομηνίες είναι οι ΚΑΝΟΝΙΚΕΣ προθεσμίες του νόμου. Παρατάσεις και ειδικές ρυθμίσεις (π.χ. ΑΠΔ, ΓΕΜΗ, δηλώσεις
εισοδήματος) ανακοινώνονται ανά έτος· γι' αυτό υπερισχύουν πάντα οι πραγματικές ημερομηνίες των feeds/ΑΑΔΕ. Κάθε κανόνας
δείχνει την πηγή του. Επαληθεύτηκαν (2026-09) από: gov.gr/ΑΑΔΕ (ΦΠΑ, VIES, Intrastat, OSS), e-ΕΦΚΑ (ΑΠΔ), Επιθεώρηση Εργασίας (Ε4).
"""
from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Any, Callable, Optional

MONTHS_GEN = ["Ιανουαρίου", "Φεβρουαρίου", "Μαρτίου", "Απριλίου", "Μαΐου", "Ιουνίου", "Ιουλίου", "Αυγούστου",
              "Σεπτεμβρίου", "Οκτωβρίου", "Νοεμβρίου", "Δεκεμβρίου"]
MONTHS_NOM = ["Ιανουάριος", "Φεβρουάριος", "Μάρτιος", "Απρίλιος", "Μάιος", "Ιούνιος", "Ιούλιος", "Αύγουστος",
              "Σεπτέμβριος", "Οκτώβριος", "Νοέμβριος", "Δεκέμβριος"]
QUARTERS = ["Α΄ τρίμηνο", "Β΄ τρίμηνο", "Γ΄ τρίμηνο", "Δ΄ τρίμηνο"]


# ------------------------------------------------------------------ εργάσιμες ημέρες

def orthodox_easter(year: int) -> date:
    """Ορθόδοξο Πάσχα (Meeus, Ιουλιανό ημερολόγιο) μετατρεμμένο σε Γρηγοριανό (+13 ημέρες για 1900–2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    return date(year, month, day + 1) + timedelta(days=13)


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset[date]:
    easter = orthodox_easter(year)
    fixed = [(1, 1), (1, 6), (3, 25), (5, 1), (8, 15), (10, 28), (12, 25), (12, 26)]
    days = {date(year, m, d) for m, d in fixed}
    days |= {easter - timedelta(days=48),      # Καθαρά Δευτέρα
             easter - timedelta(days=2),       # Μεγάλη Παρασκευή
             easter + timedelta(days=1),       # Δευτέρα του Πάσχα
             easter + timedelta(days=50)}      # Αγίου Πνεύματος
    return frozenset(days)


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def next_business_day(d: date) -> date:
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def last_business_day(year: int, month: int) -> date:
    d = date(year, month, _cal.monthrange(year, month)[1])
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


# ------------------------------------------------------------------ κανόνες

@dataclass(frozen=True)
class Rule:
    id: str
    title: str                                   # μπορεί να περιέχει {period}
    description: str
    source_url: str
    freq: str                                    # monthly | quarterly | annual
    due: Callable[[int, int], date]              # (έτος, μήνας ΛΗΞΗΣ) -> ημερομηνία
    due_months: tuple[int, ...] = tuple(range(1, 13))
    applies: Callable[[Optional[dict[str, Any]]], tuple[bool, bool]] = lambda b: (True, False)  # (ισχύει, υπό προϋποθέσεις)
    hide_if_feed_terms: tuple[str, ...] = ()     # κρύψε αν το feed έχει ήδη ίδια μέρα γεγονός με αυτούς τους όρους
    period: Optional[Callable[[int, int], str]] = None


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _period_month(year: int, month: int) -> str:
    y, m = _prev_month(year, month)
    return f"{MONTHS_NOM[m - 1]} {y}"


def _period_quarter(year: int, month: int) -> str:
    y, m = _prev_month(year, month)
    return f"{QUARTERS[(m - 1) // 3]} {y}"


def _vat_monthly(b):
    if b is None:
        return True, False
    if b.get("vat_subject") == 0:
        return False, False
    pt = b.get("vat_period_type") or ""
    return (True, False) if pt == "monthly" else ((False, False) if pt == "quarterly" else (True, True))


def _vat_quarterly(b):
    if b is None:
        return True, False
    if b.get("vat_subject") == 0:
        return False, False
    pt = b.get("vat_period_type") or ""
    return (True, False) if pt == "quarterly" else ((False, False) if pt == "monthly" else (True, True))


def _conditional(b):                              # ισχύει μόνο αν ο πελάτης έχει τη σχετική δραστηριότητα (δεν το γνωρίζουμε)
    return True, True


def _conditional_vat(b):
    if b is not None and b.get("vat_subject") == 0:
        return False, False
    return True, True


def _due_vies(y: int, m: int) -> date:
    return next_business_day(date(y, m, 26))


RULES: list[Rule] = [
    Rule("vat_monthly", "Δήλωση ΦΠΑ (μηνιαία) — {period}",
         "Περιοδική δήλωση ΦΠΑ και πληρωμή του φόρου έως την τελευταία εργάσιμη ημέρα του επόμενου μήνα.",
         "https://www.gov.gr/sdg/taxes/vat/vat-return/deadlines-for-submitting-vat-returns", "monthly",
         lambda y, m: last_business_day(y, m), applies=_vat_monthly, hide_if_feed_terms=("δηλωσης", "φπα"),
         period=_period_month),
    Rule("vat_quarterly", "Δήλωση ΦΠΑ (τριμηνιαία) — {period}",
         "Περιοδική δήλωση ΦΠΑ τριμήνου και πληρωμή έως την τελευταία εργάσιμη ημέρα του επόμενου μήνα "
         "(30/4, 31/7, 31/10, 31/1 όταν είναι εργάσιμες).",
         "https://www.gov.gr/sdg/taxes/vat/vat-return/deadlines-for-submitting-vat-returns", "quarterly",
         lambda y, m: last_business_day(y, m), due_months=(1, 4, 7, 10), applies=_vat_quarterly,
         hide_if_feed_terms=("δηλωσης", "φπα"), period=_period_quarter),
    Rule("vies", "Πίνακας VIES (ενδοκοινοτικές συναλλαγές) — {period}",
         "Ανακεφαλαιωτικός πίνακας ενδοκοινοτικών παραδόσεων/αποκτήσεων (Φ4/Φ5) έως την 26η του επόμενου μήνα· "
         "αν είναι αργία, την επόμενη εργάσιμη (άρθρο 41 ΚΦΠΑ). Μόνο αν υπήρξαν ενδοκοινοτικές συναλλαγές.",
         "https://www.gov.gr/en/sdg/taxes/vat/vat-return/vies-recapitulative-statements", "monthly", _due_vies,
         applies=_conditional_vat, hide_if_feed_terms=("vies",), period=_period_month),
    Rule("intrastat", "Δήλωση Intrastat — {period}",
         "Στατιστική δήλωση ενδοκοινοτικών συναλλαγών στην ΕΛΣΤΑΤ, ταυτόχρονα με τον πίνακα VIES και το αργότερο μέχρι "
         "την προθεσμία του. Μόνο αν ξεπεραστούν τα ετήσια στατιστικά κατώφλια.",
         "https://www.gov.gr/arxes/ellenike-statistike-arkhe-elstat/ellenike-statistike-arkhe-elstat/Intrastat", "monthly",
         _due_vies, applies=_conditional_vat, hide_if_feed_terms=("intrastat",), period=_period_month),
    Rule("oss", "Δήλωση ΦΠΑ OSS (ενιαία, εξ αποστάσεως πωλήσεις) — {period}",
         "Τριμηνιαία ειδική δήλωση EU OSS / non-EU OSS έως το τέλος του επόμενου μήνα (π.χ. 30/4 για το Α΄ τρίμηνο). "
         "Μόνο για εγγεγραμμένους στο καθεστώς OSS.",
         "https://mitos.gov.gr/index.php/%CE%94%CE%94:%CE%94%CE%AE%CE%BB%CF%89%CF%83%CE%B7_%CE%A6%CE%A0%CE%91", "quarterly",
         lambda y, m: last_business_day(y, m), due_months=(1, 4, 7, 10), applies=_conditional_vat,
         hide_if_feed_terms=("oss",), period=_period_quarter),
    Rule("ioss", "Δήλωση ΦΠΑ IOSS (εισαγόμενα αγαθά) — {period}",
         "Μηνιαία δήλωση IOSS έως το τέλος του επόμενου μήνα. Μόνο για εγγεγραμμένους στο καθεστώς IOSS.",
         "https://mitos.gov.gr/index.php/%CE%94%CE%94:%CE%94%CE%AE%CE%BB%CF%89%CF%83%CE%B7_%CE%A6%CE%A0%CE%91", "monthly",
         lambda y, m: last_business_day(y, m), applies=_conditional_vat, hide_if_feed_terms=("ioss",),
         period=_period_month),
    Rule("apd", "ΑΠΔ e-ΕΦΚΑ & πληρωμή εισφορών — {period}",
         "Υποβολή Αναλυτικής Περιοδικής Δήλωσης και καταβολή εισφορών έως το τέλος του επόμενου μήνα. Μόνο για εργοδότες. "
         "Παρατάσεις υποβολής ανακοινώνονται συχνά από τον e-ΕΦΚΑ (η πληρωμή συνήθως μένει ίδια).",
         "https://www.e-efka.gov.gr/el/deltia-typoy", "monthly", lambda y, m: last_business_day(y, m),
         applies=_conditional, hide_if_feed_terms=("απδ",), period=_period_month),
    Rule("ergani_e4", "ΕΡΓΑΝΗ — Ετήσιος Πίνακας Προσωπικού (Ε4)",
         "Υποβολή του Ε4 από 1 έως 31 Οκτωβρίου κάθε έτους· η προθεσμία παρατείνεται συχνά με υπουργική απόφαση. "
         "Μόνο για εργοδότες.",
         "https://www.gov.gr/upourgeia/oloi-foreis/epitheorese-ergasias/etesios-pinakas-prosopikou-ergane-e4", "annual",
         lambda y, m: date(y, 10, 31), due_months=(10,), applies=_conditional, hide_if_feed_terms=("ε4",)),
    Rule("income_tax_return", "Προθεσμία δήλωσης φορολογίας εισοδήματος (Ε1/Ε2/Ε3/Ν)",
         "Η προθεσμία υποβολής των δηλώσεων εισοδήματος είναι συνήθως η 15η Ιουλίου (ανακοινώνεται ετησίως· "
         "παρατεινόμενη για ορισμένες κατηγορίες). Εφάπαξ πληρωμή/α΄ δόση: τελευταία εργάσιμη Ιουλίου.",
         "https://www.taxheaven.gr/news/69263/to-neo-plaisio-ypobolhs-twn-forologikwn-dhlwsewn-oi-hmeromhnies-ypobolhs-kai-oi-prooesmies-katabolhs-twn-forwn",
         "annual", lambda y, m: date(y, 7, 15), due_months=(7,), applies=_conditional, hide_if_feed_terms=("εισοδηματος",)),
]

RULES_BY_ID = {r.id: r for r in RULES}


@dataclass
class Occurrence:
    rule_id: str
    title: str
    date: date
    description: str
    source_url: str
    conditional: bool = False


def occurrences(start: date, end: date, business: Optional[dict[str, Any]] = None) -> list[Occurrence]:
    """Όλες οι προθεσμίες κανόνων με ημερομηνία στο [start, end]. `business`: dict με vat_subject, vat_period_type ή None (γενικό)."""
    out: list[Occurrence] = []
    for rule in RULES:
        applies, conditional = rule.applies(business)
        if not applies:
            continue
        seen: set[date] = set()
        cy, cm = _prev_month(start.year, start.month)      # η προθεσμία ενός μήνα λήξης μπορεί να πέφτει νωρίτερα/αργότερα
        last = (end.year, end.month)
        while (cy, cm) <= last:
            if cm in rule.due_months:
                d = rule.due(cy, cm)
                if start <= d <= end and d not in seen:
                    seen.add(d)
                    period = rule.period(cy, cm) if rule.period else ""
                    out.append(Occurrence(rule.id, rule.title.format(period=period), d, rule.description,
                                          rule.source_url, conditional))
            cy, cm = (cy + 1, 1) if cm == 12 else (cy, cm + 1)
    out.sort(key=lambda o: (o.date, o.rule_id))
    return out
