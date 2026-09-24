"""Μοντέλο ανάλυσης κινδύνου πελάτη για τη δέουσα επιμέλεια (ν. 4557/2018).

Ο νόμος ΑΠΑΙΤΕΙ εκτίμηση κινδύνου ανά πελάτη (άρθρο 13 παρ. 9, άρθρο 35) με συνεκτίμηση των παραγόντων των
Παραρτημάτων Ι (χαμηλότερος κίνδυνος) και ΙΙ (υψηλότερος κίνδυνος), αλλά ΔΕΝ ορίζει βαθμούς. Οι βαθμοί εδώ είναι
επιλογή οργάνωσης του γραφείου — τεκμηριώνεται μία φορά στη γραπτή μεθοδολογία και εφαρμόζεται σε ΟΛΟΥΣ τους πελάτες.

Δύο ισοδύναμες κλίμακες (ίδιοι παράγοντες, ίδια κατάταξη στις συνήθεις περιπτώσεις — διαφέρει μόνο η αριθμητική):
* Μοντέλο Α: αφετηρία 0· οι παράγοντες του Παραρτήματος Ι αφαιρούν, του ΙΙ προσθέτουν.
      ΧΑΜΗΛΟΣ: άθροισμα ≤ 0 ΚΑΙ ≥1 παράγοντας Π.Ι ΚΑΙ κανένας Π.ΙΙ· ΥΨΗΛΟΣ: ≥ +6· αλλιώς ΜΕΤΡΙΟΣ.
* Μοντέλο Β: 4 άξονες × 0–25 = 0–100· σε κάθε άξονα ο βαρύτερος παράγοντας + 5 για κάθε επιπλέον (ταβάνι 25).
      ΧΑΜΗΛΟΣ: ≤ 15 ΚΑΙ ≥1 Π.Ι ΚΑΙ κανένας Π.ΙΙ· ΥΨΗΛΟΣ: ≥ 46· αλλιώς ΜΕΤΡΙΟΣ.
Και στα δύο: μία περίπτωση «ΥΠΕΡΙΣΧΥΕΙ» (ΠΕΠ, τρίτη χώρα υψηλού κινδύνου ΕΕ, κυρώσεις, τρομοκρατία) ⇒ ΥΨΗΛΟΣ
ανεξαρτήτως αθροίσματος.

Η κατάταξη του μοντέλου μπορεί μόνο να ΑΝΕΒΕΙ με ρητή αιτιολογία του αξιολογητή (ανθρώπινη κρίση) — ποτέ να κατέβει:
ένα μοντέλο που «διορθώνεται» προς τα κάτω ανά πελάτη είναι ακριβώς το cherry-picking που ψάχνει ο έλεγχος.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..identifiers import kad_digits
from ..textutil import strip_accents

LOW, MEDIUM, HIGH = "low", "medium", "high"
CATEGORIES = (LOW, MEDIUM, HIGH)
CATEGORY_LABEL = {LOW: "ΧΑΜΗΛΟΣ", MEDIUM: "ΜΕΤΡΙΟΣ", HIGH: "ΥΨΗΛΟΣ"}
DD_LABEL = {LOW: "Απλουστευμένη δέουσα επιμέλεια (άρθρο 15)",
            MEDIUM: "Συνήθης δέουσα επιμέλεια (άρθρο 13)",
            HIGH: "Αυξημένη δέουσα επιμέλεια (άρθρα 16–18)"}
DD_SHORT = {LOW: "Απλουστευμένη", MEDIUM: "Συνήθης", HIGH: "Αυξημένη"}

#: Διαστήματα επικαιροποίησης (μήνες) — ΔΕΝ προβλέπονται από τον νόμο· προτεινόμενα από το ενημερωτικό υλικό της
#: ΑΑΔΕ (χαμηλός: ανά διετία, μέτριος/υψηλός: τουλάχιστον ετησίως). Ρυθμίζονται στις Ρυθμίσεις (`aml_review_*`).
DEFAULT_REVIEW_MONTHS = {LOW: 24, MEDIUM: 12, HIGH: 12}

AXES = (
    ("A", "Άξονας Α — Τύπος και φύση του πελάτη"),
    ("B", "Άξονας Β — Γεωγραφία"),
    ("G", "Άξονας Γ — Υπηρεσίες και δραστηριότητα"),
    ("D", "Άξονας Δ — Συναλλαγές και δίαυλος"),
)
AXIS_LABEL = dict(AXES)


@dataclass(frozen=True)
class Factor:
    key: str
    axis: str
    text: str
    ref: str                      # Παράρτημα/διάταξη
    a: int                        # βαθμοί Μοντέλου Α
    b: Optional[int]              # βαθμοί Μοντέλου Β (None = γραμμή βάσης, δεν βαθμολογείται)
    annex: str = ""               # "I" | "II" | "" (επιλογή οργάνωσης, όχι παράγοντας Παραρτήματος)
    weighted: bool = True         # μετρά στο «βαρύτερος + 5/επιπλέον» του Μοντέλου Β
    par3: bool = False            # συναλλαγή της παρ. 3 άρθρου 16 (εξέταση + εντατικότερη παρακολούθηση ΠΑΝΤΑ)
    tip: str = ""


FACTORS: tuple[Factor, ...] = (
    # ---- Άξονας Α — πελάτης
    Factor("a_listed", "A", "Εισηγμένη σε ρυθμιζόμενη αγορά (ΕΕ ή ισοδύναμη) με υποχρεώσεις διαφάνειας", "Π.Ι-1α",
           -2, 0, "I", weighted=False),
    Factor("a_public", "A", "Δημόσια αρχή, ΝΠΔΔ ή δημόσια επιχείρηση", "Π.Ι-1β", -2, 0, "I", weighted=False),
    Factor("a_simple", "A", "Φυσικό πρόσωπο ή απλή οντότητα με σαφή, επαληθεύσιμη ταυτότητα και σταθερή δραστηριότητα",
           "βάση", 0, None, weighted=False),
    Factor("a_unusual", "A", "Η σχέση αναπτύσσεται σε ασυνήθιστες περιστάσεις", "Π.ΙΙ-1α", 3, 15, "II"),
    Factor("a_vehicle", "A", "Νομικό πρόσωπο/οντότητα-φορέας κατοχής προσωπικών περιουσιακών στοιχείων", "Π.ΙΙ-1γ",
           3, 15, "II"),
    Factor("a_nominee", "A", "Μέτοχοι για λογαριασμό τρίτου (nominee) ή μετοχές στον κομιστή", "Π.ΙΙ-1δ", 4, 20, "II"),
    Factor("a_cash", "A", "Επιχείρηση έντασης μετρητών", "Π.ΙΙ-1ε", 3, 15, "II",
           tip="π.χ. εστίαση, λιανικό με πολλά μετρητά, πρατήρια, περίπτερα"),
    Factor("a_complex", "A", "Ασυνήθιστη ή υπερβολικά πολύπλοκη ιδιοκτησιακή δομή για τη φύση της δραστηριότητας",
           "Π.ΙΙ-1στ", 4, 20, "II"),
    Factor("a_golden_visa", "A", "Υπήκοος τρίτης χώρας που ζητά άδεια διαμονής/ιθαγένεια έναντι επένδυσης", "Π.ΙΙ-1ζ",
           4, 20, "II"),
    Factor("a_ubo_unknown", "A", "Ο πραγματικός δικαιούχος δεν προσδιορίστηκε παρά την εξάντληση των μέσων "
           "(ορίστηκε ανώτερο διοικητικό στέλεχος)", "άρθρο 3 παρ. 17", 4, 25),

    # ---- Άξονας Β — γεωγραφία
    Factor("b_eu", "B", "Ελλάδα ή άλλο κράτος μέλος της ΕΕ", "Π.Ι-3α", -1, 0, "I"),
    Factor("b_third_ok", "B", "Τρίτη χώρα με τεκμηριωμένα αποτελεσματικό σύστημα AML/CFT (αξιολόγηση FATF)",
           "Π.Ι-3β–δ", 0, 5, "I"),
    Factor("b_third", "B", "Τρίτη χώρα χωρίς ιδιαίτερα ευρήματα, αλλά ούτε θετική αξιολόγηση", "—", 2, 10),
    Factor("b_corrupt", "B", "Χώρα με υψηλά επίπεδα διαφθοράς ή οργανωμένου εγκλήματος (αξιόπιστες πηγές)",
           "Π.ΙΙ-3β", 3, 15, "II"),
    Factor("b_weak", "B", "Χώρα χωρίς αποτελεσματικά συστήματα AML/CFT", "Π.ΙΙ-3α", 4, 20, "II"),

    # ---- Άξονας Γ — υπηρεσίες/δραστηριότητα
    Factor("g_basic_small", "G", "Μόνο τήρηση βιβλίων/δηλώσεις, μικρή κλίμακα, πλήρης διαφάνεια ταυτότητας",
           "Π.Ι-2 (κατ' αναλογία)", -1, 0, "I", weighted=False),
    Factor("g_basic", "G", "Τήρηση βιβλίων και δηλώσεις χωρίς ιδιαίτερα χαρακτηριστικά", "βάση", 0, None,
           weighted=False),
    Factor("g_corporate", "G", "Σύσταση εταιρειών, εταιρικές μεταβολές ή παροχή έδρας/διεύθυνσης", "—", 2, 10,
           tip="Υπηρεσίες παρόχου εταιρικών υπηρεσιών (TCSP) — τομέας αυξημένου κινδύνου κατά την ΕΕΚ"),
    Factor("g_sector", "G", "Τομέας αυξημένου κινδύνου κατά την Εθνική Εκτίμηση Κινδύνου (π.χ. ακίνητα, κατασκευές, "
           "τυχερά παίγνια)", "ΕΕΚ", 2, 10),
    Factor("g_private_banking", "G", "Ο πελάτης διατηρεί σχέση ιδιωτικής τραπεζικής", "Π.ΙΙ-2α", 2, 10, "II"),
    Factor("g_new_tech", "G", "Νέα προϊόντα/πρακτικές ή νέες τεχνολογίες (π.χ. κρυπτοστοιχεία)", "Π.ΙΙ-2ε", 3, 15, "II"),
    Factor("g_goods", "G", "Πετρέλαιο, όπλα, πολύτιμα μέταλλα, καπνός, πολιτιστικά/αρχαιολογικά αντικείμενα, "
           "ελεφαντοστό, προστατευόμενα είδη", "Π.ΙΙ-2στ", 3, 15, "II"),
    Factor("g_anonymity", "G", "Προϊόντα ή συναλλαγές που ευνοούν την ανωνυμία", "Π.ΙΙ-2β", 4, 20, "II"),

    # ---- Άξονας Δ — συναλλαγές/δίαυλος
    Factor("d_face", "D", "Φυσική παρουσία και τραπεζικές συναλλαγές", "βάση", 0, None, weighted=False),
    Factor("d_remote_eid", "D", "Εξ αποστάσεως, με αναγνωρισμένο μέσο ηλεκτρονικής ταυτοποίησης (π.χ. gov.gr/TAXISnet)",
           "Π.ΙΙ-2γ (εξουδετερωμένο)", 0, 0, "II", weighted=False),
    Factor("d_proxy", "D", "Ο πελάτης ενεργεί μέσω εξουσιοδοτημένου προσώπου ή πληρεξουσίου", "—", 1, 5),
    Factor("d_remote", "D", "Εξ αποστάσεως χωρίς τις διασφαλίσεις του Κανονισμού (ΕΕ) 910/2014 ή ισοδύναμες",
           "Π.ΙΙ-2γ", 3, 15, "II"),
    Factor("d_third_payer", "D", "Πληρωμές από τρίτους χωρίς προφανή σχέση με τον πελάτη", "Π.ΙΙ-2δ", 4, 20, "II"),
    Factor("d_unusual_tx", "D", "Συναλλαγές πολύπλοκες, ασυνήθιστα μεγάλες, ασυνήθιστης πρακτικής ή χωρίς προφανή "
           "οικονομικό/νόμιμο σκοπό", "άρθρο 16 παρ. 3", 5, 20, par3=True),
)
FACTORS_BY_KEY = {f.key: f for f in FACTORS}


@dataclass(frozen=True)
class Override:
    key: str
    text: str
    ref: str
    basis: str                    # γιατί υπερισχύει (για τη γραπτή μεθοδολογία)
    url: str = ""


OVERRIDES: tuple[Override, ...] = (
    Override("o_pep", "Πελάτης ή πραγματικός δικαιούχος ΠΕΠ, μέλος οικογένειας ή στενός συνεργάτης",
             "άρθρο 18 (ορισμοί: άρθρο 3 παρ. 9–11)", "Εκ του νόμου: το άρθρο 18 επιβάλλει τα μέτρα, χωρίς στάθμιση."),
    Override("o_high_risk_country", "Εγκατάσταση σε τρίτη χώρα υψηλού κινδύνου του καταλόγου της Ευρωπαϊκής Επιτροπής",
             "άρθρο 16 παρ. 1 και άρθρο 16Α", "Εκ του νόμου: τα αυξημένα μέτρα «εφαρμόζονται» υποχρεωτικά.",
             "https://finance.ec.europa.eu/financial-crime/anti-money-laundering-and-countering-financing-terrorism-international-level_en"),
    Override("o_sanctions", "Χώρα/πρόσωπο υπό κυρώσεις ή περιοριστικά μέτρα ΕΕ ή ΟΗΕ", "Π.ΙΙ-3γ",
             "Αυτοτελές έρεισμα: οι κανονισμοί κυρώσεων εφαρμόζονται άμεσα και η παραβίασή τους είναι βασικό αδίκημα.",
             "https://www.sanctionsmap.eu/"),
    Override("o_terror", "Χώρα που χρηματοδοτεί ή υποστηρίζει τρομοκρατικές δραστηριότητες", "Π.ΙΙ-3δ",
             "Επιλογή μεθοδολογίας (το Παράρτημα ΙΙ είναι ενδεικτικό) — δηλώνεται και αιτιολογείται στη γραπτή πολιτική.",
             "https://www.fatf-gafi.org/en/countries/black-and-grey-lists.html"),
)
OVERRIDES_BY_KEY = {o.key: o for o in OVERRIDES}

PEP_STATUS = (
    ("no", "Όχι ΠΕΠ"),
    ("domestic", "ΠΕΠ ημεδαπό"),
    ("foreign", "ΠΕΠ αλλοδαπό / διεθνούς οργανισμού"),
    ("family", "Μέλος οικογένειας ΠΕΠ"),
    ("associate", "Στενός συνεργάτης ΠΕΠ"),
    ("unknown", "Δεν έχει ελεγχθεί"),
)
PEP_LABEL = dict(PEP_STATUS)


@dataclass
class Result:
    sum_a: int = 0
    cat_a: str = MEDIUM
    subs_b: dict[str, int] = field(default_factory=dict)
    total_b: int = 0
    cat_b: str = MEDIUM
    n_annex_i: int = 0
    n_annex_ii: int = 0
    overrides: list[str] = field(default_factory=list)
    par3: bool = False
    notes: list[str] = field(default_factory=list)

    def category(self, model: str) -> str:
        return self.cat_b if model == "B" else self.cat_a

    def score(self, model: str) -> int:
        return self.total_b if model == "B" else self.sum_a


def compute(factors: Iterable[str], overrides: Iterable[str] = ()) -> Result:
    """Βαθμολόγηση και κατάταξη και στα δύο μοντέλα. Άγνωστα κλειδιά αγνοούνται (π.χ. παλιά αποθηκευμένη αξιολόγηση
    μετά από αλλαγή καταλόγου)."""
    chosen = [FACTORS_BY_KEY[k] for k in dict.fromkeys(factors) if k in FACTORS_BY_KEY]
    ovs = [k for k in dict.fromkeys(overrides) if k in OVERRIDES_BY_KEY]
    r = Result(overrides=ovs)
    r.sum_a = sum(f.a for f in chosen)
    r.n_annex_i = sum(1 for f in chosen if f.annex == "I")
    r.n_annex_ii = sum(1 for f in chosen if f.annex == "II")
    r.par3 = any(f.par3 for f in chosen)

    for axis, _label in AXES:
        mine = [f for f in chosen if f.axis == axis]
        weights = [f.b for f in mine if f.weighted and f.b is not None]
        if weights:
            v = min(25, max(weights) + 5 * (len(weights) - 1))
        elif axis in ("B", "D"):
            v = 0                                   # χωρίς ένδειξη: γεωγραφία/δίαυλος χωρίς επιβάρυνση
        else:
            # Πελάτης/υπηρεσίες: 0 μόνο αν δηλώθηκε ρητά παράγοντας χαμηλού κινδύνου — αλλιώς ουδέτερο 5
            v = 0 if any(f.b == 0 and not f.weighted for f in mine) else 5
        r.subs_b[axis] = v
    r.total_b = sum(r.subs_b.values())

    low_ok = r.n_annex_i > 0 and r.n_annex_ii == 0
    r.cat_a = HIGH if ovs or r.sum_a >= 6 else LOW if r.sum_a <= 0 and low_ok else MEDIUM
    r.cat_b = HIGH if ovs or r.total_b >= 46 else LOW if r.total_b <= 15 and low_ok else MEDIUM

    if r.par3:
        r.notes.append("Συναλλαγές της παρ. 3 του άρθρου 16: ανεξαρτήτως κατάταξης απαιτούνται γραπτή εξέταση ιστορικού "
                       "και σκοπού και εντατικότερη παρακολούθηση της σχέσης.")
    if r.n_annex_ii and r.cat_a != HIGH:
        r.notes.append("Συντρέχει παράγοντας του Παραρτήματος ΙΙ χωρίς υψηλή κατάταξη: γράψτε στην αιτιολογία γιατί, "
                       "στη συγκεκριμένη περίπτωση, δεν ανεβάζει τον κίνδυνο.")
    if r.sum_a <= 0 and not low_ok and not ovs:
        r.notes.append("Απλουστευμένη επιτρέπεται μόνο με τεκμηριωμένο παράγοντα του Παραρτήματος Ι και χωρίς κανέναν "
                       "του Παραρτήματος ΙΙ (άρθρο 15 παρ. 1) — γι' αυτό η κατάταξη είναι ΜΕΤΡΙΟΣ.")
    return r


def final_category(model_category: str, escalate_to: str = "") -> str:
    """Η τελική κατάταξη: του μοντέλου, ή ΥΨΗΛΟΤΕΡΗ αν την ανέβασε ο αξιολογητής — ποτέ χαμηλότερη."""
    if escalate_to in CATEGORIES and CATEGORIES.index(escalate_to) > CATEGORIES.index(model_category):
        return escalate_to
    return model_category


def overrides_from_profile(pep_status: str) -> set[str]:
    """ΠΕΠ (ή μέλος οικογένειας/στενός συνεργάτης) από την καρτέλα KYC ⇒ υποχρεωτικά η περίπτωση ΠΕΠ."""
    return {"o_pep"} if pep_status in ("domestic", "foreign", "family", "associate") else set()


# ---------------------------------------------------------------- προτάσεις από το προφίλ του πελάτη

# Προθέματα ΚΑΔ (ψηφία) -> (παράγοντας, αιτιολογία). ΠΡΟΤΑΣΕΙΣ μόνο: ο αξιολογητής επιβεβαιώνει ή τις αφαιρεί —
# ένας ΚΑΔ λέει τι ΜΠΟΡΕΙ να κάνει ο πελάτης, όχι τι κάνει (κανόνας «δεν εφευρίσκουμε δεδομένα»).
_KAD_HINTS: tuple[tuple[str, str, str], ...] = (
    ("56", "a_cash", "εστίαση"),
    ("5510", "a_cash", "ξενοδοχεία/καταλύματα"),
    ("4730", "a_cash", "πρατήριο καυσίμων"),
    ("4726", "a_cash", "λιανικό καπνού (περίπτερο)"),
    ("4932", "a_cash", "ταξί"),
    ("9602", "a_cash", "κομμωτήρια/κουρεία"),
    ("4520", "a_cash", "συνεργείο/πλυντήριο οχημάτων"),
    ("4711", "a_cash", "λιανικό τροφίμων"),
    ("0610", "g_goods", "εξόρυξη πετρελαίου"),
    ("192", "g_goods", "προϊόντα διύλισης πετρελαίου"),
    ("4671", "g_goods", "χονδρικό καυσίμων"),
    ("4730", "g_goods", "πρατήριο καυσίμων"),
    ("254", "g_goods", "κατασκευή όπλων/πυρομαχικών"),
    ("2441", "g_goods", "πολύτιμα μέταλλα"),
    ("3212", "g_goods", "κοσμήματα"),
    ("4648", "g_goods", "χονδρικό ρολογιών/κοσμημάτων"),
    ("4777", "g_goods", "λιανικό ρολογιών/κοσμημάτων"),
    ("12", "g_goods", "προϊόντα καπνού"),
    ("4635", "g_goods", "χονδρικό καπνού"),
    ("4726", "g_goods", "λιανικό καπνού"),
    ("47791", "g_goods", "αντίκες"),
    ("4778", "g_goods", "έργα τέχνης/λοιπό εξειδικευμένο λιανικό (να ελεγχθεί)"),
    ("68", "g_sector", "ακίνητα (μεσιτεία/διαχείριση/εκμετάλλευση)"),
    ("41", "g_sector", "κατασκευές κτιρίων"),
    ("43", "g_sector", "εξειδικευμένες κατασκευαστικές εργασίες"),
    ("92", "g_sector", "τυχερά παίγνια"),
    ("4511", "g_sector", "εμπόριο αυτοκινήτων"),
    ("6612", "g_sector", "χρηματιστηριακές/επενδυτικές υπηρεσίες"),
    ("6619", "g_sector", "βοηθητικές χρηματοπιστωτικές δραστηριότητες"),
    ("6492", "g_sector", "χορήγηση πιστώσεων"),
    ("6420", "a_vehicle", "εταιρεία συμμετοχών (holding)"),
    ("8211", "g_corporate", "υπηρεσίες γραφείου/έδρας"),
)
_TEXT_HINTS = (("κρυπτο", "g_new_tech", "κρυπτοστοιχεία"), ("crypto", "g_new_tech", "κρυπτοστοιχεία"),
               ("ενεχυροδαν", "a_cash", "ενεχυροδανειστήριο"), ("αργυραμοιβ", "a_cash", "αργυραμοιβός"))

_LISTED_OR_PUBLIC = ("ΝΠΔΔ", "Ν.Π.Δ.Δ", "ΔΗΜΟΣ ", "ΠΕΡΙΦΕΡΕΙΑ ", "ΥΠΟΥΡΓΕΙΟ")


def suggest(business: dict[str, Any]) -> dict[str, str]:
    """{factor_key: αιτιολογία} από τα ήδη γνωστά στοιχεία του πελάτη (ΚΑΔ, επωνυμία, νομική μορφή, έδρα)."""
    out: dict[str, str] = {}
    kads = business.get("kads") or []
    for k in kads:
        digits = kad_digits(k.get("code", ""))
        descr = strip_accents(str(k.get("descr", ""))).lower()
        for prefix, key, why in _KAD_HINTS:
            if digits.startswith(prefix):
                out.setdefault(key, f"ΚΑΔ {k.get('code')}: {why}")
        for needle, key, why in _TEXT_HINTS:
            if needle in descr:
                out.setdefault(key, f"ΚΑΔ {k.get('code')}: {why}")
    name = strip_accents(str(business.get("name", ""))).upper()
    if any(tag in name for tag in _LISTED_OR_PUBLIC):
        out.setdefault("a_public", "επωνυμία δημόσιου φορέα")
    # Ελληνικός ΑΦΜ με έδρα/διεύθυνση στην Ελλάδα -> ΕΕ (Π.Ι-3α) — πρόταση, η έδρα μπορεί να είναι αλλού
    out.setdefault("b_eu", "ελληνικός ΑΦΜ (έδρα στην Ελλάδα — επιβεβαιώστε)")
    out.setdefault("g_basic", "τήρηση βιβλίων/δηλώσεις (προεπιλογή)")
    if not any(key in out for key in ("a_public", "a_listed", "a_vehicle")):
        out.setdefault("a_simple", "προεπιλογή — επιβεβαιώστε ταυτότητα και δραστηριότητα")
    out.setdefault("d_face", "προεπιλογή")
    return out


def describe(result: Result, model: str) -> str:
    cat = result.category(model)
    score = f"{result.total_b}/100" if model == "B" else f"{result.sum_a:+d}"
    extra = f" · {len(result.overrides)} «ΥΠΕΡΙΣΧΥΕΙ»" if result.overrides else ""
    return f"{CATEGORY_LABEL[cat]} ({score}{extra}) → {DD_LABEL[cat]}"


# ---------------------------------------------------------------- προφίλ συναλλαγών -> παράγοντες

_TX_CHANNEL_FACTORS = {"cash_gt10k": ("a_cash", "μετρητά άνω των 10.000 €"), "crypto": ("g_new_tech", "πληρωμές σε κρυπτοστοιχεία")}
_TX_ACTIVITY_FACTORS = {
    "construction": ("g_sector", "κατασκευές"), "real_estate": ("g_sector", "ακίνητα"),
    "gambling": ("g_sector", "τυχερά παίγνια"), "jewelry": ("g_goods", "κοσμήματα/πολύτιμα μέταλλα"),
    "goods": ("g_goods", "πετρέλαιο/καπνός/όπλα/έργα τέχνης"), "pawn": ("a_cash", "ενεχυροδανειστήριο"),
    "crypto": ("g_new_tech", "κρυπτοστοιχεία"),
}
_TX_COUNTRY_FACTORS = {"gr": "b_eu", "eu": "b_eu", "fatf_ok": "b_third_ok", "third": "b_third", "low_tax": "b_third",
                       "weak": "b_weak"}
_TX_COUNTRY_OVERRIDES = {"eu_high_risk": "o_high_risk_country", "sanctions": "o_sanctions"}


def factors_from_transactions(transactions: Iterable[dict[str, Any]], fee_payment: str = "") -> tuple[
        dict[str, str], dict[str, str], list[str]]:
    """Από τα ρεύματα συναλλαγών του πελάτη (και τον τρόπο εξόφλησης της αμοιβής του γραφείου) προκύπτουν:
    (προτεινόμενοι παράγοντες {key: αιτιολογία}, περιπτώσεις «ΥΠΕΡΙΣΧΥΕΙ» {key: αιτιολογία}, προειδοποιήσεις).

    Σκόπιμα ΔΕΝ προσθέτει πόντους ανά γραμμή: ένα άθροισμα ανά συναλλαγή αραιώνει τον κίνδυνο (π.χ. εβδομαδιαία
    μετρητά σε ακίνητα βγαίνουν «χαμηλός») — εδώ κάθε ένδειξη γίνεται παράγοντας Παραρτήματος και κρίνεται από το μοντέλο.
    """
    from .content import TX_JUSTIFY_FROM, TX_LABELS
    factors: dict[str, str] = {}
    overrides: dict[str, str] = {}
    warnings: list[str] = []
    for i, tx in enumerate(transactions, 1):
        label = f"συναλλαγή {i}"
        channel, freq, activity = tx.get("channel", ""), tx.get("frequency", ""), tx.get("activity", "")
        country, amount = tx.get("country", ""), tx.get("amount", "")
        if channel in _TX_CHANNEL_FACTORS:
            key, why = _TX_CHANNEL_FACTORS[channel]
            factors.setdefault(key, f"{label}: {why}")
        if channel == "cash_1_10k" and freq in ("weekly", "daily"):
            factors.setdefault("a_cash", f"{label}: συχνά μετρητά ({TX_LABELS['frequency'][freq].lower()})")
        if channel == "cash_gt10k":
            factors.setdefault("d_unusual_tx", f"{label}: μετρητά άνω των 10.000 € — εξέταση ιστορικού/σκοπού")
            warnings.append(f"Συναλλαγή {i}: μετρητά άνω των 10.000 € — ελέγξτε και τα όρια χρήσης μετρητών της "
                            "φορολογικής νομοθεσίας και αν συντρέχει λόγος αναφοράς (άρθρο 22).")
        if channel.startswith("cash") and freq == "daily":
            warnings.append(f"Συναλλαγή {i}: καθημερινά μετρητά — προσοχή σε κατακερματισμό (structuring).")
        if activity in _TX_ACTIVITY_FACTORS:
            key, why = _TX_ACTIVITY_FACTORS[activity]
            factors.setdefault(key, f"{label}: {why}")
        if country in _TX_COUNTRY_FACTORS:
            factors.setdefault(_TX_COUNTRY_FACTORS[country], f"{label}: {TX_LABELS['country'][country]}")
        if country in _TX_COUNTRY_OVERRIDES:
            overrides.setdefault(_TX_COUNTRY_OVERRIDES[country], f"{label}: {TX_LABELS['country'][country]}")
        if country == "low_tax":
            warnings.append(f"Συναλλαγή {i}: κράτος με προνομιακό φορολογικό καθεστώς — ελέγξτε και τις ειδικές "
                            "υποχρεώσεις της φορολογικής νομοθεσίας.")
        if amount in TX_JUSTIFY_FROM and not (tx.get("justification") or "").strip():
            factors.setdefault("d_unusual_tx", f"{label}: μεγάλο ποσό χωρίς καταγεγραμμένη αιτιολόγηση")
            warnings.append(f"Συναλλαγή {i}: ποσό {TX_LABELS['amount'][amount]} χωρίς αιτιολόγηση σκοπού "
                            "(άρθρο 16 παρ. 3).")
    if fee_payment == "third":
        factors.setdefault("d_third_payer", "η αμοιβή του γραφείου εξοφλείται από τρίτο πρόσωπο")
    elif fee_payment == "cash":
        warnings.append("Η αμοιβή του γραφείου εξοφλείται με μετρητά — προτιμήστε ιχνηλάσιμο μέσο πληρωμής.")
    elif fee_payment == "foreign":
        warnings.append("Η αμοιβή εξοφλείται από λογαριασμό αλλοδαπής — επιβεβαιώστε τη χώρα (άξονας Β).")
    return factors, overrides, warnings


def factors_from_ubos(ubos: Iterable[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    """Οι πραγματικοί δικαιούχοι ενός νομικού προσώπου μεταφέρουν τον δικό τους κίνδυνο στον πελάτη: ΠΕΠ δικαιούχος
    ⇒ «ΥΠΕΡΙΣΧΥΕΙ» (άρθρο 18 — ο νόμος μιλά για «πελάτη ή πραγματικό δικαιούχο»)· χώρα κατοικίας/υπηκοότητας ⇒ άξονας Β.
    Ο πιο επιβαρυντικός δικαιούχος μετρά — όχι μέσος όρος."""
    factors: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for u in ubos:
        who = u.get("name") or u.get("afm") or "δικαιούχος"
        if u.get("pep") in ("domestic", "foreign", "family", "associate"):
            overrides.setdefault("o_pep", f"πραγματικός δικαιούχος {who}: {PEP_LABEL[u['pep']]}")
        c = u.get("country_risk", "")
        if c in _TX_COUNTRY_OVERRIDES:
            overrides.setdefault(_TX_COUNTRY_OVERRIDES[c], f"πραγματικός δικαιούχος {who}")
        elif c in ("third", "low_tax", "weak", "fatf_ok"):
            factors.setdefault(_TX_COUNTRY_FACTORS[c], f"πραγματικός δικαιούχος {who}")
    return factors, overrides
