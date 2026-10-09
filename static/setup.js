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

  function save(partial, reload) {
    return send("PUT", "/api/answers", partial).then(r => {
      show(r.body.errors);
      if (r.body.ok && reload) window.location.reload();
    });
  }

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
    });
  }
})();
