(function () {
  "use strict";
  const root = document.getElementById("execution");
  const state = { busy: false, timer: null, execution: null };
  const get = id => document.getElementById(id);
  const terminal = new Set(["passed", "failed", "interrupted"]);

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  }

  async function request() {
    const response = await fetch(root.dataset.api, { cache: "no-store" });
    if (response.status === 401) {
      window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname));
      throw new Error("Please log in again.");
    }
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.error || "host slow, retry");
      error.notFound = response.status === 404;
      throw error;
    }
    return data;
  }

  function target(data) {
    const value = data.approval && data.approval.target || {};
    return { stack: value.stack || "unknown", service: value.service || "unknown" };
  }

  function targetUrl(data) {
    const value = target(data);
    return "/containers/" + encodeURIComponent(value.stack) + "/" + encodeURIComponent(value.service);
  }

  function duration(data) {
    const end = data.finished_at || Date.now() / 1000;
    const seconds = Math.max(0, Math.floor(end - data.started_at));
    return String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
  }

  function marker(kind) {
    const value = node("span", "pe-marker " + kind);
    if (kind === "done") value.textContent = "✓";
    if (kind === "failed") value.textContent = "✕";
    return value;
  }

  function railItem(label, kind, meta) {
    const row = node("div", "pe-rail-item is-" + (kind === "done" ? "done" : kind === "pending" ? "pending" : "active"));
    const gutter = node("div", "pe-rail-gutter");
    gutter.append(marker(kind));
    const copy = node("div");
    copy.append(node("div", "pe-rail-text", label));
    if (meta) copy.append(node("div", "pe-rail-meta", meta));
    row.append(gutter, copy);
    return row;
  }

  function renderRail(data) {
    const rail = get("execution-rail");
    rail.replaceChildren();
    const status = data.status;
    let restartKind = "active";
    let verifyKind = "pending";
    if (["verifying", "passed"].includes(status)) restartKind = "done";
    if (status === "verifying") verifyKind = "active";
    if (status === "passed") verifyKind = "done";
    if (status === "failed") {
      const preVerification = /^(target no longer valid|container changed since approval|restart command failed|failed to start|crashed:)/.test(data.reason || "");
      restartKind = preVerification ? "failed" : "done";
      verifyKind = preVerification ? "pending" : "failed";
    }
    if (status === "interrupted") {
      restartKind = "pending";
      verifyKind = "pending";
    }
    rail.append(railItem("Restart " + target(data).stack + "/" + target(data).service, restartKind,
      restartKind === "active" ? "command in progress" : restartKind === "done" ? "command finished"
        : restartKind === "failed" ? "command failed" : "completion not confirmed"));
    rail.append(railItem("Verify the service answers", verifyKind,
      verifyKind === "active" ? "health checks in progress" : verifyKind === "done" ? data.reason : "waiting"));
  }

  function action(label, href, primary) {
    const link = node("a", "pe-btn" + (primary ? " primary" : ""), label);
    link.href = href;
    return link;
  }

  function renderActions(data) {
    const actions = get("execution-actions");
    actions.replaceChildren();
    if (data.status === "passed" || data.status === "failed") {
      actions.append(action("DONE", targetUrl(data), true));
    } else if (data.status === "interrupted") {
      actions.append(action("RE-SCAN THE HOST", "/#overview", true), action("RE-PROPOSE", targetUrl(data), false));
    }
    // Add future controls only when both a capability and its matching endpoint exist.
  }

  function render(data) {
    state.execution = data;
    const status = data.status;
    const treatments = {
      running: ["warn", "RUNNING", "Restart command is in progress."],
      verifying: ["execution-verifying", "VERIFYING", "Commands finished. The run isn't done until the service answers."],
      passed: ["ok", "VERIFIED GOOD", data.reason || "The service answered its verification checks."],
      failed: ["crit", "EXECUTION FAILED", data.reason || "The action did not pass verification."],
      interrupted: ["execution-interrupted", "WHAT WE KNOW", data.reason || "Contact ended before the host state could be confirmed."],
    };
    const treatment = treatments[status] || ["unknown", "UNKNOWN EXECUTION STATE", status];
    get("execution-verdict").className = "pe-verdict " + treatment[0];
    get("execution-orb").textContent = status === "passed" ? "✓" : status === "failed" ? "✕" : status === "interrupted" ? "⏻" : "●";
    get("execution-title").textContent = treatment[1];
    get("execution-reason").textContent = treatment[2];
    get("execution-phase").textContent = status === "running" ? "STEP 1 OF 2" : status === "verifying" ? "STEP 2 OF 2" : status.toUpperCase();
    get("execution-elapsed").textContent = duration(data);
    renderRail(data);
    const summary = get("execution-summary");
    summary.hidden = !terminal.has(status);
    if (!summary.hidden) {
      get("summary-runtime").textContent = duration(data);
      get("summary-operator").textContent = data.approval && data.approval.decided_by || "unknown";
      get("summary-target").textContent = target(data).stack + "/" + target(data).service;
    }
    renderActions(data);
    get("execution-message").textContent = "";
    if (terminal.has(status)) clearInterval(state.timer);
  }

  function renderEmpty(reason) {
    clearInterval(state.timer);
    get("execution-verdict").className = "pe-verdict unknown execution-empty";
    get("execution-title").textContent = "RUN NOT FOUND";
    get("execution-reason").textContent = reason;
    get("execution-progress").hidden = true;
    get("execution-summary").hidden = true;
    get("execution-actions").replaceChildren(action("BACK TO ACTIONS", "/#actions", true));
    get("execution-message").textContent = "";
  }

  async function poll() {
    if (state.busy || document.visibilityState !== "visible" || (state.execution && terminal.has(state.execution.status))) return;
    state.busy = true;
    try {
      render(await request());
    } catch (error) {
      if (error.notFound) renderEmpty(error.message);
      else get("execution-message").textContent = error.message;
    } finally {
      state.busy = false;
    }
  }

  function visibility() {
    clearInterval(state.timer);
    if (document.visibilityState === "visible" && !(state.execution && terminal.has(state.execution.status))) {
      poll();
      state.timer = setInterval(poll, 2000);
    }
  }

  document.addEventListener("visibilitychange", visibility);
  visibility();
})();
