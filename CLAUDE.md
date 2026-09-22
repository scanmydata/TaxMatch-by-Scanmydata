# CLAUDE.md — TaxMatch by ScanMyData

Τοπική desktop εφαρμογή (Windows) για λογιστικό γραφείο: παρακολουθεί φορολογικά/λογιστικά/εργατικά-μισθοδοσίας νέα, τα αντιστοιχίζει
στους πελάτες του γραφείου (ΚΑΔ, κατηγορία βιβλίων, ΦΠΑ, νομική μορφή) και δείχνει καθημερινό digest + ημερολόγιο υποχρεώσεων.
Reference: `PRODUCT_SPEC.md` και `MIGRATION_PLAN.md` (στο Downloads του χρήστη, δεν είναι στο repo). Μελλοντική ενσωμάτωση στη σουίτα **ScanMyData**.

**Native εφαρμογή (PySide6/Qt), ΟΧΙ Flask/webview** — ρητή απόφαση του χρήστη (2026-09-22): ίδιος τρόπος με το
αδελφό εργαλείο `mydata-etimologio-bridge/desktop` (Timologio Downloader), όχι τοπικός web server σε παράθυρο browser.

## Εντολές

```bash
.venv/Scripts/python.exe -m pytest                 # 216 tests, ~20s, χωρίς δίκτυο (και GUI tests, offscreen)
.venv/Scripts/python.exe -m taxmatch               # native GUI (PySide6)
.venv/Scripts/python.exe -m taxmatch --daily       # headless έλεγχος (ό,τι τρέχει το Task Scheduler)
.venv/Scripts/python.exe -m taxmatch --serve       # ΕΣΩΤΕΡΙΚΟ: Flask server για tests/ανάπτυξη — ΟΧΙ η πραγματική εφαρμογή
powershell -ExecutionPolicy Bypass -File packaging\build.ps1   # PyInstaller + Inno Setup -> installer-output\
```
Python **3.12** (`uv venv --python 3.12 .venv`).
Για δοκιμές με δικά σου δεδομένα: `TAXMATCH_DATA_DIR=<temp>` — ΠΟΤΕ τα πραγματικά δεδομένα του χρήστη.
GUI tests: `QT_QPA_PLATFORM=offscreen` (ρυθμίζεται αυτόματα από `tests/test_gui_smoke.py`) — δεν χρειάζεται οθόνη.

## Αρχιτεκτονική

```
taxmatch/
  config.py, db.py            data dir (%LOCALAPPDATA%\TaxMatch), SQLite WAL + versioned migrations (PRAGMA user_version)
  crypto.py, settings_store.py  Fernet `enc:1:`· .enckey ακάλυπτο ή προστατευμένο με ΠΡΟΑΙΡΕΤΙΚΟ κύριο κωδικό (Argon2id) — ίδιο
                              σχήμα με το timologio downloader· διαβάζει ακόμη τα παλιά DPAPI-wrapped .enckey (μόνο για μετάβαση).
                              Ρυθμίσεις: DB > env/.env > default.
  logs.py                     ένα κοινό αρχείο καταγραφής (`logs/taxmatch.log`, RotatingFileHandler, ώρα Ελλάδας) — ίδιο με το timologio.
  gui/                        NATIVE UI (PySide6) — η πραγματική εφαρμογή:
    app.py                     entry point: single-instance guard (QLocalServer), θέμα, ξεκλείδωμα κύριου κωδικού πριν ανοίξει το παράθυρο
    main_window.py             QMainWindow: SideMenu + QStackedWidget (Αρχική/Πελάτες/Ημερολόγιο/Νέα/Ρυθμίσεις)· καλεί ΑΠΕΥΘΕΙΑΣ
                               (χωρίς HTTP) τα business_profiles/matching/deadlines/pipeline/notices/settings_store κ.λπ.
    workers.py                 `run_task()`: οτιδήποτε αγγίζει δίκτυο/LLM/ΑΑΔΕ τρέχει σε QThread, ΠΟΤΕ στο UI thread
    client_dialog.py            «Νέος πελάτης»: ΑΦΜ + live VIES lookup (background) + προαιρετικοί κωδικοί TAXISnet
    client_detail_dialog.py     καρτέλα πελάτη: προφίλ/κωδικοί/προθεσμίες/matches, tabs
    news_dialog.py              προεπισκόπηση άρθρου/υποχρέωσης ΠΡΙΝ τον εξωτερικό σύνδεσμο (QDesktopServices.openUrl μόνο με ρητό κλικ)
    toast.py                    `toast(window, msg, level, ms)` — «side flash message» κάτω-δεξιά (ίδια ιδέα με το toast() του παλιού
                               web UI)· ΓΙ' ΑΥΤΟ αντί για `QMessageBox.information/warning` σε κάθε νέο κώδικα (βλ. §Κανόνες)
    theme.py, icons.py, widgets.py, table_filter.py, tour.py, unlock.py, busy.py, tray.py, i18n.py, side_menu.py, manual.py
                               ΑΝΤΙΓΡΑΦΗ+προσαρμογή από `mydata-etimologio-bridge/desktop/src/timologio/gui/` — ίδιο θέμα/εικονίδια/
                               φίλτρα στηλών (χωνί, στυλ Excel)/page tour/εγχειρίδιο PDF (QTextDocument+QPdfWriter, όχι εξωτερική βιβλιοθήκη)·
                               `i18n.install(app)` καλείται στο `app.py` (Qt Yes/No/OK ελληνικά + QLocale)· ΠΡΟΣΟΧΗ: το δικό μας
                               `datetime.strftime('%B'/'%A')` ΔΕΝ ακολουθεί το QLocale (άλλο μηχανισμό, της C runtime) — χρησιμοποίησε
                               `i18n.month_year()/long_date()/short_day()` για ελληνικά μηνών/ημερών, όχι strftime απευθείας
  web/, desktop.py             Flask — ΟΧΙ η εμφάνιση της εφαρμογής πλέον. Μένει ΜΟΝΟ ως: (α) επιφάνεια για τα tests του business-logic
                              layer (test_web.py κ.ά. ασκούν service/matching/deadlines μέσω HTTP, φθηνό να τα κρατήσουμε)·
                              (β) `taxmatch --serve` (εσωτερικό εργαλείο ανάπτυξης). Μην προσθέτεις νέα features ΜΟΝΟ εκεί — το gui/
                              πρέπει να τα έχει κι αυτό.
  ingestion/                  sources.py (feeds), rss_fetch.py (fetch+dedup+άρθρα), taxheaven_calendar.py (πλήρες ημερολόγιο — βλ. παρακάτω)
  extraction/                 prompts/scope_extraction.md, llm_extract.py (Groq/OpenRouter, δωρεάν-πρώτα με αυτόματη εναλλαγή, retries/rate-limit)
  scope.py                    σχήμα scope άρθρου + normalize() (ανθεκτικό σε άκυρη έξοδο LLM)
  matching/match.py, engine.py  καθαρό matching· rematch() στη βάση· digest()/digest_articles()
  business_profiles/          import_excel (πίνακας + μπλοκ «Κωδικοί Υπηρεσιών μέσω Internet», με κωδικούς TAXISnet), vies (επωνυμία από ΑΦΜ,
                              χωρίς key), credentials (κωδικοί TAXISnet ΑΝΑ πελάτη, κρυπτογραφημένοι), lookup_business_portal (ΓΕΜΗ),
                              lookup_aade (Μητρώο ΑΑΔΕ: ΚΑΔ, διακοπή, είδος, ΔΟΥ, ΦΠΑ, βιβλία), legal_form (νομική μορφή από επωνυμία, τελευταίο
                              fallback), vat_profile, service (αλυσίδα lookup: ΑΑΔΕ → ΓΕΜΗ fallback → VIES για επωνυμία)
  notices.py, jobs.py         ειδοποιήσεις (λάθος κωδικοί TAXISnet, αποτυχία LLM) — το `web/` τις δείχνει ως banner, το `gui/` ίδιο μέσω
                              `notices.collect()`. `jobs.py`: JobManager/LookupQueue του Flask· το gui/ έχει δικό του `workers.run_task`.
  obligations.py, deadlines.py  επαναλαμβανόμενες προθεσμίες από κανόνες (ΦΠΑ, VIES, Intrastat, OSS/IOSS, ΑΠΔ, Ε4, δήλωση εισοδήματος)
                              με ελληνικές αργίες· ενοποίηση με πλήρες ημερολόγιο ΑΑΔΕ/taxheaven + προθεσμίες άρθρων
  ingestion/filters.py        prefilter λέξεων-κλειδιών (φορολογικά ΚΑΙ εργατικά/μισθοδοσίας) για γενικά portals + αποδιπλασιασμός θεμάτων
  pipeline.py                 ingest -> enrich -> extract -> match, με cross-process lock (pipeline.lock)
  daily.py, scheduler_win.py  headless entry + Windows Task Scheduler (StartWhenAvailable, per-user, χωρίς admin)
packaging/                    entry.py (PyInstaller entry — μπαίνει στο taxmatch.gui.app.main), taxmatch.spec (PySide6, ΟΧΙ webview),
                              installer.iss, build.ps1, make_icons.py
```
Ροή: RSS -> `articles` (dedup url_hash) -> LLM -> `extracted_json` (scope) -> `matches` (article × business) -> UI.
Το πλήρες ημερολόγιο (`taxheaven_calendar.py`) πάει κατευθείαν σε `obligations_general`, ΧΩΡΙΣ LLM.

## Πλήρες ημερολόγιο — ΟΧΙ το `soft_dat.xml`
Επαληθεύτηκε (2026-09-22): το `soft_dat.xml` feed είναι **κυλιόμενο παράθυρο ~6 εβδομάδων** γύρω από το "τώρα" (π.χ. για
τον Ιούλιο δεν είχε ΚΑΜΙΑ εγγραφή, ενώ η σελίδα ημερολογίου δείχνει ~90). Το `taxheaven_calendar.py` διαβάζει απευθείας
`taxheaven.gr/calendar?m=..&y=..` (ενσωματωμένο `var calendarData = {...}` JSON στη σελίδα — πιο εύθραυστο από RSS αν
αλλάξει η σελίδα τους, αλλά είναι ό,τι δείχνει και η ίδια η σελίδα). «Κοντινοί» μήνες (`ROLLING_BACK`/`ROLLING_FORWARD`
γύρω από σήμερα) φρεσκάρονται σε κάθε έλεγχο· ο μήνας που βλέπει ο χρήστης ΤΩΡΑ κατεβαίνει on-demand
(`ensure_month_synced`, καλείται από το calendar page/view)· μακρινός μήνας: μία φορά αρκεί, δεν αλλάζει το παρελθόν.
Πίνακας `calendar_sync` (year_month, synced_at) θυμάται τι έχει ήδη κατέβει.

## Schema (db.py — πηγή αλήθειας)
`businesses`, `business_kad`, `articles`, `matches` (UNIQUE article_id+afm, `user_feedback` 1/-1/NULL), `obligations_general`,
`settings` (`encrypted` flag· και κατάσταση: `aade_office_status`, `llm_last_error`), `runs`, `client_credentials` (κωδικοί TAXISnet ανά πελάτη, `enc:1:`, χωριστά από το `businesses` ώστε
να μη διαρρέουν σε ερωτήματα λίστας), `calendar_sync` (ποιοι μήνες του πλήρους ημερολογίου έχουν κατέβει). Άρθρα: `extraction_status` = pending | done | failed | irrelevant | **skipped** (prefilter) |
**duplicate** (`duplicate_of`). `businesses`: `activity_state` (active|ceased|none), `cease_date`, `cease_reason`, `start_date`. Νέα αλλαγή schema = νέο στοιχείο στο `MIGRATIONS` (ποτέ επεξεργασία υπάρχοντος).

## Κανόνες
- **Ποτέ credentials** (TAXISnet, API keys) σε κώδικα, docs, comments, commits, tests, logs. Το repo είναι public. `.env`/`.enckey` είναι στο `.gitignore`.
- Ό,τι αγγίζει δίκτυο/LLM/ΑΑΔΕ στο `gui/` τρέχει σε background thread (`gui/workers.py: run_task`) — ΠΟΤΕ στο UI thread (παγώνει το παράθυρο).
- **Κανένα native μήνυμα browser στο web/** (νεκρό πλέον, αλλά αν αγγιχτεί): όχι `alert/confirm`, όχι φυσαλίδες επικύρωσης. Στο `gui/`: κανένα
  `QMessageBox`/απευθείας σύνδεσμος για νέα/προθεσμίες — πάντα `news_dialog.NewsDialog` πρώτα (προεπισκόπηση), ο σύνδεσμος ανοίγει μόνο με ρητό κλικ «Άνοιγμα συνδέσμου».
- **Ειδοποιήσεις στο `gui/` = `toast.toast()` (side flash message), ΟΧΙ `QMessageBox.information/warning`.** Εξαίρεση: `QMessageBox.question`
  παραμένει για επιβεβαιώσεις Ναι/Όχι πριν από κάτι μη αναστρέψιμο (διαγραφή πελάτη, εισαγωγή Excel) — αυτό ΔΕΝ είναι ειδοποίηση, είναι
  απόφαση που πρέπει σκόπιμα να μπλοκάρει. Επίσης εξαίρεση: `unlock.py`'s προειδοποιήσεις για κύριο κωδικό (ενεργοποίηση/αφαίρεση) μένουν
  blocking `QMessageBox` επίτηδες — το παράθυρο κλείνει αμέσως μετά (`super().accept()`), άρα ένα toast θα εξαφανιζόταν μαζί του πριν
  προλάβει να το διαβάσει κανείς για ένα μήνυμα ασφαλείας χωρίς επαναφορά.
- UI και μηνύματα στα **ελληνικά**. Ελληνικά κεφαλαία: το `str.upper()` ΚΡΑΤΑ τους τόνους (`'ί'.upper() == 'Ί'`)· χρησιμοποίησε `textutil.strip_accents()` πριν από συγκρίσεις.
- Τα ΚΑΔ συγκρίνονται ως ψηφία (`identifiers.kad_matches`), όχι ως string με τελείες.
- Δεν εφευρίσκουμε δεδομένα: άγνωστο κριτήριο ⇒ match με confidence 0.5 «να επιβεβαιωθεί», όχι σιωπηλή εξαίρεση ή ψευδές match.
- LLM: προτιμώνται ΠΑΝΤΑ δωρεάν μοντέλα (`llm_extract.is_free_model`/`pick_model(free_only=True)`)· αυτόματη εναλλαγή σε άλλο δωρεάν αν το
  τρέχον απορριφθεί. Ποτέ σιωπηλή μετάβαση σε πληρωμένο — μόνο ρητή πρόταση στον χρήστη (`recover_model`).
- Το `TableColumnFilter.apply()` (gui/table_filter.py) ξαναγράφει το `setRowHidden` ΟΛΩΝ των γραμμών βάσει ΜΟΝΟ των φίλτρων στηλών του — αν
  συνδυάζεται με άλλο φίλτρο (π.χ. αναζήτηση κειμένου), γράψε ΜΙΑ συνάρτηση που συνδυάζει όλα τα κριτήρια σε ένα πέρασμα (βλ.
  `main_window._apply_client_filter`) και σύνδεσε το `filtersChanged` σε ΑΥΤΗΝ, όχι στο `.apply()` απευθείας — αλλιώς η αναζήτηση σβήνεται.
- Windowed exe: uncaught exception ανοίγει dialog και κρεμάει το scheduled task — το `packaging/entry.py` το γράφει σε `logs/crash.log`.

## ΑΝΟΙΧΤΑ ΘΕΜΑΤΑ (να τα θυμίζεις στον χρήστη αντί να υποθέτεις)
1. **Credentials — ΑΠΟΦΑΣΙΣΤΗΚΕ: υποστηρίζονται και τα δύο, προαιρετικά.** Κωδικοί TAXISnet ανά πελάτη (ατομικά στο προφίλ/διάλογο,
   μαζικά από Excel) με fallback στον λογαριασμό γραφείου των Ρυθμίσεων. Ποτέ σε logs/CSV/HTML· η φόρμα δείχνει το όνομα
   χρήστη αλλά ΠΟΤΕ τον κωδικό. Προαιρετικός **κύριος κωδικός** (Ρυθμίσεις → Κύριος κωδικός / μενού ΑΣΦΑΛΕΙΑ) προστατεύει το `.enckey` με
   Argon2id — χωρίς αυτόν, το κλειδί είναι ακάλυπτο δίπλα στη βάση (προστατεύει μόνο από κλεμμένο αντίγραφο ΜΟΝΟ της βάσης).
2. **OS:** Windows-only (installer, Task Scheduler). Ο κώδικας του πυρήνα είναι cross-platform· τα Windows-specific σημεία είναι απομονωμένα (`scheduler_win.py`).
3. **Business Portal (ΓΕΜΗ): το σχήμα των ΚΑΔ ΔΕΝ έχει επαληθευτεί με ζωντανό key.** Χρησιμοποιείται πλέον ΜΟΝΟ ως fallback (η ΑΑΔΕ δίνει ΚΑΔ όταν υπάρχουν κωδικοί TAXISnet του πελάτη). Ο parser (`extract_kads`) είναι ανεκτικός και το ωμό JSON μένει στο `businesses.lookup_raw` — αν λείπουν ΚΑΔ, κοίτα εκεί.
4. **ΑΑΔΕ Μητρώο — ΕΠΑΛΗΘΕΥΤΗΚΕ ζωντανά (2 λογαριασμοί δοκιμής):** (α) ΚΑΔ = `GET .../infomytaxisnet/getMhtrwoDrastiriothtesEpixeir/{afm}` (JSON: `kwdikos`, `drasthriothta`, `eidos`=ΚΥΡΙΑ|…, `hmdiakophs`)·
   (β) **διακοπή = `hmdiakophs` της επιχείρησης** — το `katastashepixeirhshs` γράφει ΕΝΕΡΓΗ ΚΑΙ μετά τη διακοπή· (γ) φυσικό πρόσωπο με κλειστή ατομική = ΙΔΙΩΤΗΣ, χωρίς ΚΑΔ·
   (δ) **το Μητρώο δείχνει ΜΟΝΟ το ΑΦΜ του λογαριασμού που συνδέθηκε** — ξένο ΑΦΜ επιστρέφει `NoRegistry`. Άρα ο λογαριασμός γραφείου των Ρυθμίσεων ΔΕΝ φέρνει στοιχεία πελατών· χρειάζονται οι δικοί
   τους κωδικοί (ή ΓΕΜΗ). Δεν έχει δοκιμαστεί ο ρόλος «λογιστής με εξουσιοδοτήσεις». Νομικά πρόσωπα: το Μητρώο δεν έχει δοκιμαστεί (μόνο ατομικές)· η νομική μορφή (ΙΚΕ/ΑΕ) έρχεται από ΓΕΜΗ ή, αν λείπει, από την κατάληξη της επωνυμίας (`legal_form.py`). Δεν αποθηκεύονται προσωπικά πεδία (ταυτότητα, ημ. γέννησης) στο `lookup_raw`.
5. **LLM:** δωρεάν-πρώτα με αυτόματη εναλλαγή (§Κανόνες) — **δεν έχει δοκιμαστεί με ζωντανό key** (μόνο με fake HTTP στα tests) και δεν έχει μετρηθεί ακρίβεια εξαγωγής — δείγμα με 👍/👎.
   **«HTTP 403: άκυρο ή χωρίς δικαιώματα API key» ΜΟΝΙΜΑ (2026-09-22, πραγματικό key του χρήστη, Groq+dist):** ελέγχθηκε ο κώδικας
   (`llm_extract.py`: headers/Bearer/strip() σωστά, ίδιο σφάλμα σε dev python ΚΑΙ στο packaged dist — άρα ΔΕΝ είναι θέμα packaging, ο
   403 σημαίνει ότι το request έφτασε κανονικά στον πάροχο και απορρίφθηκε εκεί). Πιθανότερη αιτία: το ίδιο το κλειδί είναι
   άκυρο/ανακλήθηκε/έληξε ή λείπουν δικαιώματα στον λογαριασμό — όχι bug εδώ. Το μήνυμα σφάλματος τώρα δίνει οδηγίες (κενά,
   ανάκληση, «Αποθήκευση κλειδιών»). Αν επανέλθει: ζήτα να δοκιμάσει νέο key από https://console.groq.com (ή openrouter.ai/keys) και
   «Δοκιμή LLM» στις Ρυθμίσεις — αν ΞΑΝΑδώσει 403 με σίγουρα σωστό/φρέσκο key, τότε αξίζει βαθύτερη διερεύνηση.
6. **Πηγές:** ενεργές: taxheaven ×3 (νέα/αποφάσεις + πλήρες ημερολόγιο μέσω `taxheaven_calendar.py`), e-forologia ×6 (incl. Εργατικά), ot.gr Φορολογία, forologikanea.gr, naftemporiki (tag `forologia`), capital.gr (γενικό), eforiakoi.org (κυρίως συνδικαλιστικά).
   Τα γενικά (forologikanea/naftemporiki/capital/eforiakoi) περνούν από prefilter λέξεων-κλειδιών (`ingestion/filters.py`, ΚΑΙ φορολογικά ΚΑΙ εργατικά/μισθοδοσίας: εργασιακ, εργατικ, απολυσ, προσληψ, συλλογικ, κατωτατος μισθ, αδειας, ικα, ενσημ…) — αν χάνονται σχετικά άρθρα, διεύρυνε τα `KEYWORD_STEMS`.
   **forin.gr: ΜΗ ΔΙΑΘΕΣΙΜΗ** — Cloudflare challenge σε κάθε URL· δεν το παρακάμπτουμε (`Source.available=False`). Το ίδιο θέμα από δύο πηγές ενώνεται (Jaccard ≥ 0.8 στον τίτλο, 14 ημέρες).
6b. **Ημερολόγιο — κανόνες (`obligations.py`).** Επαληθεύτηκαν με αναζήτηση: ΦΠΑ = τελευταία εργάσιμη του επόμενου μήνα (τριμηνιαίες: 30/4, 31/7, 31/10, 31/1), VIES = 26η του επόμενου μήνα (αργία → επόμενη εργάσιμη, άρθρο 41 ΚΦΠΑ), Intrastat μαζί με VIES, OSS τριμηνιαία / IOSS μηνιαία (τέλος επόμενου μήνα), ΑΠΔ (τέλος επόμενου μήνα), Ε4 (1–31/10), δήλωση εισοδήματος (συνήθως 15/7). Το πλήρες ημερολόγιο ΑΑΔΕ/taxheaven (§ πάνω) υπερισχύει πάντα όταν έχει το ίδιο γεγονός.
   **Δεν επαληθεύτηκαν πλήρως:** το «τελευταία εργάσιμη» vs «επόμενη εργάσιμη» όταν το τέλος μήνα πέφτει Σαβ/Κυρ· ΦΜΥ, ΓΕΜΗ οικονομικές καταστάσεις, δόσεις φόρου δεν κωδικοποιήθηκαν.
   Οι εργάσιμες υπολογίζουν ελληνικές αργίες με Ορθόδοξο Πάσχα (Meeus)· δεν καλύπτονται τοπικές/έκτακτες αργίες.
7. **Φάση 7 (ML):** τα labeled δεδομένα συσσωρεύονται ήδη στο `matches.user_feedback`. Δεν έχει γραφτεί ML.
8. `data/kad_catalog.csv` (στατικός πίνακας ΚΑΔ 2008) δεν υπάρχει — τα ΚΑΔ εμφανίζονται με την περιγραφή που δίνει η ΑΑΔΕ/ΓΕΜΗ.
8b. **Excel με κωδικούς:** οι επικεφαλίδες TAXISnet του πλατιού πίνακα «Κωδικοί Υπόχρεων» δεν έχουν επιβεβαιωθεί με πραγματικό αρχείο (whole-string ταίριασμα ονομάτων). Αν ένα πραγματικό αρχείο δεν αναγνωριστεί, πρόσθεσε το alias στο `FIELD_ALIASES` + test.
9. **Ενσωμάτωση στη σουίτα ScanMyData:** σημεία επαφής σήμερα: `TAXMATCH_DATA_DIR`, CLI (`--daily`), το `taxmatch.business_profiles.service` (πελάτες) και `taxmatch.matching.engine.digest*`. Δεν υπάρχει ακόμη σταθερό public API/SSO — να σχεδιαστεί όταν οριστεί ο τρόπος ενσωμάτωσης.
10. Ο installer δεν έχει δοκιμαστεί σε καθαρό μηχάνημα (μόνο silent install/uninstall εδώ) και δεν είναι code-signed (θα εμφανιστεί SmartScreen).
   Τα ελληνικά του οδηγού είναι δικά μας `[Messages]` (δεν υπάρχει επίσημο Greek.isl).
   **Antivirus χτυπάει το exe ως malware — ΤΑΥΤΟΠΟΙΗΘΗΚΕ (2026-09-22): είναι το Acronis Cyber Protect, ΟΧΙ το Windows Defender.**
   Το μηχάνημα του χρήστη έχει και τα δύο εγκατεστημένα (`Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct`).
   Επιβεβαιώθηκε ζωντανά: το φρεσκοχτισμένο/εγκατεστημένο `TaxMatch.exe` εξαφανίστηκε από το `%LOCALAPPDATA%\Programs\TaxMatch\` λίγα
   δευτερόλεπτα μετά την εκκίνηση/αλληλεπίδραση — ΧΩΡΙΣ crash.log και ΧΩΡΙΣ καμία εγγραφή στο δικό του Defender log/`Get-MpThreat`, άρα
   είναι το «Active Protection» heuristic του Acronis (τυπικό ψευδο-θετικό: ανυπόγραφο exe που μόλις εγκαταστάθηκε γράφει αρχεία στο δικό
   του φάκελο data — μοιάζει heuristically με ransomware). Το `packaging/taxmatch.spec` ΗΔΗ έχει τους συνηθισμένους μετριασμούς
   (`upx=False` σε EXE ΚΑΙ COLLECT, πλήρες `version_info.txt`) — αυτοί δεν βοηθούν έναντι του Acronis. Η λύση είναι ΣΤΟ Acronis Cyber
   Protect (όχι στον κώδικα): επαναφορά από το quarantine του + εξαίρεση (exclusion) για το `%LOCALAPPDATA%\Programs\TaxMatch\` (και το
   dev `dist\TaxMatch\` για build/test). Δύο εναλλακτικές μόνιμες λύσεις παραμένουν: (α) **code signing certificate** (πληρωμένο)·
   (β) υποβολή false-positive στον πάροχο. Μην ξαναπροτείνεις αλλαγές στο spec γι' αυτό — έχει ήδη διερευνηθεί.
11. **Native GUI (2026-09-22, πρώτη έκδοση + διορθώσεις):** επαληθεύτηκε οπτικά σε πραγματικό παράθυρο Windows (dashboard, sidebar, tour,
   notices, πίνακας πελατών, ημερολόγιο με πραγματικά δεδομένα, διάλογος «Νέος πελάτης») και με 12 headless (offscreen) tests
   (`test_gui_smoke.py`). Το **εγχειρίδιο PDF επαληθεύτηκε ΟΠΤΙΚΑ (rendered→PNG) με το πραγματικό "windows" Qt platform** — σωστά ελληνικά,
   στοιχειοθεσία, λογότυπο (βρέθηκε και διορθώθηκε leftover τίτλος/creator "Λήψη Παραστατικών myDATA" από το timologio). ΠΡΟΣΟΧΗ: το ίδιο
   rendering με `QT_QPA_PLATFORM=offscreen` (όπως τρέχουν τα tests) βγάζει το κείμενο ως συμπαγή μαύρα τετράγωνα (tofu — απουσία
   γραμματοσειράς στο headless font backend, ΟΧΙ bug της εφαρμογής)· ΜΗΝ κρίνεις την ποιότητα του PDF από offscreen render, μόνο από
   πραγματικό "windows" platform. Το page tour βρέθηκε να δείχνει σε αόρατο widget μετά το calendar day-drill-down (§παρακάτω) — διορθώθηκε
   και τώρα καλύπτεται από `test_tour_steps_all_target_existing_visible_widgets`. Επίσης διορθώθηκε: `gui/tray.py` είχε tooltip "Timologio
   Downloader" leftover (τώρα `APP_TITLE`). **Δεν έχει δοκιμαστεί:** ο πλήρης κύκλος «Εισαγωγή από Excel» μέσα από το native UI (μόνο μέσω
   του business-logic layer), tray/minimize-to-tray διαδραστικά.
   Η σελίδα Ημερολόγιο έχει πλέον ΔΥΟ επίπεδα (2026-09-22): μηνιαία λίστα ανά ημέρα (`cal_days_list`) → κλικ σε ημέρα → ημερήσια λίστα
   (`cal_list`, με κουμπί «‹ Πίσω στον μήνα»)· `self._cal_view_day` (None = μηνιαία προβολή). Ίδια απλή αρχιτεκτονική (agenda, όχι grid μήνα).
