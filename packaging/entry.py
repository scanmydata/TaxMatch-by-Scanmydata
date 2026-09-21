"""Entry point για το PyInstaller (το `taxmatch/__main__.py` δεν μπορεί να τρέξει ως top-level script).

Σε windowed exe ένα uncaught exception εμφανίζει dialog που θα κρεμούσε το scheduled task (--daily). Γι' αυτό τα
σφάλματα εκκίνησης γράφονται στο <data dir>\\logs\\crash.log και το process τερματίζει με κωδικό 1.
"""
import sys
import traceback


def _crash_log(text: str) -> None:
    try:
        from taxmatch import config
        from datetime import datetime
        with open(config.log_dir() / "crash.log", "a", encoding="utf-8") as fh:
            fh.write(f"--- {datetime.now().isoformat(timespec='seconds')}\n{text}\n")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        from taxmatch.__main__ import main
        code = main()
    except SystemExit as exc:
        raise
    except BaseException:
        _crash_log(traceback.format_exc())
        if sys.stderr:
            traceback.print_exc()
        sys.exit(1)
    sys.exit(code)
