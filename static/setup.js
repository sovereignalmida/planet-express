// Setup wizard client. Talks only to /api/* with the CSRF header; never builds a path or command.
(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf"]').content;
  const send = (method, url, body) => fetch(url, {
    method, credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(r => r.json().then(j => ({status: r.status, body: j})));
  const errorBox = document.getElementById("answer-errors");

  function show(errors) {
    errorBox.textContent = "";
    for (const e of errors || []) {
      const line = document.createElement("div");
      line.className = "pe-hint";
      line.textContent = e.field + ": " + e.message;
      errorBox.appendChild(line);
    }
  }

  const pending = new Set();   // saves in flight: navigation waits for them, so a typed value is never lost
  function save(partial, reload) {
    const job = send("PUT", "/api/answers", partial).then(r => {
      show(r.body.errors);
      if (r.body.ok && reload) window.location.reload();
      return r.body.ok;
    });
    pending.add(job);
    job.finally(() => pending.delete(job));
    return job;
  }

  // Leaving a stage: finish saving, and only go on if every save was accepted.
  document.querySelectorAll("a.pe-btn[href]").forEach(link => link.addEventListener("click", ev => {
    const active = document.activeElement;
    if (active && active.tagName === "INPUT" && active.dataset.answer) active.dispatchEvent(new Event("change"));
    if (!pending.size) return;
    ev.preventDefault();
    Promise.all(Array.from(pending)).then(results => {
      if (results.every(Boolean)) window.location.assign(link.href);
    });
  }));

  document.querySelectorAll("[data-answer]").forEach(el => {
    const field = el.dataset.answer;
    if (el.tagName === "INPUT") {
      el.addEventListener("change", () => save({[field]: el.value}, false));
    } else {
      el.addEventListener("click", () => save({[field]: el.dataset.value}, true));
    }
  });

  document.querySelectorAll("[data-ignore]").forEach(box => box.addEventListener("change", () => {
    const names = Array.from(document.querySelectorAll("[data-ignore]")).filter(b => b.checked).map(b => b.dataset.ignore);
    save({ignored_stacks: names}, true);
  }));

  document.querySelectorAll('[data-action="discover"]').forEach(btn => btn.addEventListener("click", () => {
    btn.disabled = true;
    send("POST", "/api/discover").then(() => window.location.reload());
  }));

  const clock = document.getElementById("setup-clock");
  if (clock) {
    let left = parseInt(clock.dataset.seconds, 10);
    setInterval(() => {
      left = Math.max(0, left - 1);
      const m = String(Math.floor(left / 60)).padStart(2, "0"), s = String(left % 60).padStart(2, "0");
      clock.textContent = "SETUP MODE · CLOSES IN " + m + ":" + s;
    }, 1000);
  }

  const plan = document.getElementById("plan");
  if (plan) {
    send("POST", "/api/plan").then(r => {
      plan.textContent = "";
      const p = r.body;
      const add = (tag, cls, text, parent) => {
        const el = document.createElement(tag);
        if (cls) el.className = cls;
        el.textContent = text;
        (parent || plan).appendChild(el);
        return el;
      };
      if (p.error) { add("div", "pe-card crit", p.error); return; }
      if (p.blocked && p.blocked.length) p.blocked.forEach(b => add("div", "pe-card crit", b));
      (p.warnings || []).forEach(w => add("div", "pe-card warn", w));
      add("div", "pe-kicker", "PLAN " + p.plan_id + " · " + p.summary.steps + " STEPS · HIGHEST RISK " + p.summary.highest_risk);
      (p.steps || []).forEach((s, i) => {
        const row = add("details", "pe-check", "");
        const head = add("summary", "", (i + 1) + ". " + s.title + " ");
        add("span", "pe-badge " + (s.risk === "R0" || s.risk === "R1" ? "ok" : "warn"), s.risk, head);
        add("span", "pe-chip " + (s.reversible ? "ok" : "warn"), s.reversible ? "REVERSIBLE" : "NOT REVERSIBLE", head);
        add("div", "pe-panel-note", s.target, row);
        if (s.needs_root) add("div", "pe-panel-note", "NEEDS ROOT", row);
        const text = (s.preview && (s.preview.content || s.preview.text)) || "";
        if (text) add("pre", "pe-output", text, row);
      });
      add("h3", "", "WILL NOT TOUCH");
      (p.will_not_touch || []).forEach(t => add("div", "pe-panel-note", "• " + t));
      const approve = document.getElementById("approve");
      if (approve && p.applicable) {
        approve.disabled = false;
        approve.addEventListener("click", () => {
          approve.disabled = true;
          send("POST", "/api/apply", {plan_id: p.plan_id}).then(res => {
            if (res.status === 200) window.location.assign("/stage/install");
            else { show([{field: "install", message: res.body.error || "refused"}]); approve.disabled = false; }
          });
        });
      }
    });
  }

  const progress = document.getElementById("progress");
  if (progress) {
    let seq = 0;
    const lines = [];
    const mark = {pending: "·", started: "▶", ok: "✓", failed: "✗", undone: "↺"};
    const draw = (data) => {
      progress.textContent = "";
      const add = (tag, cls, text, parent) => {
        const el = document.createElement(tag);
        if (cls) el.className = cls;
        el.textContent = text;
        (parent || progress).appendChild(el);
        return el;
      };
      const done = data.steps.filter(s => s.status === "ok").length;
      const failed = data.steps.find(s => s.status === "failed");
      const verdict = add("section", "pe-verdict " + (failed || data.phase === "stopped" ? "crit" : data.phase === "done" ? "ok" : "warn"), "");
      const text = add("div", "pe-verdict-text", "", verdict);
      add("h2", "", {applying: "INSTALLING", done: "INSTALLED", stopped: "STOPPED", refused: "NOT STARTED", undoing: "UNDOING",
                     undone: "UNDONE", undo_stopped: "UNDO STOPPED"}[data.phase] || data.phase.toUpperCase(), text);
      add("p", "", done + " of " + data.steps.length + " steps finished", text);
      if (data.outcome && data.outcome.reason) add("p", "", data.outcome.reason, text);
      data.steps.forEach(s => {
        const row = add("div", "pe-check " + (s.status === "ok" ? "passed" : s.status === "failed" ? "failed" : s.status === "started" ? "running" : ""), "");
        add("strong", "", (mark[s.status] || "·") + " " + s.title, row);
        add("span", "pe-panel-note", " " + s.target + (s.satisfied ? " · already in place" : ""), row);
        if (s.status === "failed" && s.reason) add("div", "pe-output crit", s.reason, row);
        if (s.undo_note) add("div", "pe-panel-note", s.undo_note, row);
      });
      if (data.phase === "stopped" || data.phase === "refused") {
        add("div", "pe-hint", "NOTHING FURTHER WILL RUN. Fix the cause, then retry the step, or go back to the plan.");
        const retry = add("button", "pe-btn accent", "RETRY STEP", progress);
        retry.addEventListener("click", () => send("POST", "/api/retry", {step: (data.outcome || {}).step}).then(r => {
          if (r.status !== 200) show([{field: "retry", message: r.body.error || "refused"}]);
        }));
        add("a", "pe-btn", "BACK TO PLAN", progress).href = "/stage/review";
      }
      if (data.phase === "stopped" || data.phase === "done" || data.phase === "undo_stopped") {
        const undo = add("button", "pe-btn warn", "UNDO WHAT WAS INSTALLED", progress);
        undo.addEventListener("click", () => send("POST", "/api/undo").then(r => {
          if (r.status !== 200) show([{field: "undo", message: r.body.error || "refused"}]);
        }));
      }
      if (data.outcome && data.outcome.not_undone && data.outcome.not_undone.length) {
        add("h3", "", "LEFT IN PLACE");
        data.outcome.not_undone.forEach(n => add("div", "pe-panel-note", "• " + n.reason));
      }
      const log = add("div", "pe-logwell", "");
      lines.slice(-200).forEach(l => add("div", "pe-logline", l, log));
    };
    const tick = () => send("GET", "/api/events?after=" + seq).then(r => {
      const data = r.body;
      seq = data.seq;
      (data.events || []).forEach(e => {
        if (e.type === "log") lines.push(e.line);
        else if (e.type === "step_failed") lines.push("✗ " + e.step + ": " + e.reason);
        else if (e.type === "step_ok") lines.push("✓ " + e.step);
      });
      draw(data);
      if (progress.dataset.run === "install" && data.phase === "done") { window.location.assign("/stage/done"); return; }
      setTimeout(tick, ["applying", "undoing"].includes(data.phase) ? 1000 : 3000);
    }).catch(() => setTimeout(tick, 3000));
    tick();
  }
})();
