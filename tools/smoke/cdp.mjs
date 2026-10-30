// Drive an already-running headless Chromium over the DevTools protocol with Node's built-in
// WebSocket (no packages). usage: node cdp.mjs <devtools-port> <url> <width> <height> <png> [hash-after]
import { writeFileSync } from "node:fs";

const [port, url, width, height, png, hashAfter] = process.argv.slice(2);
const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let id = 0;
const pending = new Map();
const logs = [];
ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.exceptionThrown") logs.push("EXCEPTION " + JSON.stringify(m.params.exceptionDetails));
  if (m.method === "Runtime.consoleAPICalled") logs.push("console." + m.params.type + " " + m.params.args.map((a) => a.value ?? a.description).join(" "));
  if (m.method === "Log.entryAdded") logs.push("log " + m.params.entry.level + " " + m.params.entry.text + " " + (m.params.entry.url || ""));
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: +width, height: +height, deviceScaleFactor: 1, mobile: +width < 600 });
await send("Page.navigate", { url });
await sleep(4000);

const probe = `(() => {
  const q = (s) => Array.from(document.querySelectorAll(s));
  return JSON.stringify({
    hosts: q('.host').map(e => ({ host: e.dataset.host, class: e.className, note: (e.querySelector('.host-note')||{}).textContent || null, gridColumn: getComputedStyle(e).gridColumn })),
    endpoints: q('.endpoint').map(e => ({ endpoint: e.dataset.endpoint, class: e.className, text: e.textContent.replace(/\\s+/g,' ').trim().slice(0, 400) })),
    jobRows: q('.jobs tbody tr').map(r => Array.from(r.cells).map(c => c.textContent.trim())),
    jobsEmpty: (document.querySelector('.jobs .empty')||{}).textContent || null,
    jobHeaders: q('.jobs thead th').map(e => e.textContent),
    // 3.7k: the shared requests table's headers, first three rows and pills
    reqHeaders: q('.requests-table thead th').map(e => e.textContent),
    reqRows: q('.requests-table tbody tr').slice(0, 3).map(r => Array.from(r.cells).map(c => c.textContent.trim())),
    reqPills: q('.requests-table .pill').slice(0, 5).map(e => e.className + ':' + e.textContent),
    reqRowClasses: q('.requests-table tbody tr').slice(0, 5).map(r => r.className),
    // 3.7l: the fleet charts are gone — the canvas fields stay, and their zeros are the proof
    canvases: q('.fleet canvas').length,
    chartTitles: q('.fleet .chart h3').map(e => e.textContent),
    legends: q('.fleet .chart').map(c => Array.from(c.querySelectorAll('.u-legend .u-series th')).map(t => t.textContent.trim())),
    // the timeline: one row per live endpoint (label, band count, job bars with their inline
    // geometry), the axis ticks, the heading's window, and the section's rendered height
    tlTitle: document.getElementById('fleet-title').textContent,
    tlHeight: (document.getElementById('timeline') || {}).offsetHeight,
    tlRows: q('.tl-row').map(r => ({
      label: r.querySelector('.tl-label').textContent,
      bands: r.querySelectorAll('.tl-band').length,
      jobs: Array.from(r.querySelectorAll('.tl-job')).map(j => ({
        text: j.textContent, left: j.style.left, width: j.style.width,
        href: j.getAttribute('href'), title: j.getAttribute('title'),
        top: j.style.top || getComputedStyle(j).top, cls: j.className,
      })),
    })),
    tlAxis: q('.tl-axis span').map(e => e.textContent),
    tlGrid: q('.tl-grid').length, tlNow: q('.tl-now').length,
    // the clock the drawn percentages rest on: the snapshot the last timeline draw used
    snapNow: localagentsMonitor.timeline.lastNow,
    rangeButtons: q('.range button').map(b => b.textContent + (b.getAttribute('aria-pressed') === 'true' ? '*' : '')),
    conn: document.getElementById('conn').className + ' / ' + document.getElementById('conn-label').textContent,
    hiddenLine: (document.getElementById('hosts-hidden')||{}).textContent || null,
    meta: document.getElementById('meta').textContent,
    // 3.7r: #updated is gone from the header (the phrase moved into #conn-label); the field
    // stays valid by reading the new text
    updated: document.getElementById('conn-label').textContent,
    banner: document.getElementById('banner').hidden ? null : document.getElementById('banner').textContent,
    strips: q('.strip').map(s => s.children.length),
    stripNow: q('.strip-now').map(s => s.textContent),
    sparklines: q('.fig svg path').length,
    // the live endpoint card (stories 3.7d-f): the twelve tile labels per live card, the jobs
    // block text, the sparkline runs (the decode tile's SVG is polyline+polygon now, so the
    // path count above reads 0 by design), and the state dots with their variants
    tiles: q('.endpoint.state-live').map(e => Array.from(e.querySelectorAll('.fig .label')).map(l => l.textContent)),
    jobsBlocks: q('.ep-jobs').map(e => e.textContent.replace(/\\s+/g,' ').trim()),
    sparkPolylines: q('.fig svg polyline').length,
    sdots: q('.sdot').map(e => e.className),
    // 3.7n height invariant: a live card's rendered height; two hand-rendered variants of the
    // same snapshot (shortest/longest values) must report the same height here
    cardHeights: q('.endpoint.state-live').map(e => ({ endpoint: e.dataset.endpoint, h: e.getBoundingClientRect().height })),
    // 3.7-fold-1 (3.7n/o/q): the twelve tile labels per live card (units in the labels now,
    // the decode label naming the window), the shared-scale peak labels, the strip rows'
    // range words, each card's sparkline runs over its own history, the job rows' row form,
    // and the sparklines' aria labels (own in-range peak) and drawn points (the shared scale)
    tileLabels: q('.endpoint.state-live').map(e => Array.from(e.querySelectorAll('.fig .label')).map(l => l.textContent)),
    sparkPeaks: q('.spark-peak').map(e => e.textContent),
    stripRanges: q('.strip-range').map(e => e.textContent),
    sparkRuns: q('.endpoint.state-live').map(e => ({ polylines: e.querySelectorAll('.fig svg polyline').length, circles: e.querySelectorAll('.fig svg circle').length })),
    sparkAria: q('.endpoint.state-live .spark-wrap svg').map(s => s.getAttribute('aria-label')),
    sparkPoints: q('.endpoint.state-live .spark-wrap svg polyline').map(p => p.getAttribute('points')),
    // (not "jobRows": the table's row field above already owns that key — a duplicate in an
    // object literal silently shadows it)
    epJobRows: q('.ep-job').map(e => e.textContent.replace(/\\s+/g, ' ').trim()),
    // 3.7-fold-5: the card's job row as its two fixed lines — the figures line, the activity
    // line (empty when the job has none), and the row's rendered height (the no-jump witness:
    // two lines whatever the job carries)
    epJobLines: q('.ep-job').map(e => ({ figs: (e.querySelector('.ep-job-figs')||{}).textContent, act: (e.querySelector('.ep-act')||{}).textContent, h: e.getBoundingClientRect().height })),
    // 3.7g-j (3.7g/3.7i): the Recent jobs log's status pills (class + word) and the
    // tool-errors badge links (href, so a broken route shows in the data, not the pixels)
    pills: q('.jobs .pill').map(e => e.className + ':' + e.textContent),
    errorLinks: q('.jobs a.badge, .jobs .badge a').map(a => a.getAttribute('href')),
    jobsNote: (document.querySelector('.jobs-note')||{}).textContent || null,
    detail: document.getElementById('detail').hidden ? null : document.getElementById('detail').textContent.replace(/\\s+/g,' ').trim(),
    overviewHidden: document.getElementById('overview').hidden,
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
    // 3.7-fold-3 + 3.7r: the render counters; the last-active texts on live heads; the one
    // header phrase; and a stamp on the first live endpoint element — re-probing keeps the
    // same number only while the page keeps that very DOM node (in-place updates), a rebuilt
    // card loses the expando (it is a property, never an attribute, so the HTML compares miss
    // it by design)
    renders: localagentsMonitor.renders,
    activeTexts: q('.ep-active').map(e => e.textContent),
    connText: document.getElementById('conn-label').textContent,
    cardNodeId: (function () { var e = document.querySelector('.endpoint.state-live'); if (!e.__id) e.__id = Math.random(); return e.__id; })(),
    // 3.7-fold-5: heights captured synchronously by a hand-rendering js: script (the next
    // SSE push overwrites the hand-edited DOM before this probe reads it, so the script
    // measures as it goes and leaves the numbers here)
    t5: (typeof window !== "undefined" && window.__t5) || null,
    // 3.7u: no card text hidden. Every one-line card element with its scroll and client widths
    // (scrollWidth > clientWidth is text the element cannot show at its width), its text length
    // and whether its text ends in an ellipsis the page added; the overflowing ones by class;
    // the grid's column floor (the page's high-water mark, a pixel number, null on a page
    // without one) beside the #hosts width it is capped at, the column tracks it resolves to,
    // and every card's rendered box
    cardFit: q('.host .ep-head, .host .ep-model, .host .ep-active, .host .ep-job-figs, .host .ep-act, .host .figures .fig .label, .host .figures .fig .num')
      .map(e => ({ cls: e.className, sw: e.scrollWidth, cw: e.clientWidth, len: e.textContent.length, ell: /\u2026\s*$/.test(e.textContent) })),
    cardOverflow: q('.host .ep-head, .host .ep-model, .host .ep-active, .host .ep-job-figs, .host .ep-act, .host .figures .fig .label, .host .figures .fig .num')
      .filter(e => e.scrollWidth > e.clientWidth).map(e => e.className + ' ' + e.scrollWidth + '>' + e.clientWidth),
    cardFloor: (localagentsMonitor.cardLayout || {}).floor == null ? null : localagentsMonitor.cardLayout.floor,
    hostsWidth: document.getElementById('hosts').clientWidth,
    gridCols: getComputedStyle(document.getElementById('hosts')).gridTemplateColumns,
    columns: getComputedStyle(document.getElementById('hosts')).gridTemplateColumns.split(' ').length,
    hostBoxes: q('.host').map(e => ({ host: e.dataset.host, w: e.getBoundingClientRect().width, h: e.getBoundingClientRect().height })),
    // 3.7u: the measurements a hand-rendering js: script (wide.js) takes as it goes
    u: (typeof window !== "undefined" && window.__u) || null,
    // 3.7u-fix (U1/U2): the long-line measurements from lines.js (seeded vs the hand-edited
    // snapshot with a 200-char dormant name, a 300-char down error and a 200-char dropped job)
    lines: (typeof window !== "undefined" && window.__lines) || null,
  });
})()`;
let r = await send("Runtime.evaluate", { expression: probe, returnByValue: true });
console.log(JSON.stringify(JSON.parse(r.result.result.value), null, 1));
const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
writeFileSync(png, Buffer.from(shot.result.data, "base64"));
if (hashAfter) {
  const expr = hashAfter.startsWith("js:") ? hashAfter.slice(3) : `location.hash = ${JSON.stringify(hashAfter)}`;
  await send("Runtime.evaluate", { expression: expr });
  await sleep(2500);
  r = await send("Runtime.evaluate", { expression: probe, returnByValue: true });
  const o = JSON.parse(r.result.result.value);
  console.log("AFTER " + hashAfter + ": detail=" + JSON.stringify(o.detail) + " overviewHidden=" + o.overviewHidden + " range=" + o.rangeButtons + " canvases=" + o.canvases + " strips=" + o.strips + " sparklines=" + o.sparklines + " banner=" + o.banner);
  // the hand-rendered states (js: hash-after) are read through the log's fields: the summary
  // line above long predates the Recent jobs log and cannot carry them
  // 3.7l: the timeline fields ride along so a `js:` range switch (range.set('15m')) is read
  // 3.7-fold-5: the two-line job row's fields and the hand-measured heights ride along too
  // (the first probe long predates the js: script that captures them)
  console.log("AFTER_JSON " + JSON.stringify({ jobRows: o.jobRows, jobHeaders: o.jobHeaders, pills: o.pills, errorLinks: o.errorLinks, jobsEmpty: o.jobsEmpty, jobsNote: o.jobsNote, tlTitle: o.tlTitle, tlAxis: o.tlAxis, tlRows: o.tlRows.map(r => ({ label: r.label, bands: r.bands, jobs: r.jobs.length })), tlHeight: o.tlHeight, epJobLines: o.epJobLines, cardHeights: o.cardHeights, tileLabels: o.tileLabels, t5: o.t5,
    cardOverflow: o.cardOverflow, cardFloor: o.cardFloor, hostsWidth: o.hostsWidth, gridCols: o.gridCols, columns: o.columns, hostBoxes: o.hostBoxes, u: o.u, lines: o.lines }));
  console.log("AFTER " + hashAfter + ": detail=" + JSON.stringify(o.detail) + " overviewHidden=" + o.overviewHidden + " range=" + o.rangeButtons + " canvases=" + o.canvases + " strips=" + o.strips + " sparklines=" + o.sparklines + " banner=" + o.banner
    + " reqHeaders=" + JSON.stringify(o.reqHeaders) + " reqRows=" + JSON.stringify(o.reqRows)
    + " reqPills=" + JSON.stringify(o.reqPills) + " reqRowClasses=" + JSON.stringify(o.reqRowClasses));
  if (process.argv[8]) writeFileSync(process.argv[8], Buffer.from((await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true })).result.data, "base64"));
}
console.log("LOGS " + JSON.stringify(logs, null, 1));
await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`);
ws.close();
process.exit(0);
