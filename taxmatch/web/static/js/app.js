/* TaxMatch — vanilla JS: θέμα/μενού, διάλογος «Νέος πελάτης» με αυτόματη επωνυμία (VIES), πίνακας πελατών (επιλογή,
   φίλτρο, μαζικές ενέργειες), feedback 👍/👎, «Έλεγχος τώρα» με πρόοδο. */
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

  // ---------------------------------------------------------------- θέμα, tooltips, μαζεμένο μενού
  const themeToggle = $("#themeToggle");
  if (themeToggle) {
    themeToggle.checked = document.documentElement.getAttribute("data-theme") === "light";
    themeToggle.addEventListener("change", function () {
      const t = themeToggle.checked ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", t);
      store.set("tm-theme", t);
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

  // ---------------------------------------------------------------- πίνακας πελατών
  const table = $("#clientsTable");
  if (table) {
    const rows = $$("tbody tr", table), all = $("#selAll"), filterInput = $("#tblFilter"), statusSel = $("#tblStatus"), sb = $("#statusbar");
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
        if (ok && st) ok = st === "creds" ? r.dataset.creds === "1" : st === "nocreds" ? r.dataset.creds !== "1" : r.dataset.status === st;
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
        if (msg && !window.confirm(msg)) return;
        submitBulk(btn.dataset.bulk, afms, {});
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
    refresh();
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

  // ---------------------------------------------------------------- εξωτερικοί σύνδεσμοι / επιβεβαιώσεις
  document.addEventListener("click", function (e) {
    const a = e.target.closest("a.ext");
    if (!a) return;
    e.preventDefault();
    postJSON("/api/open-external", { url: a.href });
  });
  document.addEventListener("submit", function (e) {
    const msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // ---------------------------------------------------------------- job bar
  const bar = $("#jobbar"), barMsg = $("#jobmsg");
  let sawRunning = false, timer = null;
  const runBtns = $$("[data-run-check]");
  const setBusy = busy => runBtns.forEach(b => { b.disabled = busy; });
  function render(s) {
    if (!bar) return;
    if (s.running) {
      sawRunning = true; bar.className = "on"; barMsg.textContent = s.message || "Σε εξέλιξη…"; setBusy(true);
    } else if (sawRunning) {
      bar.className = "on done"; barMsg.textContent = s.error ? "Σφάλμα: " + s.error : "Ολοκληρώθηκε ✓"; setBusy(false);
      clearInterval(timer);
      if (!s.error) setTimeout(function () { window.location.reload(); }, 900);
    } else setBusy(false);
  }
  async function poll() {
    try {
      const s = await (await fetch("/api/job", { credentials: "same-origin" })).json();
      render(s);
      if (s.running && !timer) timer = setInterval(poll, 1200);
    } catch (e) { /* προσωρινό σφάλμα */ }
  }
  runBtns.forEach(b => b.addEventListener("click", async function () {
    setBusy(true);
    await postJSON("/api/run", {});
    sawRunning = true;
    await poll();
    if (!timer) timer = setInterval(poll, 1200);
  }));
  poll();
})();
