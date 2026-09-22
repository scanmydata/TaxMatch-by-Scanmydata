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
    p.add_argument("--time", default="08:00", help="ώρα για --install-task (ΩΩ:ΛΛ)")
    args = p.parse_args(argv)

    if args.version:
        print(f"{APP_TITLE} {__version__}")
        return 0
    if args.daily:
        from .daily import main as daily_main
        return daily_main()
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
