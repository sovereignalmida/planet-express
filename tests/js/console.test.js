// Node tests for static/console.js: the pure helpers, and the whole page drawn against a tiny fake DOM and fake fetch. The point of the DOM
// half is the security property: whatever the agent or the server sends is drawn as text, never as an element.
const assert = require("assert");
const C = require("../../static/console.js");

let failures = 0;
function test(name, fn) { try { fn(); console.log("ok   " + name); } catch (e) { failures++; console.log("FAIL " + name + "\n" + (e.stack || e)); } }
async function atest(name, fn) { try { await fn(); console.log("ok   " + name); } catch (e) { failures++; console.log("FAIL " + name + "\n" + (e.stack || e)); } }

// ── a tiny DOM ──
class FakeNode {
  constructor(tag, doc) { this.tag = tag; this.doc = doc; this.children = []; this.attrs = {}; this.listeners = {}; this.style = {}; this.className = ""; this._text = ""; this.hidden = false; this.value = ""; this.disabled = false; this.dataset = {}; this.open = false; this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0; this.id = ""; }
  get classList() { const n = this; return { add: c => { if (!n.className.split(" ").includes(c)) n.className = (n.className + " " + c).trim(); }, remove: c => { n.className = n.className.split(" ").filter(x => x && x !== c).join(" "); } }; }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...c) { this.children = c; this._text = ""; }
  get lastChild() { return this.children[this.children.length - 1]; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  fire(ev, arg) { for (const fn of this.listeners[ev] || []) fn(arg || {}); }
  focus() {}
  set innerHTML(_v) { throw new Error("innerHTML must never be used: agent text would become markup"); }
  set outerHTML(_v) { throw new Error("outerHTML must never be used"); }
  insertAdjacentHTML() { throw new Error("insertAdjacentHTML must never be used"); }
}
class FakeText extends FakeNode { constructor(text, doc) { super("#text", doc); this._text = String(text); } }
function makeDoc(json) {
  const doc = { _byId: {}, listeners: {}, hidden: false };
  doc.createElement = tag => new FakeNode(tag, doc);
  doc.createTextNode = t => new FakeText(t, doc);
  doc.getElementById = id => doc._byId[id] || null;
  doc.querySelector = () => ({ content: "csrf-xyz" });
  doc.addEventListener = (ev, fn) => { (doc.listeners[ev] = doc.listeners[ev] || []).push(fn); };
  for (const id of ["pe-con", "con-sheet", "con-list", "con-top", "con-scroll", "con-thread", "con-composer", "con-ctx", "con-text", "con-send", "con-stop", "con-note", "con-new"]) {
    const n = new FakeNode("div", doc); n.id = id; doc._byId[id] = n;
  }
  const boot = new FakeNode("script", doc); boot._text = JSON.stringify(json); doc._byId["con-boot"] = boot;
  return doc;
}
function walk(node, out = []) { out.push(node); for (const c of node.children) walk(c, out); return out; }

// ── pure ──
test("prose: paragraphs, lists, code, inline code and bold, and markup stays text", () => {
  const blocks = C.proseBlocks("First line\ncontinues here.\n\n- one\n- two with `code`\n\n1. step\n2. **bold** step\n\n```\nls -la\n```\n<script>alert(1)</script>");
  assert.deepStrictEqual(blocks.map(b => b.t), ["p", "ul", "ol", "pre", "p"]);
  assert.strictEqual(blocks[0].c[0].v, "First line continues here.");
  assert.deepStrictEqual(blocks[1].items[1].map(i => i.t), ["text", "code"]);
  assert.strictEqual(blocks[3].v, "ls -la");
  assert.strictEqual(blocks[4].c[0].t, "text");                       // a tag is a text token: the renderer can only make a text node of it
  assert.strictEqual(blocks[4].c[0].v, "<script>alert(1)</script>");
});
test("prose: an unterminated code fence is closed, empty input is empty", () => {
  assert.deepStrictEqual(C.proseBlocks("```\nopen").map(b => b.t), ["pre"]);
  assert.deepStrictEqual(C.proseBlocks(""), []);
  assert.deepStrictEqual(C.proseBlocks(null), []);
});
test("trail: calls pair with their results, failures and refusals are counted, the summary is plain words", () => {
  const ev = [
    { turn: 1, type: "call", kind: "read", name: "Read", detail: "/a" }, { turn: 1, type: "result", name: "Read", excerpt: "x", failed: false },
    { turn: 1, type: "call", kind: "ran", name: "Bash", detail: "ls" }, { turn: 1, type: "result", name: "Bash", excerpt: "err", failed: true },
    { turn: 1, type: "call", kind: "changed", name: "Write", detail: "plan.json" },
    { turn: 1, type: "denied", name: "Bash", detail: "rm -rf /", why: "not allowed" },
    { turn: 1, type: "delta", text: "partial " }, { turn: 1, type: "delta", text: "text" }, { turn: 1, type: "omitted", count: 4 },
    { turn: 2, type: "call", kind: "read", name: "Grep", detail: "x" }];
  const t = C.trailFor(ev, 1);
  assert.strictEqual(t.steps.length, 4);
  assert.strictEqual(t.steps[0].out, "x");
  assert.strictEqual(t.steps[1].kind, "failed");
  assert.strictEqual(t.stream, "partial text");
  assert.strictEqual(t.omitted, 4);
  assert.strictEqual(t.summary, "Looked at 1 thing, wrote 1 file, 1 refused, 1 failed");
  assert.strictEqual(C.trailFor(ev, 2).steps.length, 1);
  assert.strictEqual(C.trailFor(ev, 3).summary, "No activity recorded");
});
test("a result with no matching call still appears", () => {
  assert.strictEqual(C.trailFor([{ turn: 1, type: "result", name: "Read", excerpt: "orphan", failed: false }], 1).steps[0].out, "orphan");
});
test("checks are phrased in words, unknown kinds fall back", () => {
  assert.strictEqual(C.checkPhrase({ check: "container_state", params: { container: "gluetun" } }), "gluetun is running and stable");
  assert.strictEqual(C.checkPhrase({ check: "http", params: { url: "http://x/" } }), "http://x/ answers");
  assert.strictEqual(C.checkPhrase({ check: "new_kind", params: { a: 1 } }), "new_kind (a=1)");
  assert.strictEqual(C.checkPhrase(null), "a check");
});
test("the rail groups by who needs what and drops empty groups", () => {
  const rows = [{ id: "a", status: { group: "Recent" } }, { id: "b", status: { group: "Needs you" } }];
  assert.deepStrictEqual(C.groupRail(rows).map(g => g.name), ["Needs you", "Recent"]);
});
test("budget labels", () => {
  assert.strictEqual(C.budgetLabel({ state: "ok", used_pct: 24 }), "24% used");
  assert.strictEqual(C.budgetLabel({ state: "stop", used_pct: 99 }), "used up");
});

// ── the page ──
const EVIL = '<img src=x onerror=alert(1)><script>alert(2)</script>';
const LIST = { conversations: [{ id: "0123456789abcdef", title: EVIL, origin: "operator", state: "open", age: "2m", turns: 1, status: { text: "Needs your call", level: "warn", group: "Needs you" } }],
               status: { enabled: true, proposal_only: false } };
const PLAN = { known: ["gateway healthy " + EVIL], unsure: ["start order"], options: [
  { name: "Recreate", recommended: true, summary: "Recreate " + EVIL, changes: ["4 services"], will_not_touch: ["the gateway"], risk: { level: "low", text: "a minute" },
    rolls_back_to: "the checkpoint", effort: "2 min", checks: [{ id: "u", check: "container_state", params: { container: "gluetun" } }] },
  { name: "Restart", recommended: false, summary: "s", changes: ["g"], will_not_touch: ["d"], risk: { level: "high", text: "bad" }, rolls_back_to: "r", effort: "1 min", checks: [] }] };
const VIEW = { execution_id: 4, mission_id: "M-9", grant_hash: "a".repeat(64), fingerprint: "aaaaaaaa", operator: "chris", option: "Recreate " + EVIL, summary: "s",
  scope: { stacks: ["media"], services: ["media/qbittorrent"] }, warnings: ["BACKUP NOTE " + EVIL], backup: { covered: true, text: "A recent verified backup covers this scope." },
  protected: { count: 7, enforced: ["service:media/gluetun"], advisory: ["the gateway"] }, limits: { minutes: 15, steps: 200 }, checks: ["qbittorrent is running and stable"],
  on_verify_fail: "needs_operator", needs_elevation: false, phases: ["grant"] };
const EXEC = { id: 4, option: 0, name: "Recreate", fingerprint: "aaaaaaaa", status: { text: "Working", level: "ok" }, finished: false,
  timeline: [{ label: "Authorised", state: "done", meta: "chris" }, { label: "Checkpoint taken", state: "done", meta: "so it can be rolled back" },
             { label: "Carrying out the plan", state: "now", meta: "" }, { label: "PE checks it worked", state: "todo", meta: "PE's own checks" }],
  pause: null, verification: null, controls: { pause: true, resume: false, rollback: false, takeover: true } };
const FEED = { conversation: { id: "0123456789abcdef", title: EVIL, origin: "operator", state: "open", turns: 1, status: { text: "Needs your call", level: "warn", group: "Needs you" },
                               verdict: { h: "Waiting for your choice between 2 options", p: "Recommended: Recreate. Nothing has been changed." } },
  budget: { used_pct: 24, tokens_used: 24000, token_budget: 100000, tokens_left: 76000, state: "ok" }, running: false, composer: { mode: "idle", reason: null },
  messages: [{ seq: 1, role: "operator", turn: null, at: 1, text: "why? " + EVIL, by: "chris" }, { seq: 2, role: "agent", turn: 1, at: 2, text: "It is **the gateway**.\n\n- " + EVIL, by: null },
             { seq: 3, role: "system", turn: 1, at: 3, text: "note " + EVIL, by: null }],
  last_message: 3, events: [{ seq: 5, at: 1, turn: 1, type: "call", kind: "ran", name: "Bash", detail: EVIL }, { seq: 6, at: 1, turn: 1, type: "result", name: "Bash", excerpt: EVIL, failed: false }], last_event: 6,
  turn_starts: [4], context: { evidence: { incident: EVIL, turns: 1, budget_used_pct: 24 }, scope: { kind: "read_only", stacks: [EVIL], services: [], paths: [], protected: 2 },
    limits: { token_budget: 100000, minutes: 30, steps: 200 }, record: { events: 9, checkpoint: false, runs: 0 } }, plan: { id: 1, turn: 1, status: "pending", created_at: 1, hash: "abcd1234", expired: false, plan: PLAN }, status: { enabled: true, proposal_only: false } };

(async () => {
  const requests = [];
  const doc = makeDoc({ status: { enabled: true, proposal_only: false }, selected: "0123456789abcdef" });
  const win = { history: { pushState() {} }, location: { assign() {}, pathname: "/console" }, URLSearchParams, clearTimeout() {}, setTimeout: () => 0, addEventListener() {},
    fetch: async (url, opts) => { requests.push([url, opts && opts.method || "GET", opts && opts.body && String(opts.body)]);
      const forced = win.__override && win.__override(url); if (forced) return forced;
      const body = url.includes("/feed") ? FEED : url.includes("/list") ? LIST : url.endsWith("/confirm/prepare") ? VIEW : { id: "0123456789abcdef", scrapped: true };
      return { ok: true, status: 200, json: async () => body }; } };
  const page = C.boot(doc, win);
  await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
  const all = walk(doc.getElementById("pe-con"));
  const thread = walk(doc.getElementById("con-thread")), rail = walk(doc.getElementById("con-list")), top = walk(doc.getElementById("con-top"));

  await atest("hostile text is drawn as text and never creates an element", () => {
    const tags = new Set([...thread, ...rail, ...top].map(n => n.tag));
    for (const bad of ["img", "script", "iframe", "a"]) if (bad !== "a") assert.ok(!tags.has(bad), bad + " must not exist in the drawn page");
    assert.ok(thread.some(n => n.tag === "#text" && n.textContent.includes("<img src=x onerror=alert(1)>")), "the hostile string is present as text");
    assert.ok(rail.some(n => n.textContent.includes("<img src=x")), "the rail title is text too");
    assert.ok(![...thread, ...rail, ...top].some(n => n.attrs.onerror || n.onerror));
  });
  await atest("the thread has the operator message, the agent prose, a trail, the plan and the system note in order", () => {
    const classes = doc.getElementById("con-thread").children.map(c => c.className.split(" ")[0]);
    assert.deepStrictEqual(classes, ["pe-opmsg", "pe-trail", "pe-agent", "pe-plan", "pe-note"]);
    const plan = doc.getElementById("con-thread").children[3];
    assert.ok(plan.textContent.includes("RECOMMENDED") && plan.textContent.includes("gluetun is running and stable") && plan.textContent.includes("Nothing changes until you authorise"));
    const confirm = walk(plan).find(n => n.tag === "button" && n.textContent === "Confirm option A");
    assert.ok(confirm && confirm.className.includes("go"), "the primary action names the option");
  });
  await atest("the verdict strip is a polite live region and says what PE wrote", () => {
    const verdict = top.find(n => n.className.startsWith("pe-verdict"));
    assert.strictEqual(verdict.attrs["aria-live"], "polite");
    assert.ok(verdict.textContent.includes("Waiting for your choice between 2 options"));
  });
  await atest("the rail row says what needs the operator, in plain words", () => {
    const row = rail.find(n => n.tag === "button");
    assert.ok(row.textContent.includes("Needs your call") && row.className.includes("pe-con-row"));
  });
  await atest("sending posts the text with the CSRF token as form data to the right URL", async () => {
    doc.getElementById("con-text").value = "and the logs?";
    await page.send();
    const sent = requests.find(r => r[1] === "POST");
    assert.strictEqual(sent[0], "/api/console/0123456789abcdef/message");
    assert.ok(sent[2].includes("csrf_token=csrf-xyz") && sent[2].includes("text=and+the+logs%3F"));
  });
  await atest("scrapping a plan takes two clicks", async () => {
    const find = () => walk(doc.getElementById("con-thread")).find(n => n.tag === "button" && n.textContent.startsWith("Scrap"));
    find().fire("click"); await new Promise(r => setImmediate(r));
    assert.ok(find().textContent.includes("Click again"));
    assert.ok(!requests.some(r => r[0].endsWith("/plan/scrap")), "the first click only arms it");
    find().fire("click"); await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
    assert.ok(requests.some(r => r[0].endsWith("/plan/scrap") && r[1] === "POST"));
  });
  await atest("the invite offers example prompts and a new conversation posts to /new", async () => {
    page.startNew(false);
    const prompts = walk(doc.getElementById("con-thread")).filter(n => n.className === "pe-prompt");
    assert.strictEqual(prompts.length, 3);
    prompts[0].fire("click");
    assert.ok(doc.getElementById("con-text").value.includes("download apps"));
    await page.send();
    assert.ok(requests.some(r => r[0] === "/api/console/new" && r[1] === "POST" && r[2].includes("csrf_token=csrf-xyz")));
  });
  await atest("overlapping feed loads never duplicate trail events and settle with one record of each", async () => {
    page.select("0123456789abcdef", false);
    await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
    await Promise.all([page.loadFeed(), page.loadFeed(), page.loadFeed()]);
    const seqs = page.state.events.map(e => e.seq);
    assert.strictEqual(new Set(seqs).size, seqs.length, "no event twice");
    assert.strictEqual(seqs.length, FEED.events.length);
    assert.strictEqual(page.state.messages.length, FEED.messages.length);
  });
  await atest("the context panel shows PE's records as text, and switches tabs", async () => {
    const ctx = doc.getElementById("con-ctx");
    assert.strictEqual(ctx.hidden, false);
    assert.ok(ctx.textContent.includes("Incident") && ctx.textContent.includes(EVIL));
    const tabs = ctx.children[0].children;
    assert.deepStrictEqual(tabs.map(t => t.textContent), ["Evidence", "Scope", "Limits", "Record"]);
    tabs[1].fire("click");
    assert.ok(ctx.textContent.includes("nothing: read-only") && ctx.textContent.includes(EVIL) && ctx.textContent.includes("Protected"));
    ctx.children[0].children[3].fire("click");
    assert.ok(ctx.textContent.includes("Events") && ctx.textContent.includes("9"));
  });
  await atest("a poll that finds nothing new leaves the drawn nodes alone (selection and focus survive)", async () => {
    const before = doc.getElementById("con-thread").children.slice();
    const railBefore = doc.getElementById("con-list").children.slice();
    await page.loadFeed();
    page.renderThread(); page.renderRail();
    assert.ok(before.every((n, i) => doc.getElementById("con-thread").children[i] === n), "thread nodes are the same objects");
    assert.ok(railBefore.every((n, i) => doc.getElementById("con-list").children[i] === n), "rail nodes are the same objects");
  });
  await atest("on a narrow screen the context sheet is a dialog, Escape closes it, and it does not follow the operator to another conversation", async () => {
    const ctx = doc.getElementById("con-ctx"), con = doc.getElementById("pe-con");
    const find = (n, pred) => pred(n) ? n : n.children.map(c => find(c, pred)).find(Boolean);
    const btn = find(doc.getElementById("con-top"), n => /pe-con-ctxbtn/.test(n.className));
    btn.fire("click");
    assert.ok(/ctx-open/.test(con.className) && ctx.attrs.role === "dialog" && ctx.attrs["aria-modal"] === "true");
    assert.strictEqual(doc.getElementById("con-scroll").inert, true, "the thread is inert behind the sheet");
    ctx.fire("keydown", { key: "Escape", preventDefault() {} });
    assert.ok(!/ctx-open/.test(con.className) && ctx.attrs.role === "complementary");
    assert.strictEqual(doc.getElementById("con-scroll").inert, false);
    btn.fire("click");
    assert.ok(/ctx-open/.test(con.className));
    page.select("0123456789abcdef", false);
    assert.strictEqual(page.state.ctxOpen, false);
  });
  await atest("opening a conversation shows a loading note, never the new-conversation invite, until its feed arrives", async () => {
    const slow = makeDoc({ status: { enabled: true, proposal_only: false }, selected: null });
    let release;
    const gate = new Promise(r => { release = r; });
    const win2 = { ...win, fetch: async (url) => { if (url.includes("/feed")) await gate; return { ok: true, status: 200, json: async () => (url.includes("/feed") ? FEED : LIST) }; } };
    const p2 = C.boot(slow, win2);
    p2.select("0123456789abcdef", false);
    const texts = walk(slow.getElementById("con-thread")).map(n => n.textContent).join(" ");
    assert.ok(texts.includes("Loading the conversation"), texts);
    assert.ok(!texts.includes("What do you want to look into"), "no invite inside an existing conversation");
    release();
  });
  await atest("the title bar has an Archive button that needs a second click", async () => {
    const find = () => walk(doc.getElementById("con-top")).find(n => n.tag === "button" && n.textContent.startsWith("Archive"));
    page.select("0123456789abcdef", false);
    await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
    const before = requests.filter(r => r[0].endsWith("/archive")).length;
    find().fire("click");
    assert.ok(find().textContent.includes("Click again"));
    assert.strictEqual(requests.filter(r => r[0].endsWith("/archive")).length, before, "the first click only arms it");
    find().fire("click"); await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
    assert.ok(requests.some(r => r[0].endsWith("/archive") && r[1] === "POST"));
  });
  await atest("Stop shows only when the turn is really running, and a dead plan has no Scrap", async () => {
    const stopHidden = () => doc.getElementById("con-stop").hidden;
    page.select("0123456789abcdef", false);
    await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r));
    assert.strictEqual(stopHidden(), true);                                  // FEED has no stoppable turn
    FEED.running = true; FEED.stoppable = false; await page.loadFeed();
    assert.strictEqual(stopHidden(), true, "queued, not stoppable: no Stop");
    FEED.stoppable = true; await page.loadFeed();
    assert.strictEqual(stopHidden(), false);
    FEED.running = false; FEED.stoppable = false;
    FEED.plan = { ...FEED.plan, status: "expired", expired: true }; await page.loadFeed();
    const buttons = walk(doc.getElementById("con-thread")).filter(n => n.tag === "button").map(n => n.textContent);
    assert.ok(!buttons.some(t => t.startsWith("Scrap")) && !buttons.includes("Modify"), "no actions on an expired plan: " + buttons);
    assert.ok(walk(doc.getElementById("con-thread")).some(n => n.textContent.includes("has expired")));
  });
  await atest("confirming prepares the repair, shows the sheet with the fingerprint, and authorising posts exactly that hash", async () => {
    FEED.running = false; FEED.stoppable = false; FEED.plan = { id: 1, turn: 1, status: "pending", created_at: 1, hash: "abcd1234", expired: false, plan: PLAN };
    page.select("0123456789abcdef", false);
    for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r));
    const confirm = walk(doc.getElementById("con-thread")).find(n => n.tag === "button" && n.textContent.startsWith("Confirm option"));
    confirm.fire("click");
    for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r));
    const prep = requests.find(r => r[0].endsWith("/confirm/prepare"));
    assert.ok(prep && prep[2].includes("plan_id=1") && prep[2].includes("option=0") && prep[2].includes("csrf_token=csrf-xyz"));
    const sheet = walk(doc.getElementById("con-sheet"));
    const dialog = sheet.find(n => n.attrs.role === "dialog");
    assert.ok(dialog && dialog.attrs["aria-modal"] === "true");
    const text = dialog.textContent;
    for (const want of ["CONFIRM · STEP 2 OF 2", "aaaaaaaa", "Logged as chris", "media (stack)", "service:media/gluetun", "15 min", "qbittorrent is running and stable", "Back to the plan"]) assert.ok(text.includes(want), want);
    assert.ok(sheet.some(n => n.tag === "button" && n.textContent.startsWith("Authorise: Recreate")), "the primary button names the action");
    assert.ok(!sheet.some(n => ["img", "script"].includes(n.tag)), "hostile text in the sheet stays text");
    sheet.find(n => n.tag === "button" && n.textContent.startsWith("Authorise")).fire("click");
    for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
    const auth = requests.find(r => r[0].endsWith("/confirm/authorise"));
    assert.ok(auth && auth[2].includes("execution_id=4") && auth[2].includes("grant_hash=" + "a".repeat(64)));
    assert.strictEqual(doc.getElementById("con-sheet").children.length, 0, "the sheet closes once authorised");
  });
  await atest("Back to the plan withdraws the proposed repair and closes the sheet", async () => {
    FEED.plan = { id: 1, turn: 1, status: "pending", created_at: 1, hash: "abcd1234", expired: false, plan: PLAN }; FEED.execution = null; await page.loadFeed();
    const confirm = walk(doc.getElementById("con-thread")).find(n => n.tag === "button" && n.textContent.startsWith("Confirm option"));
    confirm.fire("click");
    for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r));
    walk(doc.getElementById("con-sheet")).find(n => n.tag === "button" && n.textContent === "Back to the plan").fire("click");
    for (let i = 0; i < 3; i++) await new Promise(r => setImmediate(r));
    assert.ok(requests.some(r => r[0].endsWith("/confirm/withdraw") && r[2].includes("execution_id=4")));
    assert.strictEqual(doc.getElementById("con-sheet").children.length, 0);
  });
  await atest("a run shows its timeline in PE's words and a Pause only while it is running", async () => {
    FEED.plan = null; FEED.execution = JSON.parse(JSON.stringify(EXEC)); await page.loadFeed();
    const thread = walk(doc.getElementById("con-thread"));
    const run = thread.find(n => n.className.startsWith("pe-run"));
    assert.ok(run && run.textContent.includes("RUN · OPTION A · Recreate") && run.textContent.includes("WORKING") && run.textContent.includes("grant aaaaaaaa"));
    assert.deepStrictEqual(walk(run).filter(n => n.className.startsWith("pe-marker")).map(n => n.className.split(" ")[1] || ""), ["done", "done", "active", ""]);
    assert.ok(thread.some(n => n.tag === "button" && n.textContent === "Pause"));
    await page.loadFeed();
  });
  await atest("a tripwire pause is an alert with what changed and the three controls; rolling back takes two clicks", async () => {
    FEED.execution = { ...JSON.parse(JSON.stringify(EXEC)), status: { text: "Paused: something unexpected changed", level: "crit" },
      pause: { by_operator: false, cause: "firewall", changes: ["host:local/path:/etc/hosts: modified"], more: 0, rollback_available: true, checkpoint: "ck-1", who: null },
      controls: { pause: false, resume: true, rollback: true, takeover: true } };
    await page.loadFeed();
    const thread = walk(doc.getElementById("con-thread"));
    const tw = thread.find(n => n.className === "pe-tripwire");
    assert.strictEqual(tw.attrs.role, "alert");
    assert.ok(tw.textContent.includes("PE PAUSED THE RUN") && tw.textContent.includes("/etc/hosts") && tw.textContent.includes("checkpoint ck-1"));
    const labels = walk(tw).filter(n => n.tag === "button").map(n => n.textContent);
    assert.deepStrictEqual(labels, ["Resume", "Roll back to the checkpoint", "Take over"]);
    const before = requests.filter(r => r[0].includes("/run/rollback")).length;
    const roll = () => walk(doc.getElementById("con-thread")).find(n => n.tag === "button" && n.textContent.startsWith("Roll back"));
    roll().fire("click");
    assert.ok(roll().textContent.includes("Click again") && requests.filter(r => r[0].includes("/run/rollback")).length === before);
    roll().fire("click"); for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r));
    assert.ok(requests.some(r => r[0].endsWith("/run/rollback") && r[2].includes("execution_id=4")));
  });
  await atest("a pause by the operator is not drawn as a tripwire alert", async () => {
    FEED.execution = { ...JSON.parse(JSON.stringify(EXEC)), status: { text: "Paused by you", level: "warn" }, controls: { pause: false, resume: true, rollback: true, takeover: true },
      pause: { by_operator: true, cause: "operator", changes: [], more: 0, rollback_available: true, checkpoint: "ck-1", who: "operator:chris" } };
    await page.loadFeed();
    const thread = walk(doc.getElementById("con-thread"));
    assert.ok(!thread.some(n => n.className === "pe-tripwire") && thread.some(n => n.textContent.includes("Paused by you.")));
  });
  await atest("an operator pause shows what changed since the last scan before Resume is offered", async () => {
    FEED.execution = { ...JSON.parse(JSON.stringify(EXEC)), status: { text: "Paused by you", level: "warn" }, controls: { pause: false, resume: true, rollback: true, takeover: true },
      pause: { by_operator: true, cause: "operator", changes: ["host:local/path:/etc/hosts: modified"], more: 2, rollback_available: true, checkpoint: "ck-1", who: "operator:chris" } };
    await page.loadFeed();
    const block = walk(doc.getElementById("con-thread")).find(n => n.className === "pe-con-pause");
    assert.ok(block.textContent.includes("Changed since PE last looked: host:local/path:/etc/hosts: modified · +2 more"));
    const order = block.children.map(c => c.className.split(" ")[0] + ":" + c.textContent.slice(0, 7));
    assert.ok(order.findIndex(t => t.includes("Changed")) > -1 && order.findIndex(t => t.includes("Changed")) < order.length - 1, order.join("|"));   // the buttons come after the warning
  });
  await atest("PE's verification is labelled as PE's, marks each check, and a could-not-check is its own state", async () => {
    FEED.execution = { ...JSON.parse(JSON.stringify(EXEC)), status: { text: "Needs you: PE isn't sure what state this is in", level: "crit" }, finished: false, pause: null,
      controls: { pause: false, resume: false, rollback: true, takeover: false },
      verification: { headline: "1 of 3 failed: qbittorrent is running and stable", level: "high", by_pe: true, checks: [
        { state: "pass", label: "sonarr is running and stable", evidence: "ok" }, { state: "could_not_check", label: "the media project has all its services", evidence: "bad params" },
        { state: "fail", label: "qbittorrent is running and stable", evidence: "restarting " + EVIL }] } };
    await page.loadFeed();
    const v = walk(doc.getElementById("con-thread")).find(n => n.className.startsWith("pe-verify"));
    assert.ok(v.className.includes("crit") && v.textContent.includes("CHECKED BY PE") && v.textContent.includes("NOT THE AGENT'S OPINION"));
    assert.deepStrictEqual(walk(v).filter(n => n.className.startsWith("pe-check ")).map(n => n.className), ["pe-check passed", "pe-check unknown", "pe-check failed"]);
    assert.deepStrictEqual(walk(v).filter(n => n.className === "pe-check-meta").map(n => n.textContent), ["PASS", "COULD NOT CHECK", "FAIL"]);
    assert.ok(walk(v).some(n => n.tag === "button" && n.textContent === "Keep working on it"));
    assert.ok(!walk(v).some(n => ["img", "script"].includes(n.tag)));
  });
  const settle = async (n = 5) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };
  const reply = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });
  const rollButtons = () => walk(doc.getElementById("con-thread")).filter(n => n.tag === "button").map(n => n.textContent);
  await atest("a run that needs the operator but has no pause or verification still offers its controls", async () => {
    FEED.execution = { ...JSON.parse(JSON.stringify(EXEC)), status: { text: "Needs you", level: "crit" }, pause: null, verification: null,
      controls: { pause: false, resume: false, rollback: true, takeover: true } };
    await page.loadFeed();
    const labels = rollButtons();
    assert.ok(labels.includes("Roll back to the checkpoint") && labels.includes("Take over"), labels.join("|"));
  });
  await atest("a refused rollback offers an explicit second choice, and only that choice sends ack_drift", async () => {
    FEED.execution = { ...JSON.parse(JSON.stringify(EXEC)), status: { text: "Needs you", level: "crit" }, pause: null, verification: null,
      controls: { pause: false, resume: false, rollback: true, takeover: true } };
    await page.loadFeed();
    win.__override = url => url.endsWith("/run/rollback") ? reply({ done: "rollback", status: "Needs you", restore: "refused", reason: "files changed after the checkpoint" }) : null;
    const roll = () => walk(doc.getElementById("con-thread")).find(n => n.tag === "button" && n.textContent.startsWith("Roll back to the checkpoint"));
    roll().fire("click"); roll().fire("click"); await settle();
    assert.ok(!requests.filter(r => r[0].endsWith("/run/rollback")).some(r => r[2].includes("ack_drift")), "the first attempt never acknowledges drift");
    const thread = walk(doc.getElementById("con-thread"));
    assert.ok(thread.some(n => n.textContent.includes("PE did not restore: files changed after the checkpoint")));
    const anyway = () => walk(doc.getElementById("con-thread")).find(n => n.tag === "button" && (n.textContent.startsWith("Roll back anyway") || n.textContent.startsWith("Overwrite")));
    anyway().fire("click");
    assert.ok(anyway().textContent.includes("Click again"), "overwriting is two clicks");
    anyway().fire("click"); await settle();
    assert.ok(requests.some(r => r[0].endsWith("/run/rollback") && r[2].includes("ack_drift=1")));
    win.__override = null;
  });
  await atest("an armed rollback does not follow the operator into another conversation", async () => {
    FEED.execution = { ...JSON.parse(JSON.stringify(EXEC)), pause: null, verification: null, controls: { pause: false, resume: false, rollback: true, takeover: true } };
    await page.loadFeed();
    walk(doc.getElementById("con-thread")).find(n => n.tag === "button" && n.textContent === "Roll back to the checkpoint").fire("click");
    assert.ok(rollButtons().some(t => t.includes("Click again")));
    page.select("0123456789abcdef", false); await settle(6);
    assert.ok(!rollButtons().some(t => t.includes("Click again")), rollButtons().join("|"));
  });
  await atest("a scope with no backup asks first, and only the explicit choice prepares it with no_backup", async () => {
    FEED.execution = null; FEED.plan = { id: 1, turn: 1, status: "pending", created_at: 1, hash: "abcd1234", expired: false, plan: PLAN }; await page.loadFeed();
    win.__override = (url) => url.endsWith("/confirm/prepare") && !win.__ok ? reply({ error: "This scope has no recent verified backup.", code: "no_backup" }, 409) : null;
    walk(doc.getElementById("con-thread")).find(n => n.tag === "button" && n.textContent.startsWith("Confirm option")).fire("click"); await settle();
    const ask = walk(doc.getElementById("con-sheet"));
    assert.ok(ask.some(n => n.textContent.includes("NO RECENT BACKUP")) && ask.some(n => n.textContent.includes("no recent verified backup")));
    assert.ok(!requests.filter(r => r[0].endsWith("/confirm/prepare")).some(r => r[2].includes("no_backup")));
    win.__ok = true;
    ask.find(n => n.tag === "button" && n.textContent.startsWith("Prepare it anyway")).fire("click"); await settle();
    assert.ok(requests.some(r => r[0].endsWith("/confirm/prepare") && r[2].includes("no_backup=1")));
    walk(doc.getElementById("con-sheet")).find(n => n.tag === "button" && n.textContent === "Back to the plan").fire("click"); await settle();
    win.__override = null; win.__ok = false;
  });
  await atest("while an authorisation is in flight the sheet cannot be dismissed and nothing is withdrawn", async () => {
    FEED.plan = { id: 1, turn: 1, status: "pending", created_at: 1, hash: "abcd1234", expired: false, plan: PLAN }; FEED.execution = null; await page.loadFeed();
    walk(doc.getElementById("con-thread")).find(n => n.tag === "button" && n.textContent.startsWith("Confirm option")).fire("click"); await settle();
    let release; const held = new Promise(r => { release = r; });
    win.__override = url => url.endsWith("/confirm/authorise") ? held.then(() => reply({ authorised: true })) : null;
    const before = requests.filter(r => r[0].endsWith("/confirm/withdraw")).length;
    walk(doc.getElementById("con-sheet")).find(n => n.tag === "button" && n.textContent.startsWith("Authorise")).fire("click"); await settle();
    const dialog = walk(doc.getElementById("con-sheet")).find(n => n.attrs.role === "dialog");
    dialog.fire("keydown", { key: "Escape", preventDefault() {} });
    walk(doc.getElementById("con-sheet")).find(n => n.className === "pe-sheet-scrim").fire("click");
    await settle();
    assert.strictEqual(requests.filter(r => r[0].endsWith("/confirm/withdraw")).length, before, "no withdrawal while authorising");
    assert.ok(doc.getElementById("con-sheet").children.length > 0, "the sheet stays until the answer");
    release(); await settle(8); win.__override = null;
    assert.strictEqual(doc.getElementById("con-sheet").children.length, 0);
  });
  await atest("lessons are offered with Keep, Edit and Discard; hostile text stays text; nothing is kept without a click", async () => {
    FEED.execution = null; FEED.plan = null;
    FEED.lessons = [{ id: "cl-1", title: "T " + EVIL, statement: "S " + EVIL, scope: ["stack:media"], state: "waiting" },
                    { id: "cl-2", title: "done one", statement: "kept text", scope: [], state: "kept" }];
    await page.loadFeed();
    const box = walk(doc.getElementById("con-thread")).find(n => (n.className || "").includes("pe-con-lessons"));
    assert.ok(box.textContent.includes("WORTH REMEMBERING · 1 WAITING") && box.textContent.includes("KEPT") && box.textContent.includes("until you keep it"));
    assert.ok(!walk(box).some(n => ["img", "script"].includes(n.tag)) && box.textContent.includes("<img src=x"));
    const before = requests.filter(r => r[0].endsWith("/lesson")).length;
    assert.strictEqual(before, 0, "showing a lesson posts nothing");
    const btn = label => walk(doc.getElementById("con-thread")).find(n => n.tag === "button" && n.textContent === label);
    btn("Keep").fire("click"); await settle();
    assert.ok(requests.some(r => r[0].endsWith("/lesson") && r[2].includes("card_id=cl-1") && r[2].includes("decision=keep") && !r[2].includes("statement=")));
    btn("Discard").fire("click"); await settle();
    assert.ok(requests.some(r => r[0].endsWith("/lesson") && r[2].includes("decision=discard")));
    btn("Edit").fire("click");
    const area = walk(doc.getElementById("con-thread")).find(n => n.tag === "textarea");
    assert.ok(area && area.value.startsWith("S "), "the edit box starts from the agent's words");
    area.value = "my own words"; area.fire("input");
    btn("Save and keep").fire("click"); await settle();
    assert.ok(requests.some(r => r[0].endsWith("/lesson") && r[2].includes("decision=keep") && r[2].includes("statement=my+own+words")));
    FEED.lessons = [];
  });
  void all;
  if (failures) { console.log(failures + " failed"); process.exit(1); }
  console.log("all passed");
})();
