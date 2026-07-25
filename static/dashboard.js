// dashboard.js — Planet Express dashboard interactions. All data is already
// server-rendered on page load (no fetch calls here); this only handles
// client-side view state: which tab is showing, the network router filter,
// and dismissing the pending-plan card. "Approve" is a plain link to Telegram,
// not JS-driven -- this dashboard has no route to actually approve anything.
(function () {
  "use strict";

  function setActiveTab(name) {
    document.querySelectorAll(".tab").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.tab === name);
    });
    document.querySelectorAll(".tab-panel").forEach(function (panel) {
      panel.classList.toggle("active", panel.dataset.tabPanel === name);
    });
    document.querySelectorAll(".speech-line").forEach(function (line) {
      line.classList.toggle("hidden", line.dataset.tabLine !== name);
    });
  }

  var TAB_NAMES = ["overview", "backups", "network", "actions"];

  document.querySelectorAll(".tab").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setActiveTab(btn.dataset.tab);
      window.location.hash = btn.dataset.tab;
    });
  });

  // The 60s meta-refresh reloads this same URL, and browsers preserve the
  // fragment across that reload -- so the hash is what survives auto-refresh.
  function applyHashTab() {
    var hashTab = window.location.hash.slice(1);
    if (TAB_NAMES.indexOf(hashTab) !== -1) {
      setActiveTab(hashTab);
    }
  }
  applyHashTab();
  // Tab clicks push a new fragment onto history, so Back/Forward change the
  // hash without re-running this script -- listen for that too, or the visible
  // tab and the URL drift apart.
  window.addEventListener("hashchange", applyHashTab);

  var filterInput = document.getElementById("net-filter");
  if (filterInput) {
    var rows = Array.prototype.slice.call(document.querySelectorAll(".net-filter-row"));
    var counter = document.getElementById("net-count");
    var total = rows.length;

    filterInput.addEventListener("input", function () {
      var q = filterInput.value.trim().toLowerCase();
      var shown = 0;
      rows.forEach(function (row) {
        var match = !q || row.dataset.filterText.indexOf(q) !== -1;
        row.classList.toggle("hidden", !match);
        if (match) shown++;
      });
      if (counter) counter.textContent = "showing " + shown + " of " + total;
    });
  }

  // Dismissal only lasts for this browser session and only for the plan ID
  // dismissed -- the 60s meta-refresh reloads the page, and a plan ID is
  // reused as "the same plan" across reloads but a *different* plan ID
  // (new pending plan) should always show up regardless of a past dismissal.
  var DISMISS_KEY = "planetexpress-dismissed-plan-id";

  document.querySelectorAll(".plan-card").forEach(function (card) {
    if (card.dataset.planId && card.dataset.planId === sessionStorage.getItem(DISMISS_KEY)) {
      card.classList.add("hidden");
    }
  });

  document.querySelectorAll(".btn-dismiss").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var card = btn.closest(".plan-card");
      if (!card) return;
      card.classList.add("hidden");
      if (card.dataset.planId) {
        sessionStorage.setItem(DISMISS_KEY, card.dataset.planId);
      }
    });
  });

  // Hull Diagnostics low/medium drawer -- one folded disclosure instead of two,
  // so routine lows/mediums can never bury a real signal. Client-side only, same
  // data the server already rendered inside it.
  document.querySelectorAll("[data-hull-drawer-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var drawer = btn.closest("[data-hull-drawer]");
      if (drawer) drawer.classList.toggle("open");
    });
  });

  // Routing matrix: click a node to expand it in place (full rule/service/router
  // id/status). All detail markup is already server-rendered, just visually
  // collapsed -- no refetch, matches the low/medium drawer's pattern.
  document.querySelectorAll("[data-net-node]").forEach(function (node) {
    function toggle() {
      node.classList.toggle("expanded");
    }
    node.addEventListener("click", toggle);
    node.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });
  });

  // Deployment manifest: group the flat update-history list (rendered into a JSON
  // data island, not fetched) into "runs" -- a batch update writes many entries
  // seconds apart, so cluster by stack + a small time-proximity gap and render a
  // run card + timeline + chip board per cluster. Same data as the old flat table,
  // just reorganized client-side -- no new backend endpoint.
  (function buildDeployManifest() {
    var dataEl = document.getElementById("update-history-data");
    var manifestEl = document.querySelector("[data-deploy-manifest]");
    if (!dataEl || !manifestEl) return;

    var entries;
    try {
      entries = JSON.parse(dataEl.textContent);
    } catch (e) {
      return;
    }
    if (!entries || !entries.length) return;

    var GAP_MS = 10 * 60 * 1000;
    // Cluster in chronological order (oldest first); the source list is newest-first.
    var chrono = entries.slice().reverse();
    var runs = [];
    chrono.forEach(function (e) {
      var t = new Date(e.ts).getTime();
      var last = runs[runs.length - 1];
      if (last && last.stack === e.stack && !isNaN(t) && (t - last.lastTs) <= GAP_MS) {
        last.entries.push(e);
        last.lastTs = t;
      } else {
        runs.push({ stack: e.stack, entries: [e], lastTs: t });
      }
    });
    runs.reverse(); // newest run first

    function fmtTime(ts) {
      var d = new Date(ts);
      if (isNaN(d.getTime())) return ts;
      return d.toISOString().slice(11, 16) + "z";
    }
    function fmtSpan(ms) {
      var mins = Math.round(ms / 60000);
      if (mins < 1) return "<1 min";
      if (mins < 60) return "~" + mins + " min";
      return "~" + (mins / 60).toFixed(1) + " hr";
    }
    function el(tag, className, text) {
      var node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }

    manifestEl.innerHTML = "";
    runs.forEach(function (run) {
      var first = run.entries[0];
      var lastEntry = run.entries[run.entries.length - 1];
      var hasAlert = run.entries.some(function (e) { return e.is_alert; });
      var startMs = new Date(first.ts).getTime();
      var endMs = new Date(lastEntry.ts).getTime();
      var span = Math.max(endMs - startMs, 0);

      var card = el("div", "deploy-run" + (hasAlert ? " alert" : ""));

      var head = el("div", "deploy-run-head");
      head.appendChild(el("div", "deploy-glyph", "↑"));
      var titleWrap = el("div");
      titleWrap.appendChild(el("div", "deploy-run-title", (run.stack || "unknown") + " stack · batch update"));
      titleWrap.appendChild(el("div", "deploy-run-sub", fmtTime(first.ts) + " → " + fmtTime(lastEntry.ts) + " · " + fmtSpan(span)));
      head.appendChild(titleWrap);
      var countWrap = el("div", "deploy-run-count");
      countWrap.appendChild(el("div", "n", String(run.entries.length)));
      countWrap.appendChild(el("div", "lbl", "SERVICES"));
      head.appendChild(countWrap);
      card.appendChild(head);

      var timeline = el("div", "deploy-timeline");
      timeline.appendChild(el("div", "deploy-timeline-track"));
      run.entries.forEach(function (e) {
        var t = new Date(e.ts).getTime();
        var pct = span > 0 && !isNaN(t) ? ((t - startMs) / span) * 100 : 50;
        var node = el("div", "deploy-timeline-node" + (e.is_alert ? " bad" : ""));
        node.style.left = pct + "%";
        node.title = e.service + " · " + fmtTime(e.ts);
        timeline.appendChild(node);
      });
      timeline.appendChild(el("span", "deploy-timeline-end tl-start", fmtTime(first.ts)));
      timeline.appendChild(el("span", "deploy-timeline-end tl-end", fmtTime(lastEntry.ts)));
      card.appendChild(timeline);

      var chips = el("div", "chip-board");
      run.entries.forEach(function (e) {
        chips.appendChild(el("span", "deploy-chip" + (e.is_alert ? " bad" : ""), (e.is_alert ? "✗ " : "✓ ") + e.service + " · " + fmtTime(e.ts)));
      });
      card.appendChild(chips);

      manifestEl.appendChild(card);
    });
  })();
})();
