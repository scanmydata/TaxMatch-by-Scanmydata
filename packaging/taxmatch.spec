# -*- mode: python ; coding: utf-8 -*-
# PyInstaller (onedir):  pyinstaller packaging/taxmatch.spec --noconfirm
# ΕΝΑ exe: TaxMatch.exe — native GUI (PySide6, taxmatch/gui/). Το ίδιο exe με `--daily` τρέχει τον έλεγχο χωρίς UI
# (Task Scheduler). Το Flask (taxmatch/web/) ΔΕΝ είναι η εμφάνιση της εφαρμογής πλέον — μπαίνει στο πακέτο μόνο
# επειδή το `--serve` (εσωτερικό εργαλείο ανάπτυξης) το χρειάζεται.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "taxmatch" / "gui" / "assets"), "taxmatch/gui/assets"),
    (str(ROOT / "taxmatch" / "web" / "templates"), "taxmatch/web/templates"),
    (str(ROOT / "taxmatch" / "web" / "static"), "taxmatch/web/static"),
    (str(ROOT / "taxmatch" / "extraction" / "prompts"), "taxmatch/extraction/prompts"),
    (str(ROOT / "packaging" / "taxmatch.ico"), "packaging"),
]
if (ROOT / "packaging" / "manual.pdf").exists():          # το χτίζει το build.ps1 (βήμα 3c)
    datas.append((str(ROOT / "packaging" / "manual.pdf"), "."))
datas += collect_data_files("feedparser")
datas += collect_data_files("tzdata")            # ζώνες ώρας (Europe/Athens) για το αρχείο καταγραφής
# ddddocr: αυτόματη λύση captcha στην ΑΚΕΕΔ ΕΔΟΕΑΠ (advertising.py — ρητό αίτημα 2026-11-02). Μοντέλα onnx
# (common_old = προεπιλογή, common = beta)· το common_det (ανίχνευση αντικειμένων) δεν χρειάζεται — −20 MB.
datas += collect_data_files("ddddocr", excludes=["**/common_det.onnx", "**/README.md"])

hiddenimports = (
    collect_submodules("feedparser")
    + ["openpyxl.cell._writer", "PySide6.QtSvg", "PySide6.QtNetwork"]
    + collect_submodules("ddddocr")
    # «stealth» λήψη PDF παραστατικών πίσω από Cloudflare (mydata/stealth.py — port του timologio, 2026-11-04)
    + collect_submodules("undetected_chromedriver")
    + collect_submodules("selenium")
)

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Μόνο PySide6-essentials χρειάζεται — τα υπόλοιπα bindings Qt, και ό,τι δεν αγγίζει καθόλου η εφαρμογή.
    # numpy / PIL / cv2 / onnxruntime ΧΡΕΙΑΖΟΝΤΑΙ πλέον (ddddocr) — δεν εξαιρούνται
    excludes=["tkinter", "matplotlib", "pandas", "pytest", "PyQt5", "PyQt6", "PySide2", "gi", "qtpy",
             "webview", "clr_loader", "pythonnet"],
    noarchive=False,
)
# selenium-manager (εκτελέσιμα linux/macos/windows): ΔΕΝ χρειάζεται — το undetected-chromedriver δίνει δικό του driver.
# Το antivirus του μηχανήματος ακύρωνε τη μεταγλώττιση του Inno Setup πάνω σε αυτά (2026-11-04, «contains a virus or
# potentially unwanted software»).
_no_manager = lambda entries: [e for e in entries if "selenium-manager" not in e[0].replace("\\", "/")]  # noqa: E731
a.datas, a.binaries = _no_manager(a.datas), _no_manager(a.binaries)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TaxMatch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=(os.environ.get("TAXMATCH_CONSOLE") == "1"),   # False: GUI subsystem — χωρίς παράθυρο κονσόλας (και στο scheduled task)
    icon=str(ROOT / "packaging" / "taxmatch.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TaxMatch")
