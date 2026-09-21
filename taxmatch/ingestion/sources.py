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
]

BY_ID = {s.id: s for s in SOURCES}
