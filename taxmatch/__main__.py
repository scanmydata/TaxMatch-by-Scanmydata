"""Είσοδος: `TaxMatch.exe` (native GUI, PySide6) ή `python -m taxmatch [--daily | --install-task | --remove-task]`.

Χωρίς ορίσματα ανοίγει το native παράθυρο (`gui/app.py`) — καμία εξάρτηση από Flask/webview σε κανονική χρήση.
Το `--serve` μένει ΜΟΝΟ για το test suite/ανάπτυξη (εσωτερικό εργαλείο, βλ. `desktop.py`) — δεν είναι ο τρόπος
που ανοίγει η εφαρμογή σε πραγματική χρήση."""
from __future__ import annotations

import argparse
import sys

from . import APP_TITLE, __version__


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="TaxMatch", description=f"{APP_TITLE} v{__version__}")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--daily", action="store_true", help="τρέχει τον καθημερινό έλεγχο χωρίς UI (για το Task Scheduler)")
    g.add_argument("--install-task", action="store_true", help="δημιουργεί το καθημερινό task στο Windows Task Scheduler")
    g.add_argument("--remove-task", action="store_true", help="αφαιρεί το καθημερινό task")
    g.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)   # εσωτερικό: test suite/ανάπτυξη μόνο
    g.add_argument("--version", action="store_true")
    g.add_argument("--check-ocr", action="store_true", help=argparse.SUPPRESS)   # διάγνωση: ddddocr στο πακέτο
    p.add_argument("--time", default="08:00", help="ώρα για --install-task (ΩΩ:ΛΛ)")
    p.add_argument("--all", action="store_true", help="με --daily: όλες οι ενεργές εργασίες, όχι μόνο όσες οφείλονται")
    args = p.parse_args(argv)

    if args.version:
        print(f"{APP_TITLE} {__version__}")
        return 0
    if args.check_ocr:
        from . import advertising
        ok = advertising.ddddocr_available()
        if ok:
            from PySide6.QtGui import QGuiApplication, QImage, QPainter, QFont, QColor
            app = QGuiApplication.instance() or QGuiApplication(["x"])   # ΟΧΙ offscreen: εκεί το κείμενο βγαίνει «τετράγωνα»
            img = QImage(200, 50, QImage.Format.Format_RGB32)
            img.fill(QColor("white"))
            painter = QPainter(img)
            painter.setFont(QFont("Arial", 28))
            painter.drawText(20, 38, "4k7w")
            painter.end()
            from PySide6.QtCore import QBuffer, QByteArray
            buf = QByteArray()
            qb = QBuffer(buf)
            qb.open(QBuffer.OpenModeFlag.WriteOnly)
            img.save(qb, "PNG")
            guess = advertising.solve_captcha(bytes(buf))
            del app
        else:
            guess = ""
        out = f"ddddocr={'ok' if ok else 'missing'} guess={guess}"
        try:
            from . import config
            (config.log_dir() / "check_ocr.txt").write_text(out, encoding="utf-8")   # windowed exe: χωρίς stdout
        except OSError:
            pass
        if sys.stdout:
            print(out)
        return 0 if ok else 1
    if args.daily:
        from .daily import main as daily_main
        return daily_main(force=args.all)
    if args.install_task or args.remove_task:
        from . import scheduler_win
        ok, msg = scheduler_win.install(args.time) if args.install_task else scheduler_win.remove()
        if sys.stdout:
            print(msg)
        return 0 if ok else 1
    if args.serve:
        from .desktop import run_gui
        return run_gui(headless=True)
    from .gui.app import main as gui_main
    return gui_main(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
