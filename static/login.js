(function () {
  "use strict";
  var form = document.getElementById("login-form");
  if (form) form.addEventListener("submit", function (event) {
    if (form.getAttribute("aria-busy") === "true") { event.preventDefault(); return; }
    form.setAttribute("aria-busy", "true");
    document.getElementById("login-fields").classList.add("pe-is-busy");
    var button = document.getElementById("engage");
    button.classList.add("is-busy");
    button.innerHTML = '<span class="pe-spinner" aria-hidden="true"></span>VERIFYING<span class="pe-shimmer" aria-hidden="true"></span>';
    document.getElementById("login-quip").textContent = '"Checking the manifest…"';
  });
  var countdown = document.querySelector("[data-seconds]");
  if (countdown) {
    var deadline = Date.now() + Number(countdown.dataset.seconds) * 1000;
    setInterval(function () {
      var seconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      document.getElementById("retry-countdown").textContent = String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
      document.getElementById("retry-progress").value = seconds;
      if (seconds === 0) window.location.replace("/login");
    }, 1000);
  }
})();
