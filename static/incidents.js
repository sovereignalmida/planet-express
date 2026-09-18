(function () {
  "use strict";
  const root = document.getElementById("incident-panel");
  if (!root) return;

  const cards = document.getElementById("incident-cards");
  const message = document.getElementById("incident-message");
  const state = {
    loading: false, submitting: new Set(), poller: null, revision: 0,
    refreshPending: false, notice: "",
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

  function render(item) {
    const card = node("article", "pe-card incident-card " + (item.status || "open"));
    card.dataset.incidentId = item.id;
    const head = node("div", "incident-head");
    head.append(node("span", "incident-severity severity-" + (item.severity || "none").toLowerCase(), item.severity || "INFO"),
      node("span", "incident-state", item.source_current ? "CURRENT" : "STALE"),
      node("span", "approval-plan", "INCIDENT " + item.id));
    card.append(head, node("h3", "incident-title", item.summary),
      node("p", "incident-resource", item.kind + " · " + item.resource));

    const facts = node("dl", "incident-facts");
    [["OCCURRENCES", item.occurrences], ["FIRST SEEN", date(item.first_seen)],
      ["LAST SEEN", date(item.last_seen)]].forEach(([term, value]) => {
      facts.append(node("dt", "", term), node("dd", "", value));
    });
    card.append(facts);

    const hint = item.hint || {};
    const callout = node("div", "incident-hint " + (hint.state || "unknown"));
    callout.append(node("strong", "", (hint.agent || "Crew") + ": "), node("span", "", hint.message || "No hint available."));
    card.append(callout);
    if (hint.state === "proposal_available") {
      const button = node("button", "pe-btn primary", "PROPOSE RESTART");
      button.type = "button";
      button.disabled = state.submitting.has(item.id);
      button.addEventListener("click", () => propose(item, button));
      card.append(button);
    }
    return card;
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
      cards.replaceChildren();
      const items = [...(Array.isArray(open) ? open : []), ...(Array.isArray(resolved) ? resolved : [])];
      if (!items.length) cards.append(node("p", "muted-body", "No incidents recorded yet."));
      items.forEach(item => cards.append(render(item)));
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

  document.addEventListener("visibilitychange", visibility);
  visibility();
})();
