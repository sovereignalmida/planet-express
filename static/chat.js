// Kept outside the dashboard refresh lifecycle; server text never becomes HTML.
(function () {
  "use strict";
  const state = { pending: null, submitting: false, tickets: [], quota: null, timer: null,
                  selected: null };
  const form = document.getElementById("chat-form");
  const question = document.getElementById("chat-question");
  const askButton = document.getElementById("chat-ask");
  const message = document.getElementById("chat-message");
  const quotaLine = document.getElementById("chat-quota");
  const transcript = document.getElementById("chat-transcript");
  const answerEl = document.getElementById("chat-answer");

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
      quotaLine.textContent = state.quota.used + " of " + state.quota.limit + " calls today";
      state.tickets.forEach(entry => {
        if (entry.quotaLabel) entry.quotaLabel.textContent = quotaMessage();
      });
    } catch (error) {
      quotaLine.textContent = "Daily quota unavailable. " + error.message;
    }
  }

  // Card face: a tick, what was checked, and the exit code. The argv is real and useful, but
  // it is the answer to "how", not "what", so it opens with the output rather than being the
  // first thing an operator has to parse.
  function evidenceRecord(record) {
    const card = el("li");
    card.className = "chat-evidence-card" + (record.exit_code === 0 ? "" : " bad");

    const summary = document.createElement("summary");
    summary.className = "chat-evidence-head";
    summary.append(el("span", record.exit_code === 0 ? "✓" : "✗"));
    summary.firstChild.className = "chat-evidence-tick";
    summary.append(el("span", record.label || "Read-only check"));
    summary.lastChild.className = "chat-evidence-name";
    const spacer = el("span");
    spacer.className = "pe-spacer";
    summary.append(spacer, el("span", "exit " + record.exit_code + " ›"));
    summary.lastChild.className = "chat-evidence-exit";

    const details = document.createElement("details");
    details.className = "chat-evidence-detail";
    details.append(summary);
    const body = el("div");
    body.className = "chat-evidence-body";
    body.append(el("h4", "COMMAND"), el("pre", record.command || ""),
      el("h4", "STDOUT"), el("pre", record.stdout || ""),
      el("h4", "STDERR"), el("pre", record.stderr || ""));
    details.append(body);
    card.append(details);

    const firstLine = (record.stdout || record.stderr || "").split("\n")[0];
    if (firstLine) {
      const result = el("div", firstLine);
      result.className = "chat-evidence-result";
      card.append(result);
    }
    return card;
  }

  const OUTCOME_WORD = {
    answer: "answered", proposal: "proposed plan",
    insufficient_evidence: "not enough evidence", unsupported_fix: "no typed action covers it",
    quota_exhausted: "quota reached",
  };

  function hhmm(seconds) {
    if (typeof seconds !== "number") return "";
    const d = new Date(seconds * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  function verdictLevel(ticket) {
    if (ticket.status === "failed" || ticket.status === "interrupted") return "crit";
    if (ticket.outcome === "proposal") return "warn";
    if (ticket.outcome === "insufficient_evidence" || ticket.outcome === "unsupported_fix"
        || ticket.outcome === "quota_exhausted") return "warn";
    return "ok";
  }

  // One line for the left column: what you asked, when, how many checks it took, how it ended.
  function renderRow(entry) {
    const ticket = entry.ticket || {};
    entry.row.className = "chat-history-row" + (entry.id === state.selected ? " is-selected" : "");
    entry.row.replaceChildren();
    entry.row.append(el("div", entry.question));
    entry.row.firstChild.className = "chat-history-q";

    const checks = (ticket.evidence || []).length;
    const parts = [];
    if (ticket.finished_at) parts.push(hhmm(ticket.finished_at));
    else if (ticket.created_at) parts.push(hhmm(ticket.created_at));
    if (checks) parts.push(checks + " check" + (checks === 1 ? "" : "s"));
    parts.push(entry.status === "queued" ? "queued"
      : entry.status === "running" ? "working"
      : OUTCOME_WORD[ticket.outcome] || entry.status);
    entry.row.append(el("div", parts.join(" · ")));
    entry.row.lastChild.className = "chat-history-meta";
  }

  function select(entry) {
    state.selected = entry.id;
    state.tickets.forEach(renderRow);
    renderAnswer(entry);
  }

  function renderAnswer(entry) {
    if (!answerEl || state.selected !== entry.id) return;
    const ticket = entry.ticket || {};
    answerEl.replaceChildren();

    const head = el("div");
    head.className = "chat-answer-head";
    const portrait = document.createElement("img");
    portrait.src = "/static/characters/futurama/farnsworth.png";
    portrait.alt = "";
    head.append(portrait);
    const heading = el("div");
    heading.className = "chat-answer-heading";
    heading.append(el("h2", entry.question));
    const checks = (ticket.evidence || []).length;
    const when = ticket.finished_at ? "answered " + hhmm(ticket.finished_at) : entry.status;
    heading.append(el("div", when + " · " + checks + " read-only check" + (checks === 1 ? "" : "s")));
    heading.lastChild.className = "chat-answer-meta";
    head.append(heading);
    const badge = el("span", (OUTCOME_WORD[ticket.outcome] || entry.status).toUpperCase());
    badge.className = "chat-answer-badge " + verdictLevel(ticket);
    head.append(badge);
    answerEl.append(head);

    if (entry.status === "queued" || entry.status === "running") {
      // The layout must not jump when the answer lands, so the working state occupies the
      // same place the verdict will.
      const working = el("div", entry.status === "queued" ? "Queued…" : "Investigating…");
      working.className = "chat-verdict working";
      answerEl.append(working);
      return;
    }
    if (entry.status === "failed" || entry.status === "interrupted") {
      const failed = el("div", ticket.error || "Chat investigation failed");
      failed.className = "chat-verdict crit";
      answerEl.append(failed);
      const retry = el("button", "Retry");
      retry.type = "button";
      retry.className = "pe-btn";
      retry.addEventListener("click", () => {
        if (state.submitting || state.pending) {
          message.textContent = "Finish or retry the pending submission first.";
          return;
        }
        submit(entry.question); // A terminal ticket's retry gets a new submission id.
      });
      answerEl.append(retry);
      return;
    }

    // The verdict is the answer; everything under it is support. Paragraph one carries it.
    const paragraphs = String(ticket.answer || "").split(/\n{2,}/).filter(p => p.trim());
    if (paragraphs.length) {
      const verdict = el("div", paragraphs[0]);
      verdict.className = "chat-verdict " + verdictLevel(ticket);
      answerEl.append(verdict);
      if (paragraphs.length > 1) {
        const prose = el("div");
        prose.className = "chat-prose";
        paragraphs.slice(1).forEach(text => prose.append(el("p", text)));
        answerEl.append(prose);
      }
    }

    const extra = {
      proposal: "Proposed: a restart plan — approval card sent to Telegram · Approval ID: " + ticket.approval_id,
      insufficient_evidence: "Not enough evidence to answer.",
      unsupported_fix: "No approved action covers this fix.",
    }[ticket.outcome];
    if (extra) {
      const note = el("p", extra);
      note.className = "chat-answer-note";
      answerEl.append(note);
    }
    if (ticket.outcome === "quota_exhausted") {
      entry.quotaLabel = el("p", quotaMessage());
      entry.quotaLabel.className = "chat-answer-note";
      answerEl.append(entry.quotaLabel);
    }

    const cited = new Set(ticket.cited || []);
    const records = ticket.evidence || [];
    const citedRecords = records.filter((_, index) => cited.has(index));
    const otherRecords = records.filter((_, index) => !cited.has(index));
    if (citedRecords.length) {
      answerEl.append(evidenceSection("EVIDENCE · " + citedRecords.length + " CHECK" +
        (citedRecords.length === 1 ? "" : "S"), citedRecords));
    }
    if (otherRecords.length) {
      const details = document.createElement("details");
      details.className = "chat-evidence-other";
      const summary = document.createElement("summary");
      summary.textContent = otherRecords.length + " other check" +
        (otherRecords.length === 1 ? "" : "s") + " run";
      details.append(summary, evidenceSection("", otherRecords));
      answerEl.append(details);
    }
    refreshQuota();
  }

  function evidenceSection(label, records) {
    const wrap = el("div");
    wrap.className = "chat-evidence-section";
    if (label) {
      wrap.append(el("div", label));
      wrap.firstChild.className = "chat-evidence-label";
    }
    const list = el("ul");
    list.className = "chat-evidence-grid";
    records.forEach(record => list.append(evidenceRecord(record)));
    wrap.append(list);
    return wrap;
  }

  function render(entry, ticket) {
    entry.status = ticket.status;
    entry.ticket = ticket;
    renderRow(entry);
    if (state.selected === entry.id) renderAnswer(entry);
    else if (!state.selected) select(entry);
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
      const entry = { question: pending.question, id: ticket.ticket_id,
                      row: document.createElement("button"), polling: false };
      entry.row.type = "button";
      entry.row.addEventListener("click", () => select(entry));
      item.append(entry.row);
      transcript.prepend(item);
      state.tickets.push(entry);
      state.selected = entry.id;
      render(entry, ticket);
      select(entry);
      if (question.value.trim() === pending.question) question.value = "";
      message.textContent = "";
      askButton.textContent = "ASK ⏎";
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
      message.textContent = error.message + " Checking again shortly…";
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
  // An empty right column reads as a broken panel, so it invites the first question instead.
  function emptyAnswer() {
    if (!answerEl || state.tickets.length) return;
    answerEl.replaceChildren();
    const well = el("div");
    well.className = "chat-answer-empty";
    const portrait = document.createElement("img");
    portrait.src = "/static/characters/futurama/farnsworth.png";
    portrait.alt = "";
    well.append(portrait);
    const copy = el("div");
    copy.append(el("div", "NOTHING ASKED YET"));
    copy.firstChild.className = "chat-answer-empty-title";
    copy.append(el("div", "Ask a question on the left. The ship runs read-only checks and shows every one of them here."));
    copy.lastChild.className = "chat-answer-empty-sub";
    well.append(copy);
    answerEl.append(well);
  }

  emptyAnswer();
  refreshQuota();
})();
