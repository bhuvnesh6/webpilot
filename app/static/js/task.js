(function () {
  "use strict";
  var root = document.getElementById("task-root");
  if (!root) return;
  var lastSeq = parseInt(root.getAttribute("data-last-seq"), 10) || 0;
  var terminal = root.getAttribute("data-terminal") === "1";
  var pollUrl = root.getAttribute("data-poll-url"), respondUrl = root.getAttribute("data-respond-url");
  var outcomeClass = { success: "completed", failed: "failed", blocked: "failed", denied: "cancelled", aborted: "cancelled" };
  var TERMINAL = ["completed", "failed", "cancelled"];

  function setText(id, v) { var n = document.getElementById(id); if (n) n.textContent = v; }

  function addAction(a) {
    var body = document.getElementById("actions-body");
    var none = body.querySelector(".wp-none"); if (none) none.remove();
    var tr = document.createElement("tr");
    tr.appendChild(WP.el("td", null, a.seq));
    tr.appendChild(WP.el("td", "wp-mono small", a.action));
    var p = a.params || {};
    tr.appendChild(WP.el("td", "wp-mono small", (a.target || p.url || p.direction || "").slice(0, 50)));
    var o = WP.el("td"); o.appendChild(WP.el("span", "wp-status wp-status-" + (outcomeClass[a.outcome] || "running"), a.outcome)); tr.appendChild(o);
    var v = a.verification || {};
    tr.appendChild(WP.el("td", "small", v.ok === null || v.ok === undefined ? "-" : (v.ok ? "✓ " : "✗ ") + (v.detail || "").slice(0, 60)));
    tr.appendChild(WP.el("td", "small", (a.detail || "").slice(0, 110)));
    tr.appendChild(WP.el("td", "small", a.method || ""));
    body.appendChild(tr);
  }

  function showPrompt(pp) {
    var panel = document.getElementById("prompt-panel");
    if (!pp) { panel.hidden = true; return; }
    panel.hidden = false;
    setText("prompt-message", pp.message || "");
    document.getElementById("prompt-actions").setAttribute("data-prompt-id", pp.id);
    var isApproval = pp.kind === "approval";
    document.getElementById("prompt-deny").hidden = !isApproval;
    document.getElementById("prompt-approve").textContent = isApproval ? "Approve this action" : "Send reply";
    setText("prompt-title", isApproval ? "Approval needed for a consequential action" : "The agent is asking you something");
  }

  function tick() {
    WP.getJSON(pollUrl + "?after_seq=" + lastSeq).then(function (d) {
      var t = d.task;
      var holder = document.getElementById("status-badge"); holder.replaceChildren(WP.badge(t.status));
      setText("m-progress", (t.throttled ? "⏳ " : "") + (t.progress_message || "-"));
      setText("m-url", t.current_url || "-"); setText("m-action", t.current_action || "-");
      setText("m-actions", t.actions_executed + " of " + (document.getElementById("m-actions").textContent.split(" of ")[1] || "?"));
      if (t.token_usage) setText("m-tokens", t.token_usage.total + " in " + t.token_usage.calls + " call(s)");
      d.actions.forEach(function (a) { addAction(a); lastSeq = Math.max(lastSeq, a.seq); });
      showPrompt(t.pending_prompt);
      if (TERMINAL.indexOf(t.status) !== -1) { terminal = true; window.location.reload(); }
    }).catch(function () {});
  }

  document.getElementById("prompt-actions").addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-answer]"); if (!btn) return;
    var box = document.getElementById("prompt-actions");
    WP.postJSON(respondUrl, { prompt_id: box.getAttribute("data-prompt-id"), approved: btn.getAttribute("data-answer") === "1",
      message: document.getElementById("prompt-text").value }).then(function (r) {
      if (!r.ok) window.alert(r.data.error || "Could not send the response.");
      else { document.getElementById("prompt-text").value = ""; document.getElementById("prompt-panel").hidden = true; tick(); }
    });
  });
  if (!terminal) setInterval(tick, 2500);
})();
