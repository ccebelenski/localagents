// 3.10c: every table pages, sorts and filters, client-side, and keeps the operator's view
// across pushes. 3.12a extended the filters by column kind (the status pick is a select, turns
// reads ranges and comparators, and a "clear filters" link ends the pager line); 3.12c gave the
// started column a two-level picker (button + panel of date/hour selects) — step (c) exercises
// those, (d) the link and the push, (f) keeps the F1 title-needle witness (the full job id). 3.12b adds (h): one field changed across a push — the wrap, the sideways scroll and the unchanged rows' elements all survive the patch — and (i): the identical rows again — level 1 skips the table without render skipping the snapshot. Runs over the seeded world on the overview (empty hash): the seeded snapshot
// holds two jobs, so a snapshot COPY with 120 jobs (varied status/started/elapsed/turns) is
// hand-rendered into #jobs-body — the same table the push draws. Reads (a)-(e), (h) and (i)
// into window.__d1; nothing awaits between the renders and the reads, so a real SSE push cannot
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
  // 3.12a §4: pick columns draw a native select; §7: "clear filters" ends the pager line
  const filterSel = (name) => box().querySelector('.table-filters select[data-filter="' + name + '"]');
  const setSel = (sel, v) => { sel.value = v; sel.dispatchEvent(new Event("change")); };
  const clearBtn = () => Array.from(box().querySelectorAll(".pager-btn")).find((b) => b.textContent === "clear filters");
  const optLabels = (name) => Array.from(filterSel(name).options).map((o) => o.textContent);
  // the matched count is the pager's total ("1–50 of 120"), never the page's 50
  const matched = () => {
    const p = pagerText() || "";
    let m = / of (\d+)/.exec(p);
    if (m) return +m[1];
    m = /^(\d+) rows?$/.exec(p);
    return m ? +m[1] : 0;
  };
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

  // (c) 3.12a §10: status is the pick — the option reads "done (40)"; setting it leaves only
  //     done pills, "1–40 of 40", page back to 1. ">100" in turns is unchanged (7 rows: the
  //     done rows with turns 101..119), and the status counts facet to "done (7)". Then
  //     "101-119" — §5's inclusive range — selects the SAME 7 rows. Then the started box
  //     (a time column): "11:00-11:30" (§6, inside the hand-made day) keeps the rows whose
  //     started_at falls in [11:00:00, 11:30:59] — the hand rows are noon - 60 i, so the
  //     expected count is computed from them, not hard-coded.
  const c0 = { options: optLabels("status") };
  setSel(filterSel("status"), "done");
  const c1 = { pills: Array.from(new Set(bodyRows().map((r) => r.querySelector(".pill").textContent))),
    pager: pagerText(), page: L.TABLE_STATE.jobs.page };
  typeIn(filterInput("turns"), ">100");
  const over = colText("turns").map(Number);
  const c2 = { turns: over, allOver100: over.length > 0 && over.every((v) => v > 100),
    pager: pagerText(), page: L.TABLE_STATE.jobs.page, statusOptions: optLabels("status") };
  typeIn(filterInput("turns"), "101-119");
  const ranged = colText("turns").map(Number);
  const c2b = { turns: ranged, sameSeven: JSON.stringify(ranged) === JSON.stringify(over),
    pager: pagerText() };
  typeIn(filterInput("turns"), ""); // a blank box deletes the key (3.12a §7)
  setSel(filterSel("status"), ""); // "any" deletes the key
  // 3.12d: the elapsed box reads the unit its CELLS print — "<10m" is ten MINUTES, not ten
  // seconds. The expected count is computed from the hand rows' elapsed_s under 600, as (c)
  // computes its own expectations (not hard-coded); the box is cleared after.
  const underTenM = copy.jobs.filter((j) => j.elapsed_s < 600).length;
  typeIn(filterInput("elapsed"), "<10m");
  const c2c = { under: underTenM, matched: matched(), pager: pagerText() };
  typeIn(filterInput("elapsed"), "");
  // 3.12c: started is the two-level PICKER, not a box. The filter cell's control: the button
  // and the cell's widths read unset. Open the panel — its parent is the block, its rect
  // inside the block's and its top at or below the wrap's bottom (a child of the wrapper
  // would be clipped or scroll it, spec §5/§7; the three rects go with the booleans).
  // Choose the "today" option (every hand row sits on NOW's local day -> all 120), then from
  // "11" and to "11" -> the 11-o'clock rows (expected computed from the hand rows' local
  // hours, as (c) computes its own), then the panel's "clear" -> 120 and the state's key
  // gone, then "done" closes. The summary and the widths read with the control set.
  const timeTh = box().querySelector('.table-filters th[data-filter-kind="time"]');
  const startedBtn = box().querySelector('button.filter-pick[data-filter="started"]');
  if (!startedBtn) { window.__d1 = { error: "no started filter-pick button" }; return; }
  const thUnset = timeTh.getBoundingClientRect().width;
  const btnUnset = startedBtn.getBoundingClientRect().width;
  startedBtn.click(); // open
  const panel = box().querySelector(".filter-panel");
  const blockRect = box().querySelector(".table-block").getBoundingClientRect();
  const wrapRect = box().querySelector(".table-wrap").getBoundingClientRect();
  const panelRect = panel ? panel.getBoundingClientRect() : null;
  const hourRows = copy.jobs.filter((j) => new Date(j.started_at * 1000).getHours() === 11).length;
  const dateSel = panel.querySelector("select.fp-date");
  const todayOpt = Array.from(dateSel.options).find((o) => o.textContent.indexOf("today") === 0);
  setSel(dateSel, todayOpt.value);
  const c3date = { label: todayOpt.textContent, value: todayOpt.value, matched: matched() };
  setSel(panel.querySelector("select.fp-from"), "11");
  setSel(panel.querySelector("select.fp-to"), "11");
  const c3 = {
    window: hourRows, matched: matched(), pager: pagerText(),
    filters: Object.assign({}, L.TABLE_STATE.jobs.filters),
    summary: startedBtn.textContent,
    date: c3date,
    panel: {
      parent: panel ? panel.parentNode.className : null,
      insideBlock: !!panelRect && panelRect.left >= blockRect.left - 1 &&
        panelRect.right <= blockRect.right + 1 && panelRect.top >= blockRect.top - 1 &&
        panelRect.bottom <= blockRect.bottom + 1,
      // the panel hangs UNDER its header cell, so it overlaps the wrapper's area — the point
      // of the parentage is that it is not a child of .table-wrap (no clip, and the wrapper
      // never gains scroll from it: scrollWidth == clientWidth here), and it stays inside the
      // block's box. The three rects are reported with the booleans.
      belowWrapTop: !!panelRect && panelRect.top >= wrapRect.top - 1,
      wrapDoesNotScroll: box().querySelector(".table-wrap").scrollWidth ===
        box().querySelector(".table-wrap").clientWidth,
      block: { left: blockRect.left, top: blockRect.top, right: blockRect.right, bottom: blockRect.bottom },
      wrap: { left: wrapRect.left, top: wrapRect.top, right: wrapRect.right, bottom: wrapRect.bottom },
      panel: panelRect ? { left: panelRect.left, top: panelRect.top,
        right: panelRect.right, bottom: panelRect.bottom } : null,
    },
    widths: { th: thUnset, btn: btnUnset },
  };
  c3.widthsSet = { th: timeTh.getBoundingClientRect().width,
    btn: startedBtn.getBoundingClientRect().width,
    // no ellipsis, no clipping: the set summary fits the button it is written in (3.7u)
    btnScroll: startedBtn.scrollWidth, btnClient: startedBtn.clientWidth,
    summary: startedBtn.textContent };
  Array.from(panel.querySelectorAll(".pager-btn")).find((b) => b.textContent === "clear").click();
  c3.afterClear = { matched: matched(), keyGone: !("started" in L.TABLE_STATE.jobs.filters),
    summary: startedBtn.textContent };
  Array.from(panel.querySelectorAll(".pager-btn")).find((b) => b.textContent === "done").click();
  c3.closed = box().querySelector(".filter-panel") === null;

  // (d) 3.12a §7: "clear filters" ends the pager line whenever a filter is set — the picker's
  //     own clear already emptied the state in (c), so the link is guarded; then a PUSH (a new
  //     rows array): the same sort and page survive — they live in TABLE_STATE.jobs, not the DOM
  const dClear = clearBtn();
  if (dClear) dClear.click();
  const d0 = { pager: pagerText(), filters: Object.keys(L.TABLE_STATE.jobs.filters) };
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

  // (h) 3.12b level 2: the patch keeps the DOM. Mark the current wrap, scroll it sideways, and
  //     push again with ONE job's num_turns changed (a mid-page row, so a neighbour swap cannot
  //     move anything across the page edge): the marked wrap is still the box's wrap and still
  //     scrolled (headless Chromium may clamp scrollLeft to 0 if the table is not wider than
  //     the box — scrollWidth/clientWidth are read with it and what was seen is reported), the
  //     changed row's tr is a new element reading the new figure, and an unchanged row keeps
  //     its exact tr element.
  const wrapEl = box().querySelector(".table-wrap");
  wrapEl.__mark = 1;
  wrapEl.scrollLeft = 40;
  const rowsH = bodyRows();
  const turnsIdxH = colIdx("turns");
  const changedId = rowsH[20].getAttribute("data-job");
  const oldChangedTr = rowsH[20];
  const keptTr = rowsH[0];
  keptTr.__trmark = 1;
  const copyH = JSON.parse(JSON.stringify(real));
  copyH.jobs = copy2.jobs.map((j) => {
    const c = Object.assign({}, j);
    if (c.id === changedId) c.num_turns += 1;
    return c;
  });
  L.render(copyH);
  const hRows = bodyRows();
  const changedTr = hRows.find((r) => r.getAttribute("data-job") === changedId);
  const wantTurns = String(copyH.jobs.find((j) => j.id === changedId).num_turns);
  const h = {
    mark: wrapEl.__mark, sameWrap: box().querySelector(".table-wrap") === wrapEl,
    scrollLeft: wrapEl.scrollLeft, scrollWidth: wrapEl.scrollWidth, clientWidth: wrapEl.clientWidth,
    changedTrNew: !!changedTr && changedTr !== oldChangedTr,
    newTurns: changedTr ? changedTr.cells[turnsIdxH].textContent.trim() : null,
    wantTurns: wantTurns,
    keptTrStillThere: hRows.indexOf(keptTr) >= 0,
    keptMark: keptTr.__trmark === 1,
    pager: pagerText(),
  };
  h.ok = h.mark === 1 && h.sameWrap && h.changedTrNew && h.newTurns === h.wantTurns &&
    h.keptTrStillThere && h.keptMark;

  // (i) 3.12b level 1: the SAME rows again. A deep copy whose only change is OUTSIDE the table
  //     (window_s, which render's own snapSig reads but the jobs table never shows): render
  //     proceeds past its snapshot skip, the table's row hash matches, and
  //     renders.tableSkipped advances by exactly one with the marked wrap untouched.
  const skipBefore = L.renders.tableSkipped;
  const copyI = JSON.parse(JSON.stringify(copyH));
  copyI.window_s = copyH.window_s + 1;
  L.render(copyI);
  const i = {
    tableSkipped: L.renders.tableSkipped - skipBefore,
    mark: wrapEl.__mark,
    sameWrap: box().querySelector(".table-wrap") === wrapEl,
    pager: pagerText(),
  };
  i.ok = i.tableSkipped === 1 && i.mark === 1 && i.sameWrap;

  // (f) F1 (fold of the page-batch review 2026-10-30): the filter matches the cell's TITLE as
  //     well as its text whenever the title adds characters — one row's full 36-character UUID
  //     behind its 8-character short id. (The F1 family's other needle, the date string behind
  //     the started cell's clock, went with the started box in 3.12c: the picker's date level
  //     does that job now, witnessed by c3.date.)
  const cellAttr = (col, attr) => {
    const idx = colIdx(col), row = bodyRows()[0];
    return row && row.cells[idx] ? row.cells[idx].getAttribute(attr) : null;
  };
  // F4 (3.12a fold): the id cell is a td holding the jobLink `a`; the full uuid is that
  // CHILD's title, not the td's (the old read was null in every d1)
  const childTitle = (col, row) => {
    const idx = colIdx(col), cell = row ? row.cells[idx] : null;
    const a = cell ? cell.querySelector("a") : null;
    return a ? a.getAttribute("title") : null;
  };
  const fullId = copy2.jobs[0].id;
  typeIn(filterInput("job"), fullId);
  const jobRow = bodyRows()[0];
  const fJob = { id: fullId, idLength: fullId.length, matched: matched(), rows: bodyRows().length,
    pager: pagerText(),
    text: jobRow ? jobRow.cells[colIdx("job")].textContent.trim() : null,
    title: childTitle("job", jobRow),
    tdTitle: cellAttr("job", "title") }; // the old read: null, the witness's whole defect
  fJob.ok = fJob.title === fullId; // F4: the witness must BE the full id, never null
  typeIn(filterInput("job"), "");
  const f = { jobFullId: fJob, ok: fJob.ok };

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
  // 3.12b: a sort or filter click PATCHES the table in place, but every read re-queries the
  // block anyway — re-querying always works
  const probeBlock = (blockSel) => {
    const block = document.querySelector(blockSel);
    const tableEl = block.querySelector("table");
    const ths = Array.from(tableEl.querySelectorAll("thead tr:first-child th"));
    const idx = ths.findIndex((th) => th.textContent === "duration");
    const rows = Array.from(tableEl.querySelectorAll("tbody tr"));
    const vals = rows.map((r) => { const v = durMs(r.cells[idx].textContent.trim()); return v == null ? Infinity : v; });
    return {
      filterInputs: block.querySelectorAll(".table-filters input").length,
      // 3.12a: the pick columns (status, path) draw selects in the same row
      filterSelects: block.querySelectorAll(".table-filters select").length,
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
    // a filter on the job page's table narrows it in place — 3.12a §4: "path" is a pick now,
    // so this picks one of the listed paths and keeps every row of that path
    const blockSel = "#detail .job-requests .table-block";
    const pathSel = document.querySelector(blockSel + ' .table-filters select[data-filter="path"]');
    const pathValue = pathSel.options[1] ? pathSel.options[1].value : "";
    pathSel.value = pathValue;
    pathSel.dispatchEvent(new Event("change"));
    e.jobFiltered = probeBlock(blockSel);
    e.jobFiltered.pathPicked = pathValue; // the pager total ("N of M") is the narrowing witness
  } else e.jobTable = null;
  location.hash = "#/endpoint/ai1";
  if (await wait("#detail .requests .table-block")) {
    e.epBefore = probeBlock("#detail .requests .table-block");
    clickDuration("#detail .requests .table-block");
    e.epAfterDurationSort = probeBlock("#detail .requests .table-block");
  } else e.epTable = null;

  window.__d1 = { now: NOW, a, b1, b2, c0, c1, c2, c2b, c2c, c3, d0, d1, d2, h, i, f, g, e };
  // F4: the witness is asserted. d1 lands before the throw, so a failed run still hands back
  // the JSON (dom.js prints SCRIPT_THREW; cdp.mjs's d1 carries f.ok false). 3.12b: h and i
  // assert the patch identity and the level-1 skip the same way.
  if (!f.ok) throw new Error("f.jobFullId.title " + JSON.stringify(f.jobFullId.title) + " is not the full id");
  if (!h.ok) throw new Error("3.12b h witness failed: " + JSON.stringify(h));
  if (!i.ok) throw new Error("3.12b i witness failed: " + JSON.stringify(i));
  return "ok";
})();
