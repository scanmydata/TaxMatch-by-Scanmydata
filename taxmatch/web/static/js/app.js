/* TaxMatch — vanilla JS: θέμα/μενού, διάλογος «Νέος πελάτης» με αυτόματη επωνυμία (VIES), πίνακας πελατών (επιλογή,
   φίλτρο, μαζικές ενέργειες), feedback 👍/👎, «Έλεγχος τώρα» και ανάκτηση στοιχείων με πρόοδο.
   Όλα τα μηνύματα/επιβεβαιώσεις/σφάλματα πεδίων εμφανίζονται ΜΕΣΑ στην εφαρμογή (toasts, διάλογος) — ποτέ native
   alert/confirm ή φυσαλίδες επικύρωσης του browser. */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode κ.λπ. */ } },
  };

  async function postJSON(url, body) {
    const r = await fetch(url, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let data = {};
    try { data = await r.json(); } catch (e) { /* όχι JSON */ }
    return { ok: r.ok, status: r.status, data: data };
  }

  // ---------------------------------------------------------------- μηνύματα μέσα στην εφαρμογή
  function toast(msg, level, ms) {
    const dlg = document.querySelector("dialog[open]");          // ανοιχτός διάλογος = top layer: το toast μπαίνει μέσα του
    let box = dlg ? dlg.querySelector(".toasts-host") : $("#toasts");
    if (dlg && !box) { box = document.createElement("div"); box.className = "toasts-host"; dlg.appendChild(box); }
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast " + (level || "info");
    const m = document.createElement("div"); m.className = "msg"; m.textContent = msg;
    const x = document.createElement("button"); x.type = "button"; x.className = "x"; x.textContent = "×"; x.setAttribute("aria-label", "Κλείσιμο");
    x.addEventListener("click", function () { el.remove(); });
    el.appendChild(m); el.appendChild(x); box.appendChild(el);
    if (ms !== 0) setTimeout(function () { el.remove(); }, ms || (level === "danger" ? 12000 : 7000));
  }
  function confirmDialog(msg, okLabel) {
    const d = $("#confirmDialog");
    if (!d || !d.showModal) return Promise.resolve(true);
    $("#confirmMsg").textContent = msg;
    $("#confirmOk").textContent = okLabel || "Συνέχεια";
    return new Promise(function (resolve) {
      const done = function (v) { d.removeEventListener("close", onClose); if (d.open) d.close(); resolve(v); };
      const onClose = function () { resolve(false); };
      $("#confirmOk").onclick = function () { done(true); };
      $("#confirmCancel").onclick = function () { done(false); };
      d.addEventListener("close", onClose, { once: true });
      d.showModal();
      $("#confirmCancel").focus();                  // προεπιλογή το ασφαλές: Άκυρο
    });
  }
  try {                                             // μήνυμα που έμεινε από την προηγούμενη σελίδα (π.χ. μετά από ανανέωση)
    const pending = sessionStorage.getItem("tm-toast");
    if (pending) { sessionStorage.removeItem("tm-toast"); const t = JSON.parse(pending); toast(t.msg, t.level, t.ms); }
  } catch (e) { /* χωρίς sessionStorage */ }
  function toastAfterReload(msg, level) { try { sessionStorage.setItem("tm-toast", JSON.stringify({ msg: msg, level: level })); } catch (e) { toast(msg, level); } }

  // ---------------------------------------------------------------- έλεγχος πεδίων (αντί για τις φυσαλίδες του browser)
  function fieldLabel(el) {
    const id = el.id && document.querySelector('label[for="' + el.id + '"]');
    let lab = id || (el.closest("div,.frow") && el.closest("div,.frow").querySelector("label"));
    let t = lab ? lab.textContent.replace(/[*:]/g, "").replace(/\s+/g, " ").trim() : (el.name || "πεδίο");
    return t.length > 40 ? t.slice(0, 40) : t;
  }
  function invalidMessage(el) {
    const v = el.validity, label = "«" + fieldLabel(el) + "»";
    if (v.valueMissing) return el.type === "file" ? "Επιλέξτε αρχείο." : "Συμπληρώστε το πεδίο " + label + ".";
    if (v.rangeUnderflow || v.rangeOverflow) return "Η τιμή του " + label + " πρέπει να είναι από " + (el.min || "…") + " έως " + (el.max || "…") + ".";
    if (v.typeMismatch || v.patternMismatch) return "Μη έγκυρη τιμή στο πεδίο " + label + ".";
    if (v.tooShort) return "Το πεδίο " + label + " είναι πολύ σύντομο.";
    return "Ελέγξτε το πεδίο " + label + ".";
  }
  $$("form").forEach(function (f) { f.setAttribute("novalidate", ""); });
  document.addEventListener("input", function (e) { if (e.target.classList) e.target.classList.remove("invalid-field"); });

  // ---------------------------------------------------------------- θέμα, tooltips, μαζεμένο μενού
  // Το native παράθυρο (τίτλος/μπάρα) ακολουθεί το θέμα μέσω pywebview (desktop.py) — αθόρυβο όταν τρέχει σε browser.
  function syncTitlebar(theme) {
    try { if (window.pywebview && window.pywebview.api && window.pywebview.api.set_theme) window.pywebview.api.set_theme(theme); } catch (e) { /* browser */ }
  }
  window.addEventListener("pywebviewready", function () { syncTitlebar(document.documentElement.getAttribute("data-theme") || "dark"); });
  syncTitlebar(document.documentElement.getAttribute("data-theme") || "dark");
  const themeToggle = $("#themeToggle");
  if (themeToggle) {
    themeToggle.checked = document.documentElement.getAttribute("data-theme") === "light";
    themeToggle.addEventListener("change", function () {
      const t = themeToggle.checked ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", t);
      store.set("tm-theme", t);
      syncTitlebar(t);
    });
  }
  function applyTips(on) {
    $$("[title],[data-title]").forEach(function (el) {
      if (on && el.hasAttribute("data-title")) { el.setAttribute("title", el.getAttribute("data-title")); el.removeAttribute("data-title"); }
      else if (!on && el.hasAttribute("title")) { el.setAttribute("data-title", el.getAttribute("title")); el.removeAttribute("title"); }
    });
  }
  const tipToggle = $("#tipToggle");
  if (tipToggle) {
    tipToggle.checked = store.get("tm-tips") !== "0";
    applyTips(tipToggle.checked);
    tipToggle.addEventListener("change", function () { store.set("tm-tips", tipToggle.checked ? "1" : "0"); applyTips(tipToggle.checked); });
  }
  if (store.get("tm-collapsed") === "1") document.body.classList.add("collapsed");
  const collapseBtn = $("#collapseBtn");
  if (collapseBtn) collapseBtn.addEventListener("click", function () {
    document.body.classList.toggle("collapsed");
    store.set("tm-collapsed", document.body.classList.contains("collapsed") ? "1" : "0");
  });

  // ---------------------------------------------------------------- διάλογοι
  document.addEventListener("click", function (e) {
    const opener = e.target.closest("[data-open]");
    if (opener) {
      const d = document.getElementById(opener.dataset.open);
      if (d && d.showModal) { d.showModal(); const f = d.querySelector("[autofocus],input:not([type=hidden])"); if (f) f.focus(); }
    }
    if (e.target.closest("[data-close]")) { const d = e.target.closest("dialog"); if (d) d.close(); }
    if (e.target.tagName === "DIALOG") e.target.close();        // κλικ στο σκούρο φόντο
  });

  // Deep-link: /clients#new-client ανοίγει τον διάλογο (και για μελλοντική ενσωμάτωση από άλλη εφαρμογή της σουίτας)
  if (location.hash === "#new-client") { const d = $("#clientDialog"); if (d && d.showModal) d.showModal(); }

  // ---------------------------------------------------------------- αυτόματη επωνυμία από ΑΦΜ (τοπικά, μετά VIES)
  const afmInput = $("#cd_afm"), nameInput = $("#cd_name"), statusLine = $("#cd_status"), lookupBtn = $("#cd_lookup");
  let lastLooked = "", busy = false;
  function say(text, cls) { if (statusLine) { statusLine.textContent = text; statusLine.className = "status-line " + (cls || "muted"); } }
  async function lookupAfm(force) {
    const afm = (afmInput.value || "").replace(/\D/g, "");
    if (afm.length !== 9) { say("Το ΑΦΜ πρέπει να έχει 9 ψηφία.", "st-bad"); return; }
    if (busy || (!force && afm === lastLooked)) return;
    if (!force && nameInput.value.trim()) return;                 // δεν αντικαθιστούμε ό,τι έγραψε ο χρήστης
    busy = true; lastLooked = afm; lookupBtn.disabled = true; say("Αναζήτηση στο VIES…", "muted");
    try {
      const r = await fetch("/api/lookup-afm?afm=" + afm, { credentials: "same-origin" });
      const d = await r.json();
      if (d.ok) {
        if (force || !nameInput.value.trim()) nameInput.value = d.name;
        const bits = [d.source === "local" ? "Βρέθηκε στους πελάτες σας: " : "Βρέθηκε στο VIES: ", d.name];
        say(bits.join("") + (d.exists ? " (ο πελάτης υπάρχει ήδη)" : "") + (d.checksum_ok ? "" : " — προσοχή: το ΑΦΜ δεν περνά τον έλεγχο ψηφίου"),
            d.exists ? "st-warn" : "st-ok");
      } else say(d.error || "Δεν βρέθηκε επωνυμία.", "st-warn");
    } catch (err) { say("Δεν ήταν δυνατή η αναζήτηση — γράψτε την επωνυμία χειροκίνητα.", "st-warn"); }
    busy = false; lookupBtn.disabled = false;
  }
  if (afmInput) {
    afmInput.addEventListener("input", function () {
      afmInput.value = afmInput.value.replace(/\D/g, "").slice(0, 9);
      // ΜΟΝΟ στο 9ο ψηφίο: με λιγότερα θα ξεκινούσε πρόωρη (λάθος) αναζήτηση
      if (afmInput.value.length === 9) lookupAfm(false); else { lastLooked = ""; say(""); }
    });
    lookupBtn.addEventListener("click", function () { lookupAfm(true); });
  }

  // ---------------------------------------------------------------- φίλτρο στηλών (χωνί, στυλ Excel — timologio downloader)
  // Κλικ στο χωνί μιας επικεφαλίδας ανοίγει λίστα με τις τιμές της στήλης (αναζήτηση + «(Όλα)» + κουτάκια)· η
  // επιλογή φιλτράρει τον πίνακα ζωντανά και συνδυάζεται με την αναζήτηση κειμένου/το φίλτρο κατάστασης.
  function cellText(row, col) {
    const cell = row.children[col];
    return cell ? cell.textContent.replace(/\s+/g, " ").trim() : "";
  }
  let openColFilterPopup = null;
  function closeColFilterPopup() { if (openColFilterPopup) { openColFilterPopup.remove(); openColFilterPopup = null; } }
  document.addEventListener("click", function (e) { if (openColFilterPopup && !openColFilterPopup.contains(e.target) && !e.target.closest(".th-funnel")) closeColFilterPopup(); });
  function initColumnFilters(table, colIndexes, onChange) {
    const filters = {};                                     // { colIndex: Set(τιμές) }
    const ths = $$("thead th", table);
    colIndexes.forEach(function (col) {
      const th = ths[col];
      if (!th) return;
      th.classList.add("th-filterable");
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "th-funnel";
      btn.setAttribute("aria-label", "Φίλτρο στήλης «" + th.textContent.trim() + "»");
      btn.textContent = "▾";
      th.appendChild(btn);
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        if (openColFilterPopup && openColFilterPopup.dataset.col === String(col)) { closeColFilterPopup(); return; }
        openFilterPopup(table, col, th, btn, filters, onChange);
      });
    });
    return { matches: function (row) {
      return Object.keys(filters).every(function (col) {
        const allowed = filters[col];
        return !allowed || allowed.has(cellText(row, col));
      });
    } };
  }
  function openFilterPopup(table, col, th, anchorBtn, filters, onChange) {
    closeColFilterPopup();
    const rows = $$("tbody tr", table);
    const values = Array.from(new Set(rows.map(r => cellText(r, col)))).sort(function (a, b) { return a.localeCompare(b, "el"); });
    const selected = filters[col] || new Set(values);
    const pop = document.createElement("div");
    pop.className = "col-filter-popup"; pop.dataset.col = String(col);
    pop.innerHTML = '<input type="search" class="cf-search" placeholder="Αναζήτηση τιμής…">' +
      '<label class="cf-item cf-all"><input type="checkbox" class="cf-all-chk"><span>(Όλα)</span></label>' +
      '<div class="cf-list"></div><button type="button" class="btn cf-close">Κλείσιμο</button>';
    const list = pop.querySelector(".cf-list"), allChk = pop.querySelector(".cf-all-chk"), search = pop.querySelector(".cf-search");
    function syncAll() {
      const boxes = $$("input", list);
      const checked = boxes.filter(b => b.checked).length;
      allChk.checked = boxes.length > 0 && checked === boxes.length;
      allChk.indeterminate = checked > 0 && checked < boxes.length;
    }
    function build(needle) {
      list.innerHTML = "";
      values.filter(v => !needle || v.toLocaleLowerCase("el").indexOf(needle) >= 0).forEach(function (v) {
        const row = document.createElement("label"); row.className = "cf-item";
        const chk = document.createElement("input"); chk.type = "checkbox"; chk.value = v; chk.checked = selected.has(v);
        const span = document.createElement("span"); span.textContent = v || "—";
        row.appendChild(chk); row.appendChild(span); list.appendChild(row);
      });
      syncAll();
    }
    function apply() {
      const checkedValues = $$("input", list).filter(b => b.checked).map(b => b.value);
      if (checkedValues.length === values.length || checkedValues.length === 0) delete filters[col];
      else filters[col] = new Set(checkedValues);
      th.classList.toggle("active-filter", !!filters[col]);
      onChange();
    }
    list.addEventListener("change", function (e) { if (e.target.matches("input")) { syncAll(); apply(); } });
    allChk.addEventListener("change", function () { $$("input", list).forEach(b => { b.checked = allChk.checked; }); apply(); });
    search.addEventListener("input", function () { build(search.value.trim().toLocaleLowerCase("el")); });
    pop.querySelector(".cf-close").addEventListener("click", closeColFilterPopup);
    document.body.appendChild(pop);
    build("");
    const r = anchorBtn.getBoundingClientRect();
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 12)) + "px";
    pop.style.top = (r.bottom + window.scrollY + 4) + "px";
    openColFilterPopup = pop;
  }

  // ---------------------------------------------------------------- πίνακας πελατών
  const table = $("#clientsTable");
  if (table) {
    const rows = $$("tbody tr", table), all = $("#selAll"), filterInput = $("#tblFilter"), statusSel = $("#tblStatus"), sb = $("#statusbar");
    const colFilters = initColumnFilters(table, [3, 4, 6, 7, 8, 9], applyFilter);
    const selected = () => rows.filter(r => r.querySelector(".rowchk").checked);
    function refresh() {
      const n = selected().length;
      rows.forEach(r => r.classList.toggle("sel", r.querySelector(".rowchk").checked));
      $$("[data-needs-selection]").forEach(b => { b.disabled = n === 0; });
      const label = $("#selLabel");
      if (label) label.textContent = n ? n + " επιλεγμένοι" : "Κανένας πελάτης επιλεγμένος";
      if (sb) sb.textContent = sb.dataset.default + (n ? " · " + n + " επιλεγμένοι" : "");
    }
    function applyFilter() {
      const q = (filterInput.value || "").toLocaleLowerCase("el"), st = statusSel.value;
      rows.forEach(function (r) {
        let ok = !q || r.textContent.toLocaleLowerCase("el").indexOf(q) >= 0;
        if (ok && st) ok = st === "creds" ? r.dataset.creds === "1" : st === "nocreds" ? r.dataset.creds !== "1"
          : st === "badcreds" ? r.dataset.credstatus === "invalid" : (st === "ceased" || st === "active") ? r.dataset.activity === st
          : r.dataset.status === st;
        if (ok) ok = colFilters.matches(r);
        r.classList.toggle("hidden", !ok);
        if (!ok) r.querySelector(".rowchk").checked = false;
      });
      refresh();
    }
    all.addEventListener("change", function () {
      rows.forEach(r => { if (!r.classList.contains("hidden")) r.querySelector(".rowchk").checked = all.checked; });
      refresh();
    });
    rows.forEach(r => r.querySelector(".rowchk").addEventListener("change", refresh));
    filterInput.addEventListener("input", applyFilter);
    statusSel.addEventListener("change", applyFilter);
    $("#btnSelAll").addEventListener("click", function () { all.checked = true; all.dispatchEvent(new Event("change")); });
    $("#btnSelNone").addEventListener("click", function () { all.checked = false; rows.forEach(r => r.querySelector(".rowchk").checked = false); refresh(); });
    $$("[data-bulk]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const afms = selected().map(r => r.dataset.afm);
        if (!afms.length) return;
        const msg = btn.dataset.confirm && btn.dataset.confirm.replace("{n}", afms.length);
        if (!msg) { submitBulk(btn.dataset.bulk, afms, {}); return; }
        const dlg = btn.closest("dialog"); if (dlg) dlg.close();
        confirmDialog(msg, "Ναι, συνέχεια").then(function (yes) { if (yes) submitBulk(btn.dataset.bulk, afms, {}); });
      });
    });
    const credForm = $("#bulkCredForm");
    if (credForm) credForm.addEventListener("submit", function (e) {
      e.preventDefault();
      const afms = selected().map(r => r.dataset.afm);
      if (!afms.length) return;
      submitBulk("set_creds", afms, { taxis_user: credForm.taxis_user.value, taxis_pass: credForm.taxis_pass.value });
    });
    function submitBulk(action, afms, extra) {
      const f = document.createElement("form");
      f.method = "post"; f.action = "/clients/bulk";
      const add = (k, v) => { const i = document.createElement("input"); i.type = "hidden"; i.name = k; i.value = v; f.appendChild(i); };
      add("action", action); afms.forEach(a => add("afms", a)); Object.keys(extra).forEach(k => add(k, extra[k]));
      document.body.appendChild(f); f.submit();
    }
    const wanted = location.hash.replace("#", "");            // π.χ. /clients#badcreds από την ειδοποίηση
    if (wanted && Array.from(statusSel.options).some(o => o.value === wanted)) { statusSel.value = wanted; applyFilter(); }
    else refresh();
  }

  // ---------------------------------------------------------------- feedback
  document.addEventListener("click", async function (e) {
    const btn = e.target.closest(".fb-btn");
    if (!btn) return;
    const box = btn.closest(".fb");
    const current = parseInt(box.dataset.value || "0", 10), clicked = parseInt(btn.dataset.v, 10);
    const next = current === clicked ? 0 : clicked;             // δεύτερο κλικ = καθάρισμα
    const res = await postJSON("/api/feedback", { match_id: parseInt(box.dataset.match, 10), value: next });
    if (!res.ok) return;
    box.dataset.value = String(next);
    $$(".fb-btn", box).forEach(b => b.classList.toggle("on", parseInt(b.dataset.v, 10) === next));
  });

  // ---------------------------------------------------------------- νέα/ειδοποιήσεις: πρώτα προεπισκόπηση, μετά ο σύνδεσμος
  // Κλικ σε άρθρο/υποχρέωση ΔΕΝ πηγαίνει κατευθείαν στον browser — ανοίγει popup μέσα στην εφαρμογή με ό,τι ήδη
  // ξέρουμε (τίτλος, περίληψη, ενέργεια) και ο χρήστης αποφασίζει αν θα ανοίξει τον σύνδεσμο.
  const newsDialog = $("#newsDialog");
  document.addEventListener("click", function (e) {
    const a = e.target.closest("a.ext");
    if (!a) return;
    e.preventDefault();
    if (!newsDialog || !newsDialog.showModal) { postJSON("/api/open-external", { url: a.href }); return; }
    $("#newsTitle").textContent = a.textContent.trim();
    const meta = [a.dataset.source, a.dataset.date].filter(Boolean).join(" · ");
    const metaEl = $("#newsMeta"); metaEl.textContent = meta; metaEl.style.display = meta ? "" : "none";
    const summaryEl = $("#newsSummary"); summaryEl.textContent = a.dataset.summary || "";
    summaryEl.style.display = a.dataset.summary ? "" : "none";
    const actionEl = $("#newsAction");
    if (a.dataset.action) { actionEl.textContent = "➜ " + a.dataset.action; actionEl.style.display = ""; }
    else actionEl.style.display = "none";
    const openLink = $("#newsOpenLink");
    openLink.href = a.href;
    openLink.onclick = function (ev) { ev.preventDefault(); postJSON("/api/open-external", { url: a.href }); newsDialog.close(); };
    newsDialog.showModal();
  });
  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (!form.checkValidity()) {                                 // μήνυμα μέσα στην εφαρμογή, όχι φυσαλίδα browser
      e.preventDefault(); e.stopImmediatePropagation();
      const bad = form.querySelector(":invalid");
      if (bad) { bad.classList.add("invalid-field"); bad.focus(); toast(invalidMessage(bad), "danger"); }
      return;
    }
    const submitter = e.submitter;                                // το κουμπί που πάτησε ο χρήστης (όταν η φόρμα έχει πολλές ενέργειες)
    const msg = (submitter && submitter.dataset && submitter.dataset.confirm) || (form.dataset && form.dataset.confirm);
    if (msg && !form._confirmed) {
      e.preventDefault(); e.stopImmediatePropagation();
      confirmDialog(msg, "Ναι, συνέχεια").then(function (yes) {
        if (!yes) return;
        form._confirmed = true;
        if (form.requestSubmit) form.requestSubmit(submitter || undefined); else form.submit();
      });
    }
  }, true);

  // ---------------------------------------------------------------- job bar (έλεγχος + ουρά ανάκτησης στοιχείων)
  const bar = $("#jobbar"), barMsg = $("#jobmsg");
  let sawMain = false, sawLookup = false, timer = null, lastLookup = null;
  const runBtns = $$("[data-run-check]");
  const setBusy = busy => runBtns.forEach(b => { b.disabled = busy; });
  function lookupSummary(l) {
    const parts = [];
    if (l.ok) parts.push(l.ok + " πλήρη");
    if (l.partial) parts.push(l.partial + " μερικά");
    if (l.failed) parts.push(l.failed + " χωρίς αποτέλεσμα");
    if (l.pending) parts.push(l.pending + " σε αναμονή (δεν βρέθηκε πηγή)");
    return "Ανάκτηση στοιχείων: " + l.done + " πελάτες" + (parts.length ? " — " + parts.join(", ") : "") + ".";
  }
  function finishLookup(l) {
    let level = "ok", msg = lookupSummary(l);
    if (l.bad_creds || l.bad_office_creds) {
      level = "danger";
      msg += " ΛΑΘΟΣ κωδικοί TAXISnet: " + (l.bad_creds ? l.bad_creds + " πελάτες" : "") + (l.bad_creds && l.bad_office_creds ? " και " : "")
        + (l.bad_office_creds ? "κωδικοί γραφείου" : "") + " — διορθώστε τους (δείτε την ειδοποίηση πάνω).";
    } else if (l.failed || l.pending) level = "warn";
    toastAfterReload(msg, level);
    setTimeout(function () { window.location.reload(); }, 600);
  }
  function render(s) {
    if (!bar) return;
    const l = s.lookup || {};
    if (s.running) {
      sawMain = true; bar.className = "on"; barMsg.textContent = s.message || "Σε εξέλιξη…"; setBusy(true);
    } else if (sawMain) {
      sawMain = false;
      bar.className = "on done"; barMsg.textContent = s.error ? "Σφάλμα: " + s.error : "Ολοκληρώθηκε ✓"; setBusy(false);
      if (s.error) toast(s.error, "danger", 0);
      else if (!l.running) setTimeout(function () { window.location.reload(); }, 900);
    } else setBusy(false);
    if (l.running) {
      sawLookup = true; lastLookup = l;
      if (!s.running) { bar.className = "on"; barMsg.textContent = (l.message || "Ανάκτηση στοιχείων…") + (l.queued ? " · " + l.queued + " στην ουρά" : ""); }
    } else if (sawLookup) {
      sawLookup = false;
      if (!s.running) bar.className = "";
      finishLookup(l && l.total ? l : lastLookup);
    }
    if (!s.running && !l.running && !sawMain) { clearInterval(timer); timer = null; }
  }
  async function poll() {
    try {
      const s = await (await fetch("/api/job", { credentials: "same-origin" })).json();
      render(s);
      if ((s.running || (s.lookup && s.lookup.running)) && !timer) timer = setInterval(poll, 1200);
    } catch (e) { /* προσωρινό σφάλμα */ }
  }
  runBtns.forEach(b => b.addEventListener("click", async function () {
    setBusy(true);
    const r = await postJSON("/api/run", {});
    if (!r.ok && r.status === 409) toast("Ένας έλεγχος τρέχει ήδη.", "warn");
    sawMain = true;
    await poll();
    if (!timer) timer = setInterval(poll, 1200);
  }));
  poll();
})();
