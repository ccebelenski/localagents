// 3.7u-fix P-1: the column floor across window sizes. Opens the overview at the first width, then
// resizes through the rest, waiting `settle_s` (several snapshot pushes: the floor is measured
// after every render, there is no resize listener) before each sample, and evaluates floor.js at
// each step: the page's floor beside the text's need measured independently, the grid's columns.
// The floor is a high-water mark of TEXT: it must hold at the widest line's need whatever the
// window did, and the columns must come back when the window does.
// usage: node tools/smoke/floor.mjs <devtools-port> <url> [widths=2200,2000,2200,1400,2200] [settle_s=3]
// prints one JSON line per step and a SUMMARY line; exits 1 if any step's floorIsText or
// needIsText is false.
import { readFileSync } from "node:fs";

const [port, url, widthsArg = "2200,2000,2200,1400,2200", settleArg = "3"] = process.argv.slice(2);
const widths = widthsArg.split(",").map(Number);
const settle = +settleArg * 1000;
const probe = readFileSync(new URL("./floor.js", import.meta.url), "utf8");
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
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const size = (w) => send("Emulation.setDeviceMetricsOverride", { width: w, height: 900, deviceScaleFactor: 1, mobile: false });

await send("Runtime.enable");
await send("Page.enable");
await size(widths[0]);
await send("Page.navigate", { url });
await sleep(4000);
const steps = [];
for (let i = 0; i < widths.length; i++) {
  if (i > 0) { await size(widths[i]); await sleep(settle); }
  const r = await send("Runtime.evaluate", { expression: probe, returnByValue: true });
  const o = JSON.parse(r.result.result.value);
  steps.push(o);
  console.log(JSON.stringify(o));
}
const ok = steps.every((s) => s.floorIsText && s.needIsText);
console.log("SUMMARY " + JSON.stringify({ ok, steps: steps.map((s) => `${s.w}: floor ${s.floor} need ${s.need} text ${s.oracle} cols ${s.columns}`), logs }));
await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`);
ws.close();
process.exit(ok ? 0 : 1);
