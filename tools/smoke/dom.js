// A self-contained CDP driver (Node's built-in WebSocket, no packages): navigate to <url>, poll
// until the page is ready (localagentsMonitor is an object and the overview has drawn an
// endpoint card), evaluate the committed smoke script <script-file> (which may switch the hash
// and await its own witnesses), then print window.__d1 (the script's d1 field) as JSON.
//
// Where cdp.mjs's fixed 4000 ms settle is not enough for the first render (the page's card or
// timeline is still drawing, so cdp.mjs's initial probe throws), this driver polls for the real
// readiness instead. usage: node dom.js <devtools-port> <url> <script-file>
import { readFileSync } from "node:fs";

const [port, url, script] = process.argv.slice(2);
if (!port || !url || !script) {
  console.error("usage: node dom.js <devtools-port> <url> <script-file>");
  process.exit(2);
}
const src = readFileSync(script, "utf8");
const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let id = 0;
const pending = new Map();
ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url });

// the real readiness: app.js has loaded localagentsMonitor and the overview has drawn a card
// (the page's router and the shared renderer are live). Poll up to 20 s instead of a fixed
// sleep, so a slow first render does not throw in a probe.
const readyExpr = "typeof localagentsMonitor === 'object' && !!document.querySelector('.endpoint')";
let ready = false;
for (let t = 0; t < 40 && !ready; t++) {
  await sleep(500);
  const r = await send("Runtime.evaluate", { expression: readyExpr, returnByValue: true });
  ready = !!(r.result && r.result.result && r.result.result.value === true);
}
if (!ready) {
  console.log("D1 " + JSON.stringify({ threw: "page did not become ready (no endpoint card in 20 s)", ok: false }));
  process.exit(1);
}
// run the committed smoke script; it may switch the hash and await its own witnesses
const ev = await send("Runtime.evaluate", { expression: src, awaitPromise: true, returnByValue: true });
await sleep(500); // let the __d1 write land
const d1 = await send("Runtime.evaluate", { expression: "JSON.stringify(window.__d1)", returnByValue: true });
const d1val = d1.result && d1.result.result ? d1.result.result.value : null;
if (ev.result && ev.result.exceptionDetails) {
  console.log("SCRIPT_THREW " + JSON.stringify(ev.result.exceptionDetails));
}
console.log("D1 " + (d1val || "null"));
await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`);
ws.close();
process.exit(0);
