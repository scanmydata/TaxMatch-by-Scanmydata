/* TaxMatch — μικρό vanilla JS: feedback 👍/👎, «Έλεγξε τώρα» με πρόοδο, εξωτερικοί σύνδεσμοι, επιβεβαιώσεις. */
(function () {
  "use strict";

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await r.json(); } catch (e) { /* όχι JSON */ }
    return { ok: r.ok, status: r.status, data: data };
  }

  // ---- feedback --------------------------------------------------------------
  document.addEventListener("click", async function (e) {
    const btn = e.target.closest(".fb-btn");
    if (!btn) return;
    const box = btn.closest(".fb");
    const current = parseInt(box.dataset.value || "0", 10);
    const clicked = parseInt(btn.dataset.v, 10);
    const next = current === clicked ? 0 : clicked;          // δεύτερο κλικ = καθάρισμα
    const res = await postJSON("/api/feedback", { match_id: parseInt(box.dataset.match, 10), value: next });
    if (!res.ok) return;
    box.dataset.value = String(next);
    box.querySelectorAll(".fb-btn").forEach(function (b) {
      b.classList.toggle("on", parseInt(b.dataset.v, 10) === next);
    });
  });

  // ---- εξωτερικοί σύνδεσμοι: στον browser του συστήματος ---------------------------
  document.addEventListener("click", function (e) {
    const a = e.target.closest("a.ext");
    if (!a) return;
    e.preventDefault();
    postJSON("/api/open-external", { url: a.href });
  });

  // ---- επιβεβαίωση ------------------------------------------------------------
  document.addEventListener("submit", function (e) {
    const msg = e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // ---- job bar (έλεγχος / lookup / εμπλουτισμός) -------------------------------------
  const bar = document.getElementById("jobbar");
  const barMsg = document.getElementById("jobmsg");
  const runBtns = document.querySelectorAll("[data-run-check]");
  let sawRunning = false, timer = null;

  function setBusy(busy) { runBtns.forEach(function (b) { b.disabled = busy; }); }

  function render(s) {
    if (!bar) return;
    if (s.running) {
      sawRunning = true;
      bar.className = "on";
      barMsg.textContent = s.message || "Σε εξέλιξη…";
      setBusy(true);
    } else if (sawRunning) {
      bar.className = "on done";
      barMsg.textContent = s.error ? "Σφάλμα: " + s.error : "Ολοκληρώθηκε ✓";
      setBusy(false);
      clearInterval(timer);
      if (!s.error) setTimeout(function () { window.location.reload(); }, 900);
    } else {
      setBusy(false);
    }
  }

  async function poll() {
    try {
      const r = await fetch("/api/job", { credentials: "same-origin" });
      const s = await r.json();
      render(s);
      if (s.running && !timer) timer = setInterval(poll, 1200);
    } catch (e) { /* προσωρινό σφάλμα — δοκιμή στον επόμενο κύκλο */ }
  }

  runBtns.forEach(function (b) {
    b.addEventListener("click", async function () {
      setBusy(true);
      const res = await postJSON("/api/run", {});
      if (res.status === 409) { /* τρέχει ήδη κάτι — απλώς παρακολουθούμε */ }
      sawRunning = true;
      await poll();
      if (!timer) timer = setInterval(poll, 1200);
    });
  });

  poll();
})();
