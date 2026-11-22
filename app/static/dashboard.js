(function () {
  var live = document.querySelector("[data-live]");
  if (!live) return;
  var runId = live.getAttribute("data-run");
  var img = document.getElementById("live-frame");
  var log = document.getElementById("live-log");
  var statusNode = document.getElementById("live-status");
  var captionNode = document.getElementById("live-caption");
  var startedRunning = document.body.getAttribute("data-status") === "queued"
    || document.body.getAttribute("data-status") === "running";

  function apply(data) {
    if (statusNode) statusNode.textContent = data.status + " · " + data.phase;
    if (img && data.frame) {
      img.hidden = false;
      img.src = data.frame + "?t=" + Date.now();
    }
    var rows = data.events || [];
    if (captionNode && rows.length) {
      captionNode.textContent = rows[rows.length - 1].caption || "Watching the page";
    }
    if (log) {
      log.replaceChildren();
      rows.forEach(function (row, index) {
        var item = document.createElement("li");
        item.textContent = row.caption || row.kind || "";
        if (index === rows.length - 1) item.className = "latest";
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
