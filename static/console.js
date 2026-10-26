// The Operator Console. Draws the conversation list and the thread from the JSON feed. Server and agent text only ever becomes text nodes
// (textContent and createTextNode only): nothing here builds markup from text, so nothing an agent writes can become markup. The pure functions at the
// top (prose blocks, trail steps, check phrasing) have no DOM and are exported for the node tests in tests/js.
(function (root) {
  "use strict";

  // ── pure ─────────────────────────────────────────────────────────────────────────────────────────
  // A small, safe subset of markdown, as a tree: fenced code, bullet and numbered lists, paragraphs; inline `code` and **bold**.
  function inline(text) {
    const out = [];
    const re = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) out.push({ t: "text", v: text.slice(last, m.index) });
      const tok = m[0];
      out.push(tok[0] === "`" ? { t: "code", v: tok.slice(1, -1) } : { t: "bold", v: tok.slice(2, -2) });
      last = m.index + tok.length;
    }
    if (last < text.length) out.push({ t: "text", v: text.slice(last) });
    return out;
  }

  function proseBlocks(text) {
    const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let para = [], list = null, code = null;
    const flushPara = () => { if (para.length) { blocks.push({ t: "p", c: inline(para.join(" ")) }); para = []; } };
    const flushList = () => { if (list) { blocks.push(list); list = null; } };
    for (const raw of lines) {
      if (code !== null) {
        if (/^\s*```/.test(raw)) { blocks.push({ t: "pre", v: code.join("\n") }); code = null; } else code.push(raw);
        continue;
      }
      if (/^\s*```/.test(raw)) { flushPara(); flushList(); code = []; continue; }
      const bullet = /^\s*[-*]\s+(.*)$/.exec(raw), numbered = /^\s*\d+[.)]\s+(.*)$/.exec(raw);
      if (bullet || numbered) {
        flushPara();
        const kind = bullet ? "ul" : "ol";
        if (!list || list.t !== kind) { flushList(); list = { t: kind, items: [] }; }
        list.items.push(inline((bullet || numbered)[1]));
        continue;
      }
      if (!raw.trim()) { flushPara(); flushList(); continue; }
      flushList();
      para.push(raw.trim());
    }
    if (code !== null) blocks.push({ t: "pre", v: code.join("\n") });
    flushPara(); flushList();
    return blocks;
  }

  // The agent's activity for one turn: each call with the result it got, a count line, and the text so far while it streams.
  function trailFor(events, turn) {
    const steps = [];
    let stream = "", omitted = 0;
    for (const e of events) {
      if (e.turn !== turn) continue;
      if (e.type === "call") steps.push({ kind: e.kind, name: e.name, detail: e.detail, out: null, why: null });
      else if (e.type === "result") {
        const open = [...steps].reverse().find(s => s.out === null && s.name === e.name && s.kind !== "denied");
        if (open) { open.out = e.excerpt; if (e.failed) open.kind = "failed"; }
        else steps.push({ kind: e.failed ? "failed" : "read", name: e.name, detail: "", out: e.excerpt, why: null });
      } else if (e.type === "denied") steps.push({ kind: "denied", name: e.name, detail: e.detail, out: null, why: e.why });
      else if (e.type === "delta") stream += e.text;
      else if (e.type === "omitted") omitted += e.count || 0;
    }
    const count = k => steps.filter(s => s.kind === k).length;
    const parts = [];
    if (count("read")) parts.push("looked at " + count("read") + (count("read") === 1 ? " thing" : " things"));
    if (count("ran")) parts.push("ran " + count("ran") + (count("ran") === 1 ? " command" : " commands"));
    if (count("changed")) parts.push("wrote " + count("changed") + (count("changed") === 1 ? " file" : " files"));
    if (count("denied")) parts.push(count("denied") + " refused");
    if (count("failed")) parts.push(count("failed") + " failed");
    const summary = parts.length ? parts[0][0].toUpperCase() + parts.join(", ").slice(1) : "No activity recorded";
    return { steps, stream, omitted, summary, changed: count("changed"), refused: count("denied") + count("failed") };
  }

  // A check in words, from PE's catalogue kind and its parameters. Unknown kinds fall back to the kind and its short parameters.
  function checkPhrase(check) {
    const p = (check && check.params) || {};
    const kind = check && check.check;
    if (kind === "container_state") return (p.container || "a container") + " is running and stable";
    if (kind === "http") return (p.url || "a URL") + " answers";
    if (kind === "mount_readable") return (p.path || "a path") + " is readable" + (p.container ? " inside " + p.container : "");
    if (kind === "project_complete") return "the " + (p.project || "stack") + " project has all its services";
    if (kind === "exec_probe") return (p.container || "a container") + " can run " + ((p.argv && p.argv[0]) || "a command");
    const bits = Object.keys(p).slice(0, 3).map(k => k + "=" + String(p[k]).slice(0, 40));
    return String(kind || "a check") + (bits.length ? " (" + bits.join(", ") + ")" : "");
  }

  function groupRail(list) {
    return ["Needs you", "Working", "Recent"].map(name => ({ name, rows: list.filter(c => c.status.group === name) })).filter(g => g.rows.length);
  }

  function budgetLabel(b) {
    if (!b) return "";
    return b.state === "stop" ? "used up" : b.used_pct + "% used";
  }

  const pure = { inline, proseBlocks, trailFor, checkPhrase, groupRail, budgetLabel };
  if (typeof module !== "undefined" && module.exports) { module.exports = { ...pure, boot: (doc, win) => boot(doc, win) }; }

  // ── DOM ──────────────────────────────────────────────────────────────────────────────────────────
  function boot(document, window) {
    const con = document.getElementById("pe-con");
    if (!con) return null;
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const bootData = JSON.parse(document.getElementById("con-boot").textContent);
    const $ = id => document.getElementById(id);
    const refs = { sheet: $("con-sheet"), list: $("con-list"), top: $("con-top"), scroll: $("con-scroll"), thread: $("con-thread"), composer: $("con-composer"),
                   text: $("con-text"), send: $("con-send"), stop: $("con-stop"), note: $("con-note"), newBtn: $("con-new") };
    const state = { selected: null, list: [], feed: null, messages: [], events: [], lastMessage: 0, lastEvent: 0, newMode: true,
                    picked: {}, openTrails: {}, scrapArmed: false, archiveArmed: false, timer: null, error: null, sending: false,
                    inflight: null, again: false, sig: { rail: "", top: "", thread: "" }, sheet: null, sheetError: null, sheetBusy: false, armed: {}, drift: null, preparing: false };
    const ORIGINS = { operator: ["Y", "You asked"], incident: ["!", "From an incident"], pe: ["P", "PE started it"] };
    const EXAMPLES = ["The download apps lost internet after the reboot. Figure out why.", "Why is the media stack slow right now?",
                      "Which containers restarted in the last day, and why?"];

    function el(tag, cls, text) {
      const node = document.createElement(tag);
      if (cls) node.className = cls;
      if (text !== undefined) node.textContent = text;
      return node;
    }
    function add(parent, ...children) { for (const c of children) if (c) parent.appendChild(c); return parent; }

    async function request(url, options) {
      const response = await window.fetch(url, { cache: "no-store", credentials: "same-origin", ...options });
      if (response.status === 401) { window.location.assign("/login?next=/console"); throw new Error("Please log in again."); }
      let data = {};
      try { data = await response.json(); } catch (_e) { /* an empty or non-JSON body */ }
      if (!response.ok) { const err = new Error(data.error || "The Console is unavailable; try again shortly."); err.status = response.status; err.code = data.code; throw err; }
      return data;
    }
    const post = (url, fields) => request(url, { method: "POST", body: new window.URLSearchParams({ csrf_token: csrf, ...(fields || {}) }) });

    // ── rail ──
    function renderRail() {
      const sig = JSON.stringify([state.list, state.selected]);
      if (sig === state.sig.rail) return;                      // nothing changed: leave the nodes (and any focus) alone
      state.sig.rail = sig;
      refs.list.replaceChildren();
      if (!state.list.length) {
        const card = el("div", "pe-card none");
        add(card, add(el("div", "pe-card-head"), el("span", "pe-card-title sm", "NO CONVERSATIONS YET")), el("div", "pe-footnote", "Ask something and it shows up here."));
        refs.list.appendChild(card);
        return;
      }
      for (const group of groupRail(state.list)) {
        add(refs.list, add(el("div", "pe-con-group", group.name.toUpperCase()), el("span", "n", String(group.rows.length))));
        for (const c of group.rows) {
          const row = el("button", "pe-con-row" + (c.id === state.selected ? " is-sel" : ""));
          row.type = "button";
          const origin = ORIGINS[c.origin] || ORIGINS.operator;
          const origin1 = el("span", "pe-con-origin", origin[0]); origin1.title = origin[1];
          const age = el("span", "pe-con-age", c.age);
          if (c.status.group === "Needs you") add(age, el("span", "pe-led " + (c.status.level === "high" ? "high" : "warn")));
          add(row, origin1, el("span", "pe-con-rtitle", c.title), age, add(el("span", "pe-con-rmeta"), el("span", "st " + c.status.level, c.status.text)));
          row.addEventListener("click", () => select(c.id, true));
          refs.list.appendChild(row);
        }
      }
    }

    // ── top bar ──
    function renderTop() {
      const f = state.feed;
      const sig = JSON.stringify([state.newMode, f && f.conversation, f && f.budget, state.archiveArmed]);
      if (sig === state.sig.top) return;
      state.sig.top = sig;
      refs.top.replaceChildren();
      if (!f || state.newMode) { refs.top.hidden = true; return; }
      refs.top.hidden = false;
      const c = f.conversation, b = f.budget;
      const back = el("button", "pe-con-back", "‹"); back.type = "button"; back.setAttribute("aria-label", "Back to conversations");
      back.addEventListener("click", showList);
      const title = add(el("div"), el("div", "pe-con-title", c.title), el("div", "pe-con-title-sub", (ORIGINS[c.origin] || ORIGINS.operator)[1] + " · " + c.turns + (c.turns === 1 ? " turn" : " turns")));
      title.style.cssText = "min-width:0;flex:1";
      const meter = el("span", "pe-budget" + (b.state === "ok" ? "" : " " + b.state)); meter.title = "Model budget for this conversation";
      const fill = el("span", "pe-bar-fill " + (b.state === "ok" ? "" : b.state)); fill.style.cssText = "display:block;width:" + b.used_pct + "%";
      add(meter, el("span", "", "BUDGET"), add(el("span", "pe-bar"), fill), el("span", "", budgetLabel(b)));
      const log = el("a", "", "Work log"); log.href = "/missions";
      const open = c.state === "open";
      const archive = el("button", "pe-btn sm" + (state.archiveArmed ? " danger" : " ghost"), state.archiveArmed ? "Archive? Click again" : "Archive"); archive.type = "button";
      archive.title = "Close this conversation: its working files are removed and its slot is freed";
      archive.hidden = !open;
      archive.addEventListener("click", async () => {
        if (!state.archiveArmed) { state.archiveArmed = true; state.sig.top = ""; renderTop(); return; }
        state.archiveArmed = false;
        try { await post("/api/console/" + state.selected + "/archive"); await loadFeed(); await refreshList(); } catch (e) { fail(e); }
      });
      const bar = add(el("div", "pe-con-titlebar"), back, title, meter, archive, log);
      const v = c.verdict, level = c.status.level;
      const orb = { ok: "✓", warn: "!", high: "✕", crit: "✕", none: "…" }[level] || "…";
      const verdict = el("div", "pe-verdict compact " + level); verdict.setAttribute("role", "status"); verdict.setAttribute("aria-live", "polite");
      add(verdict, el("div", "pe-verdict-orb", orb), add(el("div", "pe-verdict-text"), el("h2", "", v.h), el("p", "", v.p)));
      add(refs.top, bar, verdict);
    }

    // ── thread ──
    function prose(parent, text) {
      const box = el("div", "pe-prose pe-con-prose");
      const inl = (target, items) => { for (const i of items) add(target, i.t === "code" ? el("code", "pe-con-icode", i.v) : i.t === "bold" ? el("strong", "", i.v) : document.createTextNode(i.v)); };
      for (const b of proseBlocks(text)) {
        if (b.t === "p") inl(add(box, el("p")).lastChild, b.c);
        else if (b.t === "pre") add(box, el("pre", "pe-code pe-con-pre", b.v));
        else { const list = add(box, el(b.t)).lastChild; for (const item of b.items) inl(add(list, el("li")).lastChild, item); }
      }
      return add(parent, box);
    }

    function trailBlock(turn, running) {
      const t = trailFor(state.events, turn);
      if (!t.steps.length && !running) return null;
      const d = el("details", "pe-trail");
      if (running || state.openTrails[turn]) d.open = true;
      d.addEventListener("toggle", () => { state.openTrails[turn] = d.open; });
      const sum = add(el("summary"), el("span", "car"), el("span", "sum", running && !t.steps.length ? "Starting…" : t.summary));
      if (t.changed) add(sum, el("span", "pe-chip warn", t.changed + " written"));
      if (t.refused) add(sum, el("span", "pe-chip high", t.refused + " refused or failed"));
      const steps = el("div", "pe-trail-steps");
      for (const s of t.steps) {
        const row = el("div", "pe-trail-step");
        add(row, el("span", "pe-kind " + (s.kind === "changed" || s.kind === "denied" || s.kind === "failed" ? s.kind : ""), s.kind),
            el("span", "pe-trail-label", s.name + (s.detail ? " · " + s.detail : "")));
        if (s.out) add(row, el("div", "pe-trail-out", s.out));
        if (s.why) add(row, el("div", "pe-trail-why", s.why));
        steps.appendChild(row);
      }
      if (t.omitted) steps.appendChild(el("div", "pe-trail-why", t.omitted + " more steps were not shown."));
      return add(d, sum, steps);
    }

    function planCard(planInfo) {
      const plan = planInfo.plan, dead = planInfo.expired;
      const card = el("div", "pe-plan" + (dead ? " is-dead" : "")); card.setAttribute("role", "group"); card.setAttribute("aria-label", "Plan");
      const head = add(el("div", "pe-plan-head"), el("h3", "", "PLAN"), el("span", dead ? "pe-badge none" : "pe-badge warn", dead ? "EXPIRED" : "NEEDS YOUR CALL"),
                       el("div", "pe-spacer"), el("span", "pe-label pe-con-hash", plan.options.length + " option" + (plan.options.length === 1 ? "" : "s") + " · " + planInfo.hash));
      add(card, head);
      if (plan.known.length || plan.unsure.length) {
        const kn = el("div", "pe-kn");
        for (const [label, items, cls] of [["WHAT I KNOW", plan.known, ""], ["WHAT I'M UNSURE OF", plan.unsure, " unsure"]]) {
          const col = add(el("div", "pe-kn-col" + cls), el("span", "pe-label", label)), ul = el("ul");
          for (const it of items) add(ul, add(el("li"), el("span", "", it)));
          add(col, ul); kn.appendChild(col);
        }
        card.appendChild(kn);
      }
      const rec = Math.max(0, plan.options.findIndex(o => o.recommended));
      if (state.picked[planInfo.id] === undefined) state.picked[planInfo.id] = rec;
      const group = el("div", "pe-plan-opts"); group.setAttribute("role", "radiogroup"); group.setAttribute("aria-label", "Options");
      const options = [];
      plan.options.forEach((o, i) => {
        const selected = state.picked[planInfo.id] === i;
        const opt = el("div", "pe-opt" + (selected ? " is-sel" : "")); opt.setAttribute("role", "radio"); opt.setAttribute("aria-checked", String(selected)); opt.tabIndex = 0;
        add(opt, add(el("div", "pe-opt-top"), el("span", "pe-opt-radio"), el("span", "pe-opt-name", "OPTION " + String.fromCharCode(65 + i) + " · " + o.name),
                      o.recommended ? el("span", "pe-opt-rec", "RECOMMENDED") : null), el("div", "pe-opt-sum", o.summary));
        const sec = (label, ...body) => add(el("div", "pe-opt-sec"), el("span", "pe-label", label), ...body);
        const list = (items, cls) => { const ul = el("ul"); for (const it of items) add(ul, add(el("li", cls), el("span", "", it))); return ul; };
        add(opt, sec("WHAT CHANGES", list(o.changes)), sec("WILL NOT TOUCH", list(o.will_not_touch, "no")),
            sec("RISK", add(el("div", "pe-opt-risk"), el("span", "pe-badge " + ({ low: "ok", medium: "warn", high: "high" }[o.risk.level] || "none"), o.risk.level.toUpperCase()), el("span", "", o.risk.text))),
            sec("ROLLS BACK TO", el("p", "", o.rolls_back_to)), sec("PE WILL CHECK", list(o.checks.map(checkPhrase), "chk")), sec("EFFORT", el("span", "pe-value", o.effort)));
        const pick = () => { state.picked[planInfo.id] = i; renderThread(); };
        opt.addEventListener("click", pick);
        opt.addEventListener("keydown", ev => {
          if (ev.key === " " || ev.key === "Enter") { ev.preventDefault(); pick(); }
          else if (ev.key === "ArrowRight" || ev.key === "ArrowDown") { ev.preventDefault(); state.picked[planInfo.id] = (i + 1) % plan.options.length; renderThread(); }
          else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") { ev.preventDefault(); state.picked[planInfo.id] = (i + plan.options.length - 1) % plan.options.length; renderThread(); }
        });
        options.push(opt); group.appendChild(opt);
      });
      card.appendChild(group);
      if (dead || planInfo.status !== "pending") {
        return add(card, el("p", "pe-con-plan-foot", "This plan has expired, so it can no longer be acted on. Ask the agent for a fresh one."));
      }
      const letter = String.fromCharCode(65 + state.picked[planInfo.id]);
      const confirm = el("button", "pe-btn go", "Confirm option " + letter); confirm.type = "button";
      confirm.addEventListener("click", () => openConfirm(planInfo, state.picked[planInfo.id]));
      const modify = el("button", "pe-btn", "Modify"); modify.type = "button";
      modify.addEventListener("click", () => {
        const o = plan.options[state.picked[planInfo.id]];
        refs.text.value = "About option " + String.fromCharCode(65 + state.picked[planInfo.id]) + " (" + o.name + "): ";
        autosize(); refs.text.focus();
      });
      const scrap = el("button", "pe-btn ghost", state.scrapArmed ? "Scrap this plan? Click again" : "Scrap"); scrap.type = "button";
      scrap.addEventListener("click", async () => {
        if (!state.scrapArmed) { state.scrapArmed = true; renderThread(); return; }
        state.scrapArmed = false;
        try { await post("/api/console/" + state.selected + "/plan/scrap"); await loadFeed(); } catch (e) { fail(e); }
      });
      add(card, add(el("div", "pe-plan-actions pe-con-plan-actions"), confirm, modify, scrap),
          el("p", "pe-con-plan-foot", "Nothing changes until you authorise it on the next step. Use Modify to talk it through, or Scrap to set it aside."));
      return card;
    }

    // ── a confirmed plan's run: timeline, a pause's reason, PE's verification ──
    async function runAction(name, x, ackDrift) {
      const armKey = ackDrift ? "drift" : name;
      if ((name === "rollback") && !state.armed[armKey]) { state.armed = { [armKey]: true }; state.sig.thread = ""; renderThread(); return; }
      state.armed = {};
      const fields = { execution_id: String(x.id) };
      if (ackDrift) fields.ack_drift = "1";
      try {
        const result = await post("/api/console/" + state.selected + "/run/" + name, fields);
        // A restore PE refused because files changed since the checkpoint is never a silent success: the operator is offered the explicit choice.
        state.drift = name === "rollback" && result.restore === "refused" ? { id: x.id, reason: result.reason || "Files changed since the checkpoint." } : null;
        state.firstPaint = "bottom"; state.sig.thread = ""; await loadFeed();
      } catch (e) { fail(e); }
    }
    function runButtons(x, names) {
      const wrap = el("div", "pe-tripwire-btns");
      const labels = { pause: "Pause", resume: "Resume", rollback: state.armed.rollback ? "Roll back to the checkpoint? Click again" : "Roll back to the checkpoint", takeover: "Take over" };
      const cls = { pause: "pe-btn warn sm", resume: "pe-btn", rollback: "pe-btn warn", takeover: "pe-btn accent" };
      for (const n of names) {
        if (!x.controls[n]) continue;
        const b = el("button", cls[n], labels[n]); b.type = "button";
        b.addEventListener("click", () => runAction(n, x, false));
        wrap.appendChild(b);
      }
      if (state.drift && state.drift.id === x.id && names.includes("rollback") && x.controls.rollback) {
        const note = el("div", "pe-note warn", "PE did not restore: " + state.drift.reason); note.setAttribute("role", "alert");
        const any = el("button", "pe-btn warn", state.armed.drift ? "Overwrite those changes? Click again" : "Roll back anyway (files changed since the checkpoint)"); any.type = "button";
        any.addEventListener("click", () => runAction("rollback", x, true));
        wrap.appendChild(note); wrap.appendChild(any);
      }
      return wrap;
    }
    const MARK = { done: ["done", "✓"], now: ["active", ""], paused: ["paused", "II"], bad: ["failed", "✕"], todo: ["", ""] };
    function runBlock(x) {
      const paused = x.pause !== null && x.pause !== undefined;
      const box = el("div", "pe-run" + (paused && !x.pause.by_operator ? " is-paused" : ""));
      const badge = el("span", "pe-badge " + ({ ok: "ok", warn: "warn", high: "high", crit: "crit", none: "none" }[x.status.level] || "none"), x.status.text.toUpperCase());
      const head = add(el("div", "pe-run-head"), el("span", "pe-led " + (x.status.level === "ok" ? "ok" : x.status.level === "none" ? "" : x.status.level)),
                       el("h3", "", "RUN · OPTION " + String.fromCharCode(65 + x.option) + (x.name ? " · " + x.name : "")), badge, el("div", "pe-spacer"),
                       el("span", "pe-label pe-con-hash", "grant " + x.fingerprint));
      if (x.controls.pause) { const pause = runButtons(x, ["pause"]).children[0]; if (pause) head.appendChild(pause); }
      add(box, head);
      const rail = el("div", "pe-rail");
      x.timeline.forEach((step, i) => {
        const m = MARK[step.state] || MARK.todo;
        const row = el("div", "pe-rail-item" + (step.state === "done" ? " is-done" : step.state === "todo" ? " is-pending" : step.state === "paused" ? " is-paused" : ""));
        const marker = el("span", "pe-marker " + m[0], m[1]);
        const gutter = add(el("div", "pe-rail-gutter"), marker, i < x.timeline.length - 1 ? el("span", "pe-rail-line" + (step.state === "done" ? " done" : "")) : null);
        add(row, gutter, add(el("div"), el("div", "pe-rail-text", step.label), step.meta ? el("div", "pe-rail-meta", step.meta) : null));
        rail.appendChild(row);
      });
      return add(box, rail);
    }
    function tripwireBlock(x) {
      const p = x.pause;
      if (p.by_operator) {
        const note = add(el("div", "pe-takeover"), el("span", "pe-led warn"), add(el("span"), el("b", "", "Paused by you. "), document.createTextNode("The run is frozen. Resume it, roll back to the checkpoint, or take over.")));
        const out = add(el("div", "pe-con-pause"), note);
        if (p.changes.length) {       // what changed since PE's last scan, found while freezing: shown before Resume is offered, because a resume re-baselines
          const seen = el("div", "pe-note high", "Changed since PE last looked: " + p.changes.join(" · ") + (p.more ? " · +" + p.more + " more" : "")); seen.setAttribute("role", "alert");
          out.appendChild(seen);
        }
        return add(out, runButtons(x, ["resume", "rollback", "takeover"]));
      }
      const box = el("div", "pe-tripwire"); box.setAttribute("role", "alert");
      add(box, add(el("div", "pe-tripwire-head"), el("span", "pe-led crit"), el("h3", "", "Something outside the plan changed"), el("div", "pe-spacer"), el("span", "pe-badge crit", "PE PAUSED THE RUN")),
          el("p", "", "PE froze the run before anything else happened. Nothing further has been done."));
      const facts = el("div", "pe-con-tw-facts");
      add(facts, add(el("div"), el("span", "pe-label", "ROLLBACK"), el("div", "pe-value", p.rollback_available ? "Available" + (p.checkpoint ? " · checkpoint " + p.checkpoint : "") : "Not available")),
          add(el("div"), el("span", "pe-label", "WHAT CHANGED"), el("div", "pe-value", p.changes.length ? p.changes.join(" · ") + (p.more ? " · +" + p.more + " more" : "") : "Not recorded")));
      return add(box, facts, runButtons(x, ["resume", "rollback", "takeover"]));
    }
    function verifyBlock(x) {
      const v = x.verification;
      const box = el("div", "pe-verify " + ({ ok: "ok", warn: "warn", high: "crit", crit: "crit" }[v.level] || ""));
      add(box, el("span", "pe-by-pe", "CHECKED BY PE · NOT THE AGENT'S OPINION"), el("h3", "", v.headline));
      const list = el("div", "pe-con-checks");
      for (const c of v.checks) {
        const state = { pass: "passed", fail: "failed", could_not_check: "unknown" }[c.state];
        const row = el("div", "pe-check " + state);
        add(row, el("span", "pe-check-mark", { pass: "✓", fail: "✕", could_not_check: "?" }[c.state]),
            add(el("div", "pe-con-check-body"), el("div", "pe-check-label", c.label), c.evidence ? el("div", "pe-check-ev", c.evidence) : null),
            el("span", "pe-check-meta", { pass: "PASS", fail: "FAIL", could_not_check: "COULD NOT CHECK" }[c.state]));
        list.appendChild(row);
      }
      add(box, list);
      if (v.level !== "ok" && x.finished !== undefined) {
        const keep = el("button", "pe-btn accent", "Keep working on it"); keep.type = "button";
        keep.addEventListener("click", () => { refs.text.value = "Keep working on it: "; autosize(); refs.text.focus(); });
        const actions = el("div", "pe-plan-actions");
        add(actions, runButtons(x, ["rollback", "takeover"]), keep);
        box.appendChild(actions);
      }
      return box;
    }
    function executionBlocks(x) {
      const out = [runBlock(x)];
      if (x.pause) out.push(tripwireBlock(x));
      if (x.verification) out.push(verifyBlock(x));
      // A run that needs the operator but has neither a pause nor a verification to hang the buttons on (after a take-over, a restart, a refused
      // rollback) still gets its controls: the thread must never say "you can roll back" with nothing to click.
      if (!x.pause && !x.verification && (x.controls.resume || x.controls.rollback || x.controls.takeover)) {
        out.push(add(el("div", "pe-con-pause"), runButtons(x, ["resume", "rollback", "takeover"])));
      }
      return out;
    }

    // ── the confirm sheet: what exactly is being authorised, bound to a fingerprint ──
    async function openConfirm(planInfo, index, noBackup) {
      if (state.preparing || state.sheetBusy) return;                    // a double click prepares once
      state.preparing = true; state.error = null; state.sheetError = null;
      const fields = { plan_id: String(planInfo.id), option: String(index) };
      if (noBackup) fields.no_backup = "1";
      try {
        state.sheet = await post("/api/console/" + state.selected + "/confirm/prepare", fields);
        state.sheet.index = index; renderSheet();
      } catch (e) {
        if (e.code === "no_backup") { state.sheet = { ask: "no_backup", planInfo, index, message: e.message }; renderSheet(); }
        else fail(e);
      } finally { state.preparing = false; }
    }
    async function closeSheet(withdraw) {
      if (state.sheetBusy) return;                                       // authorising: Back, Escape and the scrim wait until it has answered
      const v = state.sheet; state.sheet = null; state.sheetError = null; renderSheet();
      if (withdraw && v && v.execution_id) { try { await post("/api/console/" + state.selected + "/confirm/withdraw", { execution_id: String(v.execution_id) }); } catch (_e) { /* it may already be gone */ } }
    }
    async function authorise() {
      const v = state.sheet; if (!v || state.sheetBusy) return;
      state.sheetBusy = true; state.sheetError = null; renderSheet();
      try {
        await post("/api/console/" + state.selected + "/confirm/authorise", { execution_id: String(v.execution_id), grant_hash: v.grant_hash });
        state.sheet = null; state.sheetBusy = false; renderSheet(); state.firstPaint = "bottom"; await loadFeed(); await refreshList();
      } catch (e) { state.sheetBusy = false; state.sheetError = e.message; renderSheet(); }
    }
    function renderAsk(v) {
      const scrim = el("div", "pe-sheet-scrim"); scrim.addEventListener("click", () => closeSheet(false));
      const dialog = el("div", "pe-sheet center"); dialog.setAttribute("role", "dialog"); dialog.setAttribute("aria-modal", "true"); dialog.setAttribute("aria-label", "No recent backup");
      const go = el("button", "pe-btn warn", "Prepare it anyway (needs elevated approval)"); go.type = "button";
      go.addEventListener("click", () => { state.sheet = null; renderSheet(); openConfirm(v.planInfo, v.index, true); });
      const back = el("button", "pe-btn", "Back to the plan"); back.type = "button"; back.addEventListener("click", () => closeSheet(false));
      add(dialog, el("div", "pe-kicker", "NO RECENT BACKUP"), el("h2", "pe-sheet-h", "PE will not run this by default"), el("p", "", v.message), add(el("div", "pe-sheet-btns"), go, back));
      add(refs.sheet, scrim, dialog);
      back.focus();
      dialog.addEventListener("keydown", ev => {
        if (ev.key === "Escape") { ev.preventDefault(); closeSheet(false); }
        else if (ev.key === "Tab") { const ring = [go, back]; const i = ring.indexOf(document.activeElement); ev.preventDefault(); ring[(i + (ev.shiftKey ? ring.length - 1 : 1)) % ring.length].focus(); }
      });
    }
    function renderSheet() {
      refs.sheet.replaceChildren();
      const v = state.sheet;
      if (!v) return;
      if (v.ask) { renderAsk(v); return; }
      const scrim = el("div", "pe-sheet-scrim"); scrim.addEventListener("click", () => closeSheet(true));
      const dialog = el("div", "pe-sheet center"); dialog.setAttribute("role", "dialog"); dialog.setAttribute("aria-modal", "true"); dialog.setAttribute("aria-label", "Confirm");
      const row = (label, value, warn) => add(el("div", "pe-auth-row" + (warn ? " warn" : "")), el("span", "pe-label", label), el("span", "", value));
      const scope = [...v.scope.stacks.map(s => s + " (stack)"), ...v.scope.services].join(" · ") || "nothing";
      const wont = (v.protected.enforced.length ? v.protected.enforced.join(" · ") + " · " : "") + "protected resources stay protected (" + v.protected.count + ")" +
        (v.protected.advisory.length ? " · " + v.protected.advisory.join(" · ") : "");
      const rows = el("div", "pe-auth");
      add(rows, row("SCOPE", scope), row("BACKUP", (v.backup.covered ? "" : "Not covered. ") + v.backup.text, !v.backup.covered), row("WILL NOT TOUCH", wont),
          row("LIMITS", v.limits.minutes + " min · " + v.limits.steps + " steps. PE pauses if anything outside this scope changes."),
          row("PE WILL CHECK", v.checks.join(" · ")), row("ROLLBACK", "PE takes a checkpoint just before it starts; you can roll back to it from here."));
      const who = add(el("div", "pe-attrib"), el("span", "pe-who", v.operator.slice(0, 2).toUpperCase()),
                      add(el("div"), el("div", "pe-attrib-name", "Logged as " + v.operator), add(el("div", "pe-attrib-meta"), document.createTextNode("operator · you approve "), el("span", "pe-fp", v.fingerprint))));
      const go = el("button", "pe-btn go", "Authorise: " + v.option); go.type = "button"; go.disabled = state.sheetBusy;
      go.addEventListener("click", authorise);
      const back = el("button", "pe-btn", "Back to the plan"); back.type = "button"; back.disabled = state.sheetBusy; back.addEventListener("click", () => closeSheet(true));
      add(dialog, el("div", "pe-kicker", "CONFIRM · STEP 2 OF 2"), el("h2", "pe-sheet-h", "Authorise: " + v.option),
          ...v.warnings.map(w => { const n = el("div", "pe-note warn", w); n.setAttribute("role", "status"); return n; }), rows, who);
      if (v.needs_elevation) add(dialog, el("div", "pe-note high", "This needs an elevated dashboard session. Elevate with your passphrase, then authorise."));
      if (state.sheetError) { const err = el("div", "pe-note high", state.sheetError); err.setAttribute("role", "alert"); dialog.appendChild(err); }
      add(dialog, add(el("div", "pe-sheet-btns"), go, back));
      add(refs.sheet, scrim, dialog);
      go.focus();
      dialog.addEventListener("keydown", ev => {
        if (ev.key === "Escape") { ev.preventDefault(); closeSheet(true); }
        else if (ev.key === "Tab") { const ring = [go, back]; const i = ring.indexOf(document.activeElement); ev.preventDefault(); ring[(i + (ev.shiftKey ? ring.length - 1 : 1)) % ring.length].focus(); }
      });
    }

    function renderThread() {
      const f0 = state.feed;
      const sig = JSON.stringify([state.newMode, !!f0, state.messages.map(m => m.seq), state.lastEvent, f0 && f0.running, f0 && f0.plan && [f0.plan.id, f0.plan.status, f0.plan.expired],
                                  f0 && f0.execution, state.picked, state.scrapArmed, state.armed, state.drift, state.error && !f0]);
      if (sig === state.sig.thread && state.firstPaint !== "load") return;     // unchanged: keep the nodes, the selection and the focus
      state.sig.thread = sig;
      const keep = refs.scroll.scrollHeight - refs.scroll.scrollTop - refs.scroll.clientHeight < 90;
      refs.thread.replaceChildren();
      if (state.newMode) { renderInvite(); return; }
      if (!state.feed) {
        const wait = el("div", "pe-note", state.error ? "Could not load this conversation. " + state.error : "Loading the conversation…");
        wait.setAttribute("role", state.error ? "alert" : "status");
        refs.thread.appendChild(wait);
        return;
      }
      const f = state.feed, planInfo = f.plan;
      const shown = new Set();
      let planShown = false;
      for (const m of state.messages) {
        if (m.role === "operator") {
          add(refs.thread, add(el("div", "pe-opmsg"), el("span", "pe-opmsg-by", (m.by || "you").toUpperCase()), el("p", "pe-con-pre", m.text)));
        } else if (m.role === "agent") {
          if (m.turn && !shown.has(m.turn)) { shown.add(m.turn); const tb = trailBlock(m.turn, false); if (tb) refs.thread.appendChild(tb); }
          const block = el("div", "pe-agent");
          add(block, add(el("div", "pe-agent-by"), el("span", "pe-led ok"), document.createTextNode("Planet Express")));
          prose(block, m.text); refs.thread.appendChild(block);
          if (planInfo && planInfo.turn === m.turn && !planShown) { planShown = true; refs.thread.appendChild(planCard(planInfo)); }
        } else {
          const note = el("div", "pe-note pe-con-sys"); note.setAttribute("role", "status");
          add(note, el("span", "g", "i"), el("span", "", m.text)); refs.thread.appendChild(note);
        }
      }
      if (planInfo && !planShown) refs.thread.appendChild(planCard(planInfo));
      if (f.execution) for (const block of executionBlocks(f.execution)) refs.thread.appendChild(block);
      if (f.running) {
        const turn = Math.max(f.conversation.turns, 1);
        if (!shown.has(turn)) { const tb = trailBlock(turn, true); if (tb) refs.thread.appendChild(tb); }
        const t = trailFor(state.events, turn);
        const live = el("div", "pe-agent");
        add(live, add(el("div", "pe-agent-by"), el("span", "pe-led warn"), document.createTextNode("Planet Express")));
        if (t.stream) prose(live, t.stream);
        const status = add(el("div", "pe-stream"), el("span", "pe-spinner"), el("span", "", "Working…"), el("span", "pe-caret", "▍")); status.setAttribute("role", "status");
        refs.thread.appendChild(add(live, status));
      }
      if (state.firstPaint === "load") {
        // Opening a conversation: start at the last answer (the plan below it is read in that context), not at the very bottom.
        const agents = Array.from(refs.thread.children).filter(c => String(c.className).startsWith("pe-agent"));
        const anchor = agents[agents.length - 1];
        const top = anchor && anchor.getBoundingClientRect
          ? anchor.getBoundingClientRect().top - refs.scroll.getBoundingClientRect().top + refs.scroll.scrollTop : 0;
        refs.scroll.scrollTop = top > 0 ? Math.max(top - 16, 0) : refs.scroll.scrollHeight;
      } else if (keep || state.firstPaint) refs.scroll.scrollTop = refs.scroll.scrollHeight;
      state.firstPaint = false;
    }

    function renderInvite() {
      const box = el("div", "pe-con-empty");
      add(box, el("h2", "", "What do you want to look into?"),
          el("p", "", "Describe an issue in your own words. The agent investigates with read-only tools, tells you what it found and how sure it is, and can propose options you confirm, change or scrap. It never changes anything on its own."));
      const prompts = el("div", "pe-prompts");
      for (const ex of EXAMPLES) {
        const b = el("button", "pe-prompt", ex); b.type = "button";
        b.addEventListener("click", () => { refs.text.value = ex; autosize(); refs.text.focus(); });
        prompts.appendChild(b);
      }
      add(refs.thread, add(box, prompts));
    }

    // ── composer ──
    function autosize() { refs.text.style.height = "auto"; refs.text.style.height = Math.min(refs.text.scrollHeight || 0, 200) + "px"; }
    function renderComposer() {
      const c = state.newMode ? { mode: state.status.proposal_only ? "off" : "idle", reason: state.status.proposal_only_reason } : (state.feed ? state.feed.composer : { mode: "idle" });
      const off = c.mode === "off";
      refs.composer.className = "pe-composer" + (off ? " is-off" : "");
      refs.text.disabled = off || state.sending;
      refs.send.disabled = off || state.sending;
      refs.send.textContent = state.newMode ? "Start" : "Send";
      refs.stop.hidden = !(state.feed && state.feed.stoppable && !state.newMode && c.mode !== "off");
      refs.note.textContent = state.error || c.reason || (state.newMode ? "Nothing runs until you press Start." : "");
      refs.note.setAttribute("role", state.error ? "alert" : "status");
    }

    async function send() {
      const text = refs.text.value.trim();
      if (!text || state.sending) return;
      state.sending = true; state.error = null; renderComposer();
      try {
        if (state.newMode) {
          const made = await post("/api/console/new", { text });
          refs.text.value = ""; autosize();
          await refreshList(); select(made.id, true);
        } else {
          await post("/api/console/" + state.selected + "/message", { text });
          refs.text.value = ""; autosize();
          state.firstPaint = true; await loadFeed();
        }
      } catch (e) { fail(e); } finally { state.sending = false; renderComposer(); schedule(true); }
    }

    // ── data ──
    function fail(e) { state.error = e && e.message ? e.message : "Something went wrong."; renderComposer(); }
    async function refreshList() {
      const data = await request("/api/console/list");
      state.list = data.conversations; state.status = data.status; renderRail();
    }
    async function loadFeed(reset) {
      // One fetch at a time: a poll and an action finishing together would otherwise both append the same events. A request that arrives
      // while one is in flight is answered by a second fetch right after it (the first may predate the action).
      if (state.inflight) {
        if (!reset) { state.again = true; return state.inflight; }
        await state.inflight.catch(() => {});
      }
      state.inflight = (async () => {
        try { await loadFeedNow(reset); while (state.again) { state.again = false; await loadFeedNow(false); } }
        finally { state.inflight = null; }
      })();
      return state.inflight;
    }
    async function loadFeedNow(reset) {
      if (!state.selected) return;
      const id = state.selected;
      if (reset) { state.messages = []; state.events = []; state.lastMessage = 0; state.lastEvent = 0; state.feed = null; state.firstPaint = "load"; }
      const feed = await request("/api/console/" + id + "/feed?after_message=" + state.lastMessage + "&after_event=" + state.lastEvent);
      if (id !== state.selected) return;                        // the operator moved on while this was in flight
      const known = new Set(state.messages.map(m => m.seq));
      for (const m of feed.messages) if (!known.has(m.seq)) state.messages.push(m);
      const seen = new Set(state.events.map(e => e.seq));
      for (const e of feed.events) if (!seen.has(e.seq)) state.events.push(e);
      state.lastMessage = feed.last_message; state.lastEvent = feed.last_event;
      state.feed = feed; state.status = feed.status; state.error = null; state.scrapArmed = state.scrapArmed && !!feed.plan;
      state.archiveArmed = state.archiveArmed && feed.conversation.state === "open";
      renderTop(); renderThread(); renderComposer(); renderRail();
    }
    function leaveConversation() {
      // Per-conversation UI state never travels with the operator: an armed rollback, a drift offer and an open sheet belong to the one they were for.
      if (state.sheetBusy) return false;
      if (state.sheet) closeSheet(true);
      state.armed = {}; state.drift = null;
      return true;
    }
    function select(id, push) {
      if (!leaveConversation()) return;
      state.selected = id; state.newMode = false; state.picked = {}; state.scrapArmed = false; state.archiveArmed = false; state.feed = null;
      state.sig = { rail: "", top: "", thread: "" };
      con.classList.remove("is-list"); con.classList.add("is-thread");
      if (push && window.history) window.history.pushState({ id }, "", "/console/" + id);
      renderRail(); renderThread(); renderComposer();
      loadFeed(true).catch(fail);
    }
    function startNew(push) {
      if (!leaveConversation()) return;
      state.selected = null; state.newMode = true; state.feed = null; state.sig = { rail: "", top: "", thread: "" };
      con.classList.remove("is-list"); con.classList.add("is-thread");
      if (push && window.history) window.history.pushState({}, "", "/console");
      renderRail(); renderTop(); renderThread(); renderComposer(); refs.text.focus();
    }
    function showList() {
      if (!leaveConversation()) return;
      con.classList.remove("is-thread"); con.classList.add("is-list");
      if (window.history) window.history.pushState({}, "", "/console");
      state.selected = null; state.feed = null; state.newMode = true; renderRail(); renderTop(); renderThread(); renderComposer();
    }
    function schedule(now) {
      window.clearTimeout(state.timer);
      const running = state.feed && state.feed.running;
      const delay = now ? 400 : (document.hidden ? 15000 : running ? 1500 : 5000);
      state.timer = window.setTimeout(tick, delay);
    }
    async function tick() {
      try { await refreshList(); if (state.selected) await loadFeed(false); } catch (e) { fail(e); }
      schedule(false);
    }

    refs.send.addEventListener("click", send);
    refs.stop.addEventListener("click", async () => {
      try { await post("/api/console/" + state.selected + "/interrupt"); await loadFeed(); } catch (e) { fail(e); }
    });
    refs.text.addEventListener("keydown", ev => { if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); send(); } });
    refs.text.addEventListener("input", autosize);
    refs.newBtn.addEventListener("click", () => startNew(true));
    window.addEventListener("popstate", () => {
      const m = /\/console\/([0-9a-f]{16})/.exec(window.location.pathname);
      if (m) select(m[1], false); else { state.selected = null; state.newMode = true; state.feed = null; con.classList.add("is-list"); con.classList.remove("is-thread"); renderRail(); renderTop(); renderThread(); renderComposer(); }
    });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) schedule(true); });

    state.status = bootData.status;
    renderRail(); renderThread(); renderComposer();
    refreshList().catch(fail).finally(() => {
      if (bootData.selected) select(bootData.selected, false);
      schedule(false);
    });
    return { state, select, startNew, send, renderThread, renderRail, loadFeed };
  }

  if (typeof document !== "undefined" && document.getElementById && document.getElementById("pe-con")) boot(document, window);
})(typeof window !== "undefined" ? window : globalThis);
