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
  // other field varies so no two rows are alike
  const mk = (i, bump) => ({
    id: "tj" + i, status: i % 3 === 0 ? "failed" : i % 3 === 1 ? "running" : "succeeded",
    server_live: true, started_at: NOW - 60 * i, model: "m" + (i % 4),
    endpoint: i % 2 ? "ai1" : "dgx1-a", elapsed_s: i * 7 + (bump ? 1 : 0), num_turns: i % 130,
    tool_calls: i % 10, errors: i % 5, compactions: 0, ctx_pct: null,
    prefill_tps: null, gen_tps: null, last_activity: "act " + i,
  });
  const copy = JSON.parse(JSON.stringify(real));
  copy.jobs = [];
  for (let i = 0; i < 120; i++) copy.jobs.push(mk(i, false));

  const box = () => document.getElementById("jobs-body");
  const pagerText = () => { const p = box().querySelector(".table-pager"); return p ? p.textContent : null; };
  const bodyRows = () => Array.from(box().querySelectorAll(".table-wrap tbody tr"));
  const headCells = () => Array.from(box().querySelectorAll("thead tr:first-child th"));
  const head = (name) => headCells().find((th) => th.textContent === name);
  const colText = (name) => {
    const idx = headCells().findIndex((th) => th.textContent === name);
    return bodyRows().map((r) => (r.cells[idx] ? r.cells[idx].textContent.trim() : null));
  };
  const filterInput = (name) => box().querySelector('.table-filters input[data-filter="' + name + '"]');
  const typeIn = (input, v) => { input.value = v; input.dispatchEvent(new Event("input")); };
  const stateOf = () => JSON.parse(JSON.stringify(L.TABLE_STATE));

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

  window.__d1 = { now: NOW, a, b1, b2, c1, c2, d1, d2, e };
  return "ok";
})();
