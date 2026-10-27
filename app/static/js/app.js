(function () {
  "use strict";
  // Confirm before submitting forms marked data-confirm.
  document.addEventListener("submit", function (e) {
    var msg = e.target && e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
  // Mobile sidebar.
  var toggle = document.querySelector("[data-toggle-sidebar]");
  if (toggle) toggle.addEventListener("click", function () { document.getElementById("sidebar").classList.toggle("open"); });

  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  window.WP = {
    csrf: csrf,
    postJSON: function (url, body) {
      return fetch(url, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, "Accept": "application/json" },
        body: JSON.stringify(body || {}) }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, data: j }; }); });
    },
    getJSON: function (url) {
      return fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } }).then(function (r) {
        if (r.status === 401) { window.location.href = "/login"; throw new Error("auth"); }
        return r.json();
      });
    },
    el: function (tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; },
    badge: function (status) { return WP.el("span", "wp-status wp-status-" + status, String(status).replace(/_/g, " ")); },
    elapsed: function (iso) {
      if (!iso) return "-";
      var s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000)), m = Math.floor(s / 60), h = Math.floor(m / 60);
      return h ? h + "h " + (m % 60) + "m " + (s % 60) + "s" : (m ? m + "m " + (s % 60) + "s" : s + "s");
    }
  };
})();
