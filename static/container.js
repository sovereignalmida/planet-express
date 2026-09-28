(function () {
  "use strict";
  const state = { detailBusy: false, logsBusy: false, restarting: false, paused: false, pausedRefusal: false,
    cursor: null, hashes: [], startedAt: null, detailTimer: null, logTimer: null, autoCollapse: true };
  const get = id => document.getElementById(id);
  const api = get("container-detail").dataset.api;
  const sheet = get("restart-dialog");

  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;

  // v2.2 C: OPEN ↗ (LAN) and WEB ↗ (public) in the header, OPEN ↗ in the phone's bottom bar.
  // Built with textContent and an http(s)-only href; a failure just leaves them out.
  const webUrl = value => typeof value === "string" && /^https?:\/\//.test(value) ? value : null;
  function launchButton(href, label, primary) {
    const a = document.createElement("a");
    a.className = "pe-launch " + (primary ? "primary" : "neutral");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener";
    a.title = href.replace(/^https?:\/\//, "");
    a.textContent = label;
    return a;
  }
  // ── v2.2 widget ──────────────────────────────────────────────────────────────────
  // Every string in a widget answer is the app's own (a queue item's title): built with
  // textContent only, never markup.
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const LEVELS = ["ok", "warn", "crit"];
  const title = name => name.charAt(0).toUpperCase() + name.slice(1);
  function ago(iso) {
    const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (!isFinite(seconds)) return "";
    if (seconds < 60) return Math.round(seconds) + "s ago";
    if (seconds < 3600) return Math.round(seconds / 60) + "m ago";
    return Math.round(seconds / 3600) + "h ago";
  }
  const REASONS = {
    "timeout": "did not answer within its time budget",
    "unreachable": "could not be reached from the dashboard",
    "not reachable from the dashboard": "has no address the dashboard can reach",
    "connection dropped": "dropped the connection mid-answer",
    "answer too large": "answered with more than the widget reads",
    "answer was not JSON": "answered with something other than JSON",
    "answer was compressed": "answered compressed, which the widget refuses",
    "busy": "is queued behind other widgets",
    "the configured key cannot be sent over HTTP": "cannot be sent the configured key: it has characters HTTP cannot carry",
    "widget failed to read the answer": "answered in a shape the widget could not read",
  };
  // Failures on the dashboard's own side: not the app's API, so not worded as if it were.
  const OWN_FAILURES = {
    "widget summary malformed": "The widget produced a summary the dashboard refuses. That is a widget bug, not the app's.",
    "widget declaration refused": "The widget's declaration was refused. See the dashboard's log.",
    "request failed": "The dashboard could not run the widget. See the dashboard's log.",
  };
  function reason(name, data) {
    if (OWN_FAILURES[data.error]) return OWN_FAILURES[data.error];
    const api = title(name) + "'s API ";
    if (data.status === 401 || data.status === 403) return api + "returned " + data.status + ". The key may have been rotated.";
    if (data.status) return api + "returned " + data.status + ".";
    return api + (REASONS[data.error] || "failed") + ".";
  }
  function stats(values, dim) {
    const grid = el("div", "pe-widget-stats" + (dim ? " is-stale" : ""));
    grid.style.setProperty("--pe-widget-cols", String(Math.max(1, Math.min(4, values.length))));
    values.forEach(stat => {
      const well = el("div", "pe-widget-stat");
      well.append(el("span", "pe-widget-k", stat.k));
      well.append(el("strong", "pe-widget-v" + (LEVELS.includes(stat.level) ? " " + stat.level : ""), stat.v));
      grid.append(well);
    });
    return grid;
  }
  function rows(summary, dim) {
    const out = [];
    if (summary.rows && summary.rows.length) {
      const list = el("div", "pe-widget-rows" + (dim ? " is-stale" : ""));
      if (summary.rows_label) list.append(el("span", "pe-widget-k", summary.rows_label));
      summary.rows.forEach(row => {
        const item = el("div", "pe-widget-row");
        item.append(el("span", "pe-widget-row-title", row.title));
        if (typeof row.pct === "number") {
          const bar = el("span", "pe-widget-bar");
          const fill = el("span");
          fill.style.width = Math.max(0, Math.min(100, row.pct)) + "%";
          bar.append(fill);
          item.append(bar, el("span", "pe-widget-pct", row.pct + "%"));
        } else if (row.meta) item.append(el("span", "pe-widget-pct", row.meta));
        list.append(item);
      });
      out.push(list);
    }
    if (summary.line) out.push(el("p", "pe-widget-line" + (dim ? " is-stale" : ""), summary.line));
    return out;
  }
  function head(led, name, sub, pill) {
    const bar = el("div", "pe-widget-head");
    bar.append(el("span", "pe-widget-led " + led));
    // The container's own icon, chosen server-side: a widget's name is not always its icon
    // slug (the adguard widget's icon is adguard-home), and this page already knows which
    // icon this container resolved to. Absent when it has none, which is a monogram in the
    // header and simply nothing here.
    const src = get("widget").dataset.icon;
    if (src) {
      const glyph = el("img", "pe-icon-bare md");
      glyph.src = src;
      glyph.alt = "";
      bar.append(glyph);
    }
    bar.append(el("span", "pe-widget-name", name));
    if (sub) bar.append(el("span", "pe-widget-via", sub));
    bar.append(el("span", "pe-widget-spacer"));
    if (pill) bar.append(el("span", "pe-widget-ro", pill));
    return bar;
  }
  function renderWidget(box, data) {
    const name = String(data.widget || "widget");
    box.replaceChildren();
    if (data.state === "ok") {
      box.className = "pe-widget";
      box.append(head("ok", name.toUpperCase(), "via " + data.via + " · " + ago(data.fetched_at), "READ-ONLY"));
      box.append(stats(data.stats, false), ...rows(data, false));
    } else if (data.state === "needs_key") {
      box.className = "pe-widget needs-key";
      box.append(head("warn", "NEEDS AN API KEY"));
      const text = el("p", "pe-widget-text", title(name) + " supports a widget, but no key is configured. Add ");
      (data.env || []).forEach((env, i) => {
        if (i) text.append(document.createTextNode(" and "));
        text.append(el("code", null, env));
      });
      text.append(document.createTextNode(" to /etc/planetexpress-dashboard.env and restart the dashboard."));
      box.append(text, el("p", "pe-widget-foot", "It can't be set from the dashboard. Keys stay on the host."));
    } else {
      box.className = "pe-widget is-error";
      box.append(head("crit", OWN_FAILURES[data.error] ? "WIDGET FAILED" : "API DIDN'T ANSWER",
        data.stale_at ? "last good " + ago(data.stale_at) : ""));
      box.append(el("p", "pe-widget-text", reason(name, data)));
      if (data.stale) box.append(stats(data.stale.stats || [], true), ...rows(data.stale, true));
      box.append(el("p", "pe-widget-foot", "The widget never marks the container as down."));
    }
    box.hidden = false;
  }
  function renderLoading(box) {
    box.className = "pe-widget is-loading";
    box.replaceChildren(head("idle", "LOADING", "first fetch"));
    const grid = el("div", "pe-widget-stats");
    grid.style.setProperty("--pe-widget-cols", "3");
    for (let i = 0; i < 3; i++) grid.append(el("div", "pe-widget-stat pe-skeleton"));
    box.append(grid);
    box.hidden = false;
  }
  // The log well's level class is rewritten by every detail poll: keep the fold with it.
  function logLevel(level) {
    get("logwell").className = "pe-logwell " + level + (state.logsCollapsed ? " is-collapsed" : "");
  }
  function collapseLogs(collapsed) {
    state.logsCollapsed = collapsed;
    const toggle = get("log-toggle");
    toggle.textContent = collapsed ? "show logs" : "hide logs";
    toggle.setAttribute("aria-expanded", String(!collapsed));
    get("logwell").classList.toggle("is-collapsed", collapsed);
    if (!collapsed) logs();
  }
  get("log-toggle").addEventListener("click", () => {
    state.autoCollapse = false;
    collapseLogs(!state.logsCollapsed);
  });
  // Screen readers hear a state change, not every 30s re-render of the box.
  const ANNOUNCE = { ok: " widget is live.", needs_key: " widget needs an API key.", error: " widget: the API didn't answer.",
    unrefreshed: " widget could not refresh; showing its last answer." };
  function announce(name, widgetState) {
    const text = widgetState ? title(name) + ANNOUNCE[widgetState] : "";
    if (state.widgetAnnounced === text) return;
    state.widgetAnnounced = text;
    get("widget-status").textContent = text;
  }
  function hideWidget(box) {
    box.hidden = true;
    box.replaceChildren();
    announce("", null);
    get("log-toggle").hidden = true;
    // A widget that went away must not leave the logs folded with nothing above them.
    if (state.logsCollapsed) collapseLogs(false);
    state.widgetLoaded = false;
  }
  function unrefreshed(box) {
    // Keep the last answer, but never let it look live: no beacon, dimmed, and says so.
    box.classList.add("is-unrefreshed");
    const led = box.querySelector(".pe-widget-led");
    if (led) led.className = "pe-widget-led idle";
    let via = box.querySelector(".pe-widget-via");
    if (!via) {
      via = el("span", "pe-widget-via");
      box.querySelector(".pe-widget-name").after(via);
    }
    via.textContent = "could not refresh" + (state.widgetShownAt ? " · shown " + ago(state.widgetShownAt) : "");
  }
  async function widget() {
    if (document.visibilityState !== "visible" || state.widgetBusy) return;
    state.widgetBusy = true;
    const box = get("widget");
    // A skeleton only on the page's first ask, and only if that answer is slow: most containers
    // have no widget, and a box that flashes up and vanishes is the "empty box" the spec rules out.
    const slow = state.widgetAsked ? null : setTimeout(() => renderLoading(box), 400);
    state.widgetAsked = true;
    try {
      const data = await request(box.dataset.api);
      clearTimeout(slow);
      if (!data || !["ok", "needs_key", "error"].includes(data.state)) { hideWidget(box); return; }
      renderWidget(box, data);
      state.widgetShownAt = new Date().toISOString();
      state.widgetName = String(data.widget || "widget");
      announce(state.widgetName, data.state);
      get("log-toggle").hidden = false;
      // Logs fold away only when the page's first answer is a working widget. Not for a key or
      // an error (the logs are what explains those), and never later, under someone reading them.
      if (state.autoCollapse && data.state === "ok") collapseLogs(true);
      state.widgetLoaded = true;
    } catch (error) {
      clearTimeout(slow);
      if (state.widgetLoaded) { unrefreshed(box); announce(state.widgetName, "unrefreshed"); } else hideWidget(box);
    } finally {
      state.autoCollapse = false;
      state.widgetBusy = false;
    }
  }

  async function launchLinks() {
    const holder = get("container-launch");
    try {
      const links = await request(holder.dataset.api);
      const lan = webUrl(links.lan);
      const web = webUrl(links.web);
      if (!lan && !web) return;
      if (lan) holder.append(launchButton(lan, "OPEN ↗", true));
      if (web) holder.append(launchButton(web, lan ? "WEB ↗" : "OPEN ↗", !lan));
      holder.hidden = false;
      const mobile = get("launch-mobile");
      mobile.href = lan || web;
      mobile.hidden = false;
    } catch (e) {
      // No buttons is the honest answer when the links cannot be read.
    }
  }

  async function request(url, options) {
    const response = await fetch(url, { cache: "no-store", ...options });
    if (response.status === 401) {
      window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname));
      throw new Error("Please log in again.");
    }
    const data = await response.json();
    if (!response.ok) throw new Error(response.status === 503 ? "host slow, retry" : data.error);
    return data;
  }
  function paused(value) {
    state.paused = value;
    get("restart").hidden = value;
    if (value) {
      get("verdict-text").textContent = "PAUSED BY OPERATOR";
      get("verdict").className = "pe-verdict warn";
      get("log-led").className = "led-dot status-warn";
      logLevel("warn");
      ["cpu", "memory"].forEach(key => {
        get(key + "-value").textContent = "—";
        get(key + "-bar").value = 0;
      });
    }
  }
  async function detail() {
    if (document.visibilityState !== "visible" || state.detailBusy) return;
    state.detailBusy = true;
    try {
      const data = await request(api);
      const errors = [];
      if (data.error) throw new Error("host slow, retry");
      if (data.facts.ok) {
        const facts = data.facts.facts;
        paused(state.pausedRefusal || data.paused === true || facts.state === "paused");
        // A healthcheck still "starting" is NOT clean: check_stack_completeness() calls that
        // unknown and verify_after_restart() keeps waiting for it (Codex review, T30).
        const starting = facts.state === "running" && facts.health === "starting";
        const level = state.paused || starting ? "warn"
          : facts.state === "running" && facts.health !== "unhealthy" ? "ok" : "crit";
        get("verdict").className = "pe-verdict " + level;
        get("verdict-text").textContent = state.paused ? "PAUSED BY OPERATOR"
          : facts.state === "restarting" || facts.health === "unhealthy" ? "CRASH LOOPING"
          : starting ? "STARTING — HEALTHCHECK PENDING"
          : facts.state === "running" ? "RUNNING CLEAN" : "NOT RUNNING";
        get("log-led").className = "led-dot status-" + level;
        logLevel(level);
        const values = { health: facts.health, policy: facts.restart_policy.name + " (max retries: " + facts.restart_policy.max_retries + ")",
          restarts: facts.restart_count, ports: facts.ports.map(p => p.host_ip + ":" + p.host_port + " → " + p.container_port + "/" + p.protocol).join(", ") || "None",
          image: facts.image_id };
        Object.entries(values).forEach(([key, value]) => { get("fact-" + key).textContent = value; });
        get("header-image").textContent = facts.image_id;
      } else errors.push("Facts: host slow, retry");
      if (data.vitals.ok && !state.paused) {
        const stats = data.vitals.stats;
        get("cpu-value").textContent = stats.cpu_percent.toFixed(1) + "%";
        get("memory-value").textContent = (stats.memory_used_bytes / 1048576).toFixed(1) + " MiB";
        get("cpu-bar").value = Math.min(100, stats.cpu_percent);
        get("memory-bar").value = Math.min(100, stats.memory_percent);
      } else if (!data.vitals.ok) errors.push("Vitals: host slow, retry");
      get("detail-message").textContent = errors.join(" · ");
    } catch (error) { get("detail-message").textContent = error.message; }
    finally { state.detailBusy = false; }
  }
  function line(text, divider) {
    const node = document.createElement("div");
    node.className = divider ? "container-log-divider" : "container-log-line";
    node.textContent = text;
    get("log-lines").append(node);
  }
  async function logs() {
    if (document.visibilityState !== "visible" || state.logsBusy || state.logsCollapsed) return;
    state.logsBusy = true;
    try {
      // POSTed in the body: a cursor can carry up to 1000 dedupe hashes, far past what a query
      // string may hold before gunicorn rejects the request line (Codex review, T30).
      const params = new URLSearchParams({ csrf_token: csrf() });
      if (state.cursor) params.set("cursor", state.cursor);
      state.hashes.forEach(hash => params.append("hash", hash));
      const well0 = get("log-lines");
      // Follow the tail only when the operator is already at the bottom: otherwise a poll every 3s
      // snatches the view back while they are reading older output (Codex review, T30).
      const following = well0.scrollHeight - well0.scrollTop - well0.clientHeight < 40;
      const data = await request(api + "/logs", { method: "POST", body: params });
      if (!data.ok) throw new Error(["timeout", "unavailable"].includes(data.error) ? "host slow, retry" : data.error);
      if (state.startedAt && data.started_at && state.startedAt !== data.started_at) line("container restarted", true);
      if (data.skipped) line("earlier lines skipped", true);
      data.lines.forEach(entry => line((entry.ts || "") + " [" + entry.stream + "] " + entry.text));
      if (data.started_at) state.startedAt = data.started_at;
      state.cursor = data.cursor;
      state.hashes = data.cursor_hashes || [];
      const well = get("log-lines");
      while (well.childElementCount > 2000) well.firstElementChild.remove();
      get("log-count").textContent = well.querySelectorAll(".container-log-line").length + " lines";
      get("log-message").textContent = well.childElementCount ? "" : state.paused
        ? "No output since the container was paused. Logs resume when it does." : "No output returned by the last log poll.";
      if (following) well.scrollTop = well.scrollHeight;
    } catch (error) { get("log-message").textContent = error.message; }
    finally { state.logsBusy = false; }
  }
  get("restart").addEventListener("click", () => {
    if (state.paused) return;
    get("restart-message").textContent = "";
    sheet.showModal();
    get("restart-cancel").focus();
  });
  get("restart-cancel").addEventListener("click", () => sheet.close());
  sheet.addEventListener("cancel", event => { if (state.restarting) event.preventDefault(); });
  get("restart-confirm").addEventListener("click", async () => {
    if (state.restarting || state.paused) return;
    state.restarting = true;
    const button = get("restart-confirm");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    get("restart-cancel").disabled = true;
    get("restart-label").textContent = "RESTARTING...";
    try {
      const data = await request(api + "/restart", { method: "POST", body: new URLSearchParams({
        csrf_token: csrf()
      }) });
      if (data.outcome === "started" && data.execution_id) {
        window.location.assign("/executions/" + encodeURIComponent(data.execution_id));
      } else {
        get("restart-message").textContent = data.message || "host slow, retry";
        if ((data.message || "").includes("paused_containers")) {
          state.pausedRefusal = true;
          paused(true);
        }
      }
    } catch (error) { get("restart-message").textContent = error.message; }
    finally {
      state.restarting = false;
      button.disabled = false;
      button.setAttribute("aria-busy", "false");
      get("restart-cancel").disabled = false;
      get("restart-label").textContent = "CONFIRM RESTART";
    }
  });
  function visibility() {
    clearInterval(state.detailTimer);
    clearInterval(state.logTimer);
    clearInterval(state.widgetTimer);
    if (document.visibilityState === "visible") {
      detail(); logs(); widget();
      state.detailTimer = setInterval(detail, 5000);
      state.logTimer = setInterval(logs, 3000);
      // The server caches an answer 30s; asking faster only re-reads its cache.
      state.widgetTimer = setInterval(widget, 30000);
    }
  }
  document.addEventListener("visibilitychange", visibility);
  visibility();
  launchLinks();
})();
