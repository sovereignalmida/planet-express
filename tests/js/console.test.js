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
  for (const id of ["pe-con", "con-list", "con-top", "con-scroll", "con-thread", "con-composer", "con-text", "con-send", "con-stop", "con-note", "con-new"]) {
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
const FEED = { conversation: { id: "0123456789abcdef", title: EVIL, origin: "operator", state: "open", turns: 1, status: { text: "Needs your call", level: "warn", group: "Needs you" },
                               verdict: { h: "Waiting for your choice between 2 options", p: "Recommended: Recreate. Nothing has been changed." } },
  budget: { used_pct: 24, tokens_used: 24000, token_budget: 100000, tokens_left: 76000, state: "ok" }, running: false, composer: { mode: "idle", reason: null },
  messages: [{ seq: 1, role: "operator", turn: null, at: 1, text: "why? " + EVIL, by: "chris" }, { seq: 2, role: "agent", turn: 1, at: 2, text: "It is **the gateway**.\n\n- " + EVIL, by: null },
             { seq: 3, role: "system", turn: 1, at: 3, text: "note " + EVIL, by: null }],
  last_message: 3, events: [{ seq: 5, at: 1, turn: 1, type: "call", kind: "ran", name: "Bash", detail: EVIL }, { seq: 6, at: 1, turn: 1, type: "result", name: "Bash", excerpt: EVIL, failed: false }], last_event: 6,
  turn_starts: [4], plan: { id: 1, turn: 1, status: "pending", created_at: 1, hash: "abcd1234", expired: false, plan: PLAN }, status: { enabled: true, proposal_only: false } };

(async () => {
  const requests = [];
  const doc = makeDoc({ status: { enabled: true, proposal_only: false }, selected: "0123456789abcdef" });
  const win = { history: { pushState() {} }, location: { assign() {}, pathname: "/console" }, URLSearchParams, clearTimeout() {}, setTimeout: () => 0, addEventListener() {},
    fetch: async (url, opts) => { requests.push([url, opts && opts.method || "GET", opts && opts.body && String(opts.body)]);
      const body = url.includes("/feed") ? FEED : url.includes("/list") ? LIST : { id: "0123456789abcdef", scrapped: true };
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
    assert.ok(plan.textContent.includes("RECOMMENDED") && plan.textContent.includes("gluetun is running and stable") && plan.textContent.includes("not available yet"));
    assert.ok(!walk(plan).some(n => n.tag === "button" && /confirm/i.test(n.textContent)), "there is no Confirm button yet");
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
  await atest("a poll that finds nothing new leaves the drawn nodes alone (selection and focus survive)", async () => {
    const before = doc.getElementById("con-thread").children.slice();
    const railBefore = doc.getElementById("con-list").children.slice();
    await page.loadFeed();
    page.renderThread(); page.renderRail();
    assert.ok(before.every((n, i) => doc.getElementById("con-thread").children[i] === n), "thread nodes are the same objects");
    assert.ok(railBefore.every((n, i) => doc.getElementById("con-list").children[i] === n), "rail nodes are the same objects");
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
  void all;
  if (failures) { console.log(failures + " failed"); process.exit(1); }
  console.log("all passed");
})();
