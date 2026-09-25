(function () {
  "use strict";
  const root = document.getElementById("incident-panel");
  if (!root) return;

  const cards = document.getElementById("incident-cards");
  const message = document.getElementById("incident-message");
  const state = {
    loading: false, submitting: new Set(), poller: null, revision: 0,
    refreshPending: false, notice: "",
    // Which half of the console you are looking at. Operator state, so it survives the
    // 15-second reload -- re-applied on every render, not just on click.
    status: "open",
    counts: { open: 0, resolved: 0 },
    items: { open: [], resolved: [] },
  };
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
      window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname + window.location.hash));
      throw new Error("Please log in again.");
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Incidents unavailable; try again shortly.");
    return data;
  }

  function date(value) {
    return typeof value === "number" ? new Date(value * 1000).toLocaleString() : "unknown";
  }

  async function propose(item, button) {
    if (state.submitting.has(item.id)) return;
    state.revision += 1;
    state.submitting.add(item.id);
    state.notice = "";
    button.disabled = true;
    message.textContent = "Checking incident evidence and policy…";
    try {
      const result = await request(root.dataset.api + "/" + encodeURIComponent(item.id) + "/propose", {
        method: "POST", body: new URLSearchParams({ csrf_token: csrf() }),
      });
      const resultMessage = result.reason || (result.ok ? "Proposal created." : "Proposal refused.");
      state.notice = resultMessage;
      window.dispatchEvent(new CustomEvent("planetexpress:approvals-refresh"));
      state.submitting.delete(item.id);
      await load(true);
      message.textContent = resultMessage;
    } catch (error) {
      state.notice = error.message;
      message.textContent = state.notice;
    } finally {
      state.submitting.delete(item.id);
      if (button.isConnected) button.disabled = false;
    }
  }

  function shortDate(value) {
    if (typeof value !== "number") return "unknown";
    const d = new Date(value * 1000);
    return (d.getMonth() + 1) + "/" + d.getDate() + " " +
      String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  // A row, not a card. Six incidents as a 3x2 card grid filled the tab; as rows they are a
  // list you can scan, and the evidence that used to be on every card face opens on click.
  function renderRow(item) {
    const row = node("div", "incident-row " + (item.status || "open"));
    row.dataset.incidentId = item.id;
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-expanded", "false");

    row.append(node("span", "incident-severity severity-" + (item.severity || "none").toLowerCase(),
                    item.severity || "INFO"));

    const middle = node("div", "incident-row-main");
    middle.append(node("div", "incident-row-title", item.summary),
                  node("div", "incident-row-where", item.kind + " · " + item.resource));
    row.append(middle);
    row.append(node("span", "incident-row-when",
                    shortDate(item.first_seen) + " → " + shortDate(item.last_seen)));
    row.append(node("span", "incident-row-tick", item.status === "resolved" ? "✓" : "•"));

    const detail = node("div", "incident-detail");
    const facts = node("dl", "incident-facts");
    [["OCCURRENCES", item.occurrences], ["FIRST SEEN", date(item.first_seen)],
      ["LAST SEEN", date(item.last_seen)],
      ["SOURCE", item.source_current ? "current" : "stale"]].forEach(([term, value]) => {
      facts.append(node("dt", "", term), node("dd", "", value));
    });
    detail.append(facts);

    const hint = item.hint || {};
    const callout = node("div", "incident-hint " + (hint.state || "unknown"));
    callout.append(node("strong", "", (hint.agent || "Crew") + ": "),
                   node("span", "", hint.message || "No hint available."));
    detail.append(callout);
    if (hint.state === "proposal_available") {
      const button = node("button", "pe-btn primary", "PROPOSE RESTART");
      button.type = "button";
      button.disabled = state.submitting.has(item.id);
      // Without this the click bubbles to the row and collapses the detail the operator is
      // reading, right as the proposal lands.
      button.addEventListener("click", event => { event.stopPropagation(); propose(item, button); });
      detail.append(button);
    }

    function toggle(event) {
      if (event.target.closest("button")) return;
      const open = row.classList.toggle("is-open");
      row.setAttribute("aria-expanded", open ? "true" : "false");
    }
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(event); }
    });

    const wrap = node("div", "incident-entry");
    wrap.append(row, detail);
    return wrap;
  }

  function applyFilter() {
    root.querySelectorAll("[data-incident-status]").forEach(button => {
      const active = button.dataset.incidentStatus === state.status;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
      const count = state.counts[button.dataset.incidentStatus] || 0;
      button.textContent = button.dataset.incidentStatus.toUpperCase() + " " + count;
    });
    const summary = document.getElementById("incident-count");
    if (summary) {
      summary.textContent = state.counts.open + " open · " + state.counts.resolved +
        " resolved this week";
    }

    const items = state.items[state.status] || [];
    cards.replaceChildren();
    if (!items.length) {
      cards.append(node("p", "muted-body", state.status === "open"
        ? "Nothing open. Leela files one the moment something goes wrong."
        : "No incidents resolved this week."));
    } else {
      items.forEach(item => cards.append(renderRow(item)));
    }
    renderRollup(items);
  }

  // Leela's one-line rollup, only when two or more incidents actually share a root cause.
  // An empty line under every list would be noise; this exists to say "these are one thing".
  function renderRollup(items) {
    const rollup = document.getElementById("incident-rollup");
    const text = document.getElementById("incident-rollup-text");
    if (!rollup || !text) return;
    const byKind = {};
    items.forEach(item => { byKind[item.kind] = (byKind[item.kind] || 0) + 1; });
    let top = null;
    Object.keys(byKind).forEach(kind => {
      if (!top || byKind[kind] > byKind[top]) top = kind;
    });
    if (!top || byKind[top] < 2) {
      rollup.hidden = true;
      return;
    }
    rollup.hidden = false;
    text.textContent = byKind[top] + " of " + items.length + " are " + top +
      " — same root cause. Click any row for evidence.";
  }

  async function load(force) {
    if (state.loading) {
      if (force) state.refreshPending = true;
      return;
    }
    if (state.submitting.size || document.visibilityState !== "visible") return;
    state.loading = true;
    const revision = state.revision;
    try {
      const [open, resolved] = await Promise.all([
        request(root.dataset.api + "?status=open&limit=20"),
        request(root.dataset.api + "?status=resolved&limit=10"),
      ]);
      if (revision !== state.revision || state.submitting.size) return;
      state.items.open = Array.isArray(open) ? open : [];
      state.items.resolved = Array.isArray(resolved) ? resolved : [];
      state.counts.open = state.items.open.length;
      state.counts.resolved = state.items.resolved.length;
      applyFilter();
      message.textContent = state.notice;
    } catch (error) {
      message.textContent = error.message;
    } finally {
      state.loading = false;
      if (state.refreshPending) {
        state.refreshPending = false;
        load();
      }
    }
  }

  function visibility() {
    clearInterval(state.poller);
    if (document.visibilityState === "visible") {
      load();
      state.poller = setInterval(load, 15000);
    }
  }

  root.querySelectorAll("[data-incident-status]").forEach(button => {
    button.addEventListener("click", () => {
      state.status = button.dataset.incidentStatus;
      applyFilter();
    });
  });

  document.addEventListener("visibilitychange", visibility);
  visibility();
})();
