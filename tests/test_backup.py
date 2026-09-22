import sqlite3
import time

import pytest

from taxmatch import backup, config, db


def test_create_backup_returns_none_without_a_database():
    assert backup.create_backup(config.db_path()) is None


def test_create_backup_snapshots_current_data(conn):
    now = db.utcnow()
    conn.execute("INSERT INTO businesses(afm,name,created_at,updated_at) VALUES ('094259216','ΔΟΚΙΜΗ',?,?)", (now, now))
    path = backup.create_backup(config.db_path(), reason="manual")
    assert path is not None and path.exists()
    snap = sqlite3.connect(str(path))
    try:
        assert snap.execute("SELECT name FROM businesses WHERE afm='094259216'").fetchone()[0] == "ΔΟΚΙΜΗ"
    finally:
        snap.close()


def test_create_backup_copies_enckey_beside_it(conn):
    key_path = config.data_dir() / ".enckey"
    key_path.write_bytes(b"not-a-real-key")
    path = backup.create_backup(config.db_path(), reason="manual")
    assert backup.key_beside(path).exists()
    assert backup.key_beside(path).read_bytes() == b"not-a-real-key"


def test_list_backups_sorted_newest_first(conn):
    first = backup.create_backup(config.db_path(), reason="manual")
    time.sleep(1.01)  # η ονομασία έχει ανάλυση δευτερολέπτου
    second = backup.create_backup(config.db_path(), reason="manual")
    rows = backup.list_backups(config.data_dir())
    assert [r[0] for r in rows] == [second, first]


def test_prune_keeps_only_newest_per_reason(conn):
    paths = []
    for _ in range(3):
        paths.append(backup.create_backup(config.db_path(), reason="scheduled"))
        time.sleep(1.01)
    target_dir = backup.backup_dir(config.data_dir())
    removed = backup.prune(target_dir, "scheduled", keep=2)
    assert removed == 1
    remaining = {p.name for p in target_dir.glob("taxmatch-*-scheduled.db")}
    assert remaining == {paths[-1].name, paths[-2].name}


def test_restore_brings_back_old_data_and_keeps_a_pre_restore_safety_copy(conn):
    now = db.utcnow()
    conn.execute("INSERT INTO businesses(afm,name,created_at,updated_at) VALUES ('094259216','ΠΑΛΙΟΣ',?,?)", (now, now))
    old_backup = backup.create_backup(config.db_path(), reason="manual")

    conn.execute("UPDATE businesses SET name='ΝΕΟΣ' WHERE afm='094259216'")
    conn.close()  # ΠΡΕΠΕΙ να κλείσει πριν αντικατασταθεί το αρχείο βάσης

    backup.restore(old_backup, config.db_path())

    restored = db.connect()
    try:
        assert restored.execute("SELECT name FROM businesses WHERE afm='094259216'").fetchone()[0] == "ΠΑΛΙΟΣ"
    finally:
        restored.close()

    safety = [p for p, _, _ in backup.list_backups(config.data_dir()) if "pre-restore" in p.name]
    assert safety, "η επαναφορά πρέπει να κρατήσει αντίγραφο της βάσης πριν αντικατασταθεί"


def test_restore_raises_for_missing_backup_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup.restore(tmp_path / "does-not-exist.db", config.db_path())
