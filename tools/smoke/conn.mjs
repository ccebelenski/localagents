// usage: node conn.mjs <devtools-port> <url> <seconds>: print the connection label every second
const [port, url, secs] = process.argv.slice(2);
const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let id = 0; const pending = new Map();
ws.addEventListener("message", (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await send("Page.enable");
await send("Page.navigate", { url });
let last = "";
for (let i = 0; i < +secs; i++) {
  await sleep(1000);
  const r = await send("Runtime.evaluate", { expression: "document.getElementById('conn').className + ' / ' + document.getElementById('conn-label').textContent + ' / ' + document.getElementById('updated').textContent + ' / banner=' + (document.getElementById('banner').hidden ? '' : document.getElementById('banner').textContent)", returnByValue: true });
  const v = r.result.result.value;
  if (v !== last) { console.log(new Date().toISOString().slice(11, 19), v); last = v; }
}
await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`);
process.exit(0);
