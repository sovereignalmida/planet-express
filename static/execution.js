(function () {
  "use strict";
  const root = document.getElementById("execution");
  const state = { busy: false, timer: null, execution: null, control: false };
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const get = id => document.getElementById(id);
  const terminal = new Set(["passed", "failed", "interrupted", "aborted", "rolled_back", "rollback_failed"]);

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
    const value = data.target || data.approval && data.approval.target || {};
    return { stack: value.stack || "unknown", service: value.service || null,
      scope: value.scope, stacks: Array.isArray(value.stacks) ? value.stacks : [] };
  }

  function targetUrl(data) {
    const value = target(data);
    if (data.action !== "docker.restart_service") return "/#overview";
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

  const STEP_KINDS = { pending: "pending", dispatched: "active", passed: "done", failed: "failed",
    skipped: "pending", aborted: "pending" };

  function renderStepRail(rail, steps) {
    // Multi-step runbooks (slice 5b): one row per step, with its effect when it did not simply apply.
    steps.forEach(step => {
      const effect = step.effect && step.effect !== "applied" && step.status !== "passed"
        ? " (" + step.effect.replace("_", " ") + ")" : "";
      const detail = step.status === "dispatched" ? "in progress"
        : (step.reason || step.status) + effect;
      rail.append(railItem(step.label, STEP_KINDS[step.status] || "pending", detail));
    });
  }

  function renderRail(data) {
    const rail = get("execution-rail");
    rail.replaceChildren();
    if (Array.isArray(data.steps) && data.steps.length > 1) {
      renderStepRail(rail, data.steps);
      return;
    }
    const status = data.status;
    let restartKind = "active";
    let verifyKind = "pending";
    if (["verifying", "passed"].includes(status)) restartKind = "done";
    if (status === "verifying") verifyKind = "active";
    if (status === "passed") verifyKind = "done";
    if (status === "failed") {
      // Stack runs report per stack; a compose command that exited non-zero failed before
      // verification for that stack (T37).
      const preVerification = /^(target no longer valid|container changed since approval|restart command failed|failed to start|crashed:|compose file (of \S+ )?changed since approval|container \S+ was recreated since approval|refused: its approved plan)/.test(data.reason || "")
        || /\(compose exited \d+/.test(data.reason || "");
      restartKind = preVerification ? "failed" : "done";
      verifyKind = preVerification ? "pending" : "failed";
    }
    if (status === "interrupted") {
      restartKind = "pending";
      verifyKind = "pending";
    }
    const isRestart = data.action === "docker.restart_service";
    rail.append(railItem(data.summary, restartKind,
      restartKind === "active" ? "command in progress" : restartKind === "done" ? "command finished"
        : restartKind === "failed" ? "command failed" : "completion not confirmed"));
    rail.append(railItem(isRestart ? "Verify the service answers" : "Verify container state", verifyKind,
      verifyKind === "active" ? "health checks in progress" : verifyKind === "done" ? data.reason
        : verifyKind === "failed" ? "verification failed" : "waiting"));
  }

  async function control(kind, confirmText) {
    if (state.control || !window.confirm(confirmText)) return;
    state.control = true;
    renderActions(state.execution);
    try {
      const response = await fetch(location.pathname.replace("/executions/", "/api/executions/") + "/" + kind,
        { method: "POST", body: new URLSearchParams({ csrf_token: csrf() }), cache: "no-store" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "host slow, retry");
      get("execution-note").textContent = data.message || "";
      // A rollback runs as its own execution: follow it.
      if (kind === "rollback" && data.outcome === "started" && data.execution_id) {
        window.location.assign("/executions/" + encodeURIComponent(data.execution_id));
        return;
      }
      await poll();
    } catch (error) {
      get("execution-note").textContent = error.message;
    } finally {
      state.control = false;
      renderActions(state.execution);
    }
  }

  function controlButton(label, kind, confirmText, primary) {
    const button = node("button", "pe-btn" + (primary ? " primary" : ""), label);
    button.type = "button";
    button.disabled = state.control;
    button.addEventListener("click", () => control(kind, confirmText));
    return button;
  }

  function action(label, href, primary) {
    const link = node("a", "pe-btn" + (primary ? " primary" : ""), label);
    link.href = href;
    return link;
  }

  function renderActions(data) {
    const actions = get("execution-actions");
    actions.replaceChildren();
    if (!data) return;
    const caps = data.capabilities || {};
    const preview = data.rollback_preview || {};
    // Only controls this run can actually honour (slice 5b-1): ABORT while steps remain, ROLL BACK
    // when some applied step has a true inverse. Never abort/rollback/resume otherwise.
    if (caps.abortable) {
      actions.append(controlButton("ABORT", "abort",
        "Stop this run before its next step? The step already running finishes.", false));
    }
    if (caps.rollbackable) {
      const undo = (preview.undo || []).length;
      const unknown = (preview.unknown || []).length;
      actions.append(controlButton(
        "ROLL BACK", "rollback",
        "Undo " + undo + " step(s) that changed the host?" +
        (unknown ? " " + unknown + " step(s) with an unknown outcome are left alone." : ""), false));
    }
    if (data.status === "passed" || data.status === "failed" || data.status === "rolled_back"
        || data.status === "rollback_failed" || data.status === "aborted") {
      actions.append(action("DONE", targetUrl(data), true));
    } else if (data.status === "interrupted") {
      actions.append(action("RE-SCAN THE HOST", "/#overview", true), action("RE-PROPOSE", targetUrl(data), false));
    }
    (data.rollbacks || []).forEach(child => {
      actions.append(action("VIEW ROLLBACK (" + child.status + ")", "/executions/" + encodeURIComponent(child.id), false));
    });
  }

  function render(data) {
    state.execution = data;
    const status = data.status;
    const isRestart = data.action === "docker.restart_service";
    const treatments = {
      running: ["warn", "RUNNING", isRestart ? "Restart command is in progress."
        : (data.summary || "Stack command") + " is in progress."],
      verifying: ["execution-verifying", "VERIFYING", isRestart
        ? "Commands finished. The run isn't done until the service answers."
        : "Compose finished. The run isn't done until the containers reach the expected state."],
      passed: ["ok", "VERIFIED GOOD", data.reason || "The service answered its verification checks."],
      failed: ["crit", "EXECUTION FAILED", data.reason || "The action did not pass verification."],
      interrupted: ["execution-interrupted", "WHAT WE KNOW", data.reason || "Contact ended before the host state could be confirmed."],
      aborted: ["warn", "ABORTED", data.reason || "Stopped by the operator between steps."],
      rolled_back: ["ok", "ROLLED BACK", data.reason || "The steps that changed the host were undone."],
      rollback_failed: ["crit", "ROLLBACK DID NOT FINISH", data.reason || "Check the host."],
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
      const value = target(data);
      get("summary-target").textContent = value.scope === "all"
        ? "every stack (" + value.stacks.length + ")"
        : data.action === "docker.restart_service" ? value.stack + "/" + value.service : value.stack;
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
