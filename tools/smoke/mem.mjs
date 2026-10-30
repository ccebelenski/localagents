// usage: node tools/smoke/mem.mjs <devtools-port> <url> <seconds> [every_s=5]
// one line per sample: DOM nodes, JS listeners, documents, heap MB (after a GC), the page's render-pass count
const [port, url, secs, every = "5"] = process.argv.slice(2);
const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let id = 0; const pending = new Map();
ws.addEventListener("message", (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PUSHES = "(() => { const r = window.localagentsMonitor && window.localagentsMonitor.renders; return r ? r.full + r.inPlace + r.skipped : -1; })()";
const sample = async (gc) => {
  if (gc) await send("HeapProfiler.collectGarbage");
  const m = (await send("Performance.getMetrics")).result.metrics;
  const get = (n) => m.find((x) => x.name === n).value;
  const p = (await send("Runtime.evaluate", { expression: PUSHES, returnByValue: true })).result.result.value;
  const row = [new Date().toISOString(), get("Nodes"), get("JSEventListeners"), get("Documents"), (get("JSHeapUsedSize") / 1048576).toFixed(1), p];
  console.log(row.join("\t"));
};
await send("Page.enable"); await send("HeapProfiler.enable"); await send("Page.navigate", { url });
await sleep(2000); await send("Performance.enable");
console.log(["time", "nodes", "listeners", "documents", "heapMB", "pushes"].join("\t"));
await sample(true);
const n = Math.floor(+secs / +every);
for (let i = 1; i <= n; i++) { await sleep(+every * 1000); await sample(i === n); }
await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`);
ws.close(); process.exit(0);
