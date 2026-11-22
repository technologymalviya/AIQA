(function () {
  var node = document.querySelector("[data-poll]");
  if (!node) return;
  var status = node.getAttribute("data-status");
  if (status === "queued" || status === "running") {
    window.setTimeout(function () { window.location.reload(); }, 1500);
  }
})();
