(function () {
  "use strict";
  const state = { detailBusy: false, logsBusy: false, restarting: false, paused: false, pausedRefusal: false,
    cursor: null, hashes: [], startedAt: null, detailTimer: null, logTimer: null };
  const get = id => document.getElementById(id);
  const api = get("container-detail").dataset.api;
  const sheet = get("restart-dialog");

  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;

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
      get("logwell").className = "pe-logwell warn";
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
        get("logwell").className = "pe-logwell " + level;
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
    if (document.visibilityState !== "visible" || state.logsBusy) return;
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
    if (document.visibilityState === "visible") {
      detail(); logs();
      state.detailTimer = setInterval(detail, 5000);
      state.logTimer = setInterval(logs, 3000);
    }
  }
  document.addEventListener("visibilitychange", visibility);
  visibility();
})();
