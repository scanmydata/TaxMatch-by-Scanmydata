import json
import sqlite3
from xml.etree import ElementTree as ET

import pytest

from taxmatch import db, obligations, pipeline
from taxmatch.mydata import analysis, client, parse, store


def test_parse_e3_info():
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <RequestedE3Info xmlns="http://www.aade.gr/myDATA/invoice/v1.0">
        <E3Info>
            <V_Afm>803105689</V_Afm>
            <V_Mark>400015474968899</V_Mark>
            <IssueDate>2026-09-30T00:00:00</IssueDate>
            <V_Class_Category>category2_5</V_Class_Category>
            <V_Class_Type>E3_585_016</V_Class_Type>
            <V_Class_Value>-540</V_Class_Value>
        </E3Info>
        <E3Info>
            <V_Afm>803105689</V_Afm>
            <V_Mark>400015077430169</V_Mark>
            <IssueDate>2026-09-01T00:00:00</IssueDate>
            <V_Class_Category>category1_1</V_Class_Category>
            <V_Class_Type>E3_561_003</V_Class_Type>
            <V_Class_Value>7.28</V_Class_Value>
        </E3Info>
        <E3Info>
            <V_Afm>803105689</V_Afm>
            <V_Mark>400099999999999</V_Mark>
            <IssueDate>2026-09-01T00:00:00</IssueDate>
            <V_Class_Category>ΜΗ ΧΑΡΑΚΤΗΡΙΣΜΕΝΑ ΕΞΟΔΑ</V_Class_Category>
            <V_Class_Type></V_Class_Type>
            <V_Class_Value>0</V_Class_Value>
        </E3Info>
    </RequestedE3Info>""".encode("utf-8")

    res = parse.parse_e3_info(xml)
    assert "400015474968899" in res
    assert res["400015474968899"][0]["kind"] == "expense"
    assert res["400015474968899"][0]["type"] == "E3_585_016"
    assert res["400015474968899"][0]["category"] == "category2_5"
    assert res["400015474968899"][0]["amount"] == 540.0

    assert "400015077430169" in res
    assert res["400015077430169"][0]["kind"] == "income"
    assert res["400015077430169"][0]["type"] == "E3_561_003"
    assert res["400015077430169"][0]["amount"] == 7.28

    # Unclassified marker should be skipped
    assert "400099999999999" not in res


def test_mydata_tab_month_summary_renders_parepid(conn, monkeypatch):
    from taxmatch.gui import mydata_tab

    afm = "999888777"
    conn.execute("INSERT OR REPLACE INTO businesses(afm, name, parepid, activity_state, created_at, updated_at) "
                 "VALUES (?, ?, 1, 'active', 'now', 'now')",
                 (afm, "ΕΣΤΙΑΤΟΡΙΟ"))
    # Insert a dummy sync row so month_summary sees it as synced
    conn.execute("INSERT OR REPLACE INTO mydata_sync(afm, ym, synced_at, n_docs) VALUES (?, '2026-09', 'now', 1)", (afm,))

    class FakeTab:
        def __init__(self, c, a):
            self.conn = c
            self.afm = a
            self.summary = type("FakeLabel", (), {"setText": lambda s, t: setattr(s, "text", t), "text": ""})()
            self.window_ = None

        _business = mydata_tab.MydataTab._business
        _render_summary = mydata_tab.MydataTab._render_summary
        _row = mydata_tab.MydataTab._row
        _table = staticmethod(mydata_tab.MydataTab._table)
        _status_html = mydata_tab.MydataTab._status_html
        _cross_check = lambda s, *a: ""
        _vat_row = lambda s, *a: ""

    tab = FakeTab(conn, afm)
    tab._render_summary("2026-09")
    assert "Παρεπιδημούντες / Ακαθάριστα" in tab.summary.text
    assert "dae.govapp.gr" in tab.summary.text


def test_notice_panel_above_toasts(conn, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from taxmatch.gui import toast
    from taxmatch.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    win._reposition_notice_panel()
    win.notice_panel.show()
    assert win.notice_panel.isVisible()

    # Trigger a toast
    toast.toast(win, "Δοκιμαστικό flash message", "info")
    host = getattr(win, "_toast_host", None)
    assert host is not None

    # Both are visible, verify notice_panel width and raise
    win._reposition_notice_panel()
    assert win.notice_panel.width() >= 460
    win.close()
