(function () {
  "use strict";
  const root = document.getElementById("approval-panel");
  if (!root) return;

  const state = { busy: false, timer: null, poller: null, revision: 0, submitting: new Set() };
  const cards = document.getElementById("approval-cards");
  const message = document.getElementById("approval-message");
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
    if (!response.ok) throw new Error(data.error || "host slow, retry");
    return data;
  }

  function target(item) {
    const value = item.target || {};
    return { stack: value.stack || "unknown", service: value.service || null,
      scope: value.scope, stacks: Array.isArray(value.stacks) ? value.stacks : [] };
  }

  function targetUrl(item) {
    const value = target(item);
    if (item.action !== "docker.restart_service") return "/#overview";
    return "/containers/" + encodeURIComponent(value.stack) + "/" + encodeURIComponent(value.service);
  }

  function date(value) {
    if (typeof value !== "number") return "unknown time";
    return new Date(value * 1000).toLocaleString();
  }

  function formatRemaining(expiresAt) {
    const seconds = Math.max(0, Math.ceil(expiresAt - Date.now() / 1000));
    return String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
  }

  function statusRow(item, label, level) {
    const row = node("div", "approval-status");
    row.append(node("span", "led-dot status-" + level), node("strong", "", label),
      node("span", "approval-plan", "PLAN " + item.id));
    return row;
  }

  function facts(item) {
    const value = target(item);
    const list = node("dl", "approval-facts");
    const radius = value.scope === "all" ? "every stack (" + value.stacks.length + ")"
      : item.action === "docker.restart_service" ? value.stack + "/" + value.service : value.stack;
    const entries = [
      ["BLAST RADIUS", radius],
      ["REVERSIBLE", item.capabilities && item.capabilities.rollbackable ? "YES" : "NO"],
    ];
    entries.forEach(([term, detail]) => list.append(node("dt", "", term), node("dd", "", detail)));
    return list;
  }

  function attribution(item, denied) {
    const box = node("div", "pe-attrib " + (denied ? "crit" : "ok"));
    const badge = node("span", "approval-avatar", (item.decided_by || "?").slice(0, 1).toUpperCase());
    const copy = node("div");
    copy.append(node("div", "pe-attrib-name", (denied ? "Denied by " : "Authorised by ") + (item.decided_by || "unknown")),
      node("div", "pe-attrib-meta", date(item.decided_at)));
    if (denied) {
      const reason = item.denial_reason;
      if (reason) copy.append(node("p", "pe-attrib-quote", reason));
    }
    box.append(badge, copy);
    return box;
  }

  function actionLink(label, href, primary) {
    const link = node("a", "pe-btn" + (primary ? " primary" : ""), label);
    link.href = href;
    return link;
  }

  async function refreshCard(id) {
    try {
      const item = await request("/api/approvals/" + encodeURIComponent(id));
      const old = cards.querySelector('[data-approval-id="' + id + '"]');
      if (old) old.replaceWith(renderCard(item));
      else await load();
    } catch (error) {
      message.textContent = error.message;
    }
  }

  async function decide(item, approve) {
    if (state.submitting.has(item.id)) return;
    state.revision += 1;
    state.submitting.add(item.id);
    const card = cards.querySelector('[data-approval-id="' + item.id + '"]');
    if (card) {
      card.classList.add("pe-is-busy");
      card.setAttribute("aria-busy", "true");
    }
    message.textContent = "Recording your " + (approve ? "authorisation" : "denial") + "… countdown paused.";
    try {
      const data = await request("/api/approvals/" + encodeURIComponent(item.id) + "/decide", {
        method: "POST", body: new URLSearchParams({ csrf_token: csrf(), approve: approve ? "1" : "0" }),
      });
      if (data.outcome === "started" && data.execution_id) {
        window.location.assign("/executions/" + encodeURIComponent(data.execution_id));
        return;
      }
      if (["denied", "refused", "already_decided", "expired", "unknown"].includes(data.outcome)) {
        await refreshCard(item.id);
        message.textContent = data.outcome === "refused"
          ? (data.message || "Refused by current policy.").replace(/<[^>]+>/g, "") : "";
      } else if (data.outcome === "busy") {
        message.textContent = data.message || "The decision could not be recorded.";
      } else {
        message.textContent = data.message || "Approval state changed.";
      }
    } catch (error) {
      message.textContent = error.message;
    } finally {
      state.submitting.delete(item.id);
      const current = cards.querySelector('[data-approval-id="' + item.id + '"]');
      if (current) {
        current.classList.remove("pe-is-busy");
        current.setAttribute("aria-busy", "false");
      }
      updateCountdowns();
    }
  }

  function pendingCard(item) {
    const card = node("article", "pe-card approval-card pending");
    card.dataset.approvalId = item.id;
    card.append(statusRow(item, "AWAITING AUTHORISATION", "warn"));
    card.append(node("h3", "approval-title", item.summary));
    card.append(node("p", "approval-requested", "Proposed via " + item.requested_via + " by " + (item.requested_by || "unknown") + " · " + date(item.created_at)));
    card.append(facts(item));
    const countdown = node("div", "pe-countdown");
    const number = node("span", "pe-countdown-num", formatRemaining(item.expires_at));
    number.dataset.expiresAt = item.expires_at;
    number.dataset.approvalId = item.id;
    countdown.append(number, node("span", "approval-countdown-label", "UNTIL THIS PLAN EXPIRES"));
    card.append(countdown);
    const actions = node("div", "approval-actions");
    const deny = node("button", "pe-btn", "DENY");
    deny.type = "button";
    deny.addEventListener("click", () => decide(item, false));
    const approve = node("button", "pe-btn primary", "AUTHORISE ✈");
    approve.type = "button";
    approve.addEventListener("click", () => decide(item, true));
    actions.append(deny, approve);
    card.append(actions);
    return card;
  }

  function resolvedCard(item) {
    const status = item.status || "expired";
    const level = status === "approved" ? "ok" : status === "denied" ? "crit" : "none";
    const card = node("article", "pe-card approval-card " + status);
    card.dataset.approvalId = item.id;
    card.append(statusRow(item, status === "approved" ? "AUTHORISED" : status.toUpperCase(), level));
    card.append(node("h3", "approval-title", item.summary));
    if (status === "expired") {
      card.append(facts(item));
      const readout = node("p", "approval-requested", "Proposed " + date(item.created_at) + " · lapsed " + date(item.expires_at));
      card.append(readout, actionLink("RE-PROPOSE", targetUrl(item), true));
      return card;
    }
    card.append(attribution(item, status === "denied"));
    const execution = item.execution || (item.executions && item.executions[0]);
    const actions = node("div", "approval-actions");
    if (status === "approved" && execution && execution.id) {
      actions.append(actionLink("WATCH EXECUTION ↗", "/executions/" + encodeURIComponent(execution.id), true));
    } else if (status === "denied") {
      actions.append(actionLink("RE-PROPOSE", targetUrl(item), true));
    }
    card.append(actions);
    return card;
  }

  function renderCard(item) {
    return item.status === "pending" ? pendingCard(item) : resolvedCard(item);
  }

  // Nothing pending is the normal state of this panel, so it gets a compact strip rather
  // than a panel-sized blank. The recent decisions below it carry the "what happened" that
  // the old empty card was trying to squeeze in.
  function emptyStrip() {
    const strip = node("div", "approval-empty");
    const portrait = document.createElement("img");
    portrait.src = "/static/characters/futurama/bender.png";
    portrait.alt = "";
    const copy = node("div", "approval-empty-copy");
    copy.append(node("div", "approval-empty-title", "NOTHING TO AUTHORISE"),
                node("div", "approval-empty-sub", "Plans land here the moment one is proposed."));
    strip.append(portrait, copy);
    return strip;
  }

  // A decision already made is a one-line receipt, not a card: title, who and when, outcome.
  // The full record is still one click away behind the chevron.
  function renderRecentRow(item) {
    const execution = item.execution || (item.executions && item.executions[0]) || {};
    // The field is execution.status, and "passed" is one of six terminal statuses. Testing
    // `state !== "failed"` made undefined pass, so a failed, aborted or still-running
    // execution rendered green and said PASSED.
    const status = item.status || "expired";
    const outcome = status === "approved"
      ? (execution.status ? execution.status.toUpperCase().replace(/_/g, " ") : "AUTHORISED")
      : status.toUpperCase();
    const passed = status === "approved" && execution.status === "passed";
    const level = passed ? "ok"
      : (status === "approved" && !execution.status) ? "none"
      : (status === "approved" || status === "denied") ? "crit" : "none";
    const row = node("div", "approval-recent " + level);
    row.append(node("span", "approval-recent-led"));

    const middle = node("div", "approval-recent-main");
    // summary is the human-readable action text; there is no title field, so the previous
    // fallback showed every decision as an opaque "Plan <id>".
    middle.append(node("div", "approval-recent-title", item.summary || ("Plan " + item.id)));
    const steps = Array.isArray(item.steps) ? item.steps.length : item.step_count;
    const parts = [item.decided_by || "system"];
    if (item.decided_at) parts.push(new Date(item.decided_at * 1000).toLocaleDateString());
    if (steps) parts.push(steps + " step" + (steps === 1 ? "" : "s"));
    middle.append(node("div", "approval-recent-meta", parts.join(" · ")));
    row.append(middle);

    row.append(node("span", "approval-recent-badge", outcome));
    const href = execution.id ? "/executions/" + encodeURIComponent(execution.id) : targetUrl(item);
    if (href) {
      const link = document.createElement("a");
      link.className = "approval-recent-more";
      link.href = href;
      link.textContent = "›";
      link.setAttribute("aria-label", "Open " + (item.title || item.id));
      row.append(link);
    }
    return row;
  }

  function updateCountdowns() {
    cards.querySelectorAll("[data-expires-at]").forEach(item => {
      if (!state.submitting.has(item.dataset.approvalId)) item.textContent = formatRemaining(Number(item.dataset.expiresAt));
    });
  }

  async function load() {
    if (state.busy || state.submitting.size || document.visibilityState !== "visible") return;
    state.busy = true;
    const revision = state.revision;
    try {
      const data = await request(root.dataset.api);
      if (revision !== state.revision || state.submitting.size) return;
      cards.replaceChildren();
      const pending = Array.isArray(data.pending) ? data.pending : [];
      const recent = Array.isArray(data.recent) ? data.recent : [];
      if (!pending.length) cards.append(emptyStrip());
      pending.forEach(item => cards.append(renderCard(item)));
      if (recent.length) {
        // data.recent holds every resolved approval, denied and expired included. Calling the
        // whole list "recently authorised" contradicts the rows underneath it.
        cards.append(node("div", "approval-recent-label", "RECENT DECISIONS"));
        recent.forEach(item => cards.append(renderRecentRow(item)));
      }
      const count = document.getElementById("approval-count");
      if (count) count.textContent = pending.length + " pending";
      message.textContent = "";
      updateCountdowns();
    } catch (error) {
      if (revision === state.revision && !state.submitting.size) message.textContent = error.message;
    } finally {
      state.busy = false;
    }
  }

  function visibility() {
    clearInterval(state.poller);
    if (document.visibilityState === "visible") {
      load();
      state.poller = setInterval(load, 10000);
    }
  }

  state.timer = setInterval(updateCountdowns, 1000);
  document.addEventListener("visibilitychange", visibility);
  window.addEventListener("planetexpress:approvals-refresh", load);
  visibility();
})();
