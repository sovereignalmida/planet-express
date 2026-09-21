// Persistent config editor. Server-provided values are rendered with textContent only.
(function () {
  "use strict";

  const root = document.getElementById("config-panel");
  if (!root) return;

  const state = {
    loaded: null,
    // What the textarea actually holds for the loaded text: browsers normalise \r\n to \n, so
    // comparing against the raw file text would mark a CRLF config dirty on load.
    baseline: "",
    loading: false,
    applying: false,
    activating: false,
    polling: false,
    lastCheck: null,
    checking: false,
    checkSeq: 0,
    activation: null,
    poller: null,
  };
  const loading = document.getElementById("config-loading");
  const workspace = document.getElementById("config-workspace");
  const editor = document.getElementById("config-editor");
  const checkButton = document.getElementById("config-check");
  const revertButton = document.getElementById("config-revert");
  const reviewButton = document.getElementById("config-review");
  const dirtyLabel = document.getElementById("config-dirty");
  const outcome = document.getElementById("config-outcome");
  const outcomeTitle = document.getElementById("config-outcome-title");
  const outcomeBody = document.getElementById("config-outcome-body");
  const dialog = document.getElementById("config-review-dialog");
  const applyButton = document.getElementById("config-apply");
  const cancelButton = document.getElementById("config-cancel");
  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  }

  async function request(url, options) {
    const response = await fetch(url, { cache: "no-store", ...options });
    if (response.status === 401) {
      window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname + "#config"));
      throw new Error("Please log in again.");
    }
    let data = {};
    try { data = await response.json(); } catch (_error) { /* handled by status below */ }
    if (!response.ok) {
      const error = new Error(data.error || "Config unavailable; try again shortly");
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function dirty() {
    return Boolean(state.loaded) && editor.value !== state.baseline;
  }

  function checkIsCurrentAndOk() {
    return Boolean(state.lastCheck && state.lastCheck.text === editor.value && state.lastCheck.result.ok === true);
  }

  function syncButtons() {
    const blocked = state.applying || state.activating || !state.loaded;
    // No typing while a draft is in flight or activating: a later reload of the editor would
    // otherwise silently discard it (Codex review, T36).
    editor.readOnly = state.applying || state.activating;
    checkButton.disabled = blocked || state.checking;
    revertButton.disabled = blocked || !dirty();
    reviewButton.disabled = blocked || !dirty() || !checkIsCurrentAndOk();
    applyButton.disabled = state.applying;
    cancelButton.disabled = state.applying;
    dirtyLabel.textContent = dirty() ? "UNSAVED DRAFT" : "SAVED";
    dirtyLabel.classList.toggle("is-dirty", dirty());
  }

  function chip(text, kind) {
    return node("span", "config-chip" + (kind ? " " + kind : ""), text);
  }

  function fieldKind(name) {
    return state.loaded && state.loaded.sensitive_fields.includes(name) ? "sensitive" : "host-only";
  }

  function renderFields(data) {
    const groups = document.getElementById("config-field-groups");
    groups.replaceChildren();
    const rows = [
      ["EDITABLE", data.editable_fields || [], "editable"],
      ["SENSITIVE", data.sensitive_fields || [], data.sensitive_edits_enabled ? "sensitive" : "locked"],
      ["EVERYTHING ELSE", ["edit on the host"], "locked"],
    ];
    rows.forEach(([label, fields, kind]) => {
      const row = node("div", "config-field-group");
      row.append(node("span", "config-group-label", label));
      fields.forEach(name => row.append(chip(
        name + (label === "SENSITIVE" && !data.sensitive_edits_enabled ? " · LOCKED" : ""), kind
      )));
      groups.append(row);
    });
    const note = document.getElementById("config-sensitive-note");
    note.hidden = Boolean(data.sensitive_edits_enabled);
    note.textContent = data.sensitive_edits_enabled ? "" :
      "Sensitive edits are locked. Set PE_ALLOW_SENSITIVE_CONFIG_EDITS=1 in " +
      "/etc/planetexpress.env (root-only), then restart core. This cannot be switched on from the dashboard.";
  }

  function renderLive(data) {
    const active = data.loaded_sha256 === data.sha256;
    const verdict = document.getElementById("config-verdict");
    verdict.className = "pe-verdict " + (active ? "ok" : "warn");
    verdict.querySelector(".pe-verdict-orb").textContent = active ? "✓" : "!";
    document.getElementById("config-live-state").textContent = active
      ? "ACTIVE" : "FILE NEWER THAN RUNNING CONFIG";
    document.getElementById("config-live-detail").textContent = active
      ? "The running core loaded file " + String(data.sha256).slice(0, 12) + "."
      : "The file and running core differ: someone edited on the host, or activation is in flight.";
    document.getElementById("config-path").textContent = data.path || "unknown";
    renderFields(data);
  }

  function setLoaded(data) {
    state.loaded = data;
    state.lastCheck = null;
    editor.value = data.text;
    state.baseline = editor.value;
    loading.hidden = true;
    workspace.hidden = false;
    renderLive(data);
    syncButtons();
  }

  async function loadConfig() {
    if (state.loading || state.loaded) return;
    state.loading = true;
    loading.textContent = "Loading config…";
    try {
      setLoaded(await request("/api/config"));
    } catch (error) {
      loading.textContent = error.message;
    } finally {
      state.loading = false;
    }
  }

  function showOutcome(title, level, paragraphs) {
    outcome.hidden = false;
    outcome.className = "pe-card config-outcome " + level;
    outcomeTitle.textContent = title;
    outcomeBody.replaceChildren();
    (paragraphs || []).forEach(text => outcomeBody.append(node("p", "", text)));
  }

  function appendErrors(errors) {
    if (!errors || !errors.length) return;
    const list = node("ul", "config-result-list");
    errors.forEach(error => {
      const item = node("li");
      if (error.loc) item.append(node("code", "", error.loc), document.createTextNode(" — "));
      item.append(document.createTextNode(error.msg || "Invalid config"));
      list.append(item);
    });
    outcomeBody.append(list);
  }

  function appendChanged(result) {
    const fields = result.changed_fields || [];
    if (!fields.length) return;
    const wrap = node("div", "config-result-chips");
    fields.forEach(name => {
      const locked = (result.locked_fields || []).includes(name);
      wrap.append(chip(name + (locked ? " · LOCKED (" + fieldKind(name) + ")" : ""),
        locked ? "locked" : "editable"));
    });
    outcomeBody.append(wrap);
  }

  async function checkDraft() {
    if (!state.loaded || state.applying || state.activating || state.checking) return;
    checkButton.disabled = true;
    // The result belongs to the text sent, not to whatever the textarea holds when it returns
    // (Codex review, T36).
    const submitted = editor.value;
    // Only the newest CHECK may report: an older reply landing late must not overwrite it
    // (Codex review round 2, T36).
    const seq = ++state.checkSeq;
    state.checking = true;
    showOutcome("CHECKING", "warn", ["Validating this exact draft. Nothing will be written."]);
    try {
      const result = await request("/api/config/validate", {
        method: "POST", body: new URLSearchParams({ csrf_token: csrf(), text: submitted }),
      });
      if (seq !== state.checkSeq) return;
      state.lastCheck = { text: submitted, result };
      if (submitted !== editor.value) {
        // Edited or reverted while in flight: the verdict describes text that is no longer in the
        // editor, so don't show it as this draft's (Codex review round 3, T36).
        showOutcome("DRAFT CHANGED WHILE CHECKING", "warn", ["CHECK again to validate the current draft. Nothing was written."]);
        return;
      }
      showOutcome(result.ok ? "CHECK PASSED" : "CHECK REFUSED", result.ok ? "ok" : "crit",
        [result.ok ? "This draft is valid and all changed fields may be edited here. Nothing was written."
          : "Nothing was written."]);
      appendErrors(result.errors);
      appendChanged(result);
    } catch (error) {
      if (seq !== state.checkSeq) return;
      state.lastCheck = null;
      if (submitted !== editor.value) {
        showOutcome("DRAFT CHANGED WHILE CHECKING", "warn", ["CHECK again to validate the current draft. Nothing was written."]);
        return;
      }
      showOutcome("CHECK FAILED", "crit", [error.message + ". Nothing was written."]);
    } finally {
      if (seq === state.checkSeq) state.checking = false;
      syncButtons();
    }
  }

  function lineDiff(before, after) {
    const left = before.split("\n");
    const right = after.split("\n");
    if (left.length > 2000 || right.length > 2000) return null;
    const table = Array.from({ length: left.length + 1 }, () => new Uint16Array(right.length + 1));
    for (let i = left.length - 1; i >= 0; i -= 1) {
      for (let j = right.length - 1; j >= 0; j -= 1) {
        table[i][j] = left[i] === right[j]
          ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
      }
    }
    const rows = [];
    let i = 0;
    let j = 0;
    while (i < left.length && j < right.length) {
      if (left[i] === right[j]) { rows.push({ type: "context", text: "  " + left[i] }); i += 1; j += 1; }
      else if (table[i + 1][j] >= table[i][j + 1]) { rows.push({ type: "del", text: "- " + left[i] }); i += 1; }
      else { rows.push({ type: "add", text: "+ " + right[j] }); j += 1; }
    }
    while (i < left.length) { rows.push({ type: "del", text: "- " + left[i] }); i += 1; }
    while (j < right.length) { rows.push({ type: "add", text: "+ " + right[j] }); j += 1; }
    const visible = new Set();
    rows.forEach((row, index) => {
      if (row.type !== "context") {
        for (let at = Math.max(0, index - 3); at <= Math.min(rows.length - 1, index + 3); at += 1) visible.add(at);
      }
    });
    const collapsed = [];
    let skipped = 0;
    rows.forEach((row, index) => {
      if (!visible.has(index)) { skipped += 1; return; }
      if (skipped) { collapsed.push({ type: "skip", text: "… " + skipped + " unchanged line" + (skipped === 1 ? "" : "s") + " …" }); skipped = 0; }
      collapsed.push(row);
    });
    if (skipped) collapsed.push({ type: "skip", text: "… " + skipped + " unchanged line" + (skipped === 1 ? "" : "s") + " …" });
    return collapsed;
  }

  function openReview() {
    if (!dirty() || !checkIsCurrentAndOk()) return;
    const fields = document.getElementById("config-review-fields");
    fields.replaceChildren();
    (state.lastCheck.result.changed_fields || []).forEach(name => fields.append(chip(name, "editable")));
    if (!(state.lastCheck.result.changed_fields || []).length) fields.append(chip("formatting/comments only", "editable"));
    const body = document.getElementById("config-diff-body");
    body.replaceChildren();
    const rows = lineDiff(state.baseline, editor.value);
    if (rows === null) body.append(node("div", "config-diff-row skip", "diff too large to display"));
    else rows.forEach(row => body.append(node("div", "config-diff-row " + row.type, row.text)));
    dialog.showModal();
  }

  async function digest(text) {
    if (!window.crypto || !window.crypto.subtle || !window.TextEncoder) return null;
    try {
      const value = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
      return Array.from(new Uint8Array(value), byte => byte.toString(16).padStart(2, "0")).join("");
    } catch (_error) { return null; }
  }

  function stopActivation() {
    state.activating = false;
    state.activation = null;
    clearInterval(state.poller);
    state.poller = null;
    syncButtons();
  }

  async function pollActivation() {
    if (!state.activating || state.polling || document.visibilityState !== "visible") return;
    const watch = state.activation;
    // Check first, then decide on the timeout: a tab hidden during activation (polling pauses)
    // must look once more on return instead of declaring failure unseen (VM rehearsal, T36).
    const expired = Date.now() - watch.started >= 60000;
    state.polling = true;
    try {
      const data = await request("/api/config");
      renderLive(data);
      const active = watch.sha256
        ? data.loaded_sha256 === watch.sha256
        : data.loaded_sha256 === data.sha256 && data.sha256 !== watch.baseSha256;
      if (active) {
        setLoaded(data);
        // Name what core loaded, not the file: the host may have edited it since (Codex review).
        showOutcome("ACTIVE", "ok", ["Core loaded config " + String(data.loaded_sha256).slice(0, 12) + "."]);
        stopActivation();
        return;
      } else if (!expired) {
        showOutcome("CORE RESTARTING…", "warn", ["The new file is present; waiting for the running core to confirm it loaded the change."]);
      }
    } catch (error) {
      if (expired) { /* fall through to the timeout below */ }
      else if (error.status === 503) showOutcome("CORE RESTARTING…", "warn", ["Core is temporarily unavailable during re-exec. Checking again shortly."]);
      else showOutcome("WAITING FOR CORE", "warn", [error.message + ". Checking again shortly."]);
    } finally {
      state.polling = false;
    }
    if (expired && state.activating && state.activation === watch) {
      showOutcome("ACTIVATION NOT CONFIRMED", "crit", [
        "Core hasn't confirmed the new config after 60 seconds. Check the host; this screen will not claim success.",
      ]);
      stopActivation();
    }
  }

  async function beginActivation(appliedText, baseSha256) {
    state.activating = true;
    state.activation = { text: appliedText, baseSha256, sha256: await digest(appliedText), started: Date.now() };
    showOutcome("CORE RESTARTING…", "warn", ["The config was written. Waiting for the running core to confirm activation."]);
    syncButtons();
    await pollActivation();
    if (state.activating && state.poller === null) state.poller = setInterval(pollActivation, 2000);
  }

  async function reconcileUnknown() {
    try {
      const data = await request("/api/config");
      renderLive(data);
      const file = data.sha256 === state.loaded.sha256 ? "The file still matches your loaded base."
        : "The file has changed since you loaded it (now " + String(data.sha256).slice(0, 12) + ").";
      const running = data.loaded_sha256 === data.sha256
        ? "The running core matches that file." : "The running core does not match that file.";
      outcomeBody.append(node("p", "", file + " " + running + " Your draft remains in the textarea."));
    } catch (error) {
      outcomeBody.append(node("p", "", "Could not re-check file and core state: " + error.message + "."));
    }
  }

  function loadLatestButton() {
    const actions = node("div", "config-outcome-actions");
    const button = node("button", "pe-btn", "LOAD LATEST");
    button.type = "button";
    button.addEventListener("click", async () => {
      if (dirty() && !window.confirm("Replace your draft with the latest config from the host?")) return;
      button.disabled = true;
      try {
        const data = await request("/api/config");
        setLoaded(data);
        showOutcome("LATEST LOADED", "ok", ["The editor now contains the latest file from the host."]);
      } catch (error) {
        showOutcome("LOAD FAILED", "crit", [error.message]);
      }
    });
    actions.append(button);
    outcomeBody.append(actions);
  }

  function renderApplyResult(result, appliedText, baseSha256) {
    const status = result.status;
    if (status === "activating") { beginActivation(appliedText, baseSha256); return; }
    if (status === "conflict") {
      showOutcome("FILE CHANGED ON HOST", "warn", [
        "The file changed since you loaded it. Your draft is still in the textarea until you choose LOAD LATEST.",
      ]);
      appendChanged(result);
      loadLatestButton();
    } else if (status === "locked") {
      showOutcome("LOCKED FIELDS", "crit", [result.reason || "These fields cannot be edited here. Nothing was written."]);
      appendChanged(result);
    } else if (status === "invalid") {
      showOutcome("INVALID CONFIG", "crit", ["Nothing was written."]);
      appendErrors(result.errors);
    } else if (status === "busy") {
      // Say what core is actually busy with: config applies need a fully idle core, so a plan
      // awaiting approval blocks them too, not only a running scan or action.
      showOutcome("CORE BUSY", "warn", [
        "Core is busy (" + (result.reason || "unknown") + "). Config changes need an idle core: a scan, " +
        "action or plan awaiting approval must finish first. Your draft is kept; try again then.",
      ]);
    } else if (status === "unchanged") {
      showOutcome("NOTHING TO APPLY", "warn", ["Nothing to apply."]);
    } else if (status === "write_failed") {
      showOutcome("WRITE FAILED", "crit", [result.reason || "Unknown failure.", "Nothing was written."]);
      appendChanged(result);
    } else if (status === "activation_failed") {
      showOutcome("ACTIVATION FAILED", "crit", [result.reason || "Unknown failure."]);
      appendChanged(result);
    } else {
      showOutcome("UNKNOWN OUTCOME", "crit", ["Core returned an unrecognised apply status. Check the host."]);
    }
  }

  async function applyDraft() {
    if (state.applying || !dirty() || !checkIsCurrentAndOk()) return;
    const appliedText = editor.value;
    const baseSha256 = state.loaded.sha256;
    state.applying = true;
    syncButtons();
    showOutcome("SUBMITTING", "warn", ["Sending the checked draft to core…"]);
    try {
      const result = await request("/api/config/apply", {
        method: "POST", body: new URLSearchParams({ csrf_token: csrf(), text: appliedText, base_sha256: baseSha256 }),
      });
      dialog.close();
      renderApplyResult(result, appliedText, baseSha256);
    } catch (error) {
      dialog.close();
      showOutcome("APPLY OUTCOME UNKNOWN", "crit", [
        "The apply request failed or core became unavailable before replying. The outcome is unknown; checking file and running-core state now.",
      ]);
      await reconcileUnknown();
    } finally {
      state.applying = false;
      syncButtons();
    }
  }

  editor.addEventListener("input", () => {
    if (state.lastCheck && state.lastCheck.text !== editor.value) state.lastCheck = null;
    syncButtons();
  });
  checkButton.addEventListener("click", checkDraft);
  revertButton.addEventListener("click", () => {
    if (!dirty() || window.confirm("Discard this draft and restore the config you loaded?")) {
      editor.value = state.baseline;
      state.lastCheck = null;
      outcome.hidden = true;
      syncButtons();
    }
  });
  reviewButton.addEventListener("click", openReview);
  cancelButton.addEventListener("click", () => dialog.close());
  applyButton.addEventListener("click", applyDraft);
  window.addEventListener("beforeunload", event => {
    if (!dirty()) return;
    event.preventDefault();
    event.returnValue = "";
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.activating) pollActivation();
  });

  function loadIfShown() {
    const shown = window.location.hash === "#config" || root.classList.contains("active");
    document.body.classList.toggle("config-active", shown);
    if (shown) loadConfig();
  }
  window.addEventListener("hashchange", loadIfShown);
  document.querySelectorAll(".tab").forEach(button => button.addEventListener("click", () => {
    document.body.classList.toggle("config-active", button.dataset.tab === "config");
    if (button.dataset.tab === "config") loadConfig();
  }));
  loadIfShown();
})();
