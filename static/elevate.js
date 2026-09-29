// Asking for the passphrase again, for the actions that need an elevated session (T47).
//
// Shared rather than owned by one tab: config apply needs it today, stack control will need
// it next, and a second copy of a security prompt is a second thing to get wrong.
//
// The server decides. This only collects the passphrase and posts it; whether a request may
// proceed is answered by the 403 it came from, and again by the next request.
(() => {
  const dialog = document.getElementById("elevate-dialog");
  if (!dialog) return;

  const form = dialog.querySelector("form");
  const input = document.getElementById("elevate-passphrase");
  const note = document.getElementById("elevate-note");
  const confirm = document.getElementById("elevate-confirm");
  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;

  let pending = null;

  function settle(value) {
    const resolve = pending;
    pending = null;
    input.value = "";                       // never leave a passphrase in a field
    if (dialog.open) dialog.close();
    if (resolve) resolve(value);
  }

  function say(text, level) {
    note.textContent = text;
    note.className = "elevate-note" + (level ? " " + level : "");
  }

  dialog.addEventListener("close", () => settle(false));
  dialog.querySelector("[data-elevate-cancel]").addEventListener("click", () => settle(false));

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!input.value) { say("Enter your passphrase.", "warn"); return; }
    confirm.disabled = true;
    say("Checking…");
    try {
      const response = await fetch("/api/elevate", {
        method: "POST",
        cache: "no-store",
        body: new URLSearchParams({ csrf_token: csrf(), passphrase: input.value }),
      });
      let data = {};
      try { data = await response.json(); } catch (_error) { /* status decides below */ }
      if (response.ok) { settle(true); return; }
      if (data.reason === "stale_session" || response.status === 401) {
        window.location.assign("/login?next=" + encodeURIComponent(
          window.location.pathname + window.location.hash));
        return;
      }
      input.value = "";
      say(data.error || "That did not work.", "crit");
    } catch (_error) {
      say("Could not reach the dashboard. Try again.", "crit");
    } finally {
      confirm.disabled = false;
      input.focus();
    }
  });

  // Resolves true when the session is elevated, false when the operator backed out.
  window.peElevate = function elevate(because) {
    if (pending) return Promise.resolve(false);   // one prompt at a time
    say(because || "This action needs your passphrase again.");
    input.value = "";
    dialog.showModal();
    input.focus();
    return new Promise(resolve => { pending = resolve; });
  };
})();
