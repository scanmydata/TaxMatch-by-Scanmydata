"""Οφειλές ΚΕΑΟ ανά φορέα (Ηλεκτρονική Πλατφόρμα Οφειλετών) και τρέχουσες εισφορές e-ΕΦΚΑ μη μισθωτών.

Πιστό port του ScanMyData Tax Center (ρητό αίτημα χρήστη 2026-10-22 — «όλη τη λογική άντλησης οφειλών»):

* `configs/keao-debts.js` (== Hyper.Server.Tax `KeaoRetrieveService.RetrieveData`): σύνδεση e-ΕΦΚΑ (φυσικό: ΑΦΜ +
  ΑΜΚΑ, νομικό: ΑΦΜ + ΑΜΕ) → σύνδεσμος «Ηλεκτρονική Πλατφόρμα Οφειλετών - KEAO» → λίστα φορέων (`amoForm:dt-table`,
  20/σελίδα) → για κάθε φορέα: EnableForea → «Κινήσεις Οφειλέτη» (Ταυτότητα Οφειλέτη, Χρεώσεις, Πιστώσεις, Ρυθμίσεις +
  «Δόσεων» ανά ρύθμιση) → «Πληρωμή/IRIS» (οφειλές εκτός ρύθμισης).
* `keao/KeaoCard.kt`: ενεργή ρύθμιση = κατάσταση που ΞΕΚΙΝΑ με «ενεργ»· οφειλή φορέα = εκτός ρύθμισης (σύνολο με
  πρόσθετα τέλη) + ό,τι μένει από τις δόσεις των ενεργών ρυθμίσεων με τις προσαυξήσεις· έντυπο ανά φορέα και ανά ενεργή
  ρύθμιση (δοσολόγιο) — το ΚΕΑΟ δεν δίνει εκτύπωση, τα φτιάχνει το γραφείο (`reports.py`).
* `configs/efka-notices.js`: «Υποχρεώσεις Ασφάλισης» → tab «Τρέχουσες Οφειλές» (είδος, μήνας, πληρωτέο, εμπρόθεσμη
  πληρωμή, RF) και PDF του τελευταίου μηνιαίου ειδοποιητηρίου (tab «Μηνιαία Ειδοποιητήρια»).

**ΜΟΝΟ ανάγνωση. Δεν έχει δοκιμαστεί ζωντανά από το TaxMatch** (tests σε ψεύτικες σελίδες της δομής του Tax Center).
"""
from __future__ import annotations

import html as _html
import logging
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

from . import reports
from .efka import AJAX, SVC, extract_update, strip_tags, view_state
from .textutil import strip_accents

log = logging.getLogger(__name__)
KEAO_LINK = "Ηλεκτρονική Πλατφόρμα Οφειλετών - KEAO"
OBLIGATIONS_PATH = "/ssp.efka.non.employees/views/insuranceObligations.xhtml"


# ------------------------------------------------------------------ βοηθοί JSF/PrimeFaces
def _update(xml: str, update_id: str = "") -> str:
    if update_id:
        m = re.search(r'<update id="' + re.escape(update_id) + r'"><!\[CDATA\[(.*?)\]\]></update>', xml, re.S)
        if m:
            return m.group(1)
    m = re.search(r"<update[^>]*><!\[CDATA\[(.*?)\]\]></update>", xml, re.S)
    return m.group(1) if m else ""


def _ri_rows(fragment: str) -> list[tuple[int, str]]:
    out = []
    for m in re.finditer(r"<tr\b([^>]*)>(.*?)</tr>", fragment, re.S | re.I):
        ri = re.search(r'data-ri="(-?\d+)"', m.group(1))
        if ri and int(ri.group(1)) >= 0:
            out.append((int(ri.group(1)), m.group(2)))
    return out


def _tds(row: str) -> list[str]:
    return [strip_tags(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S | re.I)]


def _panel_cells(fragment: str) -> list[str]:
    return [strip_tags(c) for c in re.findall(r'<div\b[^>]*class="[^"]*ui-panelgrid-cell[^"]*"[^>]*>(.*?)</div>',
                                              fragment, re.S | re.I)]


def _labeled(fragment: str, label: str) -> str:
    cells = _panel_cells(fragment)
    return next((cells[i + 1] for i in range(len(cells) - 1) if cells[i] == label), "")


def _row_count(fragment: str, widget_id: str) -> int:
    m = re.search(r'id="' + re.escape(widget_id) + r'".{0,4000}?rowCount:\s*(\d+)', fragment, re.S | re.I)
    return int(m.group(1)) if m else 0


def _total_records(xml: str) -> int:
    m = re.search(r'totalRecords(?:&#34;|&quot;|")\s*:\s*(\d+)', xml, re.I)
    return int(m.group(1)) if m else 0


def _page_form(src: str, first: int, rows: int, form: str, vs: str, extra: Optional[dict] = None) -> dict[str, str]:
    return {"jakarta.faces.partial.ajax": "true", "jakarta.faces.source": src, "jakarta.faces.partial.execute": src,
            "jakarta.faces.partial.render": src, src: src, src + "_pagination": "true", src + "_first": str(first),
            src + "_rows": str(rows), src + "_skipChildren": "true", src + "_encodeFeature": "true", form: form,
            "jakarta.faces.ViewState": vs, **(extra or {})}


# ------------------------------------------------------------------ ΚΕΑΟ (port του keao-debts.js)
def keao_url(landing: str) -> str:
    """Ο σύνδεσμος «Ηλεκτρονική Πλατφόρμα Οφειλετών - KEAO» (τελευταίο span του anchor)."""
    for a in re.finditer(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>', landing, re.S | re.I):
        spans = re.findall(r"<span[^>]*>(.*?)</span>", a.group(2), re.S | re.I)
        title = strip_tags(spans[-1]) if spans else strip_tags(a.group(2))
        if title == KEAO_LINK:
            return urljoin(SVC, _html.unescape(a.group(1)))
    return ""


def _carrier_page(http, land_url: str, page_no: int, per_page: int = 20) -> dict[str, Any]:
    land = http.follow("GET", land_url)["text"]
    form = re.search(r'<form\b[^>]*id="amoForm"[^>]*>', land, re.I)
    if not form:
        raise RuntimeError("amoForm not found")
    post_url = urljoin(SVC, _html.unescape((re.search(r'action="([^"]*)"', form.group(0)) or [None, ""])[1]))
    vs = view_state(land)
    data = re.search(r'id="amoForm:dt-table_data"[^>]*>(.*?)</tbody>', land, re.S | re.I)
    if data and "Δεν βρέθηκαν εγγραφές." in data.group(1):
        return {"rows": [], "vs": vs, "post": post_url, "total": 0}
    total = _row_count(land, "amoForm:dt-table_s") if page_no == 1 else 0
    xml = http.follow("POST", post_url, _page_form("amoForm:dt-table", (page_no - 1) * per_page, per_page, "amoForm", vs),
                      AJAX)["text"]
    return {"rows": [r for _, r in _ri_rows(_update(xml))], "vs": vs, "post": post_url, "total": total}


def _click_menu(http, vs: str, post: str, menu: str, text: str) -> str:
    src = ""
    for a in re.finditer(r'<a\b([^>]*role="menuitem"[^>]*)>(.*?)</a>', menu, re.S | re.I):
        if strip_tags(a.group(2)) == text:
            onclick = _html.unescape((re.search(r'onclick="([^"]*)"', a.group(1)) or [None, ""])[1])
            m = re.search(r'PrimeFaces\.ab\(\{s:"([^"]+)"', onclick)
            src = m.group(1) if m else ""
            break
    if not src:
        raise RuntimeError(f"menu item not found: {text}")
    return http.follow("POST", post, {"jakarta.faces.partial.ajax": "true", "jakarta.faces.source": src,
                                      "jakarta.faces.partial.execute": "@all",
                                      "jakarta.faces.partial.render": "mainContentPanel MenuForm", src: src,
                                      "MenuForm": "MenuForm", "jakarta.faces.ViewState": vs}, AJAX)["text"]


def _table_pages(http, vs: str, post: str, active: int, per_page: int = 20) -> list[list[str]]:
    src = f"debtorTransForm:mainTabView:dt-table-{active + 1}"
    out: list[list[str]] = []
    pages, page = 1, 1
    while page <= pages and page <= 50:
        xml = http.follow("POST", post, _page_form(src, (page - 1) * per_page, per_page, "debtorTransForm", vs,
                                                   {"debtorTransForm:mainTabView_activeIndex": str(active)}), AJAX)["text"]
        if page == 1:
            total = _total_records(xml)
            pages = -(-total // per_page) if total else 1
        out += [_tds(r) for _, r in _ri_rows(_update(xml))]
        page += 1
    return out


def _change_tab(http, vs: str, post: str, index: int) -> str:
    return _update(http.follow("POST", post, {
        "jakarta.faces.partial.ajax": "true", "jakarta.faces.source": "debtorTransForm:mainTabView",
        "jakarta.faces.partial.render": "debtorTransForm:mainTabView",
        "jakarta.faces.partial.execute": "debtorTransForm:mainTabView", "jakarta.faces.behavior.event": "tabChange",
        "jakarta.faces.partial.event": "tabChange", "debtorTransForm:mainTabView_contentLoad": "true",
        "debtorTransForm:mainTabView_newTab": f"debtorTransForm:mainTabView:tab{index + 1}",
        "debtorTransForm:mainTabView_tabindex": str(index), "debtorTransForm": "debtorTransForm",
        "debtorTransForm:mainTabView_activeIndex": str(index), "jakarta.faces.ViewState": vs}, AJAX)["text"],
        "debtorTransForm:mainTabView")


def _instalments(http, vs: str, post: str, source: str) -> list[dict[str, str]]:
    dlg = _update(http.follow("POST", post, {
        "jakarta.faces.partial.ajax": "true", "jakarta.faces.source": source, "jakarta.faces.partial.execute": source,
        "jakarta.faces.partial.render": "debtorTransForm:settlementInstallmentAnalysisDialog "
                                        "debtorTransForm:dt-settlement-installment-analysis",
        source: source, "debtorTransForm": "debtorTransForm", "debtorTransForm:mainTabView_activeIndex": "2",
        "jakarta.faces.ViewState": vs}, AJAX)["text"], "debtorTransForm:settlementInstallmentAnalysisDialog")
    rc = _row_count(dlg, "debtorTransForm:dt-settlement-installment-analysis_s")
    src = "debtorTransForm:dt-settlement-installment-analysis"
    out = []
    for page in range(1, (-(-rc // 10) if rc else 0) + 1):
        xml = http.follow("POST", post, _page_form(src, (page - 1) * 10, 10, "debtorTransForm", vs,
                                                   {"debtorTransForm:mainTabView_activeIndex": "2"}), AJAX)["text"]
        for _, r in _ri_rows(_update(xml)):
            c = _tds(r) + [""] * 6
            out.append({"no": c[0], "due": c[1], "amount": c[2], "paid": c[3], "balance": c[4], "increments": c[5]})
    return out


def _carrier(http, vs: str, post: str, row: str) -> dict[str, Any]:
    raw = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S | re.I) + [""] * 5
    c = {"amo": strip_tags(raw[0]), "description": strip_tags(raw[1]), "carrier_am": strip_tags(raw[2]),
         "company": strip_tags(raw[3]), "debtor_id": "", "branch": "", "totals": {}, "regulated": [], "outstanding": []}
    btn = (re.search(r'<button\b[^>]*id="([^"]*)"', raw[4]) or [None, ""])[1]
    enable = http.follow("POST", post, {"jakarta.faces.partial.ajax": "true", "jakarta.faces.source": btn,
                                        "jakarta.faces.partial.execute": "@all",
                                        "jakarta.faces.partial.render": "amoForm sidebarUserInfo MenuForm mainContentPanel",
                                        btn: btn, "amoForm": "amoForm", "jakarta.faces.ViewState": vs}, AJAX)["text"]
    menu = _update(enable, "MenuForm")
    if not menu:
        raise RuntimeError("MenuForm not found (EnableForea)")
    panel = _update(_click_menu(http, vs, post, menu, "Κινήσεις Οφειλέτη"), "mainContentPanel")

    def grid(label: str) -> str:
        m = re.search(re.escape(label) + r"</span>\s*</div>\s*<div[^>]*>(.*?)</div>", panel, re.S | re.I)
        return strip_tags(m.group(1)) if m else ""
    c["debtor_id"], c["branch"] = grid("Ταυτότητα Οφειλέτη:"), grid("Αρμόδιο Υποκ/μα:")
    c["totals"] = {k: _labeled(panel, lab) for k, lab in (("debit", "Χρέωση:"), ("credit", "Πίστωση:"),
                                                         ("deleted", "Διαγραφή:"), ("balance", "Υπόλοιπο:"),
                                                         ("reduction", "Έκπτωση:"))}
    _change_tab(http, vs, post, 2)
    src_rows = []
    pages, page = 1, 1
    while page <= pages and page <= 50:
        xml = http.follow("POST", post, _page_form("debtorTransForm:mainTabView:dt-table-3", (page - 1) * 20, 20,
                                                   "debtorTransForm", vs,
                                                   {"debtorTransForm:mainTabView_activeIndex": "2"}), AJAX)["text"]
        if page == 1:
            total = _total_records(xml)
            pages = -(-total // 20) if total else 1
        src_rows.append(_update(xml))
        page += 1
    for fragment in src_rows:
        for ri, row_html in _ri_rows(fragment):
            t = _tds(row_html) + [""] * 9
            reg = {"info": t[1].replace("\n", ""), "primary": t[2], "additional": t[3], "interest": t[4], "total": t[5],
                   "type": t[6], "pay_type": t[7], "status": t[8], "instalments": []}
            menu_id = f"debtorTransForm:mainTabView:dt-table-3:{ri}:actionSelect3_menu"
            block = re.search(r'id="' + re.escape(menu_id) + r'"(.*?)</ul>', fragment, re.S | re.I)
            if block:
                for a in re.finditer(r'<a\b[^>]*id="([^"]*)"[^>]*>(.*?)</a>', block.group(1), re.S | re.I):
                    if strip_tags(a.group(2)) == "Δόσεων":
                        reg["instalments"] = _instalments(http, vs, post, a.group(1))
                        break
            c["regulated"].append(reg)
    pay = _update(_click_menu(http, vs, post, menu, "Πληρωμή/IRIS"), "mainContentPanel")
    rc = _row_count(pay, "paymentsForm:currentDebtsTable_s")
    for page in range(1, (-(-rc // 10) if rc else 0) + 1):
        xml = http.follow("POST", post, _page_form("paymentsForm:currentDebtsTable", (page - 1) * 10, 10, "paymentsForm",
                                                   vs, {"debtorTransForm:mainTabView_activeIndex": "2"}), AJAX)["text"]
        for _, r in _ri_rows(_update(xml)):
            t = _tds(r) + [""] * 5
            c["outstanding"].append({"date": t[0], "document": t[1], "primary": t[2], "additional": t[3], "total": t[4]})
    return c


def fetch_keao(http, landing: str, only: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Όλοι οι φορείς (ή μόνο οι `only` — ΑΜΟ ή Αρ. Μητρώου) με κινήσεις, ρυθμίσεις, δόσεις και οφειλές εκτός ρύθμισης."""
    url = keao_url(landing)
    if not url:
        raise RuntimeError("Δεν βρέθηκε ο σύνδεσμος ΚΕΑΟ στην αρχική του e-ΕΦΚΑ.")
    out = []
    first = _carrier_page(http, url, 1)
    pages = max(1, -(-first["total"] // 20))
    for page_no in range(1, pages + 1):
        page = first if page_no == 1 else _carrier_page(http, url, page_no)
        for i in range(len(page["rows"])):
            row = page["rows"][i]
            t = _tds(row)
            if only and (t[0] if t else "") not in only and (t[2] if len(t) > 2 else "") not in only:
                continue
            out.append(_carrier(http, page["vs"], page["post"], row))
            page = _carrier_page(http, url, page_no)          # φρέσκο ViewState μετά από κάθε φορέα (== hyperserver)
    return out


# ------------------------------------------------------------------ ανάλυση (port του KeaoCard.kt)
def is_active(status: str) -> bool:
    """Ενεργή ρύθμιση: η κατάσταση ΞΕΚΙΝΑ με «ενεργ» (όχι contains — θα έπιανε το «Ανενεργή»)."""
    return strip_accents(status or "").strip().lower().startswith("ενεργ")


def _open(inst: dict[str, str]) -> bool:
    return bool(inst.get("balance")) and any(ch not in "0" for ch in re.sub(r"\D", "", inst["balance"]))


def regulation_owed(r: dict[str, Any]) -> tuple[float, float, list[dict[str, str]]]:
    """(οφειλή = ό,τι μένει από τις δόσεις + προσαυξήσεις, προσαυξήσεις, απλήρωτες δόσεις)· χωρίς δοσολόγιο: το σύνολο."""
    pending = [i for i in r["instalments"] if _open(i)]
    remaining = sum(reports.amount(i["balance"] or i["amount"]) for i in pending)
    increments = sum(reports.amount(i["increments"]) for i in pending)
    owed = remaining + increments if r["instalments"] else reports.amount(r["total"])
    return round(owed, 2), round(increments, 2), pending


def carrier_owed(c: dict[str, Any]) -> dict[str, Any]:
    active = [r for r in c["regulated"] if is_active(r["status"])]
    if not c["outstanding"] and not active:
        return {"total": reports.amount(c["totals"].get("balance", "")), "outside": 0.0, "regulated": 0.0, "fallback": True}
    outside = sum(reports.amount(o["total"]) if o["total"] else reports.amount(o["primary"]) + reports.amount(o["additional"])
                  for o in c["outstanding"])
    regulated = sum(regulation_owed(r)[0] for r in active)
    return {"total": round(outside + regulated, 2), "outside": round(outside, 2), "regulated": round(regulated, 2),
            "additional": round(sum(reports.amount(o["additional"]) for o in c["outstanding"]), 2), "fallback": False}


def carrier_title(description: str) -> tuple[str, str]:
    """«ΕΦΚΑ (ΠΡΩΗΝ ΙΚΑ) - ΕΡΓΟΔΟΤΗΣ» → (φορέας, κατηγορία) — ό,τι είναι μετά την τελευταία « - » είναι κατηγορία."""
    parts = [p.strip() for p in re.split(r"\s+-\s+", description or "") if p.strip()]
    return (parts[0], " - ".join(parts[1:])) if len(parts) > 1 else ((description or "").strip(), "")


def keao_lines(carriers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Γραμμές για την καρτέλα οφειλών (όπως `Debts.keao` του Tax Center): οφειλή ανά φορέα, μία γραμμή ανά ενεργή
    ρύθμιση (με τις απλήρωτες δόσεις) και οι μη ενεργές ως ιστορικό (δεν μετρούν πουθενά)."""
    out = []
    for idx, c in enumerate(carriers):
        title, category = carrier_title(c["description"])
        tag = c["amo"] or c["carrier_am"] or str(idx + 1)
        owed = carrier_owed(c)
        out.append({"kind": "carrier", "ref": f"keao|{tag}", "title": title or "Φορέας ΚΕΑΟ",
                    "overdue": owed["outside"], "non_overdue": owed["regulated"], "total": owed["total"],
                    "code": c["debtor_id"], "pdf_path": "", "next_due": "", "next_amount": 0.0, "instalments": 0,
                    "unpaid": 0, "details": {"category": category, "amo": c["amo"], "am": c["carrier_am"],
                                             "branch": c["branch"], "company": c["company"], "totals": c["totals"],
                                             "owed": owed, "outstanding": c["outstanding"], "tag": tag}})
        for n, r in enumerate(c["regulated"]):
            active = is_active(r["status"])
            owed_r, increments, pending = regulation_owed(r) if active else (reports.amount(r["total"]), 0.0, [])
            nxt = pending[0] if pending else None
            out.append({"kind": "keao_arrangement" if active else "keao_inactive",
                        "ref": f"keao|{tag}|{(r['info'].split() or [str(n + 1)])[0]}|{n}",
                        "title": r["type"] or "Ρύθμιση ΚΕΑΟ", "overdue": 0.0, "non_overdue": 0.0,
                        "total": owed_r, "code": c["debtor_id"] if active else "", "pdf_path": "",
                        "next_due": nxt["due"] if nxt else "",
                        "next_amount": round(reports.amount(nxt["balance"] or nxt["amount"]) + reports.amount(nxt["increments"]), 2)
                        if nxt else 0.0, "instalments": len(r["instalments"]), "unpaid": len(pending),
                        "details": {"carrier": title, "amo": c["amo"], "regulation": r, "increments": increments,
                                    "status": r["status"], "tag": tag}})
    return out


def keao_reports(carriers: list[dict[str, Any]], client: str, afm: str, office: str, retrieved: str,
                 folder: Path) -> dict[str, str]:
    """Έντυπα του γραφείου (το ΚΕΑΟ δεν δίνει εκτύπωση): ΕΝΑ ανά φορέα (με την Ταυτότητα Οφειλέτη ΤΟΥ φορέα — διαφορετική
    σε κάθε φορέα) και ΕΝΑ ανά ενεργή ρύθμιση (δοσολόγιο). {ref γραμμής: path}."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for idx, c in enumerate(carriers):
        title, category = carrier_title(c["description"])
        tag = c["amo"] or c["carrier_am"] or str(idx + 1)
        identity = [("Υπόχρεος", " · ".join(x for x in (client, afm) if x)), ("Φορέας", title), ("Κατηγορία", category),
                    ("ΑΜΟ", c["amo"]), ("Αριθμός Μητρώου", c["carrier_am"]), ("Επωνυμία", c["company"]),
                    ("Αρμόδιο υποκατάστημα", c["branch"])]
        owed = carrier_owed(c)
        active = [r for r in c["regulated"] if is_active(r["status"])]
        sections = []
        if active:
            rows = []
            for r in active:
                o, _inc, pending = regulation_owed(r)
                nxt = pending[0] if pending else None
                rows.append([r["info"], r["type"] or r["pay_type"], r["status"],
                             f"{len(pending)}/{len(r['instalments'])}" if r["instalments"] else "—",
                             " · ".join(x for x in ((nxt["due"], nxt["amount"]) if nxt else ()) if x) or "—", r["total"]])
            sections.append(reports.Section("Ενεργές ρυθμίσεις", reports.Table(
                ["Ρύθμιση", "Είδος", "Κατάσταση", "Δόσεις", "Επόμενη", "Σύνολο"], rows, numeric=[5]),
                "Το δοσολόγιο κάθε ρύθμισης σε χωριστό έντυπο. Η στήλη «Δόσεις» δείχνει πόσες μένουν από πόσες συνολικά."))
        if c["outstanding"]:
            sections.append(reports.Section("Οφειλές εκτός ρύθμισης", reports.table_from(
                ["Ημερομηνία", "Παραστατικό", "Κύρια", "Πρόσθετα", "Σύνολο"],
                [[o["date"], o["document"], o["primary"], o["additional"], o["total"]] for o in c["outstanding"]])))
        t = c["totals"]
        summary = [("Χρέωση", t.get("debit", "")), ("Πίστωση", t.get("credit", "")), ("Έκπτωση", t.get("reduction", "")),
                   ("Διαγραφή", t.get("deleted", "")), ("Υπόλοιπο", t.get("balance", ""))]
        if not owed["fallback"]:
            summary.append(("Συνολική οφειλή", reports.money(owed["total"])))
        inactive = len(c["regulated"]) - len(active)
        footer = [f"Άντληση από την Ηλεκτρονική Πλατφόρμα Οφειλετών ΚΕΑΟ στις {retrieved}." if retrieved else
                  "Άντληση από την Ηλεκτρονική Πλατφόρμα Οφειλετών ΚΕΑΟ."]
        if inactive:
            footer.append(f"Δεν εμφανίζ{'εται 1 ρύθμιση που δεν είναι ενεργή' if inactive == 1 else f'ονται {inactive} ρυθμίσεις που δεν είναι ενεργές'}.")
        footer += ["Τα ποσά αλλάζουν με κάθε καταβολή και με τον χρόνο (προσαυξήσεις). Για πληρωμή χρησιμοποιήστε την "
                   "παραπάνω Ταυτότητα Οφειλέτη, που ισχύει μόνο για αυτόν τον φορέα.",
                   "Ενημερωτικό έντυπο του γραφείου — δεν είναι έγγραφο του ΚΕΑΟ."]
        path = reports.fallback_path(folder, f"KEAO_{afm}_{reports.safe(tag)}.pdf", used)
        reports.write(reports.Report("Καρτέλα οφειλέτη ΚΕΑΟ", office, identity, c["debtor_id"], summary=summary,
                                     sections=sections, footer=footer), path)
        out[f"keao|{tag}"] = str(path)
        for n, r in enumerate(c["regulated"]):
            if not is_active(r["status"]):
                continue
            o, inc, pending = regulation_owed(r)
            decision = (r["info"].split() or [str(n + 1)])[0]
            inst_rows = [[i["no"], i["due"], i["amount"], i["paid"], i["balance"], i["increments"]] for i in r["instalments"]]
            sections = [reports.Section("Δοσολόγιο", reports.table_from(
                ["Α/Α", "Λήξη", "Ποσό", "Καταβλήθηκε", "Υπόλοιπο", "Προσαυξήσεις"], inst_rows))] if inst_rows else []
            path = reports.fallback_path(folder, f"KEAO_RYTHMISI_{afm}_{reports.safe(tag)}_{reports.safe(decision)}.pdf", used)
            reports.write(reports.Report(
                "Ρύθμιση οφειλών ΚΕΑΟ", office, identity + [("Ρύθμιση", r["info"]), ("Είδος", r["type"]),
                                                             ("Κατάσταση", r["status"])], c["debtor_id"],
                summary=[("Κύρια", r["primary"]), ("Πρόσθετα τέλη", r["additional"]), ("Τόκοι ρύθμισης", r["interest"]),
                         ("Σύνολο ρύθμισης", r["total"]), ("Μένουν", reports.money(o)),
                         ("Δόσεις που μένουν", f"{len(pending)}/{len(r['instalments'])}" if r["instalments"] else "")],
                sections=sections,
                footer=footer[:1] + ["Οι δόσεις πληρώνονται με την Ταυτότητα Οφειλέτη του φορέα.",
                                     "Ενημερωτικό έντυπο του γραφείου — δεν είναι έγγραφο του ΚΕΑΟ."]), path)
            out[f"keao|{tag}|{decision}|{n}"] = str(path)
    return out


# ------------------------------------------------------------------ e-ΕΦΚΑ μη μισθωτών (port του efka-notices.js)
def _tabs(page: str) -> list[dict[str, str]]:
    out = []
    for li in re.finditer(r'<li\b[^>]*class="[^"]*ui-tabs-header[^"]*"[^>]*>(.*?)</li>', page, re.S | re.I):
        out.append({"label": strip_tags(li.group(1)),
                    "index": (re.search(r'data-index="([^"]*)"', li.group(0)) or [None, ""])[1],
                    "href": _html.unescape((re.search(r'<a\b[^>]*href="([^"]*)"', li.group(1)) or [None, ""])[1])})
    return out


def _open_tab(http, url: str, label: str) -> Optional[dict[str, Any]]:
    page = http.follow("GET", url)["text"]
    vs = view_state(page)
    tab = next((t for t in _tabs(page) if t["label"] == label), None)
    if not vs or not tab or not tab["href"]:
        return None
    new_tab = tab["href"].lstrip("#")
    form_id = "form-id:accordion-insurance-obligations-citizen"
    view = form_id if f'id="{form_id}"' in page else new_tab[:new_tab.rfind(":")]
    form = view.split(":")[0] or "form-id"
    resp = http.follow("POST", url, {
        "jakarta.faces.partial.ajax": "true", "jakarta.faces.source": view, "jakarta.faces.partial.execute": view,
        "jakarta.faces.partial.render": view, "jakarta.faces.behavior.event": "tabChange",
        "jakarta.faces.partial.event": "tabChange", view + "_contentLoad": "true", view + "_newTab": new_tab,
        view + "_tabindex": tab["index"], form: form, view + "_activeIndex": tab["index"], "jakarta.faces.ViewState": vs},
        AJAX)
    return {"html": extract_update(resp["text"], view), "vs": vs, "view": view, "form": form, "tab": tab}


def _dt_rows(fragment: str) -> list[list[str]]:
    rows = []
    for tr in re.finditer(r'<tr\b[^>]*class="[^"]*ui-datatable-(?:even|odd)[^"]*"[^>]*>(.*?)</tr>', fragment, re.S | re.I):
        rows.append(re.findall(r"<td\b[^>]*>(.*?)</td>", tr.group(1), re.S | re.I))
    return rows


def _cell(c: str) -> str:
    return strip_tags(re.sub(r"<script\b.*?</script>", "", c or "", flags=re.S | re.I))


def month_key(text: str) -> str:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text or "")
    if m:
        return f"{m.group(3)}-{m.group(2)}"
    n = re.search(r"(\d{1,2})/(\d{4})", text or "")
    return f"{n.group(2)}-{int(n.group(1)):02d}" if n else ""


def efka_current(http, links: dict[str, str], afm: str, folder: Optional[Path] = None) -> dict[str, Any]:
    """«Τρέχουσες Οφειλές» (είδος, μήνας, πληρωτέο, εμπρόθεσμη πληρωμή, RF) + PDF του πιο πρόσφατου ειδοποιητηρίου."""
    label = next((k for k in links if re.sub(r"\s+", " ", k).strip().endswith("Υποχρεώσεις Ασφάλισης")), "")
    url = links.get(label) or urljoin(SVC, OBLIGATIONS_PATH)
    lines: list[dict[str, Any]] = []
    cur = _open_tab(http, url, "Τρέχουσες Οφειλές")
    if cur:
        headers = [strip_tags(h) for h in re.findall(r"<th\b[^>]*>(.*?)</th>", cur["html"], re.S | re.I)]
        headers = [h for h in headers if h]

        def col(word: str, fallback: int) -> int:
            return next((i for i, h in enumerate(headers) if word.lower() in h.lower()), fallback)
        k, mo, am, du, co = col("Είδος", 0), col("Αναφοράς", 1), col("Πληρωτέο", 2), col("Εμπρόθεσμη", 3), col("Κωδικός", 4)
        for i, cells in enumerate(_dt_rows(cur["html"])):
            t = [_cell(c) for c in cells] + [""] * 6
            total = reports.amount(t[am])
            code = t[co].replace(" ", "") if t[co].replace(" ", "").upper().startswith("RF") else ""
            if not total and not code:
                continue
            due = (re.search(r"\d{1,2}/\d{1,2}/\d{4}", t[du]) or [None])[0] or ""
            lines.append({"kind": "efka", "ref": f"efka|{month_key(t[mo]) or i}|{i}", "title": t[k] or "Εισφορά e-ΕΦΚΑ",
                          "overdue": 0.0, "non_overdue": total, "total": total, "code": code, "pdf_path": "",
                          "next_due": due, "next_amount": total, "instalments": 0, "unpaid": 1 if due else 0,
                          "details": {"month": t[mo], "due": due}})
    pdfs: list[str] = []
    if folder is not None:
        mon = _open_tab(http, url, "Μηνιαία Ειδοποιητήρια")
        if mon:
            printable = []
            for cells in _dt_rows(mon["html"]):
                text = [_cell(c) for c in cells]
                btn = next((m.group(1) for c in cells for m in [re.search(r'<button\b[^>]*id="([^"]*)"', c)] if m), "")
                month = next((month_key(x) for x in text if month_key(x)), "")
                if btn and month:
                    printable.append((month, btn, text))
            if printable:
                latest = max(p[0] for p in printable)
                for n, (month, btn, text) in enumerate(p for p in printable if p[0] == latest):
                    kind = "TEKA" if any(re.search(r"ΤΕΚΑ|TEKA", x) for x in text) else "EFKA"
                    res = http.follow("POST", url, {mon["form"]: mon["form"], btn: btn,
                                                    mon["view"] + "_activeIndex": mon["tab"]["index"],
                                                    "jakarta.faces.ViewState": mon["vs"]})
                    if res.get("is_pdf"):
                        folder.mkdir(parents=True, exist_ok=True)
                        path = folder / f"EFKA_{kind}_{afm}_{latest}{'' if n == 0 else f'_{n + 1}'}.pdf"
                        path.write_bytes(res["content"])
                        pdfs.append(str(path))
                for line in lines:
                    if line["ref"].split("|")[1] == latest and pdfs:
                        line["pdf_path"] = pdfs[0]
    return {"lines": lines, "pdfs": pdfs}
