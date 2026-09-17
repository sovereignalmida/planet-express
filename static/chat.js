// Kept outside the dashboard refresh lifecycle; server text never becomes HTML.
(function () {
  "use strict";
  const state = { pending: null, submitting: false, tickets: [], quota: null, timer: null };
  const form = document.getElementById("chat-form");
  const question = document.getElementById("chat-question");
  const askButton = document.getElementById("chat-ask");
  const message = document.getElementById("chat-message");
  const quotaLine = document.getElementById("chat-quota");
  const transcript = document.getElementById("chat-transcript");

  function el(tag, text) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function request(url, options) {
    const response = await fetch(url, { cache: "no-store", ...options });
    if (response.status === 401) {
      window.location.assign("/login?next=/%23chat");
      throw new Error("Please log in again.");
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Chat unavailable; try again shortly");
    return data;
  }

  function quotaMessage() {
    return "Daily chat limit reached; resets at " + (state.quota
      ? new Date(state.quota.resets_at * 1000).toLocaleString() : "an unavailable time (quota could not be loaded)");
  }

  async function refreshQuota() {
    try {
      state.quota = await request("/api/chat/quota");
      quotaLine.textContent = state.quota.used + " of " + state.quota.limit + " LLM calls used today";
      state.tickets.forEach(entry => {
        if (entry.quotaLabel) entry.quotaLabel.textContent = quotaMessage();
      });
    } catch (error) {
      quotaLine.textContent = "Daily quota unavailable. " + error.message;
    }
  }

  function evidenceRecord(record) {
    const item = el("li");
    item.append(el("p", record.command + " — exit code " + record.exit_code));
    const details = el("details");
    details.append(el("summary", "stdout / stderr"), el("h4", "stdout"),
      el("pre", record.stdout || ""), el("h4", "stderr"), el("pre", record.stderr || ""));
    item.append(details);
    return item;
  }

  function render(entry, ticket) {
    entry.status = ticket.status;
    entry.body.replaceChildren();
    if (ticket.status === "queued" || ticket.status === "running") {
      entry.body.append(el("p", ticket.status === "queued" ? "Queued…" : "Investigating…"));
      return;
    }
    if (ticket.status === "failed" || ticket.status === "interrupted") {
      entry.body.append(el("p", ticket.error || "Chat investigation failed"));
      const retry = el("button", "Retry");
      retry.type = "button";
      retry.className = "refresh-btn";
      retry.addEventListener("click", () => {
        if (state.submitting || state.pending) {
          message.textContent = "Finish or retry the pending submission first.";
          return;
        }
        submit(entry.question); // A terminal ticket's retry gets a new submission id.
      });
      entry.body.append(retry);
    } else {
      if (ticket.answer) entry.body.append(el("p", ticket.answer));
      const labels = {
        proposal: "Proposed: restart stack/service — approval card sent to Telegram · Approval ID: " + ticket.approval_id,
        insufficient_evidence: "Not enough evidence to answer",
        unsupported_fix: "No approved action covers this fix"
      };
      if (labels[ticket.outcome]) entry.body.append(el("p", labels[ticket.outcome]));
      if (ticket.outcome === "quota_exhausted") {
        entry.quotaLabel = el("p", quotaMessage());
        entry.body.append(entry.quotaLabel);
      }
    }
    const cited = new Set(ticket.cited || []);
    const evidence = el("ul");
    const other = el("ul");
    (ticket.evidence || []).forEach((record, index) => {
      (cited.has(index) ? evidence : other).append(evidenceRecord(record));
    });
    if (ticket.outcome === "answer" || evidence.childElementCount) {
      entry.body.append(el("h3", "Evidence"), evidence);
    }
    if (other.childElementCount) {
      const details = el("details");
      details.append(el("summary", "Other checks run"), other);
      entry.body.append(details);
    }
    refreshQuota();
  }

  // Not crypto.randomUUID(): browsers only expose it in secure contexts (HTTPS or localhost), and
  // the dashboard is served over plain HTTP on the LAN, where it would throw on every Ask.
  function submissionId() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
  }

  async function submit(text) {
    if (state.submitting) return;
    if (!state.pending) state.pending = { question: text, id: submissionId() };
    // Freeze the question with its id until acceptance, even if the textarea is edited.
    const pending = state.pending;
    state.submitting = true;
    askButton.disabled = true;
    message.textContent = "Submitting…";
    try {
      const ticket = await request("/api/chat", {
        method: "POST",
        body: new URLSearchParams({
          csrf_token: document.querySelector('meta[name="csrf-token"]').content,
          question: pending.question, submission_id: pending.id
        })
      });
      if (!ticket.ticket_id || !ticket.status) throw new Error("Invalid chat response; retry your submission.");
      state.pending = null;
      const item = el("li");
      item.className = "panel";
      const entry = { question: pending.question, id: ticket.ticket_id, body: el("div"), polling: false };
      item.append(el("h3", pending.question), entry.body);
      transcript.prepend(item);
      state.tickets.push(entry);
      render(entry, ticket);
      if (question.value.trim() === pending.question) question.value = "";
      message.textContent = "";
      askButton.textContent = "Ask";
    } catch (error) {
      message.textContent = error.message + " Retry submission to resend the same question safely.";
      askButton.textContent = "Retry submission";
    } finally {
      state.submitting = false;
      askButton.disabled = false;
    }
  }

  async function poll(entry) {
    if (entry.polling || !["queued", "running"].includes(entry.status)) return;
    entry.polling = true;
    try {
      render(entry, await request("/api/chat/" + encodeURIComponent(entry.id)));
    } catch (error) {
      entry.body.replaceChildren(el("p", error.message + " Checking again shortly…"));
    } finally {
      entry.polling = false;
    }
  }

  function pollVisible() {
    if (document.visibilityState === "visible") state.tickets.forEach(poll);
  }
  document.addEventListener("visibilitychange", () => {
    clearInterval(state.timer);
    state.timer = null;
    if (document.visibilityState === "visible") {
      pollVisible();
      state.timer = setInterval(pollVisible, 2000);
    }
  });
  if (document.visibilityState === "visible") state.timer = setInterval(pollVisible, 2000);
  // Handle pending retries even if the user cleared the textarea after a network error.
  form.noValidate = true;
  form.addEventListener("submit", event => {
    event.preventDefault();
    const text = question.value.trim();
    if (!state.pending && (!text || text.length > 2000)) {
      message.textContent = "Enter a question of 1–2000 characters.";
      return;
    }
    submit(text);
  });
  refreshQuota();
})();
