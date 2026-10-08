// dashboard.js — Planet Express dashboard interactions. Data is server-rendered on
// first load; after that, a fetch()-based refresh swaps in fresh server-rendered HTML
// every 60s (see refreshDashboard() below) instead of the page doing a hard reload --
// a <meta http-equiv="refresh"> reload blanks the whole page and repaints from scratch,
// which reads as a jarring full-screen flash. Fetching the same URL and replacing just
// #dashboard-live's contents keeps the browser tab/scroll/focus alive and never blanks.
// Approvals and SCAN are driven from here against /api/*; the claim that this dashboard
// "has no route to actually approve anything" was true before approvals.js existed.
(function () {
  "use strict";

  var TAB_NAMES = ["overview", "backups", "network", "actions", "history", "chat", "config", "crew"];
  // Tabs whose panels live OUTSIDE #dashboard-live, as siblings of it. The live region's
  // main column has nothing to show for these, so it must be hidden or the tab opens with a
  // screen of empty space above its persistent content.
  // Add a tab here the moment its panel moves out of the snapshot region — forgetting to is what
  // made Actions and History open blank when the deployment manifest left the live grid.
  var DETACHED_TABS = ["actions", "history", "chat", "config", "crew"];

  function setActiveTab(name) {
    if (name !== "overview") closeDrawer();
    document.body.classList.toggle("chat-active", name === "chat");
    document.body.classList.toggle("detached-tab", DETACHED_TABS.indexOf(name) !== -1);
    document.querySelectorAll(".tab").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.tab === name);
    });

    document.querySelectorAll(".tab-panel").forEach(function (panel) {
      panel.classList.toggle("active", panel.dataset.tabPanel === name);
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
      if (res.r.status === 401) { window.location.assign("/login?next=" + encodeURIComponent("/")); return null; }
      if (res.data.reason === "elevation_required" && window.peElevate) {
        return window.peElevate(res.data.error || "This needs your passphrase again.")
          .then(function (ok) { return ok ? post() : null; });
      }
      return res;
    }).then(function (res) {
      if (!res) { btn.disabled = false; return; }
      if (!res.r.ok) throw new Error(res.data.error || "host slow, retry");
      window.alert(res.data.message || "Done.");
      // The windows panel sits outside the live region the refresh swaps, so reload the page.
      window.location.reload();
    }).catch(function (error) {
      btn.disabled = false;
      window.alert(error.message);
    });
  }

  // Everything in here binds to DOM nodes -- must re-run after every refresh swap
  // (fresh nodes from the fetched HTML have no listeners of their own yet).
  function bindInteractions() {
    document.querySelectorAll("[data-settle-window]").forEach(function (btn) {
      // Assigned, not added: the windows panel sits outside the swapped region, so this runs
      // again on the same buttons after every refresh and a listener would stack (Codex).
      btn.onclick = function () { settleWindow(btn); };
    });
    var scanBtn = document.getElementById("scan-btn");
    if (scanBtn && !scanBtn.disabled) {
      scanBtn.addEventListener("click", function () { startScan(scanBtn); });
    }
    // A scan already in flight when this swap landed -- started here, from Telegram, or by
    // the scheduler -- keeps the fast poll going until the server says it is over.
    if (scanning()) pollWhileScanning();
    else scanPollTries = 0;             // a finished scan gives the next one a full budget

    document.querySelectorAll(".tab[data-tab]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setActiveTab(btn.dataset.tab);
        window.location.hash = btn.dataset.tab;
      });
    });

    document.querySelectorAll("[data-tile-tab]").forEach(function (tile) {
      tile.addEventListener("click", function () {
        var name = tile.dataset.tileTab;
        setActiveTab(name);
        window.location.hash = name;
      });
    });

    var filterInput = document.getElementById("net-filter");
    if (filterInput) {
      var rows = Array.prototype.slice.call(document.querySelectorAll(".net-filter-row"));
      var counter = document.getElementById("net-count");
      var total = rows.length;

      // The header reads "78 routers · 73 names · 5 LAN twins merged · all up" until you
      // filter. Overwriting it with a count and never putting it back means one keystroke
      // costs you the summary for the rest of the session.
      var summary = counter ? counter.textContent : "";
      var zones = Array.prototype.slice.call(document.querySelectorAll(".net-zone"));

      filterInput.addEventListener("input", function () {
        var q = filterInput.value.trim().toLowerCase();
        var shown = 0;
        rows.forEach(function (row) {
          var match = !q || row.dataset.filterText.indexOf(q) !== -1;
          row.classList.toggle("hidden", !match);
          if (match) shown++;
        });
        // A zone whose every pill is filtered out is a label over empty space.
        zones.forEach(function (zone) {
          zone.classList.toggle("hidden", !!q && !zone.querySelector(".net-filter-row:not(.hidden)"));
        });
        if (counter) counter.textContent = q ? "showing " + shown + " of " + total : summary;
      });
    }

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

    // v2.2 A: a stack tile opens the stack drawer. Its rows are server-rendered into a
    // <template> per stack; opening clones them in, so nothing here builds markup from data.
    // The stack-name button is the keyboard way in (a click on it bubbles here too).
    document.querySelectorAll("article[data-drawer-open]").forEach(function (tile) {
      tile.addEventListener("click", function (e) {
        if (e.target.closest("a")) return;       // a dot is its own link to container detail
        var opener = tile.querySelector(".pe-stack-open") || tile;
        openDrawer(tile.dataset.drawerOpen, opener);
      });
    });

    buildDeployManifest();
  }

  var drawerOpener = null;

  function openDrawer(name, opener) {
    var drawer = document.querySelector("[data-drawer]");
    var template = Array.prototype.find.call(
      document.querySelectorAll("template[data-drawer-for]"),
      function (t) { return t.dataset.drawerFor === name; });
    if (!drawer || !template) return;
    drawer.replaceChildren(template.content.cloneNode(true));
    drawer.hidden = false;
    drawerOpener = opener || null;
    var close = drawer.querySelector("[data-drawer-close]");
    if (close) close.focus();
  }

  function closeDrawer() {
    var drawer = document.querySelector("[data-drawer]");
    if (!drawer || drawer.hidden) return;
    drawer.hidden = true;
    drawer.replaceChildren();
    if (drawerOpener && document.contains(drawerOpener)) drawerOpener.focus();
    drawerOpener = null;
  }

  // Bound once: document and the drawer (outside #dashboard-live) are never replaced by a
  // refresh swap, so binding these in bindInteractions() would stack a copy per refresh.
  document.addEventListener("keydown", function (e) {
    // A modal dialog (the config review) owns Escape while it is open.
    if (e.key === "Escape" && !document.querySelector("dialog[open]")) closeDrawer();
  });
  var drawerEl = document.querySelector("[data-drawer]");
  if (drawerEl) {
    drawerEl.addEventListener("click", function (e) {
      if (e.target.closest("[data-drawer-close]")) { closeDrawer(); return; }
      var filter = e.target.closest("[data-drawer-filter]");
      if (!filter) return;
      var launchableOnly = filter.dataset.drawerFilter === "launchable";
      drawerEl.querySelectorAll("[data-drawer-filter]").forEach(function (btn) {
        btn.classList.toggle("active", btn === filter);
      });
      drawerEl.querySelectorAll(".pe-drawer-row").forEach(function (row) {
        row.classList.toggle("hidden", launchableOnly && row.dataset.launchable !== "yes");
      });
    });
  }
  // The drawer belongs to Overview: leaving the tab closes it, or it would float over another
  // tab and -- since an open drawer holds off the refresh -- freeze the whole page.
  window.addEventListener("hashchange", closeDrawer);

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

  // fn resolves to true only on success; skipped/failed reads do not reset
  // freshness. The initial server-rendered view starts fresh.
  function pollWhileVisible(fn, intervalMs) {
    var timer = null;
    var inFlight = false;
    var lastSuccess = Date.now();

    function poll() {
      if (inFlight || document.visibilityState !== "visible") return;
      inFlight = true;
      Promise.resolve().then(fn).then(function (success) {
        if (success === true) lastSuccess = Date.now();
        inFlight = false;
      }, function () {
        inFlight = false;
      });
    }

    function visibilityChanged() {
      if (timer !== null) clearInterval(timer);
      timer = null;
      if (document.visibilityState !== "visible") return;
      if (Date.now() - lastSuccess > intervalMs) poll();
      timer = setInterval(poll, intervalMs);
    }

    document.addEventListener("visibilitychange", visibilityChanged);
    visibilityChanged();
  }

  // ── SCAN ────────────────────────────────────────────────────────────────────
  // The button was <a href="/">: it said SCAN and reloaded the page. It now posts to
  // /api/scan, which runs the same full pipeline Telegram's /check runs.
  //
  // The server owns "is a scan running": the button's disabled state is rendered from
  // ctx.pipeline_status.state, and a refresh swap is what gives the button back. The client
  // is optimistic only for the seconds between the click and the next swap, so a scan
  // started from Telegram (or by the scheduler) disables this button too.
  var SCAN_POLL_MS = 5000;
  var SCAN_POLL_LIMIT = 120;          // 10 minutes, then the ordinary 60s poll takes over
  var scanPolling = false;
  // Budget per SCAN, not per chain: it survives the refreshes that restart the chain, and
  // resets when the pipeline goes idle. Counted in the chain alone it was no limit at all --
  // the ordinary 60s refresh calls bindInteractions() -> pollWhileScanning(), which would
  // hand a stuck scan a fresh ten minutes every minute, for ever.
  var scanPollTries = 0;

  function scanNote(text) {
    var note = document.getElementById("scan-note");
    if (!note) return;
    note.textContent = text || "";
    // The reason is the point of the note, so it is never only the truncated version.
    if (text) note.title = text; else note.removeAttribute("title");
  }

  function scanning() {
    var btn = document.getElementById("scan-btn");
    return !!(btn && btn.dataset.scanning);
  }

  // A running scan is worth watching at 5s rather than 60s. Exactly one chain: the flag stays
  // set for the whole life of the chain, including while a refresh is in flight, because each
  // refresh calls bindInteractions() -> pollWhileScanning() and a flag cleared any earlier
  // would let that start a second chain, then a third, doubling on every poll.
  function pollWhileScanning() {
    if (scanPolling || scanPollTries >= SCAN_POLL_LIMIT) return;
    scanPolling = true;
    (function tick() {
      window.setTimeout(function () {
        if (++scanPollTries >= SCAN_POLL_LIMIT) {
          // A scan this long is stuck or is doing something unusual; the ordinary 60s
          // refresh is enough to notice when it finally ends.
          scanPolling = false;
          return;
        }
        Promise.resolve(refreshDashboard()).then(function () {
          if (scanning()) tick();
          else scanPollingEnded();
        });
      }, SCAN_POLL_MS);
    })();
  }

  function scanPollingEnded() {
    scanPolling = false;
    scanPollTries = 0;
  }

  function startScan(btn) {
    btn.disabled = true;
    btn.textContent = "SCANNING…";
    btn.dataset.scanning = "1";
    scanNote("");
    var body = new URLSearchParams({ csrf_token: csrfToken() });
    fetch("/api/scan", { method: "POST", body: body, cache: "no-store" })
      .then(function (r) { return r.json().catch(function () { return null; }); })
      .then(function (result) {
        if (result && result.status === "started") {
          scanPollingEnded();           // this scan is ours and starts on a full budget
          pollWhileScanning();
          return;
        }
        // Busy, refused, or an error: hand the button back and say why. Core answers this
        // synchronously precisely so the person who clicked does not have to go and look
        // in Telegram to find out nothing happened.
        refused((result && (result.reason || result.error)) || "could not start a scan");
      })
      .catch(function () {
        refused("core unreachable");
      });
  }

  function releaseScanButton() {
    var btn = document.getElementById("scan-btn");
    if (!btn) return;
    btn.disabled = false;
    btn.textContent = "SCAN ✈";
    delete btn.dataset.scanning;
  }

  // A refusal is not necessarily "nothing is happening". Someone else -- Telegram, the
  // scheduler, another browser -- may have taken the slot after this page was rendered, and
  // then the honest button is SCANNING, not SCAN with a note beside it. The server owns that
  // answer, so ask it: on a running scan the button re-renders disabled and the note comes
  // off, because the button is already the explanation. The note is set twice on purpose,
  // once for immediate feedback and once after the swap, which replaces the header.
  function refused(reason) {
    releaseScanButton();
    scanNote(reason);
    Promise.resolve(refreshDashboard()).then(function () {
      if (scanning()) {
        scanNote("");
        pollWhileScanning();
      } else {
        scanNote(reason);
      }
    });
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  function refreshDashboard() {
    // Don't yank focus/typed text out from under someone mid-filter.
    var filterInput = document.getElementById("net-filter");
    if (filterInput && document.activeElement === filterInput) return;
    // Nor close a stack drawer someone is reading: the swap would replace it.
    var openDrawerEl = document.querySelector("[data-drawer]");
    if (openDrawerEl && !openDrawerEl.hidden) return;

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
        var freshLive = fresh.getElementById("dashboard-live");
        var currentLive = document.getElementById("dashboard-live");
        if (!freshLive || !currentLive) return;
        currentLive.innerHTML = freshLive.innerHTML;
        // The deployment manifest sits outside #dashboard-live so it can render below the two
        // docks, which have to stay outside it. Swap it by id here, or it would be the one panel
        // on the page that silently stopped updating.
        var freshManifest = fresh.getElementById("manifest-panel");
        var currentManifest = document.getElementById("manifest-panel");
        if (freshManifest && currentManifest) currentManifest.innerHTML = freshManifest.innerHTML;
        // Same treatment for the Crew tab's ship's-computer log: it sits outside the live
        // region because nothing in it holds operator state, but its lines come straight out
        // of build_professor_lines() and would otherwise contradict a dashboard that has
        // since re-scanned.
        var freshCrew = fresh.getElementById("crew-panel");
        var currentCrew = document.getElementById("crew-panel");
        if (freshCrew && currentCrew) currentCrew.innerHTML = freshCrew.innerHTML;
        var freshHull = fresh.getElementById("hull-diagnostics-panel");
        var currentHull = document.getElementById("hull-diagnostics-panel");
        if (freshHull && currentHull) currentHull.innerHTML = freshHull.innerHTML;
        // A window can appear or become held after page load; this panel is the only way out of
        // a held one, so it follows the refresh (bindInteractions rebinds its buttons).
        var freshWindows = fresh.getElementById("rollback-candidates-panel");
        var currentWindows = document.getElementById("rollback-candidates-panel");
        if (freshWindows && currentWindows) currentWindows.innerHTML = freshWindows.innerHTML;
        applyHashTab();
        bindInteractions();
        return true;
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
  pollWhileVisible(refreshDashboard, REFRESH_INTERVAL_MS);
})();
