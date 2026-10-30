// churn.js (3.7w, the Firefox side): make the overview rebuild the way the live world does.
// The seeded --no-collect world hardly changes between pushes, so a long hold exercises little
// but the skip path and the jobs-table/timeline replace that each history fetch forces. This
// script renders, every 250 ms, one of two hand-edited copies of the page's latest snapshot:
//   A: the second host's node down (that card is torn down and rebuilt), the jobs reversed
//      (every table row differs);
//   B: every endpoint's last_active moved by a minute (the in-place path swaps the "active …"
//      sub-block of every endpoint of every card), the first job's status flipped.
// Each call differs from the last and from the stream's own pushes, so every render runs the
// rebuild, the in-place swap and the table replace — the three paths that detach nodes. The
// count is in window.__churn; ffmem.py reads it beside the page's render counters.
(function () {
  var M = window.localagentsMonitor;
  var n = 0;
  window.__churn = { renders: 0, threw: null };
  function variant(base, flip) {
    var snap = JSON.parse(JSON.stringify(base));
    if (flip) {
      if (snap.hosts[1]) snap.hosts[1].state = "down";
      snap.jobs = (snap.jobs || []).slice().reverse();
    } else {
      snap.hosts.forEach(function (host) {
        host.endpoints.forEach(function (ep) {
          if (ep.last_active != null) ep.last_active -= 60 + (n % 7);
        });
      });
      if (snap.jobs && snap.jobs[0]) snap.jobs[0].status = snap.jobs[0].status === "running" ? "queued" : "running";
    }
    return snap;
  }
  window.__churnTimer = setInterval(function () {
    var base = M.state.snap;
    if (!base) return;
    n++;
    try {
      M.render(variant(base, n % 2 === 1));
      window.__churn.renders++;
    } catch (e) {
      window.__churn.threw = String(e && e.message || e);
    }
  }, 250);
})();
