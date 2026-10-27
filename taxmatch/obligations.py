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

from .textutil import strip_accents

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
    ref_offset: int = 1                          # μήνας αναφοράς = μήνας λήξης − ref_offset (για τα στοιχεία myDATA)


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _period_month(year: int, month: int) -> str:
    y, m = _prev_month(year, month)
    return f"{MONTHS_NOM[m - 1]} {y}"


def _period_quarter(year: int, month: int) -> str:
    y, m = _prev_month(year, month)
    return f"{QUARTERS[(m - 1) // 3]} {y}"


def _period_month2(year: int, month: int) -> str:
    y, m = _prev_month(*_prev_month(year, month))
    return f"{MONTHS_NOM[m - 1]} {y}"


def ref_month(year: int, month: int, offset: int) -> str:
    """'YYYY-MM' του μήνα αναφοράς μιας προθεσμίας που λήγει τον (year, month)."""
    for _ in range(offset):
        year, month = _prev_month(year, month)
    return f"{year}-{month:02d}"


def _vat_monthly(b):
    if b is None:
        return True, False
    if b.get("vat_subject") == 0:
        return False, False
    pt = vat_period(b)
    return (True, False) if pt == "monthly" else ((False, False) if pt == "quarterly" else (True, True))


def _vat_quarterly(b):
    if b is None:
        return True, False
    if b.get("vat_subject") == 0:
        return False, False
    pt = vat_period(b)
    return (True, False) if pt == "quarterly" else ((False, False) if pt == "monthly" else (True, True))


def second_last_business_day(year: int, month: int) -> date:
    d = last_business_day(year, month) - timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def _conditional(b):                              # ισχύει μόνο αν ο πελάτης έχει τη σχετική δραστηριότητα (δεν το γνωρίζουμε)
    return True, True


def _conditional_vat(b):
    if b is not None and b.get("vat_subject") == 0:
        return False, False
    return True, True


def _due_vies(y: int, m: int) -> date:
    return next_business_day(date(y, m, 26))


def month_end(y: int, m: int) -> date:
    return date(y, m, _cal.monthrange(y, m)[1])


def month_end_or_next(y: int, m: int) -> date:
    """Τέλος μήνα· αν είναι αργία/Σαββατοκύριακο → η ΕΠΟΜΕΝΗ εργάσιμη. Έτσι δείχνει το ημερολόγιο ΑΑΔΕ/taxheaven τις
    ΑΠΔ και τους παρακρατούμενους (π.χ. 31/10/2026 Σάββατο → 2/11/2026 — επαληθεύτηκε 2026-10-22). Ο ΦΠΑ, αντίθετα,
    λήγει την τελευταία εργάσιμη (30/10/2026)."""
    return next_business_day(month_end(y, m))


def _period_income_instalment(y: int, m: int) -> str:
    """Φόρος εισοδήματος σε 8 δόσεις: 1η έως 31/7, οι επόμενες την τελευταία εργάσιμη Αυγ.–Φεβ. (ΑΑΔΕ, δελτίο τύπου
    16/3/2026) — φορολογικό έτος = το προηγούμενο της δήλωσης."""
    n = m - 6 if m >= 7 else m + 6
    return f"{n}η δόση — φορολογικό έτος {y - 1 if m >= 7 else y - 2}"


def _period_enfia(y: int, m: int) -> str:
    """ΕΝΦΙΑ σε 12 μηνιαίες δόσεις: 1η έως 31/3, τελευταία τον Φεβρουάριο του επόμενου έτους (εκκαθαριστικά 2026)."""
    n = m - 2 if m >= 3 else m + 10
    return f"{n}η δόση ΕΝΦΙΑ {y if m >= 3 else y - 1}"


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
    Rule("vat_quarter_option", "Επιλογή τριμηνιαίας περιόδου ΦΠΑ (Β΄ κατηγορία σε μηνιαίο)",
         "Απλογραφικά με έναρξη από 1.1.2024 υποβάλλουν υποχρεωτικά μηνιαία δήλωση ΦΠΑ (Α.1049/2025). Όταν συμπληρωθούν "
         "24 μήνες από την έναρξη μπορούν να επανέλθουν στο τρίμηνο με δήλωση επιλογής στο myAADE (Μητρώο & Επικοινωνία → "
         "Αλλαγή στοιχείων μητρώου) έως την προτελευταία εργάσιμη του Ιανουαρίου/Απριλίου/Ιουλίου/Οκτωβρίου· ισχύει από "
         "την 1η του μήνα της δήλωσης και δεν ανακαλείται πριν από 12 μήνες.",
         "https://www.taxheaven.gr/news/70387/mhniaia-dhlwsh-fpa-dhmosieyohke-h-apofash-me-tis-leptomereies", "quarterly",
         second_last_business_day, due_months=(1, 4, 7, 10), applies=_conditional_vat,
         hide_if_feed_terms=("επιλογης",), ref_offset=0),
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
         month_end, due_months=(1, 4, 7, 10), applies=_conditional_vat,
         hide_if_feed_terms=("oss",), period=_period_quarter),
    Rule("ioss", "Δήλωση ΦΠΑ IOSS (εισαγόμενα αγαθά) — {period}",
         "Μηνιαία δήλωση IOSS έως το τέλος του επόμενου μήνα. Μόνο για εγγεγραμμένους στο καθεστώς IOSS.",
         "https://mitos.gov.gr/index.php/%CE%94%CE%94:%CE%94%CE%AE%CE%BB%CF%89%CF%83%CE%B7_%CE%A6%CE%A0%CE%91", "monthly",
         month_end, applies=_conditional_vat, hide_if_feed_terms=("ioss",),
         period=_period_month),
    Rule("apd", "ΑΠΔ e-ΕΦΚΑ (ΙΚΑ) & πληρωμή εισφορών — {period}",
         "Υποβολή Αναλυτικής Περιοδικής Δήλωσης και καταβολή εισφορών έως το τέλος του επόμενου μήνα. Μόνο για εργοδότες. "
         "Παρατάσεις υποβολής ανακοινώνονται συχνά από τον e-ΕΦΚΑ (η πληρωμή συνήθως μένει ίδια).",
         "https://www.e-efka.gov.gr/el/deltia-typoy", "monthly", month_end_or_next,
         applies=_conditional, hide_if_feed_terms=("απδ",), period=_period_month),
    # ΑΠΔ ΤΕΚΑ: ΞΕΧΩΡΙΣΤΗ υποβολή (TaxSystem: RetrievalKindPdfs.APD_Teka → «eAPDssoTEKA» / ssp.efka.apd.teka) για
    # όσους εργαζόμενους ασφαλίζονται επικουρικά στο ΤΕΚΑ (νέοι από 1/1/2022) — ρητό αίτημα χρήστη 2026-10-27.
    Rule("apd_teka", "ΑΠΔ ΤΕΚΑ & πληρωμή εισφορών — {period}",
         "Αναλυτική Περιοδική Δήλωση του Ταμείου Επικουρικής Κεφαλαιοποιητικής Ασφάλισης (ΤΕΚΑ) — χωριστή από την ΑΠΔ "
         "e-ΕΦΚΑ — έως το τέλος του επόμενου μήνα. Μόνο για εργοδότες με εργαζόμενους ασφαλισμένους επικουρικά στο ΤΕΚΑ.",
         "https://www.teka.gov.gr/", "monthly", month_end_or_next,
         applies=_conditional, hide_if_feed_terms=("τεκα",), period=_period_month),
    Rule("withholding_fees", "Παρακρατούμενος φόρος αμοιβών / εργολάβων — {period}",
         "Δήλωση και απόδοση του φόρου που παρακράτησε ο πελάτης σε αμοιβές επιχειρηματικής δραστηριότητας (20%), "
         "εργολάβους τεχνικών έργων (3%), τόκους/δικαιώματα κ.λπ., έως το τέλος του ΔΕΥΤΕΡΟΥ μήνα από την καταβολή "
         "(άρθρο 60 ΚΦΕ). Η εφαρμογή το βρίσκει από τα παραστατικά myDATA με παρακράτηση (ημερομηνία έκδοσης).",
         "https://www.taxheaven.gr/circulars/17952/arora-parakrathsh-foroy-me-ton-neo-kfe-allages-sthn-ypobolh-dhlwsewn-parakratoymenwn-forwn-enhmerwmeno",
         "monthly", month_end_or_next, applies=_conditional, hide_if_feed_terms=("παρακρατ", "64"),
         period=_period_month2, ref_offset=2),
    Rule("fmy_wages", "Παρακρατούμενος φόρος μισθωτών υπηρεσιών (ΦΜΥ) — {period}",
         "Δήλωση και απόδοση του φόρου που παρακρατήθηκε σε μισθούς/συντάξεις έως το τέλος του ΔΕΥΤΕΡΟΥ μήνα από την "
         "καταβολή (το ημερολόγιο ΑΑΔΕ: «περιόδου Αυγούστου» → 2/11). Μόνο για εργοδότες.",
         "https://www.taxheaven.gr/circulars/17952/arora-parakrathsh-foroy-me-ton-neo-kfe-allages-sthn-ypobolh-dhlwsewn-parakratoymenwn-forwn-enhmerwmeno",
         "monthly", month_end_or_next, applies=_conditional, hide_if_feed_terms=("παρακρατ", "μισθωτη εργασια"),
         period=_period_month2, ref_offset=2),
    Rule("climate_fee", "Τέλος ανθεκτικότητας στην κλιματική κρίση — {period}",
         "Δήλωση απόδοσης του τέλους ανθεκτικότητας (πρώην φόρος διαμονής) που εισπράχθηκε με ειδικά στοιχεία 8.2, έως την "
         "τελευταία ημέρα του επόμενου μήνα (myAADE → Τέλη & ειδικοί φόροι). Μόνο για καταλύματα που εκδίδουν 8.2.",
         "https://www.taxheaven.gr/news/66770/telos-anoektikothtas-pshfiakh-ypobolh-dhlwshs-apodoshs",
         "monthly", month_end_or_next, applies=_conditional, hide_if_feed_terms=("ανθεκτικοτητας",),
         period=_period_month),
    Rule("digital_fee", "Ψηφιακό τέλος συναλλαγής — {period}",
         "Δήλωση και απόδοση του ψηφιακού τέλους συναλλαγής (ν. 5135/2024, πρώην χαρτόσημο) για τον μήνα που υπήρξε "
         "συναλλαγή με τέλος, έως την τελευταία ημέρα του επόμενου μήνα (myAADE). Η εφαρμογή το εντοπίζει στα ΕΣΟΔΑ "
         "του myDATA· δεν υπάρχει ακόμη αυτόματος έλεγχος υποβολής — σημειώστε το χειροκίνητα ως ολοκληρωμένο.",
         "https://www.taxheaven.gr/news/68790/pshfiako-telos-synallagwn-h-dhlwsh-kai-o-tropos-ypobolhs-ths",
         "monthly", month_end_or_next, applies=_conditional, hide_if_feed_terms=("ψηφιακου τελους",),
         period=_period_month),
    # Τέλη που εντοπίζονται από τις κατηγορίες τελών του myDATA (mydata/analysis.FEE_GROUPS) — ρητό αίτημα 2026-10-27.
    Rule("plastic_bag_fee", "Περιβαλλοντικό τέλος πλαστικής σακούλας — {period}",
         "Δήλωση απόδοσης του περιβαλλοντικού τέλους πλαστικής σακούλας μεταφοράς (ν. 2339/2001 αρ. 6α) ανά τρίμηνο, "
         "έως το τέλος του επόμενου μήνα (TAXISnet → otherAppH7). Μόνο αν τιμολογεί το τέλος (κατηγορία τελών 8 στο myDATA).",
         "https://www.aade.gr/epiheiriseis/forologikes-ypohreoseis/periballontiko-telos-plastikis-sakoylas",
         "quarterly", month_end_or_next, due_months=(1, 4, 7, 10), applies=_conditional,
         hide_if_feed_terms=("σακουλας",), period=_period_quarter),
    Rule("plastics_fee", "Εισφορά πλαστικών προϊόντων (ν. 4736/2020) — {period}",
         "Δήλωση της περιβαλλοντικής εισφοράς στα πλαστικά προϊόντα μιας χρήσης (κύπελλα, καπάκια, περιέκτες τροφίμων) ανά "
         "τρίμηνο, έως το τέλος του επόμενου μήνα (εφαρμογή «Περιβαλλοντική Εισφορά Πλαστικών» της ΑΑΔΕ). Μόνο αν τιμολογεί "
         "την εισφορά (κατηγορία τελών 16 στο myDATA).",
         "https://www1.aade.gr/saadeapps3/Plastic/", "quarterly", month_end_or_next, due_months=(1, 4, 7, 10),
         applies=_conditional, hide_if_feed_terms=("πλαστικων",), period=_period_quarter),
    Rule("parepid_fee", "Τέλος παρεπιδημούντων / ακαθαρίστων εσόδων — {period}",
         "Δήλωση απόδοσης του τέλους διαμονής παρεπιδημούντων και του τέλους επί των ακαθαρίστων εσόδων (εστιατόρια, κέντρα "
         "διασκέδασης κ.λπ.) στην πλατφόρμα dae.govapp.gr (ΑΑΔΕ/ΚΕΔΕ, από 1/4/2025), έως το τέλος του επόμενου μήνα της "
         "περιόδου (μήνας ή τρίμηνο, όπως ο ΦΠΑ). Μόνο αν τιμολογεί τα τέλη (κατηγορίες τελών 18–22 στο myDATA).",
         "https://dae.govapp.gr/", "monthly", month_end_or_next, applies=_conditional,
         hide_if_feed_terms=("παρεπιδημουντων",), period=_period_month),
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
    # Υποχρεώσεις ΚΑΙ των ιδιωτών (ρητό αίτημα 2026-10-27: «στον ιδιώτη να μπουν οι κατάλληλες υποχρεώσεις»)
    Rule("income_tax_instalment", "Φόρος εισοδήματος — {period}",
         "Ο φόρος εισοδήματος (Ε1/Ε3) καταβάλλεται εφάπαξ ή σε 8 δόσεις: η 1η έως 31 Ιουλίου, οι υπόλοιπες την "
         "τελευταία εργάσιμη κάθε επόμενου μήνα ως τον Φεβρουάριο. Μόνο αν η εκκαθάριση της δήλωσης είναι χρεωστική.",
         "https://www.aade.gr/", "monthly", lambda y, m: last_business_day(y, m), due_months=(7, 8, 9, 10, 11, 12, 1, 2),
         applies=_conditional, hide_if_feed_terms=("φορου εισοδηματος",), period=_period_income_instalment),
    Rule("enfia", "ΕΝΦΙΑ — {period}",
         "Ο ΕΝΦΙΑ καταβάλλεται εφάπαξ ή σε 12 μηνιαίες δόσεις: η 1η έως 31 Μαρτίου και η τελευταία τον Φεβρουάριο του "
         "επόμενου έτους (τελευταία εργάσιμη κάθε μήνα). Μόνο για όσους έχουν ακίνητα (εκκαθαριστικό ΕΝΦΙΑ).",
         "https://www.aade.gr/", "monthly", lambda y, m: last_business_day(y, m),
         due_months=(3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2), applies=_conditional, hide_if_feed_terms=("ενφια",),
         period=_period_enfia),
]

RULES_BY_ID = {r.id: r for r in RULES}

# ------------------------------------------------------------------ ποιος πελάτης είναι υπόχρεος
#: κανόνες που αφορούν ΜΟΝΟ όποιον ασκεί επιχειρηματική δραστηριότητα (όχι ιδιώτη / όχι μετά τη διακοπή)
_BUSINESS_ONLY = {"vat_monthly", "vat_quarterly", "vies", "intrastat", "oss", "ioss", "apd", "ergani_e4",
                  "withholding_fees", "fmy_wages", "climate_fee", "digital_fee"}
_BUSINESS_ONLY |= {"vat_quarter_option", "apd_teka", "plastic_bag_fee", "plastics_fee", "parepid_fee"}
_MAYBE_REASON = {"vies": "μόνο αν είχε ενδοκοινοτικές συναλλαγές στην περίοδο",
                 "vat_quarter_option": "μόνο Β΄ κατηγορίας σε μηνιαίο ΦΠΑ που συμπλήρωσαν 24 μήνες από την έναρξη",
                 "intrastat": "μόνο αν ξεπερνά τα στατιστικά κατώφλια",
                 "oss": "μόνο αν είναι εγγεγραμμένος στο OSS", "ioss": "μόνο αν είναι εγγεγραμμένος στο IOSS",
                 "apd": "μόνο αν απασχολεί προσωπικό", "ergani_e4": "μόνο αν απασχολεί προσωπικό",
                 "withholding_fees": "μόνο αν παρακράτησε φόρο σε αμοιβές/εργολάβους (λείπουν στοιχεία myDATA)",
                 "fmy_wages": "μόνο αν απασχολεί προσωπικό",
                 "climate_fee": "μόνο αν εκδίδει στοιχεία 8.2 (καταλύματα) — λείπουν στοιχεία myDATA",
                 "digital_fee": "μόνο αν εισπράττει ψηφιακό τέλος συναλλαγής — λείπουν στοιχεία myDATA",
                 "apd_teka": "μόνο αν απασχολεί εργαζόμενους ασφαλισμένους επικουρικά στο ΤΕΚΑ",
                 "plastic_bag_fee": "μόνο αν τιμολογεί τέλος πλαστικής σακούλας — λείπουν στοιχεία myDATA",
                 "plastics_fee": "μόνο αν τιμολογεί εισφορά πλαστικών προϊόντων — λείπουν στοιχεία myDATA",
                 "parepid_fee": "μόνο αν τιμολογεί τέλος παρεπιδημούντων/ακαθαρίστων — λείπουν στοιχεία myDATA",
                 "income_tax_instalment": "μόνο αν η εκκαθάριση του Ε1/Ε3 είναι χρεωστική και πληρώνεται σε δόσεις",
                 "enfia": "μόνο αν έχει ακίνητα (εκκαθαριστικό ΕΝΦΙΑ) και πληρώνει σε δόσεις"}
#: Κανόνες που απαντώνται ανά μήνα αναφοράς από τα παραστατικά myDATA (mydata/analysis.month_flags).
_MYDATA_FLAG = {"vies": "vies", "intrastat": "intrastat", "withholding_fees": "withholding", "climate_fee": "climate",
                "digital_fee": "digital_fee", "plastic_bag_fee": "plastic_bag", "plastics_fee": "plastics",
                "parepid_fee": "parepid"}
#: Τέλη τριμήνου: η σημαία myDATA ισχύει αν εμφανίστηκε σε ΟΠΟΙΟΝΔΗΠΟΤΕ μήνα του τριμήνου.
_QUARTER_FLAGS = {"plastic_bag_fee", "plastics_fee"}
#: Τέλη που προκύπτουν ΜΟΝΟ από τα έσοδα του myDATA — «ποτέ στο myDATA» ⇒ «όχι» (όχι «υπό προϋποθέσεις» για όλους).
_MYDATA_ONLY = {"climate_fee", "digital_fee", "plastic_bag_fee", "plastics_fee", "parepid_fee"}
_MYDATA_REASON = {"vies": ("ενδοκοινοτικές συναλλαγές στο myDATA", "καμία ενδοκοινοτική συναλλαγή στο myDATA"),  # βλ. vies_reason
                  "intrastat": ("υπέρβαση κατωφλίου Intrastat (myDATA)", "κάτω από τα κατώφλια Intrastat (myDATA)"),
                  "withholding_fees": ("παραστατικά με παρακράτηση φόρου στο myDATA", "καμία παρακράτηση στο myDATA"),
                  "climate_fee": ("εξέδωσε στοιχεία 8.2 (τέλος ανθεκτικότητας) στο myDATA", "κανένα 8.2 στο myDATA"),
                  "digital_fee": ("ψηφιακό τέλος συναλλαγής στα έσοδα (myDATA)", "κανένα ψηφιακό τέλος στα έσοδα"),
                  "plastic_bag_fee": ("τέλος πλαστικής σακούλας στα έσοδα (myDATA, κατ. τελών 8)",
                                      "κανένα τέλος σακούλας στο τρίμηνο (myDATA)"),
                  "plastics_fee": ("εισφορά πλαστικών προϊόντων στα έσοδα (myDATA, κατ. τελών 16)",
                                   "καμία εισφορά πλαστικών στο τρίμηνο (myDATA)"),
                  "parepid_fee": ("τέλος παρεπιδημούντων/ακαθαρίστων στα έσοδα (myDATA, κατ. τελών 18–22)",
                                  "κανένα τέλος παρεπιδημούντων στο myDATA")}

#: Ποιος έλεγχος υποβολών (easycheck.OBLIGATIONS) απαντά αν υποβλήθηκε η δήλωση κάθε κανόνα. Η χρήση του ελέγχου είναι
#: το έτος του μήνα αναφοράς (δήλωση εισοδήματος: το προηγούμενο έτος της προθεσμίας).
RULE_CHECKS: dict[str, tuple[str, ...]] = {
    "vat_monthly": ("Φ2",), "vat_quarterly": ("Φ2",), "vies": ("Φ4", "Φ5"),     # ποιο από τα δύο: vies_forms
    "withholding_fees": ("ΦΜΥ_ΕΠΙΧ", "ΕΡΓΟΛΑΒΩΝ", "ΦΜΥ_ΔΙΚΑΙΩΜΑΤΑ", "ΦΜΥ_ΤΟΚΟΙ"),
    "fmy_wages": ("ΦΜΥ",), "climate_fee": ("ΔΙΑΜΟΝΗ",), "income_tax_return": ("E1", "E3", "ΕΙΣΟΔΗΜΑ_ΝΠ"),
    "apd": ("ΑΠΔ",),                       # e-ΕΦΚΑ (ΙΚΑ) ανά ΑΜΕ («ΑΠΔ#<ΑΜΕ>», ενώνονται στο deadlines) — βλ. efka.py
    "apd_teka": ("ΑΠΔ_ΤΕΚΑ",),             # ΤΕΚΑ ανά ΑΜΕ — βλ. efka.check_apd
    "plastic_bag_fee": ("ΠΕΡΙΒΑΛΛΟΝ",),    # TAXISnet otherAppH7 (easycheck)
    "plastics_fee": ("ΠΛΑΣΤΙΚΑ",),         # εφαρμογή «Plastic» — βλ. fee_portals.py
    "parepid_fee": ("ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ",),   # dae.govapp.gr — βλ. fee_portals.py
    "intrastat": ("INTRASTAT",),           # ΕΛ.ΣΤΑΤ «Αρχείο Δηλώσεων» (κωδικοί γραφείου) — βλ. intrastat.py
}
#: Έλεγχοι που μπορεί να έρθουν ανά ΤΡΙΜΗΝΟ ενώ ο κανόνας είναι μηνιαίος (dae.govapp.gr: όπως η περίοδος ΦΠΑ).
_QUARTER_FALLBACK = {"parepid_fee"}
#: Κανόνες που ολοκληρώνονται ΜΟΝΟ χειροκίνητα (δεν υπάρχει ακόμη μέθοδος ελέγχου υποβολής).
MANUAL_ONLY = {"digital_fee"}


VIES_FORM_LABEL = {"Φ4": "παραδόσεις/παροχές (Φ4)", "Φ5": "αποκτήσεις/λήψεις (Φ5)"}


def vies_forms(business: Optional[dict[str, Any]], ref_month: str) -> tuple[tuple[str, ...], bool]:
    """Ποιοι ανακεφαλαιωτικοί πίνακες οφείλονται τον μήνα: Φ4 = παραδόσεις αγαθών / παροχές υπηρεσιών (εκδοθέντα 1.2,
    2.2), Φ5 = αποκτήσεις / λήψεις υπηρεσιών (14.1, 14.3) — από το myDATA. (forms, known): χωρίς στοιχεία myDATA του
    μήνα → και τα δύο, known=False (αρκεί να υποβλήθηκε ένα). Αναφορά χρήστη 2026-10-23: εταιρεία με ΜΟΝΟ απόκτηση
    έβγαινε «Φ4 ΔΕΝ υποβλήθηκε»."""
    flags = (((business or {}).get("mydata") or {}).get(ref_month) or {})
    if "vies_out" not in flags:
        return ("Φ4", "Φ5"), False
    forms = tuple(f for f, on in (("Φ4", flags["vies_out"]), ("Φ5", flags["vies_in"])) if on)
    return forms, True


def rule_checks(rule_id: str, business: Optional[dict[str, Any]], ref_month: str) -> tuple[str, ...]:
    """Τα έντυπα του ελέγχου υποβολών που απαντούν για αυτή την προθεσμία (RULE_CHECKS, με Φ4/Φ5 ανά μήνα)."""
    if rule_id == "vies":
        forms, known = vies_forms(business, ref_month)
        return forms if known else RULE_CHECKS["vies"]
    return RULE_CHECKS.get(rule_id, ())


def check_year(rule_id: str, ref_month: str) -> int:
    """Η χρήση του ελέγχου υποβολών για μια προθεσμία: το έτος του μήνα αναφοράς — για τη δήλωση εισοδήματος (λήγει
    τον Ιούλιο) η ΠΡΟΗΓΟΥΜΕΝΗ χρήση."""
    y = int(ref_month[:4]) if ref_month else date.today().year
    return y - 1 if rule_id == "income_tax_return" else y


def _check_says_not_obligated(rule_id: str, business: dict[str, Any], ref_month: str) -> bool:
    """Ο έλεγχος υποβολών της ΑΑΔΕ («Δεν έχετε υποχρεώσεις υποβολής…») για ΟΛΑ τα σχετικά έντυπα της χρήσης."""
    keys = rule_checks(rule_id, business, ref_month)
    years = (business.get("checks") or {}).get("years") or {}
    if not keys or not ref_month:
        return False
    y = check_year(rule_id, ref_month)
    found = [years.get((k, y)) for k in keys]
    return all(v == "not_obligated" for v in found)


def vat_period(business: dict[str, Any]) -> str:
    """'monthly' | 'quarterly' | '' — από το Μητρώο (ή τις περιόδους Φ2 του ελέγχου υποβολών)· αλλιώς από την κατηγορία
    βιβλίων: Γ = μηνιαία, Β = τριμηνιαία — ΕΚΤΟΣ Β με έναρξη από 1.1.2024 (μηνιαία, Α.1049/2025)· χωρίς βιβλία / μη
    εγκατεστημένοι = τριμηνιαία (άρθρο 38 ΚΦΠΑ)."""
    from .business_profiles.vat_profile import b_monthly_by_law, keeps_no_books
    pt = business.get("vat_period_type") or ""
    if pt:
        return pt
    books = business.get("books_category") or ""
    if books == "Β" and b_monthly_by_law(business.get("start_date")):
        return "monthly"
    if not books and keeps_no_books(business.get("books_category_raw")):
        return "quarterly"
    return {"Γ": "monthly", "Β": "quarterly"}.get(books, "")


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, _cal.monthrange(y, m)[1]))


def quarter_option(business: Optional[dict[str, Any]], today: Optional[date] = None) -> Optional[dict[str, Any]]:
    """Β΄ κατηγορία σε μηνιαίο ΦΠΑ: πότε μπορεί να επανέλθει στο τρίμηνο (Α.1049/2025, άρθρο 2). None = δεν αφορά.
    {'start', 'eligible_from' (1η του πρώτου μήνα Ιαν/Απρ/Ιουλ/Οκτ μετά τους 24 μήνες), 'deadline' (επόμενη προθεσμία
    δήλωσης επιλογής), 'eligible_now', 'by_law' (υποχρεωτικά μηνιαίος) }."""
    from .business_profiles.vat_profile import B_MONTHLY_SINCE, parse_start
    if not business or business.get("vat_subject") == 0 or (business.get("books_category") or "") != "Β":
        return None
    if vat_period(business) != "monthly":
        return None
    today = today or date.today()
    start = parse_start(business.get("start_date"))
    by_law = start is not None and start >= B_MONTHLY_SINCE
    if by_law:
        e = _add_months(start, 24)
        y, m = e.year, e.month
        if e.day > 1:                                   # οι 24 μήνες κλείνουν μέσα στον μήνα → επόμενος μήνας
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        while m not in (1, 4, 7, 10):
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        eligible_from = date(y, m, 1)
    else:
        eligible_from = None                            # παλαιότερη έναρξη: μηνιαίος με ΕΠΙΛΟΓΗ — άγνωστο από πότε
    y, m = today.year, today.month
    if eligible_from and eligible_from > today:
        y, m = eligible_from.year, eligible_from.month
    while m not in (1, 4, 7, 10) or second_last_business_day(y, m) < today:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return {"start": start, "eligible_from": eligible_from, "deadline": second_last_business_day(y, m),
            "eligible_now": bool(eligible_from and eligible_from <= today), "by_law": by_law}


def vat_option_note(business: Optional[dict[str, Any]], today: Optional[date] = None) -> str:
    """Μήνυμα για Β΄ κατηγορίας σε μηνιαίο ΦΠΑ (ρητό αίτημα χρήστη 2026-10-22): πότε/αν μπορεί να επανέλθει στο τρίμηνο."""
    opt = quarter_option(business, today)
    if not opt:
        return ""
    dl = opt["deadline"].strftime("%d/%m/%Y")
    if not opt["by_law"]:
        return ("Β΄ κατηγορία σε μηνιαίο ΦΠΑ με επιλογή (Α.1049/2025, άρθρο 3): μπορεί να ανακαλέσει μετά από 12 μήνες — "
                f"δήλωση στο myAADE έως την προτελευταία εργάσιμη Ιαν/Απρ/Ιουλ/Οκτ (επόμενη: {dl}).")
    if opt["eligible_now"]:
        return ("Συμπλήρωσε 24 μήνες από την έναρξη: μπορεί να ΕΠΑΝΕΛΘΕΙ στον τριμηνιαίο ΦΠΑ — δήλωση επιλογής στο myAADE "
                f"(Μητρώο & Επικοινωνία → Αλλαγή στοιχείων) έως {dl}.")
    return (f"Μηνιαίος ΦΠΑ υποχρεωτικά (Β΄ κατηγορία, έναρξη {opt['start'].strftime('%d/%m/%Y')} — Α.1049/2025)· "
            f"τριμηνιαίος μπορεί να επιλεγεί από {opt['eligible_from'].strftime('%m/%Y')} (δήλωση έως {dl}).")


def parepid_note(business: Optional[dict[str, Any]]) -> str:
    """Πιθανή υποχρέωση τέλους παρεπιδημούντων/ακαθαρίστων από τον ΚΑΔ (`parepid.py`) — '' αν δεν ταιριάζει ΚΑΔ.
    `business['kad_codes']` (deadlines._BUSINESS_SQL) ή `business['kads']`."""
    from . import parepid
    if not business:
        return ""
    kads = business.get("kads") or [k for k in (business.get("kad_codes") or "").split(",") if k]
    return parepid.note([k["code"] if isinstance(k, dict) else k for k in kads])


def is_private_person(b: Optional[dict[str, Any]]) -> bool:
    """Ιδιώτης: φυσικό πρόσωπο χωρίς (ενεργή) επιχείρηση — «ΙΔΙΩΤΗΣ» του Μητρώου, ή ατομική με διακοπή. Δεν έχει
    myDATA, μέλη ή ΑΠΔ (ρητό αίτημα 2026-10-27: η καρτέλα του δεν δείχνει αυτά)."""
    if not b or is_legal_entity(b.get("legal_form") or ""):
        return False
    return (b.get("activity_state") in ("none", "ceased")
            or "ΙΔΙΩΤ" in strip_accents(b.get("legal_form") or "").upper())


def liability(rule_id: str, business: Optional[dict[str, Any]], ref_month: str = "") -> tuple[str, str]:
    """('yes' | 'maybe' | 'no', αιτιολογία) για ΕΝΑΝ πελάτη, από ό,τι ήδη ξέρουμε (Μητρώο ΑΑΔΕ): κατάσταση δραστηριότητας,
    υπαγωγή ΦΠΑ, περίοδος ΦΠΑ / κατηγορία βιβλίων — και εργοδότης/Intrastat από την εισαγωγή Excel. 'maybe' = εξαρτάται από κάτι που ΔΕΝ ξέρουμε — ποτέ εφεύρεση."""
    if business is None:                              # γενική προβολή (χωρίς συγκεκριμένο πελάτη)
        return ("maybe", _MAYBE_REASON.get(rule_id, "")) if rule_id in _MAYBE_REASON or rule_id == "income_tax_return" else ("yes", "")
    state = business.get("activity_state") or ""
    if rule_id in _BUSINESS_ONLY:
        if state == "ceased":
            return "no", "διακοπή εργασιών" + (f" ({business['cease_date']})" if business.get("cease_date") else "")
        if state == "none":
            return "no", "ιδιώτης — χωρίς επιχειρηματική δραστηριότητα"
    if rule_id in ("vat_monthly", "vat_quarterly", "vies", "intrastat", "oss", "ioss"):
        if business.get("vat_subject") == 0:
            return "no", "δεν υπάγεται σε ΦΠΑ"
    if rule_id == "vat_quarter_option":
        opt = quarter_option(business)
        if not opt or not opt["by_law"]:
            return "no", "δεν είναι Β΄ κατηγορίας σε υποχρεωτικά μηνιαίο ΦΠΑ"
        if ref_month and opt["eligible_from"] > date(int(ref_month[:4]), int(ref_month[5:7]), 1):
            return "no", f"συμπληρώνει 24 μήνες από την έναρξη — επιλογή από {opt['eligible_from'].strftime('%m/%Y')}"
        return "yes", "μηνιαίος ΦΠΑ (Β΄ κατηγορία) — μπορεί να επανέλθει στο τρίμηνο"
    if _check_says_not_obligated(rule_id, business, ref_month):
        return "no", "ΑΑΔΕ: δεν έχει υποχρέωση υποβολής (έλεγχος υποβολών)"
    if rule_id in ("vat_monthly", "vat_quarterly"):
        want = "monthly" if rule_id == "vat_monthly" else "quarterly"
        period = vat_period(business)
        books = business.get("books_category") or ""
        if not period:
            return "maybe", "άγνωστη περίοδος ΦΠΑ (λείπει η κατηγορία βιβλίων)"
        source = " (περίοδοι Φ2 ΑΑΔΕ)" if business.get("vat_period_source") == "easycheck" else ""
        if period != want:
            return "no", "υποβάλλει " + ("τριμηνιαία" if period == "quarterly" else "μηνιαία") + " ΦΠΑ" + source
        if business.get("vat_subject") is None:
            return "maybe", "άγνωστη υπαγωγή ΦΠΑ"
        return "yes", (("μηνιαία" if want == "monthly" else "τριμηνιαία") + " ΦΠΑ" + source
                       + (f" · βιβλία {books}" if books and not source else ""))
    # Στοιχεία myDATA του μήνα αναφοράς (ρητά ναι/όχι) — υπερισχύουν των σημαιών του Excel. Μόνο για μήνες που
    # έχουν κατέβει· αλλιώς συνεχίζουμε όπως πριν.
    flag_key = _MYDATA_FLAG.get(rule_id)
    if flag_key and ref_month:
        value = ((business.get("mydata") or {}).get(ref_month) or {}).get(flag_key)
        if rule_id in _QUARTER_FLAGS:
            y, m = int(ref_month[:4]), int(ref_month[5:7])
            first = m - (m - 1) % 3
            vals = [((business.get("mydata") or {}).get(f"{y}-{mm:02d}") or {}).get(flag_key)
                    for mm in range(first, first + 3)]
            value = True if any(vals) else (False if any(v is not None for v in vals) else None)
        if value is not None:
            yes, no = _MYDATA_REASON[rule_id]
            if rule_id == "parepid_fee" and not value and parepid_note(business):
                # εστίαση/καταλύματα συχνά ΔΕΝ γράφουν το τέλος στο myDATA — ο ΚΑΔ κρατά την πιθανή υποχρέωση
                return "maybe", parepid_note(business)
            if rule_id == "intrastat" and value:
                info = (business.get("mydata") or {}).get(ref_month) or {}
                yes = f"Intrastat {info.get('intrastat_flows') or ''} — {info.get('intrastat_reason') or yes}".replace(
                    "Intrastat  — ", "Intrastat — ")
            if rule_id == "vies" and value:
                forms, known = vies_forms(business, ref_month)
                if known:
                    yes = "ενδοκοινοτικές " + " και ".join(VIES_FORM_LABEL[f] for f in forms) + " στο myDATA"
            return ("yes", yes) if value else ("no", no)
    # Τέλη που προκύπτουν ΜΟΝΟ από τα έσοδα του myDATA (8.2, ψηφιακό τέλος): αν έχουν κατέβει μήνες του πελάτη και δεν
    # εμφανίστηκαν ποτέ, ο μήνας που λείπει δεν γίνεται «υπό προϋποθέσεις» για 150+ πελάτες — «όχι» με αιτιολογία.
    if rule_id == "parepid_fee" and parepid_note(business):
        return "maybe", parepid_note(business)
    if rule_id in _MYDATA_ONLY and business.get("mydata_ever"):
        if not business["mydata_ever"].get(flag_key):
            return "no", "δεν έχει εντοπιστεί ποτέ στα έσοδα του myDATA"
    # Σημαίες από την εισαγωγή Excel (db v11): «Μισθοδοσία» = εργοδότης· στοιχεία Intrastat = υποβάλλει Intrastat.
    # NULL = άγνωστο -> μένει «υπό προϋποθέσεις». Ποτέ «όχι» για Intrastat από απουσία στοιχείων.
    if rule_id == "apd_teka":
        if business.get("employer") == 0:
            return "no", "χωρίς μισθοδοσία"
        if not business.get("teka"):                  # μόνο με ρητή επιλογή στο Προφίλ του πελάτη
            return "no", "δεν έχει επιλεγεί «ΑΠΔ ΤΕΚΑ» στο Προφίλ του πελάτη"
        yr = ((business.get("checks") or {}).get("years") or {}).get(
            ("ΑΠΔ_ΤΕΚΑ", int(ref_month[:4]) if ref_month else date.today().year))
        if yr in ("submitted", "not_submitted", "pending"):
            return "yes", "υποβάλλει ΑΠΔ ΤΕΚΑ (έλεγχος υποβολών e-ΕΦΚΑ)"
    if rule_id in ("apd", "ergani_e4", "fmy_wages") and business.get("employer") is not None:
        return ("yes", "εργοδότης (μισθοδοσία)") if business["employer"] else ("no", "χωρίς μισθοδοσία")
    if rule_id == "intrastat" and business.get("intrastat"):
        return "yes", "υποβάλλει Intrastat"
    if rule_id == "vies" and business.get("intrastat"):
        return "maybe", "υποβάλλει Intrastat — πιθανότατα έχει ενδοκοινοτικές συναλλαγές"
    if rule_id in _MAYBE_REASON:
        return "maybe", _MAYBE_REASON[rule_id]
    if rule_id == "income_tax_return":
        if state == "none":
            form = "Ε1"
        elif is_legal_entity(business.get("legal_form") or ""):
            form = "Έντυπο Ν"
        else:
            form = "Ε1/Ε3"
        return "yes", form
    return "yes", ""


SUBMISSION_LABEL = {"submitted": "Υποβλήθηκε", "pending": "Σε εκκρεμότητα", "not_submitted": "ΔΕΝ υποβλήθηκε",
                    "done": "Ολοκληρώθηκε (χειροκίνητα)", "": "Δεν έχει ελεγχθεί"}


def submission(rule_id: str, business: Optional[dict[str, Any]], ref_month: str) -> dict[str, Any]:
    """Υποβλήθηκε η δήλωση αυτής της προθεσμίας; {'status': submitted|pending|not_submitted|done|'', 'label', 'amounts',
    'manual'}. Πηγές: χειροκίνητο «ολοκληρώθηκε» (`obligation_done`) → έλεγχος υποβολών ΑΑΔΕ (`obligation_checks`, ανά
    μήνα αναφοράς). '' = δεν έχει ελεγχθεί / δεν υπάρχει μέθοδος."""
    out: dict[str, Any] = {"status": "", "label": SUBMISSION_LABEL[""], "amounts": {}, "manual": rule_id in MANUAL_ONLY,
                           "checks": [], "done_at": ""}
    if business is None:
        return out
    if (rule_id, ref_month) in (business.get("done") or set()):
        out.update(status="done", label=SUBMISSION_LABEL["done"],
                   done_at=(business.get("done_at") or {}).get((rule_id, ref_month), ""))
    periods = (business.get("checks") or {}).get("periods") or {}
    found = []
    for key in rule_checks(rule_id, business, ref_month):
        if rule_id == "income_tax_return":
            hit = periods.get((key, f"{check_year(rule_id, ref_month)}-12"))
        elif rule_id == "vat_quarterly":
            hit = periods.get((key, ref_month))                     # περίοδος τριμήνου = μήνας λήξης του
        else:
            hit = periods.get((key, ref_month))
            if not hit and rule_id in _QUARTER_FALLBACK and ref_month:
                y, m = int(ref_month[:4]), int(ref_month[5:7])
                hit = periods.get((key, f"{y}-{m + (3 - m % 3) % 3:02d}"))      # τρίμηνο που περιέχει τον μήνα
        if hit:
            found.append(hit)
    if out["status"] == "done":
        out["checks"] = found
        return out
    if not found:
        return out
    out["checks"] = found
    statuses = [f["status"] for f in found]
    status = ("not_submitted" if "not_submitted" in statuses and "submitted" not in statuses
              else "pending" if "pending" in statuses and "submitted" not in statuses else "submitted")
    if rule_id == "vies" and vies_forms(business, ref_month)[1] and status == "submitted" and (
            len(found) < len(rule_checks(rule_id, business, ref_month)) or set(statuses) != {"submitted"}):
        # το myDATA λέει ποιοι πίνακες οφείλονται — αν έχει και παραδόσεις και αποκτήσεις, θέλει και τους δύο
        status = "not_submitted" if "not_submitted" in statuses else "pending"
    amounts = next((f["amounts"] for f in found if f.get("amounts")), {})
    out.update(status=status, label=SUBMISSION_LABEL[status], amounts=amounts)
    return out


def amounts_text(amounts: dict[str, Any]) -> str:
    """«Χρεωστική · οφειλή 1.234,00 € · RF…» — σύντομη περιγραφή των ποσών μιας δήλωσης."""
    if not amounts:
        return ""

    def eur(v: float) -> str:
        return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
    parts = [amounts["result"]] if amounts.get("result") else []
    for key, label in (("total_due", "οφειλή"), ("lump_sum", "εφάπαξ"), ("deduction", "για έκπτωση"),
                       ("refund", "για επιστροφή")):
        if amounts.get(key) is not None and (key != "lump_sum" or amounts.get("total_due") is None):
            parts.append(f"{label} {eur(amounts[key])}")
    if amounts.get("installment") is not None and amounts.get("installment") != amounts.get("total_due"):
        parts.append(f"δόση {eur(amounts['installment'])}" + (f" έως {amounts['pay_by']}" if amounts.get("pay_by") else ""))
    elif amounts.get("pay_by"):
        parts.append(f"πληρωμή έως {amounts['pay_by']}")
    if amounts.get("debt_id"):
        parts.append(f"ταυτ. οφειλής {amounts['debt_id']}")
    if amounts.get("amended"):
        parts.append("τροποποιητική")
    return " · ".join(parts)


def is_legal_entity(legal_form: str) -> bool:
    f = legal_form.upper().replace(".", "").replace(" ", "")
    return any(k in f for k in ("ΑΕ", "ΙΚΕ", "ΕΠΕ", "ΟΕ", "ΕΕ", "ΚΟΙΝΣΕΠ", "ΑΣΤΙΚΗ", "ΣΥΝΕΤΑΙΡ", "ΣΩΜΑΤΕΙ", "ΚΟΙΝΟΠΡΑΞ",
                                "ΙΔΡΥΜΑ", "ΝΠΔΔ", "ΝΠΙΔ", "ΑΜΚΕ")) and "ΑΤΟΜΙΚ" not in f


@dataclass
class Occurrence:
    rule_id: str
    title: str
    date: date
    description: str
    source_url: str
    conditional: bool = False
    ref_month: str = ""                          # 'YYYY-MM' που αφορά η προθεσμία


def occurrences(start: date, end: date, business: Optional[dict[str, Any]] = None) -> list[Occurrence]:
    """Όλες οι προθεσμίες κανόνων με ημερομηνία στο [start, end]. `business`: dict με vat_subject, vat_period_type ή None (γενικό)."""
    out: list[Occurrence] = []
    for rule in RULES:
        seen: set[date] = set()
        cy, cm = _prev_month(start.year, start.month)      # η προθεσμία ενός μήνα λήξης μπορεί να πέφτει νωρίτερα/αργότερα
        last = (end.year, end.month)
        while (cy, cm) <= last:
            if cm in rule.due_months:
                d = rule.due(cy, cm)
                ref = ref_month(cy, cm, rule.ref_offset)
                status, _reason = liability(rule.id, business, ref)
                if start <= d <= end and d not in seen and status != "no":
                    seen.add(d)
                    period = rule.period(cy, cm) if rule.period else ""
                    out.append(Occurrence(rule.id, rule.title.format(period=period), d, rule.description,
                                          rule.source_url, status == "maybe", ref))
            cy, cm = (cy + 1, 1) if cm == 12 else (cy, cm + 1)
    out.sort(key=lambda o: (o.date, o.rule_id))
    return out
