import sqlite3
import json
import pytest
from pathlib import Path
from taxmatch import db
from taxmatch.mydata import parse, store
from taxmatch.gui.checks_dialog import ChecksDialog


def test_parse_classifications_doc():
    sample_xml = b'''<?xml version="1.0" encoding="utf-8"?>
    <RequestedDoc xmlns="http://www.aade.gr/myDATA/invoice/v1.0">
        <expensesClassificationsDoc>
            <expensesInvoiceClassification>
                <invoiceMark>123456789</invoiceMark>
                <classificationMark>987654321</classificationMark>
                <invoicesExpensesClassificationDetails>
                    <lineNumber>1</lineNumber>
                    <expensesClassificationDetailData>
                        <classificationType>E3_585_009</classificationType>
                        <classificationCategory>category2_4</classificationCategory>
                        <amount>150.00</amount>
                    </expensesClassificationDetailData>
                    <expensesClassificationDetailData>
                        <classificationType>VAT_361</classificationType>
                        <classificationCategory>category2_4</classificationCategory>
                        <amount>150.00</amount>
                    </expensesClassificationDetailData>
                </invoicesExpensesClassificationDetails>
            </expensesInvoiceClassification>
        </expensesClassificationsDoc>
    </RequestedDoc>'''
    res = parse.parse_classifications_doc(sample_xml)
    assert "123456789" in res
    items = res["123456789"]
    assert len(items) == 2
    assert items[0]["type"] == "E3_585_009"
    assert items[0]["category"] == "category2_4"
    assert items[0]["amount"] == 150.0
    assert items[1]["vat_type"] == "VAT_361"


def test_store_updates_existing_and_new_docs_with_classifications(conn):
    afm = "802499326"
    now = db.utcnow()

    # Create dummy doc in DB with empty classifications
    conn.execute(
        "INSERT INTO mydata_docs(afm, mark, direction, invoice_type, series, aa, issue_date, issuer_vat, issuer_name, "
        "issuer_country, counter_vat, counter_name, counter_country, net, vat, total, withheld, withheld_json, "
        "cancelled, url, provider, fetched_at, stamp, vat_class, rent, taxes_json, detail_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (afm, "MARK_INC_1", "incoming", "1.1", "A", "100", "2026-08-15", "111111111", "SUPPLIER", "GR",
         afm, "CLIENT", "GR", 100.0, 24.0, 124.0, 0.0, "{}", 0, "", "", now, 0.0, 0, 0, "{}",
         json.dumps({"lines": [{"n": "1", "net": 100.0, "vat": 24.0, "classifications": []}],
                     "summary_classifications": []}))
    )

    classifications = {
        "MARK_INC_1": [
            {"kind": "expense", "line": "1", "type": "E3_585_009", "category": "category2_4", "amount": 100.0, "vat_type": "VAT_361"}
        ]
    }

    # Calling _store with classifications should update the existing document
    store._store(conn, afm, [], ["2026-08"], classifications=classifications)

    row = conn.execute("SELECT detail_json, vat_class FROM mydata_docs WHERE afm=? AND mark=?", (afm, "MARK_INC_1")).fetchone()
    det = json.loads(row["detail_json"])
    assert len(det["summary_classifications"]) == 1
    assert det["summary_classifications"][0]["type"] == "E3_585_009"
    assert det["lines"][0]["classifications"][0]["type"] == "E3_585_009"
    assert row["vat_class"] == 1  # VAT_361 sets vat_class


def test_checks_dialog_mydata_controls():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    dlg = ChecksDialog(None, "1 πελάτης", {}, {})

    # Check that mydata sub-controls exist
    assert hasattr(dlg, "mydata_range_combo")
    assert hasattr(dlg, "mydata_pdf_income_box")
    assert hasattr(dlg, "mydata_pdf_expenses_box")

    # When mydata is unchecked
    dlg.boxes["mydata"].setChecked(False)
    assert not dlg.mydata_range_combo.isEnabled()
    assert not dlg.mydata_pdf_income_box.isEnabled()
    assert not dlg.mydata_pdf_expenses_box.isEnabled()
    assert dlg.mydata_pdf_directions == []

    # When mydata is checked
    dlg.boxes["mydata"].setChecked(True)
    assert dlg.mydata_range_combo.isEnabled()
    assert dlg.mydata_pdf_income_box.isEnabled()
    assert dlg.mydata_pdf_expenses_box.isEnabled()

    # Check setting range
    idx = dlg.mydata_range_combo.findData("current_year")
    dlg.mydata_range_combo.setCurrentIndex(idx)
    assert dlg.mydata_range == "current_year"

    # Check setting pdf directions
    dlg.mydata_pdf_income_box.setChecked(True)
    dlg.mydata_pdf_expenses_box.setChecked(False)
    assert dlg.mydata_pdf_directions == ["outgoing"]

    dlg.mydata_pdf_expenses_box.setChecked(True)
    assert dlg.mydata_pdf_directions == ["outgoing", "incoming"]
