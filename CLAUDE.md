# CLAUDE.md — TaxMatch by ScanMyData

Τοπική desktop εφαρμογή (Windows) για λογιστικό γραφείο: παρακολουθεί φορολογικά/λογιστικά/εργατικά-μισθοδοσίας νέα, τα αντιστοιχίζει
στους πελάτες του γραφείου (ΚΑΔ, κατηγορία βιβλίων, ΦΠΑ, νομική μορφή) και δείχνει καθημερινό digest + ημερολόγιο υποχρεώσεων.
Reference: `PRODUCT_SPEC.md` και `MIGRATION_PLAN.md` (στο Downloads του χρήστη, δεν είναι στο repo). Μελλοντική ενσωμάτωση στη σουίτα **ScanMyData**.

**Native εφαρμογή (PySide6/Qt), ΟΧΙ Flask/webview** — ρητή απόφαση του χρήστη (2026-09-22): ίδιος τρόπος με το
αδελφό εργαλείο `mydata-etimologio-bridge/desktop` (Timologio Downloader), όχι τοπικός web server σε παράθυρο browser.

## Εντολές

```bash
.venv/Scripts/python.exe -m pytest                 # 243 tests, χωρίς δίκτυο (και GUI tests, offscreen)
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
    toast.py                    `toast(window, msg, level, ms)` — «side flash message» πάνω-δεξιά (2026-09-23: μετακινήθηκε εκεί από
                               κάτω-δεξιά, ρητό αίτημα χρήστη· το παλιό web UI παραμένει κάτω-δεξιά)· ΓΙ' ΑΥΤΟ αντί για
                               `QMessageBox.information/warning` σε κάθε νέο κώδικα (βλ. §Κανόνες)
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
  pipeline.py                 ingest -> enrich -> extract -> match, με cross-process lock (pipeline.lock)· κάνει backup πριν από κάθε run
  backup.py                   αντίγραφα ασφαλείας βάσης (+ .enckey δίπλα) με το sqlite3 backup API (σωστό και σε WAL)· καλείται αυτόματα
                              πριν από κάθε `pipeline.run_pipeline` και πριν από «Εισαγωγή από Excel»· gui/ έχει «Αντίγραφο τώρα»/«Επαναφορά»
                              στις Ρυθμίσεις → Ασφάλεια (ίδια ιδέα με το backup.py του timologio downloader)
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
- **`run_task`'s `on_progress/on_done/on_error` παραδίδονται ΠΑΝΤΑ στο UI thread — ΜΗΝ υποθέσεις ότι απλή σύνδεση σήματος το κάνει αυτό
  αυτόματα.** Βρέθηκε πραγματικό, συστημικό bug (2026-09-22): το παλιό `Task` ήταν plain Python class (όχι `QObject`) και τα
  `on_done=done`/`on_error=lambda ...` που περνά κάθε caller είναι closures/lambdas χωρίς δικό τους QObject thread affinity — η Qt/PySide
  ΔΕΝ μπορεί να προσδιορίσει αν πρέπει να τα παραδώσει queued σε τέτοια περίπτωση, άρα τα εκτελούσε ΣΥΓΧΡΟΝΑ μέσα στο background thread
  (επαληθεύτηκε πειραματικά: `worker.finished.connect(plain_function)` τρέχει το `plain_function` στο thread του `worker`, ΟΧΙ στο UI
  thread — ούτε καν με ρητό `Qt.ConnectionType.QueuedConnection`, γιατί χωρίς QObject receiver η Qt δεν έχει event loop στόχο). Αυτό
  σήμαινε ότι widgets (`toast()`, `combo.clear()/addItem()`) χτίζονταν/άλλαζαν από μη-UI thread σε ΚΑΘΕ `run_task` σε όλη την εφαρμογή —
  αυτό παγώνει/χαλάει πραγματικό (όχι offscreen) Windows παράθυρο, αν και τα offscreen tests δεν το έπιαναν (κανένα existing test δεν
  έλεγχε ΣΕ ΠΟΙΟ thread έτρεχε το callback, μόνο ότι έφτανε). Πιθανή αιτία πίσω από «κολλάει η Δοκιμή LLM/Ανανέωση μοντέλων» ΚΑΙ πίσω από
  το «μόνιμο 403 banner» (`toast(..., ms=0)` για danger-level errors δημιουργημένο από background thread μπορεί να μείνει κολλημένο).
  **Διορθώθηκε:** το `Task` είναι πλέον `QObject` που ΠΑΡΑΜΕΝΕΙ στο calling (UI) thread (ποτέ `moveToThread`) — τα signals του `worker`
  συνδέονται σε bound methods ΑΥΤΟΥ του `Task` (η Qt ΜΠΟΡΕΙ να βρει thread affinity από πραγματικό QObject άρα σωστά queued), κι αυτές οι
  μέθοδοι με τη σειρά τους καλούν το callable του caller — άρα πλέον ΕΓΓΥΗΜΕΝΑ στο UI thread. Regression test:
  `test_run_task_callbacks_run_on_the_ui_thread_not_the_worker_thread` (ελέγχει ρητά `threading.get_ident()`).
- **Το `work()` που περνάς στο `run_task` ΠΟΤΕ `self.conn`/`main.conn` — δικιά του σύνδεση.** Δεύτερο, ξεχωριστό cross-thread bug
  (2026-09-22, ίδια μέρα με το παραπάνω αλλά διαφορετική αιτία): αρκετά σημεία (`ClientDetailDialog._test_credentials/_start_lookup`,
  `MainWindow._start_lookup/reload_calendar/_test_llm/_run_check`) περνούσαν το **ίδιο** `sqlite3.Connection` που έχει ήδη ανοιχτεί στο UI
  thread (`self.conn = dbmod.connect()` στο `__init__`) μέσα σε το `work()` — που εκτελείται ΠΑΝΤΑ σε background `QThread`. Το sqlite3
  απαγορεύει χρήση μιας σύνδεσης από ΑΛΛΟ thread από αυτό που τη δημιούργησε («SQLite objects created in a thread can only be used in
  that same thread») — ορατό στον χρήστη ως μόνιμο popup toast πάνω σε κάθε τέτοιο κουμπί (ανανέωση στοιχείων πελάτη, Δοκιμή LLM,
  ανανέωση λίστας μοντέλων, Έλεγχος τώρα). **Διορθώθηκε:** κάθε τέτοιο `work()` ανοίγει ΔΙΚΗ του `dbmod.connect()` στην αρχή και την
  κλείνει σε `finally` (ή, για το `_run_check`, απλά ΔΕΝ περνά `conn=self.conn` στο `pipeline.run_pipeline`, ώστε να ανοίξει τη δική
  του). Ασφαλές: WAL + autocommit (`isolation_level=None`), οπότε γραφές από τη background σύνδεση είναι αμέσως ορατές στο `self.conn`
  όταν το UI ξαναδιαβάσει μετά το `on_done`. Regression tests: `test_start_lookup_uses_its_own_db_connection_not_the_uis`,
  `test_test_llm_uses_its_own_db_connection_not_the_uis` (`tests/test_gui_smoke.py`) — τρέχουν το πραγματικό background `QThread`,
  όχι mock, και ελέγχουν ότι ΔΕΝ φτάνει μήνυμα με «thread»/«sqlite» στον χρήστη.
- **Κανένα native μήνυμα browser στο web/** (νεκρό πλέον, αλλά αν αγγιχτεί): όχι `alert/confirm`, όχι φυσαλίδες επικύρωσης. Στο `gui/`: κανένα
  `QMessageBox`/απευθείας σύνδεσμος για νέα/προθεσμίες — πάντα `news_dialog.NewsDialog` πρώτα (προεπισκόπηση), ο σύνδεσμος ανοίγει μόνο με ρητό κλικ «Άνοιγμα συνδέσμου».
- **Ειδοποιήσεις στο `gui/` = `toast.toast()` (side flash message), ΟΧΙ `QMessageBox.information/warning`.** Εξαίρεση: `QMessageBox.question`
  παραμένει για επιβεβαιώσεις Ναι/Όχι πριν από κάτι μη αναστρέψιμο (διαγραφή πελάτη, εισαγωγή Excel) — αυτό ΔΕΝ είναι ειδοποίηση, είναι
  απόφαση που πρέπει σκόπιμα να μπλοκάρει. Επίσης εξαίρεση: `unlock.py`'s προειδοποιήσεις για κύριο κωδικό (ενεργοποίηση/αφαίρεση) μένουν
  blocking `QMessageBox` επίτηδες — το παράθυρο κλείνει αμέσως μετά (`super().accept()`), άρα ένα toast θα εξαφανιζόταν μαζί του πριν
  προλάβει να το διαβάσει κανείς για ένα μήνυμα ασφαλείας χωρίς επαναφορά.
- Όλα τα toasts (και τα danger/σφάλμα) εξαφανίζονται μόνα τους (`_DEFAULT_MS`, 5" για danger) — ΠΟΤΕ πια `ms=0` σε νέο κώδικα (ο χρήστης
  το ζήτησε ρητά, 2026-09-22: πριν έμεναν κολλημένα ώσπου να πατηθεί το «×»). Το `×` παραμένει πάντα διαθέσιμο για χειροκίνητο κλείσιμο.
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
5. **LLM:** δωρεάν-πρώτα με αυτόματη εναλλαγή (§Κανόνες) — **επαληθεύτηκε ζωντανά με πραγματικό OpenRouter key (2026-09-23)**, η σύνδεση δουλεύει· δεν έχει ακόμη μετρηθεί ακρίβεια εξαγωγής — δείγμα με 👍/👎.
   **Πολύ μεγάλα άρθρα (2026-09-23, ρητό αίτημα χρήστη): δεν κόβονται πλέον στα τυφλά.** Πριν, το κείμενο προς το
   LLM κοβόταν στους πρώτους `FULL_TEXT_MAX=5000` χαρακτήρες (`body[:FULL_TEXT_MAX]`) — για ένα νομοσχέδιο
   ~50.000 χαρακτήρων (επαληθεύτηκε ζωντανά, taxheaven «89 λεπτά ανάγνωση»), το LLM έβλεπε μόνο ~10% του άρθρου
   και ΠΟΤΕ την τελική διάταξη/έναρξη ισχύος. Τώρα, σε δύο επίπεδα:
   (α) **Αποθήκευση** (`llm_extract.fetch_full_text`, όριο `STORED_TEXT_MAX=8000`): παίρνει ΠΡΩΤΑ ολόκληρο το
   κείμενο (χωρίς όριο) και το περικόπτει έξυπνα με `_smart_truncate` (κρατά αρχή+τέλος, όχι μόνο αρχή) πριν
   αποθηκευτεί στο `articles.full_text` — πριν, το `html_to_text(..., max_chars)` πετούσε το τέλος αμετάκλητα
   πριν καν φτάσει στο δεύτερο στάδιο.
   (β) **Μήνυμα προς το LLM** (`extract_article`): αν το (ήδη smart-truncated) αποθηκευμένο κείμενο είναι ΑΚΟΜΗ πάνω
   από `FULL_TEXT_MAX`, αντί να το ξανακόψουμε εμείς, ζητάμε ΠΕΡΙΛΗΨΗ από το ΙΔΙΟ LLM (`summarize_long_text`,
   ξεχωριστό system prompt με ρητή οδηγία να ΜΗΝ παραλείπει ημερομηνίες/ΚΑΔ/ποσά/αριθμούς νόμων) και στέλνουμε
   ΑΥΤΗΝ στο κύριο prompt εξαγωγής — το αποθηκευμένο `full_text` στη βάση ΔΕΝ αλλάζει, μόνο το μήνυμα προς το LLM.
   Σφάλματα HTTP στην κλήση περίληψης (401/403/429/δίκτυο) διαδίδονται ΚΑΝΟΝΙΚΑ ως `LLMError` — το
   `extract_pending` τα χειρίζεται ήδη ενιαία (stop/rotate model/backoff), άρα ΔΕΝ χρειάστηκε νέο error-handling
   path, μόνο ένα ακόμη `client.complete_json` πριν το κανονικό. Σε άκυρη/κενή απάντηση JSON από την περίληψη
   πέφτει σιωπηλά πίσω στο `_smart_truncate` (δεν μπλοκάρει ποτέ την εξαγωγή). Κόστος: μία ΕΠΙΠΛΕΟΝ κλήση LLM
   ΜΟΝΟ για άρθρα πάνω από `FULL_TEXT_MAX` (η πλειοψηφία των άρθρων δεν την ενεργοποιεί καθόλου).
   **Παράλληλα διορθώθηκε ένα σχετικό, πραγματικό πρόβλημα ποιότητας δεδομένων:** μερικά `/circulars/...` άρθρα
   (νομολογία, βλ. §6 Πηγές παρακάτω) είναι πληρωμένη συνδρομή — η σελίδα δείχνει μια ελεύθερη «Περίληψη» και μετά
   διαφημιστικό κείμενο («Αποκτήστε πρόσβαση — 100,00€/έτος»). Χωρίς φίλτρο, αυτό το «100,00€/έτος» θα έμπαινε στο
   κείμενο σαν να ήταν πραγματικό ποσό/πρόστιμο του άρθρου. Το `fetch_full_text`/`_strip_paywall_upsell` το κόβει
   τώρα, κρατώντας μόνο την ελεύθερη «Περίληψη» πριν το marker.
   **Προεπιλεγμένος πάροχος πλέον OpenRouter, όχι Groq** (2026-09-22, ρητό αίτημα χρήστη — το Groq δεν πρέπει να είναι «βασικό»/υποχρεωτικό,
   βλ. και το μόνιμο 403 παρακάτω): `settings_store.DEFINITIONS["llm_provider"].default`.
   **Ο κατάλογος δωρεάν (`:free`) μοντέλων του OpenRouter αλλάζει ΣΥΧΝΑ — μην εμπιστεύεσαι κρυφά συγκεκριμένα ids.** Επαληθεύτηκε
   ζωντανά (2026-09-23, πραγματικό key): το πρώτο default που είχαμε βάλει (`meta-llama/llama-3.3-70b-instruct:free`) και ολόκληρη η
   τότε λίστα `PREFERRED_MODELS["openrouter"]` είναι ΠΛΕΟΝ HTTP 404 («This model is unavailable for free. The paid version is available
   now — use this slug instead: …» — η δωρεάν εκδοχή καταργήθηκε, η πληρωμένη μένει). Αντικαταστάθηκαν με μοντέλα που δοκιμάστηκαν
   ζωντανά την ίδια μέρα (`liquid/lfm-2.5-2.6b:free`, `nvidia/nemotron-3-super-120b-a12b:free`, …· δες τα σχόλια στο ίδιο το
   `PREFERRED_MODELS`) — αλλά ΚΙ ΑΥΤΑ θα ξεπεραστούν κάποια στιγμή. Το πραγματικό safety net είναι το live `GET /models` μέσα στο
   `pick_model`/`recover_model`, ΟΧΙ η στατική λίστα· αν ξαναδεις 404 «unavailable for free» σε νέο μοντέλο, αυτό ΕΙΝΑΙ αναμενόμενο, όχι
   bug — απλώς ξαναβρές ζωντανά μοντέλα (`GET https://openrouter.ai/api/v1/models` με το key) και ενημέρωσε τη λίστα/το default.
   Το `_is_model_error` τώρα αναγνωρίζει και αυτή τη ΝΕΑ διατύπωση («unavailable for free», «use this slug instead») — πριν δεν την
   αναγνώριζε (μόνο τις παλιότερες: model_not_found/does not exist/decommissioned/…), άρα ΔΕΝ πυροδοτούσε ποτέ το `recover_model` για
   αυτό το σενάριο και έδειχνε γενικό `bad_response` αντί για αυτόματη διόρθωση.
   Το dropdown μοντέλων (`gui/main_window.py: _sync_model_combo`) δείχνει πάντα το τρέχον/αυτόματα επιλεγμένο μοντέλο σαν κανονική
   καταχώρηση, ταξινομημένο δωρεάν-πρώτα.
   **HTTP 402 και 429 με λέξεις quota/credit** (`llm_extract._is_quota_error`) ταξινομούνται ως `LLMError(kind="credits")` ξεχωριστά
   από απλό `rate_limit` (προσωρινή καθυστέρηση). **Ξεχωριστή κατηγορία, βρέθηκε ζωντανά την ίδια μέρα**
   (`llm_extract._is_upstream_congestion`): η ΠΛΕΙΟΨΗΦΙΑ των 429 σε δωρεάν OpenRouter μοντέλα ΔΕΝ είναι όριο λογαριασμού, είναι
   στιγμιαίος συνωστισμός στον **upstream supplier ΑΥΤΟΥ του συγκεκριμένου μοντέλου** — πραγματικό body:
   `{"error":{"code":429,"metadata":{"raw":"X is temporarily rate-limited upstream…","provider_name":"ModelRun",
   "limit_source":"upstream_provider_shared_pool"}}}`. Ένα PREFERRED αρχικό συμπέρασμα (ότι το όριο είναι πάντα ανά λογαριασμό στο
   OpenRouter, άρα αλλαγή μοντέλου δεν βοηθά) αποδείχτηκε ΛΑΘΟΣ σε ζωντανή δοκιμή — άλλο δωρεάν μοντέλο (άλλος upstream) συνήθως
   δουλεύει κανονικά. Το `extract_pending`/`gui/_test_llm` δοκιμάζουν πλέον αυτόματα άλλο δωρεάν μοντέλο σε ΚΑΘΕ πάροχο σε «credits»
   σφάλμα (Groq ΚΑΙ OpenRouter) — ακόμη κι αν σπάνια είναι πραγματικά ανά λογαριασμό, η εναλλαγή κοστίζει μόνο ένα παραπάνω αίτημα.
   **Το `gui/` δεν καθάριζε ποτέ το `llm_last_error`** (2026-09-23, βρέθηκε ζωντανά): το `web/` το έκανε ήδη σε επιτυχημένη «Δοκιμή
   LLM»/αποθήκευση κλειδιού (`web/views.py`), αλλά το αντίστοιχο `gui/main_window.py: _test_llm/_save_secret_keys` όχι — αποτέλεσμα, το
   κόκκινο banner «Η ανάλυση άρθρων με LLM δεν δουλεύει» έμενε μόνιμα ορατό ακόμη κι όταν το «Δοκιμή LLM» μόλις είχε επιβεβαιώσει ότι η
   σύνδεση δουλεύει — ο χρήστης το περιέγραψε ως «το μήνυμα δεν εξαφανίζεται». Διορθώθηκε: και τα δύο σβήνουν το `llm_last_error` (και τα
   `aade_office_status/message` σε νέους κωδικούς γραφείου) σε επιτυχία, και καλούν `_reload_notices()` αμέσως — όχι μόνο στο επόμενο
   `reload_all()`. Regression test: `test_test_llm_success_clears_the_stale_error_banner`.
   **«HTTP 403: άκυρο ή χωρίς δικαιώματα API key» ΜΟΝΙΜΑ (2026-09-22, πραγματικό key του χρήστη, Groq+dist):** ελέγχθηκε ο κώδικας
   (`llm_extract.py`: headers/Bearer/strip() σωστά, ίδιο σφάλμα σε dev python ΚΑΙ στο packaged dist — άρα ΔΕΝ είναι θέμα packaging, ο
   403 σημαίνει ότι το request έφτασε κανονικά στον πάροχο και απορρίφθηκε εκεί). Πιθανότερη αιτία: το ίδιο το κλειδί είναι
   άκυρο/ανακλήθηκε/έληξε ή λείπουν δικαιώματα στον λογαριασμό — όχι bug εδώ. Το μήνυμα σφάλματος τώρα δίνει οδηγίες (κενά,
   ανάκληση, «Αποθήκευση κλειδιών»). Αν επανέλθει: ζήτα να δοκιμάσει νέο key από https://console.groq.com (ή openrouter.ai/keys) και
   «Δοκιμή LLM» στις Ρυθμίσεις — αν ΞΑΝΑδώσει 403 με σίγουρα σωστό/φρέσκο key, τότε αξίζει βαθύτερη διερεύνηση.
6. **Πηγές:** ενεργές: taxheaven ×3 (νέα/αποφάσεις + πλήρες ημερολόγιο μέσω `taxheaven_calendar.py`), e-forologia ×6 (incl. Εργατικά), ot.gr Φορολογία, forologikanea.gr, naftemporiki (tag `forologia`), capital.gr (γενικό), eforiakoi.org (κυρίως συνδικαλιστικά).
   Τα γενικά (forologikanea/naftemporiki/capital/eforiakoi) περνούν από prefilter λέξεων-κλειδιών (`ingestion/filters.py`, ΚΑΙ φορολογικά ΚΑΙ εργατικά/μισθοδοσίας: εργασιακ, εργατικ, απολυσ, προσληψ, συλλογικ, κατωτατος μισθ, αδειας, ικα, ενσημ…) — αν χάνονται σχετικά άρθρα, διεύρυνε τα `KEYWORD_STEMS`.
   **forin.gr: ΜΗ ΔΙΑΘΕΣΙΜΗ** — Cloudflare challenge σε κάθε URL· δεν το παρακάμπτουμε (`Source.available=False`). Το ίδιο θέμα από δύο πηγές ενώνεται (Jaccard ≥ 0.8 στον τίτλο, 14 ημέρες).
   **Taxheaven — Widget Creator (`taxheaven.gr/widgetcreator`) εξετάστηκε και ΔΕΝ προσφέρει τίποτα νέο (2026-09-23):**
   είναι απλά ένα JS embed (`widget_display.js`) που ξαναζωγραφίζει το ΙΔΙΟ δημόσιο RSS που ήδη κατεβάζουμε
   (`feedSrc=...soft_new.xml` κ.λπ., επιβεβαιώθηκε διαβάζοντας τον παραγόμενο κώδικα embed) — τίτλος+σύνδεσμος
   μόνο, καμία κρυφή API για πλήρες κείμενο. Μην το ξαναδιερευνήσεις.
   **Taxheaven — login/OTP για πλήρες κείμενο: ΔΕΝ χρειάζεται για νέα/διοικητικές εγκυκλίους, ΑΛΛΑ χρειάζεται
   ΠΛΗΡΩΜΕΝΗ συνδρομή (όχι απλή εγγραφή) για νομολογία.** Επαληθεύτηκε ζωντανά, ανώνυμα (χωρίς καμία σύνδεση):
   ένα κανονικό άρθρο νέων (`/news/...`) έδωσε ΟΛΟΚΛΗΡΟ το κείμενο (~50.000 χαρακτήρες, όσο λέει το ίδιο το
   taxheaven «89 λεπτά ανάγνωση») χωρίς κανένα paywall· το ίδιο και μια διοικητική εγκύκλιος ΑΑΔΕ (`/circulars/...`,
   π.χ. Ο.3043/2026) — πλήρες κείμενο, ελεύθερο. ΑΛΛΑ μια δικαστική απόφαση στο ΙΔΙΟ `/circulars/...` path
   (π.χ. https://www.taxheaven.gr/circulars/55385/ste-203-2026, ΣτΕ 203/2026) έδειξε ΜΟΝΟ μια σύντομη «Περίληψη»
   και μετά «Συνδρομητικό περιεχόμενο... Αποκτήστε πρόσβαση — 100,00€/έτος» — είναι η ξεχωριστή υπηρεσία
   «Αρχείο Νόμων και Αποφάσεων», ΠΛΗΡΩΜΕΝΗ συνδρομή, όχι κάτι που λύνει απλή εγγραφή/OTP λογαριασμού. Το
   `soft_law.xml` feed (αποφάσεις/εγκύκλιοι) δείχνει ΚΑΙ τα δύο είδη περιεχομένου κάτω από το ίδιο `/circulars/`
   path χωρίς διάκριση στο URL — δεν μπορούμε να ξέρουμε εκ των προτέρων ποιο άρθρο θα είναι παραγγελία.
   **Διορθώθηκε ταυτόχρονα (`llm_extract.fetch_full_text`/`_strip_paywall_upsell`):** όταν μια σελίδα ΕΙΝΑΙ
   συνδρομητική, το scraping τώρα κόβει ΟΤΙΔΗΠΟΤΕ μετά το «Συνδρομητικό περιεχόμενο»/«προσβάσιμο μόνο από τους
   συνδρομητές» marker, κρατώντας μόνο την ελεύθερη «Περίληψη» πριν από αυτό — πριν, το «100,00€/έτος» της τιμής
   συνδρομής θα έμπαινε στο κείμενο προς το LLM σαν να ήταν πραγματικό ποσό/πρόστιμο του άρθρου.
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
   Η εξαίρεση φακέλου στο Acronis που πρόσθεσε ο χρήστης (2026-09-22) ΔΕΝ κράτησε: το φρεσκοχτισμένο `TaxMatch.exe` (v0.3.0) εξαφανίστηκε
   ξανά μέσα σε ~15 δευτερόλεπτα μετά από silent install, ενώ έμειναν `unins000.exe`/`_internal` — άρα ο γενικός exclusion list του Acronis
   δεν αρκεί· πιθανότατα χρειάζεται ξεχωριστή εξαίρεση στο δικό του «Active Protection»/anti-ransomware τμήμα (διαφορετικό από το γενικό
   antivirus exclusion list), όχι μόνο στο γενικό. **Σύγκριση με το `mydata-etimologio-bridge/desktop` (TimologioDownloader, αδελφό
   εργαλείο) που ΔΕΝ το χτυπά το antivirus σε αυτό το μηχάνημα:** ελέγχθηκε ρητά (2026-09-22) — το `timologio.spec` έχει ΤΙΣ ΙΔΙΕΣ βασικές
   μετριάσεις με το `taxmatch.spec` (one-dir COLLECT όχι one-file, πλήρες VersionInfo με CompanyName/FileDescription/ProductName, χωρίς
   UPX), και το `TimologioDownloader.exe` είναι ΕΠΙΣΗΣ ανυπόγραφο (`Get-AuthenticodeSignature` → `NotSigned`) — δεν βρέθηκε καμία
   ουσιαστική διαφορά κώδικα/packaging που να εξηγεί τη διαφορετική συμπεριφορά. Πιθανότερη εξήγηση: το `TimologioDownloader.exe` είναι
   στον δίσκο αμετάβλητο από τις 16/09 (ίδιο hash εδώ και μέρες, πιθανώς ήδη «αξιολογημένο»/cached ως ασφαλές από το Acronis), ενώ το
   `TaxMatch.exe` ξαναχτίζεται συνέχεια μέσα στην ίδια συνεδρία (νέο hash σε κάθε build) — κάθε νέο, άγνωστο, ανυπόγραφο exe ξαναπυροδοτεί
   το heuristic από την αρχή. Άρα ΔΕΝ είναι κάτι διορθώσιμο μέσω spec/κώδικα (μην το ξαναδιερευνήσεις έτσι) — μόνο μέσω code signing,
   σταθεροποίησης του build (λιγότερα rebuilds = το ίδιο hash μένει «γνωστό» στο Acronis) ή σωστής ρύθμισης εξαίρεσης ΜΕΣΑ στο Acronis.
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
   **Μηνιαίο grid: λάθος στοίχιση κεφαλίδας ημερών (2026-09-23, βρέθηκε ζωντανά).** Οι μαθηματικές ημέρες ήταν πάντα σωστές
   (`pycal.Calendar(firstweekday=0).monthdatescalendar` επαληθεύτηκε ξεχωριστά) — το πρόβλημα ήταν οπτικό: η γραμμή «Δε Τρ Τε Πε Πα Σα
   Κυ» ζει σε ΔΙΑΦΟΡΕΤΙΚΟ layout (`QHBoxLayout`) από το grid των αριθμών ημερών από κάτω (`QGridLayout` με `setColumnStretch(col, 1)`
   σε 7 ίσες στήλες) — τα labels της κεφαλίδας προστίθονταν με `heads.addWidget(lbl)` ΧΩΡΙΣ stretch factor, άρα έμεναν στο φυσικό τους
   πλάτος (πακεταρισμένα αριστερά) αντί να απλώνονται σε 7 ίσα τμήματα σαν τις στήλες του grid — σε πλατύ παράθυρο οι επικεφαλίδες
   ολοένα και ξέφευγαν από τις αντίστοιχες στήλες. Διορθώθηκε: `heads.addWidget(lbl, 1)` (ίδιο stretch=1 με τις στήλες του grid).
   **«+N ακόμη» στο κελί ημέρας δεν είχε tooltip** (τα chips events είχαν ήδη, βλ. `_DayCell`) — προστέθηκε, δείχνει τους τίτλους
   των κρυμμένων events.
   **Εισαγωγή από Excel ενσωματώθηκε στο «Νέος πελάτης»** (2026-09-23, ίδιο pattern με το `mydata-etimologio-bridge/desktop`): ο
   διάλογος (`client_dialog.py`) έχει τώρα κουμπί «Εισαγωγή από Excel…» που ανοίγει file picker και, αν επιλεγεί αρχείο, κλείνει τον
   διάλογο ως Accepted με `dlg.excel_path` ορισμένο (ΧΩΡΙΣ να κάνει ο ίδιος την εισαγωγή)· το `MainWindow.on_add_client()` ελέγχει
   ΠΡΩΤΑ το `excel_path` και τρέχει `_import_excel_from_path()` αν υπάρχει, αλλιώς συνεχίζει με το `result_afm` όπως πριν. Το
   ξεχωριστό «Εισαγωγή από Excel» στο μενού ΔΕΔΟΜΕΝΑ παραμένει επίσης (δεν αφαιρέθηκε) — και τα δύο μονοπάτια καταλήγουν στην ίδια
   `_import_excel_from_path()`.
   **«Νέα & Matches»: το preview έδειχνε άδειο διάλογο για άρθρα που δεν έχουν αναλυθεί ακόμη με LLM** (status «σε αναμονή»/
   «φιλτραρίστηκε»/«αποτυχία») — το `summary` έρχεται ΜΟΝΟ από το `extracted_json`, που δεν υπάρχει πριν την εξαγωγή. Διορθώθηκε
   (`_open_selected_news`): αν λείπει το `summary`, δείχνει fallback `article.full_text` (αν έχει ήδη ανακτηθεί) ή `raw_summary` του
   RSS feed, ώστε να φαίνεται ΚΑΤΙ αντί για κενό παράθυρο. Τα ήδη-matched άρθρα (dashboard/client-detail) ΔΕΝ χρειάζονται αυτό το
   fallback — matching προϋποθέτει επιτυχή εξαγωγή, άρα έχουν πάντα `summary`.
   **«Έλεγχος τώρα»: το τελικό μήνυμα δεν έλεγε τίποτα για την αντιστοίχιση** — μόνο «Ολοκληρώθηκε ✓», ενώ αυτό ακριβώς που νοιάζει
   τον χρήστη (πόσα νέα matches βρέθηκαν) ήταν ήδη υπολογισμένο (`stats["match"]` από `engine.rematch()`) αλλά δεν εμφανιζόταν.
   Διορθώθηκε: το μήνυμα λέει τώρα π.χ. «Ολοκληρώθηκε ✓ — 3 νέα matches · 1 ενημερώθηκαν.».
