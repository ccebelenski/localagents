// 3.7u-fix P-2 (Opus review of the page folds): the overflow mark never costs a node its identity.
// raiseCardFloor marks the lines that scroll with the `overflows` class on the LIVE nodes; the
// in-place update compares those nodes with freshly built ones, which never carry the mark — so
// before the fold every push replaced an endpoint whose direct child scrolled (the whole live card
// body: strip, figures, jobs) and the host head whose note scrolled. Two parts, each stamping
// nodes with an expando (a property, never an attribute: the HTML compares cannot see it):
//  pushes:  the seeded world as the stream pushes it, held until 7 renders have passed (at 900 px
//           the seeded down endpoint's error line scrolls, so the mark is on a live node);
//  inPlace: a hand-built world at the page's width (a 300-char speeds note, down error and host
//           note, a 200-char job name) rendered twice through render(), the second differing only
//           in the running job's elapsed — the in-place path — every endpoint and host head kept.
// Result in window.__keep (cdp.mjs prints it as `keep`); `replaced` lists must be empty, and
// `overflowing` shows the mark was there to be tested. Evaluated as an expression (a promise).
(function () {
  var M = window.localagentsMonitor;
  var q = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };
  var clone = function (o) { return JSON.parse(JSON.stringify(o)); };
  var total = function () { return M.renders.full + M.renders.inPlace + M.renders.skipped; };
  var nodes = function () { return q("#hosts .endpoint, #hosts .host-head"); };
  var label = function (e) {
    return e.classList.contains("host-head") ? "head " + e.parentNode.dataset.host : "endpoint " + e.dataset.endpoint;
  };
  var stamp = function (tag) { nodes().forEach(function (e) { e["__keep" + tag] = true; }); };
  var replaced = function (tag) { return nodes().filter(function (e) { return !e["__keep" + tag]; }).map(label); };
  var overflowing = function () { return q("#hosts .overflows").map(function (e) { return e.className.split(" ")[0]; }); };
  var out = { width: window.innerWidth };
  return new Promise(function (resolve) {
    stamp("P");
    var start = clone(M.renders);
    var t0 = total();
    var marks = overflowing();
    var deadline = Date.now() + 20000;
    (function wait() {
      if (total() - t0 < 7 && Date.now() < deadline) return setTimeout(wait, 200);
      var r = M.renders;
      out.pushes = { renders: { full: r.full - start.full, inPlace: r.inPlace - start.inPlace, skipped: r.skipped - start.skipped },
        overflowing: marks.concat(overflowing()).filter(function (c, i, a) { return a.indexOf(c) === i; }),
        replaced: replaced("P"), kept: nodes().length - replaced("P").length };
      resolve();
    })();
  }).then(function () {
    var rep = function (unit, n) { return new Array(Math.ceil(n / unit.length) + 1).join(unit).slice(0, n); };
    var hostByName = function (s, n) { return s.hosts.filter(function (h) { return h.name === n; })[0]; };
    var base = clone(M.state.snap);
    var L = clone(base);
    var ai9 = clone(hostByName(L, "dgx1")); ai9.name = "ai9"; ai9.state = "down";
    ai9.endpoints = [Object.assign(clone(ai9.endpoints[0]), { name: "ai9-x", dropped_job: null, dropped_job_id: null,
      error: rep("node unreachable: no route to host after ", 300) })];
    L.hosts.push(ai9);
    L.jobs.forEach(function (j) { if (j.status === "running") j.name = rep("long-job-name-", 200); });
    hostByName(L, "ai1").endpoints[0].rates.reason = rep("the last probe has no counters and ", 300);
    hostByName(L, "dgx1").endpoints[0].error = rep("engine unreachable: connection refused after ", 300);
    var a = clone(L); a.now = base.now + 1;
    M.render(a);
    stamp("I");
    var r0 = clone(M.renders);
    var b = clone(L); b.now = base.now + 2;
    b.jobs.forEach(function (j) { if (j.status === "running") j.elapsed_s = (j.elapsed_s || 0) + 1; });
    M.render(b);
    var r1 = M.renders;
    out.inPlace = { path: { full: r1.full - r0.full, inPlace: r1.inPlace - r0.inPlace, skipped: r1.skipped - r0.skipped },
      overflowing: overflowing(), replaced: replaced("I"), kept: nodes().length - replaced("I").length };
    window.__keep = out;
    return JSON.stringify(out);
  });
})()
