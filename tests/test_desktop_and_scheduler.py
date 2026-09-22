import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from taxmatch import scheduler_win
from taxmatch.desktop import LocalServer

ROOT = Path(__file__).resolve().parents[1]
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_task_xml_is_valid_and_has_reliability_settings():
    xml = scheduler_win.task_xml(r"C:\Program Files\TaxMatch\TaxMatch.exe", "--daily", "07:45", start=date(2026, 9, 21))
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))          # parse χωρίς declaration encoding
    assert root.find(".//t:StartBoundary", NS).text == "2026-09-21T07:45:00"
    assert root.find(".//t:Settings/t:StartWhenAvailable", NS).text == "true"
    assert root.find(".//t:Settings/t:RunOnlyIfNetworkAvailable", NS).text == "true"
    assert root.find(".//t:Settings/t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    assert root.find(".//t:Principal/t:RunLevel", NS).text == "LeastPrivilege"      # χωρίς admin
    assert root.find(".//t:Exec/t:Arguments", NS).text == "--daily"


def test_task_xml_escapes_paths_and_rejects_bad_times():
    xml = scheduler_win.task_xml(r"C:\A & B\<x>.exe", "--daily", "08:00")
    assert "A &amp; B" in xml and "&lt;x&gt;" in xml
    for bad in ("8:00", "24:00", "07:60", "abc", ""):
        with pytest.raises(ValueError):
            scheduler_win.task_xml("x.exe", "--daily", bad)


def test_real_http_server_enforces_token_and_serves_pages():
    server = LocalServer().start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/")
        assert exc.value.code == 403
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        html = opener.open(server.url).read().decode("utf-8")          # token -> cookie -> redirect -> dashboard
        assert "Σημερινό ενημερωτικό" in html
        assert opener.open(base + "/clients").status == 200
    finally:
        server.stop()


def test_server_binds_loopback_only():
    server = LocalServer().start()
    try:
        assert server._server.server_address[0] == "127.0.0.1"
    finally:
        server.stop()


def test_cli_version_runs_as_module():
    out = subprocess.run([sys.executable, "-m", "taxmatch", "--version"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "TaxMatch by ScanMyData" in out.stdout


def test_daily_entry_returns_zero_and_records_run(tmp_path, monkeypatch):
    from taxmatch import daily, db, pipeline

    monkeypatch.setattr(pipeline, "enabled_sources", lambda c: [])          # χωρίς δίκτυο
    assert daily.main() == 0
    conn = db.connect()
    run = pipeline.last_run(conn)
    assert run["trigger"] == "scheduled" and run["finished_at"]
    assert (tmp_path / "data" / "logs" / "taxmatch.log").exists()          # ένα κοινό αρχείο, όπως το timologio downloader
