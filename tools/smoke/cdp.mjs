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
    canvases: q('.fleet canvas').length,
    chartTitles: q('.fleet .chart h3').map(e => e.textContent),
    legends: q('.fleet .chart').map(c => Array.from(c.querySelectorAll('.u-legend .u-series th')).map(t => t.textContent.trim())),
    rangeButtons: q('.range button').map(b => b.textContent + (b.getAttribute('aria-pressed') === 'true' ? '*' : '')),
    conn: document.getElementById('conn').className + ' / ' + document.getElementById('conn-label').textContent,
    hiddenLine: (document.getElementById('hosts-hidden')||{}).textContent || null,
    meta: document.getElementById('meta').textContent,
    updated: document.getElementById('updated').textContent,
    banner: document.getElementById('banner').hidden ? null : document.getElementById('banner').textContent,
    strips: q('.strip').map(s => s.children.length),
    stripNow: q('.strip-now').map(s => s.textContent),
    sparklines: q('.fig svg path').length,
    detail: document.getElementById('detail').hidden ? null : document.getElementById('detail').textContent.replace(/\\s+/g,' ').trim(),
    overviewHidden: document.getElementById('overview').hidden,
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
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
  if (process.argv[8]) writeFileSync(process.argv[8], Buffer.from((await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true })).result.data, "base64"));
}
console.log("LOGS " + JSON.stringify(logs, null, 1));
await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`);
ws.close();
process.exit(0);
