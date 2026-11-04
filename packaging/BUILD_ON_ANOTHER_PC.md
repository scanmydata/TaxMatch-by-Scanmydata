# Δημιουργία installer από άλλο υπολογιστή

Στο κύριο μηχάνημα το antivirus (Acronis Cyber Protect / Windows Defender) μπορεί να σταματήσει το Inno Setup με
«Operation did not complete successfully because the file contains a virus or potentially unwanted software»
(ψευδώς θετικό σε ανυπόγραφα εκτελέσιμα — βλ. CLAUDE.md, ανοιχτό θέμα 10). Τότε χτίστε τον installer σε άλλο PC
που δεν το μπλοκάρει.

## Προαπαιτούμενα (μία φορά)

- Git, **Python 3.12** (ή [uv](https://docs.astral.sh/uv/)) και **Inno Setup 6** (`ISCC.exe`, προεπιλεγμένη εγκατάσταση).
- Πρόσβαση στο repo (`gh auth login` αν θα γίνει και release από εκεί).

## Βήματα

```powershell
git clone https://github.com/scanmydata/TaxMatch-by-Scanmydata.git
cd TaxMatch-by-Scanmydata
git pull                      # αν υπάρχει ήδη κλώνος
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Το `build.ps1` φτιάχνει μόνο του το `.venv`, εγκαθιστά τις εξαρτήσεις, τρέχει τα tests (`-SkipTests` για παράλειψη),
PyInstaller και Inno Setup. Αποτέλεσμα: `installer-output\TaxMatch-Setup-<έκδοση>.exe`.

Ανέβασμα σε υπάρχον release (π.χ. αν το release δημιουργήθηκε χωρίς αρχείο):

```powershell
gh release upload v<έκδοση> installer-output\TaxMatch-Setup-<έκδοση>.exe --clobber
```

## Προσοχή

- ΔΕΝ χρειάζονται δεδομένα ή κωδικοί του γραφείου στο άλλο PC — τα tests δεν αγγίζουν δίκτυο ούτε πραγματικά δεδομένα.
- Μην αντιγράψετε `.env` / `.enckey` / τη βάση στο άλλο μηχάνημα.
- Η έκδοση βγαίνει από το `taxmatch/__init__.py` (`__version__`).
