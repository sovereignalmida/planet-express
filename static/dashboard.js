// dashboard.js — Planet Express dashboard interactions. Data is server-rendered on
// first load; after that, a fetch()-based refresh swaps in fresh server-rendered HTML
// every 60s (see refreshDashboard() below) instead of the page doing a hard reload --
// a <meta http-equiv="refresh"> reload blanks the whole page and repaints from scratch,
// which reads as a jarring full-screen flash. Fetching the same URL and replacing just
// #dashboard-live's contents keeps the browser tab/scroll/focus alive and never blanks.
// "Approve" is a plain link to Telegram, not JS-driven -- this dashboard has no route
// to actually approve anything.
(function () {
  "use strict";

  var TAB_NAMES = ["overview", "backups", "network", "actions"];

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

  // A refresh swap re-renders from the server default (Overview active) -- reapply
  // whichever tab the hash says is current so a refresh mid-Backups-tab doesn't
  // silently bounce the view back to Overview.
  function applyHashTab() {
    var hashTab = window.location.hash.slice(1);
    if (TAB_NAMES.indexOf(hashTab) !== -1) {
      setActiveTab(hashTab);
    }
  }

  // Dismissal only lasts for this browser session and only for the plan ID dismissed --
  // a refresh (or a real reload) should keep a dismissed plan hidden, but a *different*
  // plan ID (new pending plan) should always show up regardless of a past dismissal.
  var DISMISS_KEY = "planetexpress-dismissed-plan-id";

  // Everything in here binds to DOM nodes -- must re-run after every refresh swap
  // (fresh nodes from the fetched HTML have no listeners of their own yet).
  function bindInteractions() {
    document.querySelectorAll(".tab").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setActiveTab(btn.dataset.tab);
        window.location.hash = btn.dataset.tab;
      });
    });

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

    // Hull Diagnostics low/medium drawer -- one folded disclosure instead of two, so
    // routine lows/mediums can never bury a real signal. Client-side only, same data
    // the server already rendered inside it.
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

    buildDeployManifest();
  }

  // Deployment manifest: group the flat update-history list (rendered into a JSON data
  // island, not fetched) into "runs" -- a batch update writes many entries seconds
  // apart, so cluster by stack + a small time-proximity gap and render a run card +
  // timeline + chip board per cluster. Same data as the old flat table, just
  // reorganized client-side -- no new backend endpoint.
  function buildDeployManifest() {
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

    // Full local date/time + the browser's own timezone abbreviation -- the source
    // timestamps are UTC ISO strings, but a sysadmin reading "05:16:04" wants to know
    // it's in their own timezone, not have to mentally convert from UTC.
    function fmtDateTime(ts) {
      var d = new Date(ts);
      if (isNaN(d.getTime())) return { date: ts, time: ts, tz: "" };
      var tz = "";
      try {
        var part = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(d)
          .find(function (p) { return p.type === "timeZoneName"; });
        tz = part ? part.value : "";
      } catch (e) { /* Intl unavailable -- degrade to no tz label */ }
      return {
        date: d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }),
        time: d.toLocaleTimeString(undefined, { hour12: false }),
        tz: tz,
      };
    }
    function fmtSpan(ms) {
      var mins = Math.round(ms / 60000);
      if (mins < 1) return "<1 min";
      if (mins < 60) return "~" + mins + " min";
      return "~" + (mins / 60).toFixed(1) + " hr";
    }
    function fmtInterval(ms) {
      var mins = Math.round(ms / 60000);
      return mins < 1 ? "<1 min" : "~" + mins + " min";
    }
    function alertLabel(count) {
      return count ? count + " alert" + (count === 1 ? "" : "s") : "all clean";
    }
    function el(tag, className, text) {
      var node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }

    var summaryEl = document.getElementById("deploy-summary-chip");
    if (summaryEl) {
      var newest = runs[0];
      var newestAlerts = newest.entries.filter(function (e) { return e.is_alert; }).length;
      // "updated" and "no_change" both count as non-alert, but only "updated" actually
      // changed anything -- don't claim a no-op check "updated" a service.
      var allActuallyUpdated = newest.entries.every(function (e) { return e.status === "updated"; });
      summaryEl.classList.toggle("alert", newestAlerts > 0);
      summaryEl.innerHTML = "";
      summaryEl.appendChild(el("span", "dot"));
      summaryEl.appendChild(document.createTextNode(
        newest.entries.length + " service" + (newest.entries.length === 1 ? "" : "s") + " · " +
        (newestAlerts ? newestAlerts + " alert" + (newestAlerts === 1 ? "" : "s") : (allActuallyUpdated ? "all updated" : "all clean"))
      ));
    }

    manifestEl.innerHTML = "";
    runs.forEach(function (run) {
      var first = run.entries[0];
      var lastEntry = run.entries[run.entries.length - 1];
      var hasAlert = run.entries.some(function (e) { return e.is_alert; });
      var startMs = new Date(first.ts).getTime();
      var endMs = new Date(lastEntry.ts).getTime();
      var span = Math.max(endMs - startMs, 0);
      var startDT = fmtDateTime(first.ts);
      var endDT = fmtDateTime(lastEntry.ts);

      var card = el("div", "deploy-run" + (hasAlert ? " alert" : ""));

      var head = el("div", "deploy-run-head");
      head.appendChild(el("div", "deploy-glyph", "↑"));
      var titleWrap = el("div");
      var title = el("div", "deploy-run-title");
      var stackB = document.createElement("b");
      stackB.textContent = run.stack || "unknown";
      title.appendChild(stackB);
      title.appendChild(document.createTextNode(" stack · batch update"));
      titleWrap.appendChild(title);
      var endLabel = endDT.date !== startDT.date ? (endDT.date + " " + endDT.time) : endDT.time;
      titleWrap.appendChild(el(
        "div", "deploy-run-sub",
        startDT.date + " · " + startDT.time + " → " + endLabel + (endDT.tz ? " " + endDT.tz : "") + " · " + fmtSpan(span)
      ));
      head.appendChild(titleWrap);
      var countWrap = el("div", "deploy-run-count");
      countWrap.appendChild(el("div", "n", String(run.entries.length)));
      countWrap.appendChild(el("div", "lbl", "svcs"));
      head.appendChild(countWrap);
      card.appendChild(head);

      var timeline = el("div", "deploy-timeline");
      timeline.appendChild(el("div", "deploy-timeline-track"));
      run.entries.forEach(function (e) {
        var t = new Date(e.ts).getTime();
        var pct = span > 0 && !isNaN(t) ? ((t - startMs) / span) * 100 : 50;
        var node = el("div", "deploy-timeline-node" + (e.is_alert ? " bad" : ""));
        node.style.left = pct + "%";
        node.title = e.service + " · " + fmtDateTime(e.ts).time;
        timeline.appendChild(node);
      });
      timeline.appendChild(el("span", "deploy-timeline-end tl-start", startDT.time));
      timeline.appendChild(el("span", "deploy-timeline-end tl-end", endDT.time));
      card.appendChild(timeline);

      var gaps = [];
      for (var i = 1; i < run.entries.length; i++) {
        var g = new Date(run.entries[i].ts).getTime() - new Date(run.entries[i - 1].ts).getTime();
        if (!isNaN(g)) gaps.push(g);
      }
      var avgGap = gaps.length ? gaps.reduce(function (a, b) { return a + b; }, 0) / gaps.length : 0;
      var runAlerts = run.entries.filter(function (e) { return e.is_alert; }).length;
      card.appendChild(el(
        "div", "deploy-timeline-caption",
        (gaps.length ? "every " + fmtInterval(avgGap) + " · " : "") + alertLabel(runAlerts)
      ));

      var chips = el("div", "chip-board");
      run.entries.forEach(function (e) {
        var chip = el("div", "deploy-chip" + (e.is_alert ? " bad" : ""));
        chip.appendChild(el("span", "svc", (e.is_alert ? "✗ " : "✓ ") + e.service));
        chip.appendChild(el("span", "t", fmtDateTime(e.ts).time));
        chips.appendChild(chip);
      });
      card.appendChild(chips);

      manifestEl.appendChild(card);
    });
  }

  var REFRESH_INTERVAL_MS = 60000;

  function refreshDashboard() {
    // Don't yank focus/typed text out from under someone mid-filter.
    var filterInput = document.getElementById("net-filter");
    if (filterInput && document.activeElement === filterInput) return;

    fetch(window.location.pathname, { cache: "no-store" })
      .then(function (r) {
        if (r.redirected && new URL(r.url).pathname === "/login") {
          window.location.assign(r.url);
          return null;
        }
        if (!r.ok) throw new Error("bad response " + r.status);
        return r.text();
      })
      .then(function (html) {
        if (html === null) return;
        var freshLive = new DOMParser().parseFromString(html, "text/html").getElementById("dashboard-live");
        var currentLive = document.getElementById("dashboard-live");
        if (!freshLive || !currentLive) return;
        currentLive.innerHTML = freshLive.innerHTML;
        applyHashTab();
        bindInteractions();
      })
      .catch(function () {
        // Transient network blip -- next interval tries again, no need to surface
        // an error on a passive read-only dashboard.
      });
  }

  applyHashTab();
  // Tab clicks push a new fragment onto history, so Back/Forward change the hash
  // without re-running this script -- listen for that too, or the visible tab and the
  // URL drift apart. Bound once (not in bindInteractions()) since window itself is
  // never replaced by a refresh swap.
  window.addEventListener("hashchange", applyHashTab);
  bindInteractions();
  setInterval(refreshDashboard, REFRESH_INTERVAL_MS);
})();
