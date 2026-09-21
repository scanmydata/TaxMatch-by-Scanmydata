"""Λίστα πηγών RSS (βλ. MIGRATION_PLAN §0.1). Ενεργοποίηση/απενεργοποίηση ανά πηγή από τις Ρυθμίσεις."""
from __future__ import annotations

from dataclasses import dataclass

TAXHEAVEN = "https://www.taxheaven.gr/bibliothiki/soft/xml"
EFOROLOGIA = "https://www.e-forologia.gr/_RSS"


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    kind: str = "news"            # news -> άρθρα προς LLM extraction· calendar -> δομημένο ημερολόγιο
    publisher: str = ""
    default_enabled: bool = True
    note: str = ""
    keywords: bool = False        # γενικό portal: μόνο άρθρα με φορολογικές/λογιστικές λέξεις-κλειδιά πάνε στο LLM
    max_items: int = 0            # >0: κρατά μόνο τα τόσα νεότερα (π.χ. feed με χιλιάδες ιστορικά άρθρα)
    available: bool = True        # False: δεν μπορεί να ληφθεί αυτόματα (π.χ. Cloudflare challenge) — δεν τρέχει ποτέ


SOURCES: list[Source] = [
    Source("taxheaven_new", "Taxheaven — Νέα", f"{TAXHEAVEN}/soft_new.xml", publisher="taxheaven.gr"),
    Source("taxheaven_law", "Taxheaven — Νέες αποφάσεις/εγκύκλιοι", f"{TAXHEAVEN}/soft_law.xml", publisher="taxheaven.gr"),
    Source("taxheaven_dat", "Taxheaven — Φορολογικό ημερολόγιο", f"{TAXHEAVEN}/soft_dat.xml", kind="calendar",
           publisher="taxheaven.gr", note="Δομημένες ημερομηνίες λήξης — δεν χρησιμοποιεί LLM."),
    Source("eforologia_7", "e-forologia — Τρέχοντα Φορολογικά", f"{EFOROLOGIA}/rss_id7.xml", publisher="e-forologia.gr",
           note="Πλήρες κείμενο εγκυκλίων/αποφάσεων ΑΑΔΕ — η πιο πρωτογενής πηγή."),
    Source("eforologia_1", "e-forologia — Επικαιρότητα", f"{EFOROLOGIA}/rss_id1.xml", publisher="e-forologia.gr"),
    Source("eforologia_4", "e-forologia — Επιχειρηματικά", f"{EFOROLOGIA}/rss_id4.xml", publisher="e-forologia.gr"),
    Source("eforologia_8", "e-forologia — Εργατικά", f"{EFOROLOGIA}/rss_id8.xml", publisher="e-forologia.gr"),
    Source("eforologia_9", "e-forologia — Αναπτυξιακά", f"{EFOROLOGIA}/rss_id9.xml", publisher="e-forologia.gr"),
    Source("eforologia_5", "e-forologia — Διεθνή", f"{EFOROLOGIA}/rss_id5.xml", publisher="e-forologia.gr",
           default_enabled=False, note="Σπάνια σχετικό με ελληνικές επιχειρήσεις — off από προεπιλογή."),
    Source("ot_forologia", "Οικονομικός Ταχυδρόμος — Φορολογία", "https://www.ot.gr/category/oikonomia/forologia/feed",
           publisher="ot.gr", note="Κατηγορία Οικονομία → Φορολογία (περιλαμβάνει και εργασιακά/ασφαλιστικά)."),
    Source("forologikanea", "Φορολογικά Νέα (forologikanea.gr)", "https://www.forologikanea.gr/RSS/news/",
           publisher="forologikanea.gr", keywords=True),
    Source("naftemporiki_tax", "Ναυτεμπορική — θέμα «Φορολογία»", "https://www.naftemporiki.gr/tag/forologia/feed/",
           publisher="naftemporiki.gr", keywords=True, note="Ετικέτα «φορολογία»· φιλτράρεται με λέξεις-κλειδιά."),
    Source("capital_all", "Capital.gr (όλα τα νέα)", "https://www.capital.gr/api/tags/all/", publisher="capital.gr",
           keywords=True, note="Γενικό feed — δεν υπάρχει feed μόνο για φορολογία· φιλτράρεται με λέξεις-κλειδιά."),
    Source("eforiakoi", "ΠΟΕ-ΔΟΥ (eforiakoi.org)", "https://www.eforiakoi.org/?format=feed&type=rss",
           publisher="eforiakoi.org", keywords=True, max_items=60,
           note="Συνδικαλιστική ενημέρωση εφοριακών· κυρίως εργασιακά, μόνο περιστασιακά εγκύκλιοι (φιλτράρεται). "
                "Το feed είναι ~4 MB."),
    Source("forin", "forin.gr", "https://www.forin.gr/", publisher="forin.gr", default_enabled=False, available=False,
           note="Προστατεύεται με Cloudflare (έλεγχος ανθρώπου)· δεν λαμβάνεται αυτόματα και δεν παρακάμπτεται."),
]

BY_ID = {s.id: s for s in SOURCES}
