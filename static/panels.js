// panels.js — the panels that left the dashboard in rc14.7.3: NEEDS YOU (Missions) holds the hull
// findings and the open canary rollback windows, DEPLOYMENTS (Memory) holds Zoidberg's deployment
// manifest. Approvals and incidents keep their own scripts (approvals.js, incidents.js), which find
// their panels by id wherever they are. This file draws the manifest, binds SETTLE on a held rollback
// window, and every 60 seconds swaps in fresh server-rendered copies of those panels by id.
(function () {
  "use strict";

  // Close a held canary rollback window. Confirmed first: it releases the update's old image to
  // the next prune, which is the only thing that could restore the service.
  function settleWindow(btn) {
    var label = btn.dataset.label;
    if (!window.confirm("Close the rollback window for " + label + "? Its old image becomes " +
        "prunable, so confirm the service is healthy first.")) return;
    var meta = document.querySelector('meta[name="csrf-token"]');
    var url = "/api/canary/" + encodeURIComponent(btn.dataset.execution) + "/" +
              encodeURIComponent(btn.dataset.step) + "/settle";
    function post() {
      return fetch(url, {
        method: "POST", cache: "no-store",
        body: new URLSearchParams({ csrf_token: meta ? meta.content : "" })
      }).then(function (r) {
        return r.json().then(function (data) { return { r: r, data: data }; });
      });
    }
    btn.disabled = true;
    post().then(function (res) {
      if (res.r.status === 401) { window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname)); return null; }
      if (res.data.reason === "elevation_required" && window.peElevate) {
        return window.peElevate(res.data.error || "This needs your passphrase again.")
          .then(function (ok) { return ok ? post() : null; });
      }
      return res;
    }).then(function (res) {
      if (!res) { btn.disabled = false; return; }
      if (!res.r.ok) throw new Error(res.data.error || "host slow, retry");
      window.alert(res.data.message || "Done.");
      window.location.reload();
    }).catch(function (error) {
      btn.disabled = false;
      window.alert(error.message);
    });
  }

  // Which run the detail pane is showing. Module-level, because manifest-panel's innerHTML is
  // replaced every 60 seconds and a selection held inside buildDeployManifest() would reset to
  // the newest run under the operator every minute.
  var selectedRunKey = null;

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

    // Selection is held by identity, not by index: the list grows at the top, so an index
    // would quietly point at a different run after the next canary pass.
    function runKey(run) { return (run.stack || "unknown") + "@" + run.entries[0].ts; }

    var detailEl = document.getElementById("run-detail");
    var selected = runs.find(function (run) { return runKey(run) === selectedRunKey; }) || runs[0];
    selectedRunKey = runKey(selected);

    function renderDetail(run) {
      if (!detailEl) return;
      var alerts = run.entries.filter(function (e) { return e.is_alert; }).length;
      var startMs = new Date(run.entries[0].ts).getTime();
      var endMs = new Date(run.entries[run.entries.length - 1].ts).getTime();
      var startDT = fmtDateTime(run.entries[0].ts);
      var endDT = fmtDateTime(run.entries[run.entries.length - 1].ts);

      detailEl.innerHTML = "";
      var head = el("div", "run-detail-head");
      head.appendChild(el("span", "run-detail-eyebrow", "RUN DETAIL"));
      head.appendChild(el("div", "pe-spacer"));
      head.appendChild(el("span", "run-detail-badge" + (alerts ? " alert" : ""), alertLabel(alerts).toUpperCase()));
      detailEl.appendChild(head);

      detailEl.appendChild(el("div", "run-detail-title", (run.stack || "unknown") + " · batch update"));
      var endLabel = endDT.date !== startDT.date ? (endDT.date + " " + endDT.time) : endDT.time;
      detailEl.appendChild(el("div", "run-detail-range",
        startDT.date + " · " + startDT.time + " → " + endLabel + (endDT.tz ? " " + endDT.tz : "")));

      var stats = el("div", "run-detail-stats");
      [["SERVICES", String(run.entries.length), ""],
       ["DURATION", fmtSpan(Math.max(endMs - startMs, 0)), ""],
       ["CANARY", alerts ? "alerts" : "pass", alerts ? "alert" : "ok"]].forEach(function (cell) {
        var well = el("div", "run-detail-well");
        well.appendChild(el("div", "run-detail-well-label", cell[0]));
        well.appendChild(el("div", "run-detail-well-value " + cell[2], cell[1]));
        stats.appendChild(well);
      });
      detailEl.appendChild(stats);

      detailEl.appendChild(el("div", "run-detail-eyebrow", "TIMELINE"));
      var list = el("div", "run-timeline");
      run.entries.forEach(function (e) {
        var row = el("div", "run-timeline-row" + (e.is_alert ? " alert" : ""));
        row.appendChild(el("span", "run-timeline-tick", e.is_alert ? "✗" : "✓"));
        row.appendChild(el("span", "run-timeline-service", e.service));
        row.appendChild(el("span", "run-timeline-time", fmtDateTime(e.ts).time));
        list.appendChild(row);
      });
      detailEl.appendChild(list);

      var note = el("div", "run-detail-note");
      var portrait = document.createElement("img");
      portrait.src = "/static/characters/futurama/avatar/zoidberg.png";
      portrait.alt = "";
      note.appendChild(portrait);
      note.appendChild(el("span", "", "canary-tested by zoidberg · pulled, restarted, healthchecked"));
      detailEl.appendChild(note);
    }

    function select(run) {
      selectedRunKey = runKey(run);
      manifestEl.querySelectorAll("[data-run-key]").forEach(function (card) {
        var active = card.dataset.runKey === selectedRunKey;
        card.classList.toggle("is-selected", active);
        card.setAttribute("aria-pressed", active ? "true" : "false");
      });
      renderDetail(run);
    }

    manifestEl.innerHTML = "";
    runs.forEach(function (run) {
      var first = run.entries[0];
      var lastEntry = run.entries[run.entries.length - 1];
      var hasAlert = run.entries.some(function (e) { return e.is_alert; });
      var span = Math.max(new Date(lastEntry.ts).getTime() - new Date(first.ts).getTime(), 0);
      var startDT = fmtDateTime(first.ts);

      var card = document.createElement("button");
      card.type = "button";
      card.className = "deploy-run" + (hasAlert ? " alert" : "");
      card.dataset.runKey = runKey(run);
      card.setAttribute("aria-pressed", "false");

      var head = el("div", "deploy-run-head");
      head.appendChild(el("span", "deploy-run-led"));
      head.appendChild(el("span", "deploy-run-stack", run.stack || "unknown"));
      head.appendChild(el("div", "pe-spacer"));
      head.appendChild(el("span", "deploy-run-date", startDT.date));
      card.appendChild(head);

      var figures = el("div", "deploy-run-figures");
      figures.appendChild(el("span", "deploy-run-n", String(run.entries.length)));
      figures.appendChild(el("span", "deploy-run-n-label",
        run.entries.length === 1 ? "SERVICE" : "SERVICES"));
      figures.appendChild(el("div", "pe-spacer"));
      figures.appendChild(el("span", "deploy-run-span", fmtSpan(span)));
      card.appendChild(figures);

      // One bar per service instead of a dot floating in 2,000px of timeline. At a glance
      // the bar count is the run size and a red bar is the one that went wrong.
      var bars = el("div", "deploy-run-bars");
      run.entries.forEach(function (e) {
        var bar = el("span", "deploy-run-bar" + (e.is_alert ? " alert" : ""));
        bar.title = e.service + " · " + fmtDateTime(e.ts).time;
        bars.appendChild(bar);
      });
      card.appendChild(bars);

      card.addEventListener("click", function () { select(run); });
      manifestEl.appendChild(card);
    });

    select(selected);
  }

  var REFRESH_INTERVAL_MS = 60000;

  var pollWhileVisible = window.pePollWhileVisible;      // static/poll.js, loaded first

  // Fetch this same page again and swap in the panels that show live data. Each swap is by id, so a
  // page that does not carry a panel simply skips it.
  // mc-subtabs carries the NEEDS YOU count, which changes when a plan is approved or a window settled.
  var LIVE_PANELS = ["mc-subtabs", "hull-diagnostics-panel", "rollback-candidates-panel", "manifest-panel"];

  function refreshPanels() {
    // An open findings drawer would snap shut under the reader; wait for the next interval.
    var openDrawer = document.querySelector("#hull-diagnostics-panel details[open]");
    if (openDrawer) return;
    return fetch(window.location.pathname, { cache: "no-store" })
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
        var fresh = new DOMParser().parseFromString(html, "text/html");
        LIVE_PANELS.forEach(function (id) {
          var next = fresh.getElementById(id);
          var current = document.getElementById(id);
          if (next && current) current.innerHTML = next.innerHTML;
        });
        bind();
        return true;
      })
      .catch(function () { /* a blip: the next interval tries again */ });
  }

  function bind() {
    document.querySelectorAll("[data-settle-window]").forEach(function (btn) {
      // Assigned, not added: this runs again on the same buttons after every refresh, and a
      // listener would stack (Codex).
      btn.onclick = function () { settleWindow(btn); };
    });
    buildDeployManifest();
  }

  bind();
  pollWhileVisible(refreshPanels, REFRESH_INTERVAL_MS);
})();
