(function () {
  "use strict";
  var wrap = document.getElementById("running-wrap");
  if (!wrap) return;
  var base = wrap.getAttribute("data-detail-base");

  function row(t) {
    var tr = document.createElement("tr");
    var c1 = WP.el("td"); c1.appendChild(WP.el("span", "wp-mono small", t.id.slice(0, 8) + " "));
    var a = WP.el("a", null, t.name); a.href = base + t.id; c1.appendChild(a); tr.appendChild(c1);
    tr.appendChild(WP.el("td", null, t.website_name || ""));
    var cs = WP.el("td"); cs.appendChild(WP.badge(t.status)); tr.appendChild(cs);
    tr.appendChild(WP.el("td", "wp-mono small", t.current_action || "-"));
    tr.appendChild(WP.el("td", null, (t.progress_message || "").slice(0, 70)));
    tr.appendChild(WP.el("td", "text-muted", t.started_at ? t.started_at.replace("T", " ").slice(0, 19) + " UTC" : "queued"));
    tr.appendChild(WP.el("td", null, WP.elapsed(t.started_at)));
    var act = WP.el("td", "text-end wp-actions");
    var d = WP.el("a", "btn btn-sm btn-outline-secondary me-1", "Details"); d.href = base + t.id; act.appendChild(d);
    var b = WP.el("button", "btn btn-sm btn-outline-danger", "Request stop"); b.type = "button";
    b.disabled = t.status === "cancel_requested";
    b.addEventListener("click", function () {
      if (!window.confirm("Request a stop for this task?")) return;
      WP.postJSON(base + t.id + "/cancel", {}).then(tick);
    });
    act.appendChild(b); tr.appendChild(act);
    return tr;
  }

  function tick() {
    WP.getJSON(wrap.getAttribute("data-poll-url")).then(function (data) {
      var body = document.getElementById("running-body");
      if (!body) { if (data.tasks.length) window.location.reload(); return; }
      body.replaceChildren.apply(body, data.tasks.map(row));
      if (!data.tasks.length) window.location.reload();
    }).catch(function () {});
  }
  setInterval(tick, 3000);
})();
