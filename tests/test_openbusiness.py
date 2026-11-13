"""Πιθανοί υπόχρεοι OpenBusiness από τους ΚΑΔ (2026-11-13). Ψεύτικα ΑΦΜ/ονόματα."""
from __future__ import annotations

import pytest

from taxmatch import db as dbmod
from taxmatch import notices, openbusiness
from taxmatch.business_profiles import service

AFM, OTHER, CEASED = "094259216", "800000002", "070000005"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TAXMATCH_DATA_DIR", str(tmp_path))
    c = dbmod.connect()
    yield c
    c.close()


def test_activity_table_with_exclusions_and_most_specific_match():
    assert openbusiness.activity_of("56.10.11.09")[0].startswith("Καταστήματα Υγειονομικού")
    assert openbusiness.activity_of("46.71.12") is None                 # εξαιρείται από τα Κέντρα Αποθήκευσης
    assert openbusiness.activity_of("08.11.12")[0] == "Λατομεία"         # «8.11» της πύλης = 08.11
    assert openbusiness.activity_of("69.20.10.01") is None
    assert openbusiness.activity_of("85.59.13.06")[1] == "Γ & Ε"


def test_reasons_name_the_activity_and_skip_unrelated_kads():
    assert openbusiness.reasons(["55.20.11.04", "69.20"]) == ["ΚΑΔ 55.20.11.04 — Τουριστικά καταλύματα (Γ)"]


def test_notice_only_for_active_clients_and_can_be_read(conn):
    for afm, name, state, kad in ((AFM, "ΤΑΒΕΡΝΑ ΔΟΚΙΜΗ", "active", "56.10.11.02"),
                                  (OTHER, "ΛΟΓΙΣΤΙΚΟ ΓΡΑΦΕΙΟ", "active", "69.20.10.01"),
                                  (CEASED, "ΚΛΕΙΣΤΟ ΚΑΦΕ", "ceased", "56.30.10.03")):
        service.add(conn, afm, name)
        conn.execute("UPDATE businesses SET activity_state=? WHERE afm=?", (state, afm))
        service.set_kads(conn, afm, [{"code": kad, "descr": "", "is_main": True}])
    assert list(notices.openbusiness_candidates(conn)) == [AFM]
    found = [n for n in notices.collect(conn) if n.get("dismiss") == "openbusiness"]
    assert found and found[0]["afms"] == [AFM] and "OpenBusiness" in found[0]["text"]
    notices.mark_openbusiness_read(conn)
    assert not [n for n in notices.collect(conn) if n.get("dismiss") == "openbusiness"]
