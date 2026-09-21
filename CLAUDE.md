# CLAUDE.md — TaxMatch by ScanMyData

Τοπική desktop εφαρμογή (Windows) για λογιστικό γραφείο: παρακολουθεί φορολογικά/λογιστικά/εργατικά νέα, τα αντιστοιχίζει
στους πελάτες του γραφείου (ΚΑΔ, κατηγορία βιβλίων, ΦΠΑ, νομική μορφή) και δείχνει καθημερινό digest + ημερολόγιο υποχρεώσεων.
Reference: `PRODUCT_SPEC.md` και `MIGRATION_PLAN.md` (στο Downloads του χρήστη, δεν είναι στο repo). Μελλοντική ενσωμάτωση στη σουίτα **ScanMyData**.

## Εντολές

```bash
.venv/Scripts/python.exe -m pytest                 # 150 tests, ~7s, χωρίς δίκτυο
.venv/Scripts/python.exe -m taxmatch               # GUI (pywebview/WebView2)
.venv/Scripts/python.exe -m taxmatch --browser     # GUI στον default browser
.venv/Scripts/python.exe -m taxmatch --serve       # μόνο server, τυπώνει URL με token
.venv/Scripts/python.exe -m taxmatch --daily       # headless έλεγχος (ό,τι τρέχει το Task Scheduler)
powershell -ExecutionPolicy Bypass -File packaging\build.ps1   # PyInstaller + Inno Setup -> installer-output\
```
Python **3.12** (`uv venv --python 3.12 .venv`). Η 3.14 του συστήματος δεν έχει wheels για pythonnet/pywebview.
Για δοκιμές με δικά σου δεδομένα: `TAXMATCH_DATA_DIR=<temp>` — ΠΟΤΕ τα πραγματικά δεδομένα του χρήστη.

## Αρχιτεκτονική

```
taxmatch/
  config.py, db.py            data dir (%LOCALAPPDATA%\TaxMatch), SQLite WAL + versioned migrations (PRAGMA user_version)
  crypto.py, settings_store.py  Fernet `enc:1:` + DPAPI-wrapped .enckey· ρυθμίσεις (DB > env/.env > default)
  ingestion/                  sources.py (feeds), rss_fetch.py (fetch+dedup+calendar)
  extraction/                 prompts/scope_extraction.md, llm_extract.py (Groq/OpenRouter, retries/rate-limit)
  scope.py                    σχήμα scope άρθρου + normalize() (ανθεκτικό σε άκυρη έξοδο LLM)
  matching/match.py, engine.py  καθαρό matching· rematch() στη βάση· digest()/digest_articles()
  business_profiles/          import_excel (πίνακας + μπλοκ «Κωδικοί Υπηρεσιών μέσω Internet», με κωδικούς TAXISnet), vies (επωνυμία από ΑΦΜ,
                              χωρίς key), credentials (κωδικοί TAXISnet ΑΝΑ πελάτη, κρυπτογραφημένοι), lookup_business_portal (ΓΕΜΗ),
                              lookup_aade (TAXISnet), vat_profile, service (αλυσίδα lookup: ΓΕΜΗ → ΑΑΔΕ → VIES)
  obligations.py, deadlines.py  επαναλαμβανόμενες προθεσμίες από κανόνες (ΦΠΑ, VIES, Intrastat, OSS/IOSS, ΑΠΔ, Ε4, δήλωση εισοδήματος)
                              με ελληνικές αργίες· ενοποίηση με ημερολόγιο taxheaven + προθεσμίες άρθρων
  ingestion/filters.py        prefilter λέξεων-κλειδιών για γενικά portals + αποδιπλασιασμός ίδιων θεμάτων ανάμεσα σε πηγές
  pipeline.py                 ingest -> enrich -> extract -> match, με cross-process lock (pipeline.lock)
  daily.py, scheduler_win.py  headless entry + Windows Task Scheduler (StartWhenAvailable, per-user, χωρίς admin)
  web/                        Flask (127.0.0.1 μόνο, token cookie, Host/Origin έλεγχοι), Jinja templates, vanilla JS.
                              ΕΜΦΑΝΙΣΗ = timologio downloader (mydata-etimologio-bridge/desktop): θέμα από gui/theme.py (static/css/app.css),
                              εικονίδια από gui/icons.py (web/icons.py), πλευρικό μενού/διάλογος «Νέος πελάτης»/πίνακας-κάρτα ίδια.
                              Κράτα τα tokens του CSS συγχρονισμένα αν αλλάξει το θέμα της σουίτας. ?theme=light|dark επιβάλλει θέμα.
  desktop.py, __main__.py     LocalServer + pywebview· CLI flags
packaging/                    entry.py (PyInstaller entry), taxmatch.spec, installer.iss, build.ps1, make_icons.py
```
Ροή: RSS -> `articles` (dedup url_hash) -> LLM -> `extracted_json` (scope) -> `matches` (article × business) -> UI.
Το ημερολόγιο (`soft_dat.xml`) πάει κατευθείαν σε `obligations_general`, ΧΩΡΙΣ LLM.

## Schema (db.py — πηγή αλήθειας)
`businesses`, `business_kad`, `articles`, `matches` (UNIQUE article_id+afm, `user_feedback` 1/-1/NULL), `obligations_general`,
`settings` (`encrypted` flag), `runs`, `client_credentials` (κωδικοί TAXISnet ανά πελάτη, `enc:1:`, χωριστά από το `businesses` ώστε
να μη διαρρέουν σε ερωτήματα λίστας). Άρθρα: `extraction_status` = pending | done | failed | irrelevant | **skipped** (prefilter) |
**duplicate** (`duplicate_of`). Νέα αλλαγή schema = νέο στοιχείο στο `MIGRATIONS` (ποτέ επεξεργασία υπάρχοντος).

## Κανόνες
- **Ποτέ credentials** (TAXISnet, API keys) σε κώδικα, docs, comments, commits, tests, logs. Το repo είναι public. `.env`/`.enckey` είναι στο `.gitignore`.
- Ο server ακούει ΜΟΝΟ 127.0.0.1· κάθε αίτημα χρειάζεται το per-launch token cookie. Μην χαλαρώσεις τον `guard()` στο `web/__init__.py`.
- UI και μηνύματα στα **ελληνικά**. Ελληνικά κεφαλαία: το `str.upper()` ΚΡΑΤΑ τους τόνους (`'ί'.upper() == 'Ί'`)· χρησιμοποίησε `textutil.strip_accents()` πριν από συγκρίσεις.
- Τα ΚΑΔ συγκρίνονται ως ψηφία (`identifiers.kad_matches`), όχι ως string με τελείες.
- Δεν εφευρίσκουμε δεδομένα: άγνωστο κριτήριο ⇒ match με confidence 0.5 «να επιβεβαιωθεί», όχι σιωπηλή εξαίρεση ή ψευδές match.
- Windowed exe: uncaught exception ανοίγει dialog και κρεμάει το scheduled task — το `packaging/entry.py` το γράφει σε `logs/crash.log`.

## ΑΝΟΙΧΤΑ ΘΕΜΑΤΑ (να τα θυμίζεις στον χρήστη αντί να υποθέτεις)
1. **Credentials — ΑΠΟΦΑΣΙΣΤΗΚΕ (2026-09-21): υποστηρίζονται και τα δύο, προαιρετικά.** Κωδικοί TAXISnet ανά πελάτη (ατομικά στο προφίλ/διάλογο,
   μαζικά από Excel ή «ίδιοι σε επιλεγμένους») με fallback στον λογαριασμό γραφείου των Ρυθμίσεων. Ποτέ σε logs/CSV/HTML· η φόρμα δείχνει το όνομα
   χρήστη αλλά ΠΟΤΕ τον κωδικό. Ο κίνδυνος παραμένει (τα κλειδιά ζουν στον υπολογιστή του γραφείου, DPAPI-wrapped)· η Φάση 6 (myDATA/ΕΡΓΑΝΗ ανά πελάτη) δεν έχει ξεκινήσει.
2. **OS:** Windows-only (installer, DPAPI, Task Scheduler). Ο κώδικας του πυρήνα είναι cross-platform· τα Windows-specific σημεία είναι απομονωμένα (`scheduler_win.py`, `crypto.py`).
3. **Business Portal (ΓΕΜΗ): το σχήμα των ΚΑΔ ΔΕΝ έχει επαληθευτεί με ζωντανό key.** Ο parser (`extract_kads`) είναι ανεκτικός και το ωμό JSON μένει στο `businesses.lookup_raw` (εμφανίζεται στο προφίλ πελάτη) — αν λείπουν ΚΑΔ, κοίτα εκεί και διόρθωσε τον parser + πρόσθεσε fixture στο `tests/test_business_profiles.py`.
4. **ΑΑΔΕ Μητρώο:** το `getMhtrwo*` για ΑΦΜ διαφορετικό από του λογαριασμού μπορεί να απαιτεί εξουσιοδότηση/πρόσβαση. Τα ΚΑΔ ΔΕΝ διαβάζονται από την ΑΑΔΕ (άγνωστα tags).
5. **LLM:** τα default μοντέλα (`llama-3.3-70b-versatile` στο Groq, `meta-llama/llama-3.3-70b-instruct` στο OpenRouter) δεν έχουν δοκιμαστεί με ζωντανό key· ρυθμίζονται από τις Ρυθμίσεις. Δεν έχει μετρηθεί ακρίβεια εξαγωγής σε πραγματικά άρθρα — να γίνει δείγμα με το feedback 👍/👎.
6. **Πηγές (2026-09-21):** ενεργές: taxheaven ×3, e-forologia ×6, ot.gr Φορολογία, forologikanea.gr (`/RSS/news/`), naftemporiki (tag `forologia`), capital.gr (`/api/tags/all/`, γενικό), eforiakoi.org (feed 4 MB, κυρίως συνδικαλιστικά).
   Τα γενικά (forologikanea/naftemporiki/capital/eforiakoi) περνούν από prefilter λέξεων-κλειδιών (`ingestion/filters.py`) — αν χάνονται σχετικά άρθρα, διεύρυνε τα `KEYWORD_STEMS`.
   **forin.gr: ΜΗ ΔΙΑΘΕΣΙΜΗ** — Cloudflare challenge σε κάθε URL· δεν το παρακάμπτουμε (`Source.available=False`). taxlive.gr: άγνωστο feed. Το ίδιο θέμα από δύο πηγές ενώνεται (Jaccard ≥ 0.8 στον τίτλο, 14 ημέρες).
6b. **Ημερολόγιο — κανόνες (`obligations.py`).** Επαληθεύτηκαν με αναζήτηση 2026-09: ΦΠΑ = τελευταία εργάσιμη του επόμενου μήνα (τριμηνιαίες: 30/4, 31/7, 31/10, 31/1), VIES = 26η του επόμενου μήνα (αργία → επόμενη εργάσιμη, άρθρο 41 ΚΦΠΑ), Intrastat μαζί με VIES, OSS τριμηνιαία / IOSS μηνιαία (τέλος επόμενου μήνα), ΑΠΔ (τέλος επόμενου μήνα), Ε4 (1–31/10), δήλωση εισοδήματος (συνήθως 15/7).
   **Δεν επαληθεύτηκαν πλήρως:** το «τελευταία εργάσιμη» vs «επόμενη εργάσιμη» όταν το τέλος μήνα πέφτει Σαβ/Κυρ (ΦΠΑ/ΑΠΔ/OSS — χρησιμοποιείται η προηγούμενη εργάσιμη, συμβατό με πηγές αλλά ΟΧΙ με πρωτογενές κείμενο νόμου)· ΦΜΥ, ΓΕΜΗ οικονομικές καταστάσεις, δόσεις φόρου δεν κωδικοποιήθηκαν (ανεπαρκής/αντικρουόμενη πληροφορία). Οι παρατάσεις δεν είναι γνωστές στους κανόνες — υπερισχύει το feed του taxheaven (τα rule events κρύβονται όταν υπάρχει το ίδιο γεγονός).
   Οι εργάσιμες υπολογίζουν ελληνικές αργίες με Ορθόδοξο Πάσχα (Meeus)· δεν καλύπτονται τοπικές/έκτακτες αργίες.
7. **Φάση 7 (ML):** τα labeled δεδομένα συσσωρεύονται ήδη στο `matches.user_feedback`. Δεν έχει γραφτεί ML.
8. `data/kad_catalog.csv` (στατικός πίνακας ΚΑΔ 2008) δεν υπάρχει — τα ΚΑΔ εμφανίζονται με την περιγραφή που δίνει το ΓΕΜΗ. **Χωρίς key ΓΕΜΗ δεν υπάρχουν ΚΑΔ** (το VIES δίνει μόνο επωνυμία/διεύθυνση) — τα ΚΑΔ-based matches θέλουν είτε key είτε χειροκίνητη/Excel εισαγωγή.
8b. **Excel με κωδικούς:** οι επικεφαλίδες TAXISnet του πλατιού πίνακα «Κωδικοί Υπόχρεων» δεν έχουν επιβεβαιωθεί με πραγματικό αρχείο (γίνεται whole-string ταίριασμα ονομάτων όπως «Όνομα χρήστη TAXISnet»/«Κωδικός TAXISnet»)· το μπλοκ «Taxis Net» ακολουθεί το documented format του downloader. Αν ένα πραγματικό αρχείο δεν αναγνωριστεί, πρόσθεσε το alias στο `FIELD_ALIASES` + test.
9. **Ενσωμάτωση στη σουίτα ScanMyData:** σημεία επαφής σήμερα: `TAXMATCH_DATA_DIR`, CLI (`--daily`, `--serve`), το `taxmatch.business_profiles.service` (πελάτες) και `taxmatch.matching.engine.digest*`. Δεν υπάρχει ακόμη σταθερό public API/SSO — να σχεδιαστεί όταν οριστεί ο τρόπος ενσωμάτωσης.
10. Ο installer δεν έχει δοκιμαστεί σε καθαρό μηχάνημα (μόνο silent install/uninstall εδώ) και δεν είναι code-signed (θα εμφανιστεί SmartScreen). Τα ελληνικά του οδηγού είναι δικά μας `[Messages]` (δεν υπάρχει επίσημο Greek.isl).
