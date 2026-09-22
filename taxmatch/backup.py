"""Αντίγραφα ασφαλείας της βάσης — και του `.enckey` της (ίδια ιδέα με το `mydata-etimologio-bridge/desktop`).

Χρησιμοποιείται το sqlite3 backup API αντί για απλό copy: δουλεύει σωστά ακόμη κι όταν η βάση είναι ανοιχτή σε
WAL mode, όπου ένα σκέτο copy μπορεί να πιάσει ασυνεπές snapshot.

Το `.enckey` ταξιδεύει μαζί με κάθε αντίγραφο (ίδιο όνομα, κατάληξη `.enckey`): τα `client_credentials` (κωδικοί
TAXISnet ανά πελάτη) και τα secret API keys είναι κρυπτογραφημένα με το κλειδί ΑΥΤΟΥ του φακέλου δεδομένων — μια
επαναφορά βάσης σε φάκελο με διαφορετικό/φρέσκο `.enckey` θα άφηνε τα credentials κλειδωμένα χωρίς να φαίνεται
γιατί. ΔΕΝ αντικαθιστά αυτόματα το τρέχον `.enckey` (θα μπορούσε να χαλάσει έναν φάκελο που ήδη δουλεύει) — απλώς
μένει δίπλα στο αντίγραφο, έτοιμο για χειροκίνητη αποκατάσταση αν χρειαστεί."""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: Πόσα αντίγραφα κρατάμε ανά είδος (reason).
KEEP = 10

_STAMP = "%Y%m%d-%H%M%S"
_PREFIX = "taxmatch"


def backup_dir(data_dir: Path) -> Path:
    return data_dir / "backups"


def key_beside(backup_path: Path) -> Path:
    """Το `.enckey` που ανήκει σε ΑΥΤΟ το αντίγραφο (ίδιο όνομα, κατάληξη `.enckey`)."""
    return backup_path.with_suffix(".enckey")


def create_backup(db_path: Path, reason: str = "manual") -> Optional[Path]:
    """Φτιάχνει αντίγραφο της βάσης (και του `.enckey`, αν υπάρχει). Επιστρέφει τη διαδρομή, ή `None` αν δεν
    υπάρχει ακόμη βάση. Ποτέ δεν σηκώνει εξαίρεση προς τα πάνω — ένα αποτυχημένο backup δεν πρέπει να εμποδίσει
    τη δουλειά του χρήστη, απλώς καταγράφεται."""
    if not db_path.exists():
        return None
    target_dir = backup_dir(db_path.parent)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime(_STAMP)
    target = target_dir / f"{_PREFIX}-{stamp}-{reason}.db"

    try:
        source = sqlite3.connect(str(db_path))
        dest = sqlite3.connect(str(target))
        with dest:
            source.backup(dest)  # συνεπές snapshot ακόμη και σε WAL
        dest.close()
        source.close()
    except sqlite3.Error as exc:
        log.warning("Αποτυχία αντιγράφου ασφαλείας: %s", exc)
        target.unlink(missing_ok=True)
        return None

    key = db_path.parent / ".enckey"
    if key.exists():
        try:
            shutil.copy2(key, key_beside(target))
        except OSError as exc:  # ένα αποτυχημένο αντίγραφο κλειδιού δεν μπλοκάρει το backup της βάσης
            log.warning("Το .enckey δεν αντιγράφηκε στο αντίγραφο ασφαλείας: %s", exc)

    prune(target_dir, reason)
    log.info("Αντίγραφο ασφαλείας: %s", target.name)
    return target


def prune(target_dir: Path, reason: str, keep: int = KEEP) -> int:
    """Κρατά τα `keep` νεότερα αντίγραφα του ίδιου είδους (reason)."""
    files = sorted(target_dir.glob(f"{_PREFIX}-*-{reason}.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in files[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
        key_beside(old).unlink(missing_ok=True)
    return removed


def list_backups(data_dir: Path) -> list[tuple[Path, datetime, int]]:
    """(διαδρομή, ημερομηνία, μέγεθος) — νεότερα πρώτα."""
    target_dir = backup_dir(data_dir)
    if not target_dir.exists():
        return []
    out = []
    for path in target_dir.glob(f"{_PREFIX}-*.db"):
        stat = path.stat()
        out.append((path, datetime.fromtimestamp(stat.st_mtime), stat.st_size))
    return sorted(out, key=lambda row: row[1], reverse=True)


def restore(backup_path: Path, db_path: Path) -> Path:
    """Επαναφέρει ένα αντίγραφο. Η τρέχουσα βάση ΔΕΝ διαγράφεται: κρατιέται πρώτα ως αντίγραφο «pre-restore»,
    ώστε μια λάθος επαναφορά να είναι αναστρέψιμη."""
    if not backup_path.exists():
        raise FileNotFoundError(f"Δεν βρέθηκε το αντίγραφο: {backup_path}")

    safety = create_backup(db_path, reason="pre-restore")

    # Τα WAL/SHM του τρέχοντος αρχείου πρέπει να φύγουν, αλλιώς η SQLite μπορεί να τα ξαναπαίξει πάνω στην
    # επαναφερμένη βάση.
    for extra in (db_path.with_suffix(db_path.suffix + "-wal"), db_path.with_suffix(db_path.suffix + "-shm")):
        extra.unlink(missing_ok=True)

    source = sqlite3.connect(str(backup_path))
    dest = sqlite3.connect(str(db_path))
    with dest:
        source.backup(dest)
    dest.close()
    source.close()

    log.info("Έγινε επαναφορά από %s (ασφάλεια: %s)", backup_path.name, safety.name if safety else "—")
    return db_path
