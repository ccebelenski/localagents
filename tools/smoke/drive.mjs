// Hold a page open for N seconds and report what changed — the time axis cdp.mjs's fixed
// 2500 ms after-sleep cannot see (adopted from the 3.9D review's probe 4). Sequential CDP
// round-trips with node-side waits: open the shell, arm a fetch wrapper, switch to the hash,
// then sample every 2 s for N seconds. The report: the snapshot clock and render counters,
// the clock-relative phrases (the endpoint page's "last seen … ago", the job page's elapsed),
// a hash of the latency tile (its sparkline slides when the 30 s refetch lands), and every
// /api/ fetch with its offset from arming. JSON to [out.json] (default t/drive.json), a
// summary line to stdout. No PNG.
// usage: node tools/smoke/drive.mjs <devtools-port> <url> <hash> <seconds> [out.json]
import { writeFileSync } from "node:fs";

const [DEV, BASE, HASH, SECONDS, OUT] = process.argv.slice(2);
const SECS = Number(SECONDS || 40);
const outPath = OUT || "t/drive.json";

const target = await (await fetch(`http://127.0.0.1:${DEV}/json/new?about:blank`, { method: "PUT" })).json();
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
const ev = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true, timeout: 120000 });
  const rd = r.result || {};
  if (rd.exceptionDetails) throw new Error("page threw: " + (rd.exceptionDetails.exception?.description || rd.exceptionDetails.text));
  if (rd.result && rd.result.subtype === "error") throw new Error("page threw (uncaught): " + JSON.stringify(rd.result));
  return rd.result && rd.result.value;
};

// the page's fingerprint at this instant: the stream's clock, the render counts, the
// clock-relative phrases and tiles that a frozen page leaves untouched, and the pane's size.
const FINGERPRINT = `(() => {
  const M = window.localagentsMonitor;
  const d = document.getElementById("detail");
  const txt = d.textContent.replace(/\\s+/g, " ");
  const hash = (s) => { let n = 0; for (let i = 0; i < s.length; i++) n = (n * 31 + s.charCodeAt(i)) | 0; return s.length + ":" + n; };
  const lat = d.querySelector('.tile[data-tile="latency_ms"]');
  const ef = Array.from(d.querySelectorAll(".job-header .fig")).find((x) => (x.querySelector(".label") || {}).textContent === "elapsed");
  return JSON.stringify({
    wall: Date.now(),
    snapNow: M.state.snap ? M.state.snap.now : null,
    renders: Object.assign({}, M.renders),
    seen: (txt.match(/last seen [^,.;]+ ago/) || [null])[0],
    latHash: lat ? hash(lat.outerHTML) : null,
    elapsed: ef ? ef.textContent.trim() : null,
    detailLen: txt.length,
    conn: (document.getElementById("conn-label") || {}).textContent,
  });
})()`;

try {
  await send("Runtime.enable");
  await send("Log.enable");
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: BASE + "/" });
  await sleep(4500); // stream up, first snapshot drawn

  // arm the fetch wrapper BEFORE the route so the page's own opening fetches are counted
  await ev(`(() => {
    window.__drive = { fetches: [], armed: Date.now() };
    if (!window.__driveOrig) {
      window.__driveOrig = window.fetch;
      window.fetch = function (u) {
        try {
          const s = typeof u === "string" ? u : ((u && u.url) || "");
          if (/\\/api\\//.test(s)) window.__drive.fetches.push({ url: s, t: Date.now() });
        } catch (e) {}
        return window.__driveOrig.apply(this, arguments);
      };
    }
  })()`);
  await ev(`location.hash = ${JSON.stringify(HASH)}`);
  await sleep(4000); // the detail page's opening fetches land and draw

  const first = JSON.parse(await ev(FINGERPRINT));
  const samples = [first];
  const deadline = Date.now() + SECS * 1000;
  while (Date.now() < deadline) {
    await sleep(2000);
    samples.push(JSON.parse(await ev(FINGERPRINT)));
  }
  const last = samples[samples.length - 1];
  const raw = JSON.parse(await ev(`JSON.stringify(window.__drive.fetches.map((f) => ({ url: f.url, offsetS: +((f.t - window.__drive.armed) / 1000).toFixed(1) })))`));
  const sum = (r) => r.full + r.inPlace + r.skipped;
  const changed = {
    seenFrom: first.seen, seenTo: last.seen, seenMoved: first.seen !== last.seen,
    latHashFrom: first.latHash, latHashTo: last.latHash, latMoved: first.latHash !== last.latHash,
    elapsedFrom: first.elapsed, elapsedTo: last.elapsed, elapsedMoved: first.elapsed !== last.elapsed,
    snapNowAdvancedS: +(last.snapNow - first.snapNow).toFixed(1),
    rendersAdvanced: sum(last.renders) - sum(first.renders),
    detailLenFrom: first.detailLen, detailLenTo: last.detailLen,
  };
  const endpointRefetches = raw.filter((f) => /\/api\/endpoints\/[^/]+\/(history|requests)/.test(f.url));
  const report = {
    meta: { base: BASE, hash: HASH, seconds: SECS, samples: samples.length },
    first, last, changed,
    fetches: raw,
    endpointRefetchesAfter30s: endpointRefetches.filter((f) => f.offsetS >= 30).length,
    logs,
  };
  writeFileSync(outPath, JSON.stringify(report, null, 1));
  console.log(JSON.stringify({ page: HASH, seconds: SECS, changed,
    fetchCount: raw.length, endpointRefetches, endpointRefetchesAfter30s: report.endpointRefetchesAfter30s,
    exceptions: logs.filter((l) => l.startsWith("EXCEPTION")).length }, null, 1));
} catch (e) {
  writeFileSync(outPath, JSON.stringify({ error: String((e && e.stack) || e), logs }, null, 1));
  console.log("DRIVER THREW: " + String((e && e.stack) || e));
}
try { await fetch(`http://127.0.0.1:${DEV}/json/close/${target.id}`); } catch (e) {}
ws.close();
process.exit(0);
