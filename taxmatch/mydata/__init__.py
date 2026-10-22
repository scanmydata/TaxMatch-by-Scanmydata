"""myDATA (ΑΑΔΕ) ανά πελάτη: κωδικοί API, λήψη παραστατικών και PDF, και ό,τι προκύπτει από αυτά για τις υποχρεώσεις
(παρακρατούμενοι φόροι αμοιβών/εργολάβων, VIES, Intrastat). Port του `mydata-etimologio-bridge/desktop` (Timologio
Downloader) — ίδιο API, ίδιοι κανόνες ασφαλείας (το κλειδί πηγαίνει ΜΟΝΟ στο mydatapi.aade.gr, ποτέ στους παρόχους).

* `client.py`     — REST client (RequestDocs / RequestTransmittedDocs, pagination, 429/5xx)
* `parse.py`      — XML → παραστατικά (με γραμμές παρακράτησης και χώρα αντισυμβαλλομένου)
* `credentials.py`— κωδικοί myDATA ανά πελάτη (enc:1:)
* `store.py`      — αποθήκευση/λήψη μήνα (`mydata_docs`, `mydata_sync`)
* `pdfs.py`       — PDF από τον πάροχο (downloadingInvoiceUrl), ZIP μήνα
* `analysis.py`   — παρακράτηση φόρου ανά μήνα, ενδοκοινοτικές ροές, VIES / Intrastat
"""
