// dashboard.js — Planet Express dashboard interactions. All data is already
// server-rendered on page load (no fetch calls here); this only handles
// client-side view state: which tab is showing, the network router filter,
// and dismissing the pending-plan card. "Approve" is a plain link to Telegram,
// not JS-driven -- this dashboard has no route to actually approve anything.
(function () {
  "use strict";

  function setActiveTab(name) {
    document.querySelectorAll(".tab").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.tab === name);
    });
    document.querySelectorAll(".tab-panel").forEach(function (panel) {
      panel.classList.toggle("active", panel.dataset.tabPanel === name);
    });
    document.querySelectorAll(".speech-line").forEach(function (line) {
      line.classList.toggle("hidden", line.dataset.tabLine !== name);
    });
  }

  var TAB_NAMES = ["overview", "backups", "network", "actions"];

  document.querySelectorAll(".tab").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setActiveTab(btn.dataset.tab);
      window.location.hash = btn.dataset.tab;
    });
  });

  // The 60s meta-refresh reloads this same URL, and browsers preserve the
  // fragment across that reload -- so the hash is what survives auto-refresh.
  function applyHashTab() {
    var hashTab = window.location.hash.slice(1);
    if (TAB_NAMES.indexOf(hashTab) !== -1) {
      setActiveTab(hashTab);
    }
  }
  applyHashTab();
  // Tab clicks push a new fragment onto history, so Back/Forward change the
  // hash without re-running this script -- listen for that too, or the visible
  // tab and the URL drift apart.
  window.addEventListener("hashchange", applyHashTab);

  var filterInput = document.getElementById("net-filter");
  if (filterInput) {
    var rows = Array.prototype.slice.call(document.querySelectorAll(".net-filter-row"));
    var counter = document.getElementById("net-count");
    var total = rows.length;

    filterInput.addEventListener("input", function () {
      var q = filterInput.value.trim().toLowerCase();
      var shown = 0;
      rows.forEach(function (row) {
        var match = !q || row.dataset.filterText.indexOf(q) !== -1;
        row.classList.toggle("hidden", !match);
        if (match) shown++;
      });
      if (counter) counter.textContent = "showing " + shown + " of " + total;
    });
  }

  // Dismissal only lasts for this browser session and only for the plan ID
  // dismissed -- the 60s meta-refresh reloads the page, and a plan ID is
  // reused as "the same plan" across reloads but a *different* plan ID
  // (new pending plan) should always show up regardless of a past dismissal.
  var DISMISS_KEY = "planetexpress-dismissed-plan-id";

  document.querySelectorAll(".plan-card").forEach(function (card) {
    if (card.dataset.planId && card.dataset.planId === sessionStorage.getItem(DISMISS_KEY)) {
      card.classList.add("hidden");
    }
  });

  document.querySelectorAll(".btn-dismiss").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var card = btn.closest(".plan-card");
      if (!card) return;
      card.classList.add("hidden");
      if (card.dataset.planId) {
        sessionStorage.setItem(DISMISS_KEY, card.dataset.planId);
      }
    });
  });
})();
