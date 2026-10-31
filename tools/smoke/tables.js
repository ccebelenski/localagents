// 3.10c: every table pages, sorts and filters, client-side, and keeps the operator's view
// across pushes. Runs over the seeded world on the overview (empty hash): the seeded snapshot
// holds two jobs, so a snapshot COPY with 120 jobs (varied status/started/elapsed/turns) is
// hand-rendered into #jobs-body — the same table the push draws. Reads (a)-(e) into
// window.__d1; nothing awaits between the renders and the reads, so a real SSE push cannot
// land mid-sequence (the next push overwrites the hand-edited DOM — capture as you go).
// (e) then navigates to the job page and the endpoint page and reads their requests tables'
// filter row and duration sort.
(async () => {
  const L = localagentsMonitor;
  let real = L.state.snap;
  const t0 = Date.now();
  while (!real && Date.now() - t0 < 6000) { await new Promise((r) => setTimeout(r, 100)); real = L.state.snap; }
  if (!real) { window.__d1 = { error: "no snapshot after 6 s" }; return; }
  const NOW = real.now;

  // 120 jobs: statuses cycle (a third done), turns run 0..119 (nineteen over 100), every
  // other field varies so no two rows are alike. The ids are full 36-character UUIDs (their
  // short form is the first eight) and the started times are 120 distinct clock times inside
  // NOW's local day: the F1 witness filters on a row's full UUID and on the date the started
  // cell's title carries, so both must be deterministic. prefill t/s and last activity carry
  // blanks (every fourth / every ninth row) for the F2 witness.
  const noonDate = new Date(NOW * 1000);
  noonDate.setHours(12, 0, 0, 0);
  const noon = noonDate.getTime() / 1000;
  const mk = (i, bump) => ({
    id: String(i).padStart(8, "0") + "-1111-2222-3333-444444444444",
    status: i % 3 === 0 ? "failed" : i % 3 === 1 ? "running" : "succeeded",
    server_live: true, started_at: noon - 60 * i, model: "m" + (i % 4),
    endpoint: i % 2 ? "ai1" : "dgx1-a", elapsed_s: i * 7 + (bump ? 1 : 0), num_turns: i % 130,
    tool_calls: i % 10, errors: i % 5, compactions: 0, ctx_pct: null,
    prefill_tps: i % 4 === 0 ? null : (i % 50) * 10 + 0.5, gen_tps: null,
    last_activity: i % 9 === 0 ? null : "act " + i,
  });
  const copy = JSON.parse(JSON.stringify(real));
  copy.jobs = [];
  for (let i = 0; i < 120; i++) copy.jobs.push(mk(i, false));

  const box = () => document.getElementById("jobs-body");
  const pagerText = () => { const p = box().querySelector(".table-pager"); return p ? p.textContent : null; };
  const bodyRows = () => Array.from(box().querySelectorAll(".table-wrap tbody tr"));
  const headCells = () => Array.from(box().querySelectorAll("thead tr:first-child th"));
  const head = (name) => headCells().find((th) => th.textContent === name);
  const colIdx = (name) => headCells().findIndex((th) => th.textContent === name);
  const colText = (name) => {
    const idx = colIdx(name);
    return bodyRows().map((r) => (r.cells[idx] ? r.cells[idx].textContent.trim() : null));
  };
  const filterInput = (name) => box().querySelector('.table-filters input[data-filter="' + name + '"]');
  const typeIn = (input, v) => { input.value = v; input.dispatchEvent(new Event("input")); };
  const stateOf = () => JSON.parse(JSON.stringify(L.TABLE_STATE));
  const nextBtn = () => Array.from(box().querySelectorAll(".pager-btn")).find((b) => b.textContent === "next");
  const prevBtn = () => Array.from(box().querySelectorAll(".pager-btn")).find((b) => b.textContent === "previous");
  const blanks = (a) => a.filter((v) => v === "" || v == null).length;

  // (a) 120 rows: the pager counts them, the body holds one page of 50
  L.render(copy);
  const a = { pager: pagerText(), bodyRows: bodyRows().length };

  // (b) click "turns": ascending, arrow + aria-sort on it; click again: descending
  head("turns").click();
  const asc = colText("turns");
  const b1 = { first: asc[0], last: asc[asc.length - 1], pager: pagerText(),
    thClass: head("turns").className, ariaSort: head("turns").getAttribute("aria-sort") };
  head("turns").click();
  const desc = colText("turns");
  const b2 = { first: desc[0], last: desc[desc.length - 1],
    thClass: head("turns").className, ariaSort: head("turns").getAttribute("aria-sort") };

  // (c) "done" in the status filter: only done pills, the count drops, page back to 1;
  //     then ">100" in turns: only rows over 100 turns
  typeIn(filterInput("status"), "done");
  const c1 = { pills: Array.from(new Set(bodyRows().map((r) => r.querySelector(".pill").textContent))),
    pager: pagerText(), page: L.TABLE_STATE.jobs.page };
  typeIn(filterInput("turns"), ">100");
  const over = colText("turns").map(Number);
  const c2 = { turns: over, allOver100: over.length > 0 && over.every((v) => v > 100),
    pager: pagerText(), page: L.TABLE_STATE.jobs.page };

  // (d) clear the filters, click next -> "51–100 of 120"; then a PUSH (a new rows array):
  //     the same sort, filters and page survive — they live in TABLE_STATE.jobs, not the DOM
  typeIn(filterInput("status"), "");
  typeIn(filterInput("turns"), "");
  Array.from(box().querySelectorAll(".pager-btn")).find((b) => b.textContent === "next").click();
  const d1 = { pager: pagerText(), page: L.TABLE_STATE.jobs.page };
  const copy2 = JSON.parse(JSON.stringify(real));
  copy2.jobs = [];
  for (let i = 0; i < 120; i++) copy2.jobs.push(mk(i, i === 0)); // the push: new objects, one field moved
  L.render(copy2);
  const afterPush = colText("turns");
  const d2 = { pager: pagerText(), bodyRows: bodyRows().length,
    first: afterPush[0], last: afterPush[afterPush.length - 1],
    state: stateOf().jobs, renders: Object.assign({}, L.renders) };

  // (f) F1 (fold of the page-batch review 2026-10-30): the filter matches the cell's TITLE as
  //     well as its text whenever the title adds characters. Two needles the old guard dropped
  //     (the title was a superset of the cell): the date string behind the started cell's
  //     clock ("10/31/2026" — every hand job's title carries the same day) and one row's full
  //     36-character UUID behind its 8-character short id.
  const cellAttr = (col, attr) => {
    const idx = colIdx(col), row = bodyRows()[0];
    return row && row.cells[idx] ? row.cells[idx].getAttribute(attr) : null;
  };
  // the matched count is the pager's total ("1–50 of 120"), never the page's 50
  const matched = () => {
    const p = pagerText() || "";
    let m = / of (\d+)/.exec(p);
    if (m) return +m[1];
    m = /^(\d+) rows?$/.exec(p);
    return m ? +m[1] : 0;
  };
  const startedDate = (cellAttr("started", "title") || "").split(",")[0];
  typeIn(filterInput("started"), startedDate);
  const fStarted = { date: startedDate, matched: matched(), rows: bodyRows().length, pager: pagerText() };
  typeIn(filterInput("started"), "");
  const fullId = copy2.jobs[0].id;
  typeIn(filterInput("job"), fullId);
  const fJob = { id: fullId, idLength: fullId.length, matched: matched(), rows: bodyRows().length,
    pager: pagerText(),
    text: bodyRows()[0] ? bodyRows()[0].cells[colIdx("job")].textContent.trim() : null,
    title: cellAttr("job", "title") };
  typeIn(filterInput("job"), "");
  const f = { started: fStarted, jobFullId: fJob };

  // (g) F2 (fold of the page-batch review 2026-10-30): a missing value sorts LAST in both
  //     directions. prefill tok/s (numeric; every fourth row NULL) descending and last
  //     activity (text; every ninth row empty) ascending: page 1 holds no blank either way,
  //     and the blanks sit at the end of the sorted list. Reads all three pages so `last` is
  //     the true last row, not the page's.
  const allPages = (name) => {
    while (L.TABLE_STATE.jobs.page > 1 && prevBtn()) prevBtn().click();
    const out = colText(name);
    let guard = 0;
    while (nextBtn() && !nextBtn().hasAttribute("disabled") && guard++ < 50) {
      nextBtn().click();
      out.push(...colText(name));
    }
    while (L.TABLE_STATE.jobs.page > 1 && prevBtn()) prevBtn().click();
    return out;
  };
  const sortRead = (vals) => ({
    first: vals[0], last: vals[vals.length - 1], rows: vals.length,
    blanksFirst50: blanks(vals.slice(0, 50)), blanksLast10: blanks(vals.slice(-10)),
    blanks: blanks(vals),
  });
  head("prefill tok/s").click(); // ascending
  head("prefill tok/s").click(); // descending
  const prefillAria = head("prefill tok/s").getAttribute("aria-sort");
  const prefillDesc = sortRead(allPages("prefill tok/s"));
  head("last activity").click(); // ascending
  const activityAria = head("last activity").getAttribute("aria-sort");
  const activityAsc = sortRead(allPages("last activity"));
  const g = {
    prefillDesc: Object.assign({ ariaSort: prefillAria }, prefillDesc),
    lastActivityAsc: Object.assign({ ariaSort: activityAria }, activityAsc),
  };

  // (e) the detail pages: the job page's requests table and the endpoint page's — filter row
  //     present, "duration" sortable (the cells' own ms figures in order, blanks last)
  const wait = async (sel, ms) => {
    const stop = Date.now() + (ms || 8000);
    while (Date.now() < stop) {
      const el = document.querySelector(sel);
      if (el) return el;
      await new Promise((r) => setTimeout(r, 100));
    }
    return null;
  };
  const durMs = (txt) => {
    const t = txt.replace(/,/g, "");
    if (/ ms$/.test(t)) return parseFloat(t);
    if (/ s$/.test(t)) return parseFloat(t) * 1000;
    return null;
  };
  // a sort or filter click REBUILDS the table element (the helper redraws its own container),
  // so every read re-queries the block — a held element reference detaches mid-sequence
  const probeBlock = (blockSel) => {
    const block = document.querySelector(blockSel);
    const tableEl = block.querySelector("table");
    const ths = Array.from(tableEl.querySelectorAll("thead tr:first-child th"));
    const idx = ths.findIndex((th) => th.textContent === "duration");
    const rows = Array.from(tableEl.querySelectorAll("tbody tr"));
    const vals = rows.map((r) => { const v = durMs(r.cells[idx].textContent.trim()); return v == null ? Infinity : v; });
    return {
      filterInputs: block.querySelectorAll(".table-filters input").length,
      rows: rows.length,
      first: rows[0].cells[idx].textContent.trim(),
      last: rows[rows.length - 1].cells[idx].textContent.trim(),
      sorted: vals.every((v, i) => i === 0 || vals[i - 1] <= v),
      ariaSort: ths[idx].getAttribute("aria-sort"),
      pager: (block.querySelector(".table-pager") || {}).textContent,
    };
  };
  const clickDuration = (blockSel) => {
    const th = Array.from(document.querySelectorAll(blockSel + " thead tr:first-child th"))
      .find((t) => t.textContent === "duration");
    th.click();
  };
  const e = { stateKeys: Object.keys(stateOf()) };
  const seededJob = (real.jobs || []).find((j) => j.status === "succeeded");
  location.hash = "#/job/" + encodeURIComponent(seededJob.id);
  if (await wait("#detail .job-requests .table-block")) {
    e.jobId = seededJob.id;
    // F3 (fold of the page-batch review 2026-10-30): the job page header is the one full-id
    // display — 36 characters, never the 8-character short form the tables and links draw
    const jobName = document.querySelector("#detail .job-name");
    e.jobName = jobName ? { text: jobName.textContent, length: jobName.textContent.length } : null;
    e.jobBefore = probeBlock("#detail .job-requests .table-block");
    clickDuration("#detail .job-requests .table-block");
    e.jobAfterDurationSort = probeBlock("#detail .job-requests .table-block");
    // a filter on the job page's table (the "path" column) narrows it in place
    const pathIn = document.querySelector('#detail .job-requests .table-filters input[data-filter="path"]');
    pathIn.value = "/v1/";
    pathIn.dispatchEvent(new Event("input"));
    e.jobFiltered = probeBlock("#detail .job-requests .table-block");
  } else e.jobTable = null;
  location.hash = "#/endpoint/ai1";
  if (await wait("#detail .requests .table-block")) {
    e.epBefore = probeBlock("#detail .requests .table-block");
    clickDuration("#detail .requests .table-block");
    e.epAfterDurationSort = probeBlock("#detail .requests .table-block");
  } else e.epTable = null;

  window.__d1 = { now: NOW, a, b1, b2, c1, c2, d1, d2, f, g, e };
  return "ok";
})();
