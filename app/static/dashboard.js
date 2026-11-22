(function () {
  var live = document.querySelector("[data-live]");
  if (!live) return;
  var runId = live.getAttribute("data-run");
  var img = document.getElementById("live-frame");
  var log = document.getElementById("live-log");
  var statusNode = document.getElementById("live-status");
  var startedRunning = document.body.getAttribute("data-status") === "queued"
    || document.body.getAttribute("data-status") === "running";

  function apply(data) {
    if (statusNode) statusNode.textContent = data.status + " · " + data.phase;
    if (img && data.frame) {
      img.hidden = false;
      img.src = data.frame + "?t=" + Date.now();
    }
    if (log) {
      log.replaceChildren();
      (data.events || []).forEach(function (row) {
        var item = document.createElement("li");
        item.textContent = row.caption || row.kind || "";
        log.appendChild(item);
      });
    }
    if (data.status === "queued" || data.status === "running") {
      window.setTimeout(pull, 900);
      return;
    }
    if (startedRunning) window.location.reload();
  }

  function pull() {
    fetch("/api/v1/runs/" + runId + "/activity", { headers: { Accept: "application/json" } })
      .then(function (response) { return response.json(); })
      .then(apply)
      .catch(function () {
        if (startedRunning) window.setTimeout(pull, 1500);
      });
  }

  pull();
})();
