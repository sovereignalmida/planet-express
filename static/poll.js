// poll.js — the one visibility-aware poller, shared by dashboard.js and panels.js (rc14.7.3).
// window.pePollWhileVisible(fn, intervalMs): call fn every intervalMs while the tab is visible, never
// two at once, and once on becoming visible again if the last success is older than one interval.
(function () {
  "use strict";

  // fn resolves to true only on success; skipped/failed reads do not reset
  // freshness. The initial server-rendered view starts fresh.
  function pollWhileVisible(fn, intervalMs) {
    var timer = null;
    var inFlight = false;
    var lastSuccess = Date.now();

    function poll() {
      if (inFlight || document.visibilityState !== "visible") return;
      inFlight = true;
      Promise.resolve().then(fn).then(function (success) {
        if (success === true) lastSuccess = Date.now();
        inFlight = false;
      }, function () {
        inFlight = false;
      });
    }

    function visibilityChanged() {
      if (timer !== null) clearInterval(timer);
      timer = null;
      if (document.visibilityState !== "visible") return;
      if (Date.now() - lastSuccess > intervalMs) poll();
      timer = setInterval(poll, intervalMs);
    }

    document.addEventListener("visibilitychange", visibilityChanged);
    visibilityChanged();
  }

  window.pePollWhileVisible = pollWhileVisible;
})();
