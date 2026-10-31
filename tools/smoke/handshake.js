// P2 (commit 39fdb3a, 3.7v) + F3/F4 (fold of the page-batch review, 2026-10-30): the shared
// requests renderer's row rule, hand-rendered. Eight rows — three /api/hello handshakes (501,
// no status, 200), a 500, a status-0 backend failure, a clean 200, a context-exceeded 200, and
// a /api/hello carrying an error body — appended to the page; each row's class, its pill's
// class/text/title and its error cell are read into window.__d1 (cdp.mjs's `d1` field).
// d1.ok is true only when all eight match the oracle: the handshake rows wear `pill-muted`,
// read "handshake", put the numeric status (or "no status") in the title, and take NO `req-err`
// even with an error body; the two failures take `req-err`; the context row takes `req-ctx`.
(function () {
  var NOW = Math.floor(Date.now() / 1000);
  var row = function (path, status, error, ctx) {
    return { t: NOW - 60, path: path, http_status: status, error: error,
      context_exceeded: ctx || null, ms: 800, job_id: null,
      tokens: { input: 100, output: 10, cache_read: 0, cache_creation: 0 } };
  };
  var rows = [
    row("/api/hello", 501, ""),
    row("/api/hello", null, ""),
    row("/api/hello", 200, ""),
    row("/v1/messages", 500, ""),
    row("/v1/messages", 0, "backend unreachable"),
    row("/v1/messages", 200, ""),
    row("/v1/messages", 200, "", "x"),
    row("/api/hello", 500, "boom")
  ];
  var expect = [
    { rowClass: "", pillClass: "pill pill-muted", pillText: "handshake", pillTitle: "501", error: "" },
    { rowClass: "", pillClass: "pill pill-muted", pillText: "handshake", pillTitle: "no status", error: "" },
    { rowClass: "", pillClass: "pill pill-muted", pillText: "handshake", pillTitle: "200", error: "" },
    { rowClass: "req-err", pillClass: "pill pill-error", pillText: "error", pillTitle: "500", error: "" },
    { rowClass: "req-err", pillClass: "pill pill-error", pillText: "error", pillTitle: "0", error: "backend unreachable" },
    { rowClass: "", pillClass: "pill pill-ok", pillText: "ok", pillTitle: "200", error: "" },
    { rowClass: "req-ctx", pillClass: "pill pill-ok", pillText: "ok", pillTitle: "200", error: "context exceeded" },
    { rowClass: "", pillClass: "pill pill-muted", pillText: "handshake", pillTitle: "500", error: "boom" }
  ];
  var d1 = window.__d1 = { threw: null, checks: [] };
  try {
    var table = localagentsMonitor.requestsTable(rows, { job: false, now: NOW });
    document.body.appendChild(table);
    var trs = Array.prototype.slice.call(table.querySelectorAll("tbody tr"));
    d1.rows = trs.map(function (tr, i) {
      var pill = tr.querySelector("td:nth-child(2) .pill");
      var err = tr.querySelector("td:last-child");
      var got = {
        rowClass: tr.className || "",
        pillClass: pill ? pill.className : null,
        pillText: pill ? pill.textContent : null,
        pillTitle: pill ? pill.getAttribute("title") : null,
        error: err ? err.textContent : null,
      };
      var e = expect[i] || {};
      var diff = Object.keys(e).filter(function (k) { return got[k] !== e[k]; });
      d1.checks.push({ i: i, ok: diff.length === 0, diff: diff, expected: e, got: got });
      return got;
    });
  } catch (ex) {
    d1.threw = (ex && ex.name ? ex.name : "error") + ": " + (ex && ex.message ? ex.message : String(ex));
  }
  d1.ok = d1.threw == null && d1.checks.length === 8 &&
    d1.checks.every(function (c) { return c.ok; });
})();
