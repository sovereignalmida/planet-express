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
      // Changing who it runs as can add or remove the root acknowledgement, so that page is redrawn.
      el.addEventListener("change", () => save({[field]: el.value}, field === "run_as"));
    } else {
      el.addEventListener("click", () => save({[field]: el.dataset.value}, true));
    }
  });

  const root = document.getElementById("accept-root");
  if (root) root.addEventListener("change", () => save({accept_root_service: root.checked}, true));

  document.querySelectorAll("[data-ignore]").forEach(box => box.addEventListener("change", () => {
    const names = Array.from(document.querySelectorAll("[data-ignore]")).filter(b => b.checked).map(b => b.dataset.ignore);
    save({ignored_stacks: names}, true);
  }));

  document.querySelectorAll('[data-action="discover"]').forEach(btn => btn.addEventListener("click", () => {
    btn.disabled = true;
    send("POST", "/api/discover").then(() => window.location.reload());
  }));

  const say = (id, text, bad) => {
    const box = document.getElementById(id);
    if (!box) return;
    box.textContent = "";
    const line = document.createElement("div");
    line.className = bad ? "pe-hint" : "pe-card ok";
    line.textContent = text;
    box.appendChild(line);
  };
  const on = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener("click", fn); };

  // Telegram
  on("tg-find", () => {
    const token = document.getElementById("tg-token").value.trim();
    send("POST", "/api/telegram/find-chat", {token}).then(r => {
      const b = r.body;
      say("tg-result", b.found ? "Found chat " + b.chat + (b.bot ? " with @" + b.bot : "") + ". Now send the test message." : b.message, !b.found);
      document.getElementById("tg-test").disabled = !b.found;
      document.getElementById("tg-token").value = "";            // the server holds it now; it is not left in the page
    });
  });
  on("tg-test", () => send("POST", "/api/telegram/test").then(r => {
    say("tg-result", r.body.message || r.body.error, !r.body.verified);
    if (r.body.verified) setTimeout(() => window.location.reload(), 1200);
  }));
  on("tg-skip", () => save({telegram: null}, false).then(() => window.location.assign("/stage/operator")));

  // Operator account
  on("op-begin", () => {
    const name = document.getElementById("op-name").value.trim();
    const pass = document.getElementById("op-pass").value, again = document.getElementById("op-pass2").value;
    if (pass.length < 12) return say("op-result", "The passphrase needs at least 12 characters.", true);
    if (pass !== again) return say("op-result", "The two passphrases do not match.", true);
    send("POST", "/api/operator/totp", {name}).then(r => {
      if (!r.body.ok) return say("op-result", r.body.message, true);
      document.getElementById("op-qr").src = r.body.qr;
      document.getElementById("op-manual").textContent = r.body.manual;
      document.getElementById("op-enrol").hidden = false;
      say("op-result", "Scan the code with your authenticator, then type the 6 digits it shows.", false);
    });
  });
  on("op-verify", () => {
    const body = {name: document.getElementById("op-name").value.trim(), passphrase: document.getElementById("op-pass").value,
                  code: document.getElementById("op-code").value.trim()};
    send("POST", "/api/operator/verify", body).then(r => {
      say("op-result", r.body.message || r.body.error, !r.body.verified);
      if (r.body.verified) {
        document.getElementById("op-pass").value = document.getElementById("op-pass2").value = "";
        setTimeout(() => window.location.reload(), 1200);
      } else {
        const cell = document.getElementById("op-code");
        cell.value = "";
        cell.parentElement.classList.add("is-rejected");
      }
    });
  });

  // LLM key
  let provider = "openai";
  document.querySelectorAll("[data-provider]").forEach(btn => btn.addEventListener("click", () => {
    provider = btn.dataset.provider;
    document.querySelectorAll("[data-provider]").forEach(b => b.classList.toggle("accent", b === btn));
  }));
  on("llm-check", () => {
    const key = document.getElementById("llm-key").value.trim();
    send("POST", "/api/llm/check", {provider, api_key: key}).then(r => {
      say("llm-result", r.body.message, !r.body.ok);
      if (r.body.ok) { document.getElementById("llm-key").value = ""; setTimeout(() => window.location.reload(), 1200); }
    });
  });
  on("llm-skip", () => save({llm: null}, false).then(() => window.location.assign("/stage/review")));

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
