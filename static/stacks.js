// Stack and service controls in the Overview drawer (T47).
//
// The steps these ask for have existed since slice 5b-5. What was missing was a way to ask
// that is not a chat message.
//
// Core decides what each action costs: UP is R1 and goes straight through, DOWN is R2 and
// needs an elevated session. This file does not know or encode that -- it posts, and if the
// answer is `elevation_required` it prompts once and posts again. Keeping the rule in one
// place is deliberate: in this feature, every rule that lived in two places went stale in one
// of them.
(() => {
  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;

  function say(near, text, level) {
    const foot = near.closest("[data-drawer]")?.querySelector("[data-drawer-foot]");
    if (!foot) return;
    foot.textContent = text;
    foot.dataset.level = level || "";
  }

  async function post(url) {
    const response = await fetch(url, {
      method: "POST",
      cache: "no-store",
      body: new URLSearchParams({ csrf_token: csrf() }),
    });
    let data = {};
    try { data = await response.json(); } catch (_error) { /* status decides */ }
    return { response, data };
  }

  async function ask(button, url, what) {
    button.disabled = true;
    say(button, what + "…");
    try {
      let { response, data } = await post(url);
      if (response.status === 401) {
        window.location.assign("/login?next=" + encodeURIComponent(
          window.location.pathname + window.location.hash));
        return;
      }
      if (data.reason === "elevation_required" && window.peElevate) {
        if (!await window.peElevate(data.error || "This needs your passphrase again.")) {
          say(button, "Nothing was done.", "warn");
          return;
        }
        ({ response, data } = await post(url));
      }
      if (!response.ok) { say(button, data.error || (what + " was refused."), "crit"); return; }
      // `started` means an execution is running; anything else is core declining with a
      // reason worth reading rather than an error.
      if (data.outcome === "started") {
        say(button, what + " started. Watch it in Actions.", "ok");
      } else {
        say(button, data.message || (what + ": " + (data.outcome || "no answer")), "warn");
      }
    } catch (_error) {
      say(button, "Could not reach the dashboard.", "crit");
    } finally {
      button.disabled = false;
    }
  }

  // Delegated from document, not from the drawer: #dashboard-live's contents are replaced
  // wholesale every 60 seconds, and a listener bound to anything inside it dies with the
  // swap while a listener bound to a fresh copy each time would stack up.
  document.addEventListener("click", event => {
    const stack = event.target.closest("[data-stack-act]");
    if (stack) {
      const what = stack.dataset.stackAct;
      const name = stack.dataset.stack;
      if (what === "down" && !window.confirm(
          "Take the " + name + " stack down?\n\nEvery container in it stops.")) return;
      ask(stack, "/api/stacks/" + encodeURIComponent(name) + "/" + encodeURIComponent(what),
          name + " " + what.toUpperCase());
      return;
    }
    const service = event.target.closest("[data-svc-restart]");
    if (service) {
      const { stack: parent, service: name } = service.dataset;
      ask(service,
          "/api/containers/" + encodeURIComponent(parent) + "/" +
          encodeURIComponent(name) + "/restart",
          name + " RESTART");
    }
  });
})();
