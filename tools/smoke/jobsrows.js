// P2 (commit 39936f1, the table): hand-render a snapshot whose jobs cover the 3.10a cases,
// read every row's cells verbatim into window.__d1, and compare each started cell against the
// request table's time cell at the same instant (both go through timeTd). Runs as a js: hash-after
// over the overview (empty hash), where .jobs is drawn.
(async () => {
  const L = localagentsMonitor;
  let real = L.state.snap;
  const t0 = Date.now();
  while (!real && Date.now() - t0 < 6000) { await new Promise(r => setTimeout(r, 100)); real = L.state.snap; }
  if (!real) { window.__d1 = { error: "no snapshot after 6 s" }; return; }
  const copy = JSON.parse(JSON.stringify(real));
  const NOW = copy.now, D = 86400, H = 3600;
  copy.jobs = [
    { id: "j1", name: "running-dead",  status: "running",   server_live: false, started_at: NOW - 2 * H, model: "m1", endpoint: "ai1",    elapsed_s: 2 * H, num_turns: 5, tool_calls: 10, errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null, last_activity: "Edit /a.py" },
    { id: "j2", name: "queued-dead",   status: "queued",    server_live: false, started_at: NOW - H,     model: "m1", endpoint: "ai1",    elapsed_s: H,     num_turns: 0, tool_calls: 0,  errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null, last_activity: "" },
    { id: "j3", name: "done-dead",     status: "succeeded", server_live: false, started_at: NOW - 300,   model: "m2", endpoint: "ai1",    elapsed_s: 300,   num_turns: 9, tool_calls: 20, errors: 1, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null, last_activity: "Bash: ls" },
    { id: "j4", name: "running-live",  status: "running",   server_live: true,  started_at: NOW - 120,   model: "m1", endpoint: "ai1",    elapsed_s: 120,   num_turns: 2, tool_calls: 3,  errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null, last_activity: "Read /b.py" },
    { id: "j5", name: "two-days",      status: "succeeded", server_live: true,  started_at: NOW - 2 * D, model: "m3", endpoint: "dgx1-a", elapsed_s: 2 * D, num_turns: 1, tool_calls: 1,  errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null, last_activity: "" },
    { id: "j6", name: "two-hours",     status: "succeeded", server_live: true,  started_at: NOW - 2 * H, model: "m3", endpoint: "dgx1-a", elapsed_s: 2 * H, num_turns: 1, tool_calls: 1,  errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null, last_activity: "" },
    { id: "j7", name: "no-speed",      status: "succeeded", server_live: true,  started_at: NOW - 60,    model: "m1", endpoint: "ai1",    elapsed_s: 60,    num_turns: 1, tool_calls: 1,  errors: 0, compactions: 0, ctx_pct: null, prefill_tps: null, gen_tps: null, last_activity: "" },
    { id: "j8", name: "speed",         status: "succeeded", server_live: true,  started_at: NOW - 90,    model: "m1", endpoint: "ai1",    elapsed_s: 90,    num_turns: 1, tool_calls: 1,  errors: 0, compactions: 0, ctx_pct: null, prefill_tps: 1234.5, gen_tps: 55, last_activity: "" },
  ];
  L.render(copy);
  const rows = Array.from(document.querySelectorAll('.jobs tbody tr')).map(r => {
    const cells = Array.from(r.cells).map(c => ({ text: c.textContent, title: c.title || null }));
    const pill = r.querySelector('.pill');
    return {
      id: r.getAttribute('data-job'),
      cells: cells.map(c => c.text),
      started: cells[2],
      prefill: cells[10],
      decode: cells[11],
      pill: pill ? { cls: pill.className, text: pill.textContent } : null,
      rowText: r.textContent,
      hasServerGone: /server gone/i.test(r.textContent),
    };
  });
  const headers = Array.from(document.querySelectorAll('.jobs thead th')).map(e => e.textContent);
  const reqTimeCell = (t) => {
    const el = L.requestsTable([{ t: t, http_status: 200, path: "/v1/messages" }], { now: NOW });
    const td = el.querySelector('.requests-table tbody tr').cells[0];
    return { text: td.textContent, title: td.title || null };
  };
  const byId = (id) => rows.find(r => r.id === id);
  const j5 = byId("j5"), j6 = byId("j6");
  const req5 = reqTimeCell(NOW - 2 * D), req6 = reqTimeCell(NOW - 2 * H);
  window.__d1 = {
    now: NOW,
    headers: headers,
    rows: rows,
    reqCompare: [
      { job: "j5 (2 days)",  jobStarted: j5.started, req: req5, textMatch: j5.started.text === req5.text, titleMatch: j5.started.title === req5.title },
      { job: "j6 (2 hours)", jobStarted: j6.started, req: req6, textMatch: j6.started.text === req6.text, titleMatch: j6.started.title === req6.title },
    ],
    fmt: { time_2h: L.fmt.time(NOW - 2 * H), date_2d: L.fmt.date(NOW - 2 * D) },
  };
  return "ok";
})();
