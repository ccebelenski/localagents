// story 1.14: the job page's "server" figure and its "server code differs" note. Run as cdp.mjs's
// js: hash-after on the seeded world (the seed stamps its server row with the tree's build stamp,
// so a monitor started from the same tree draws no note). Wraps EventSource BEFORE the job page
// opens its own stream, opens the running job's page, reads the header, then dispatches two
// hand-edited snapshots on the page's stream: one whose `build` differs (the note must appear,
// muted) and the real one again (the note must clear). Results in window.__d1 (AFTER_JSON's d1).
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const streams = [];
  const ES = window.EventSource;
  window.EventSource = function (url, opts) { const s = new ES(url, opts); streams.push(s); return s; };
  window.EventSource.prototype = ES.prototype;
  const snap = await (await fetch("/api/snapshot", { cache: "no-store" })).json();
  const job = snap.jobs[0];
  location.hash = "#/job/" + job.id;
  for (let i = 0; i < 100 && !document.querySelector(".job-header .fig-row"); i++) await sleep(100);
  await sleep(300);
  const read = () => {
    const note = document.querySelector(".job-header .build-note");
    const server = Array.from(document.querySelectorAll(".job-header .fig"))
      .find((f) => (f.querySelector(".label") || {}).textContent === "server");
    return {
      figs: Array.from(document.querySelectorAll(".job-header .fig")).map((f) => f.textContent),
      server: server ? server.querySelector(".num").textContent : null,
      note: note ? note.textContent : null,
      noteTitle: note ? note.title : null,
      noteClass: note ? note.className : null,
      noteColour: note ? getComputedStyle(note).color : null,
      numColour: server ? getComputedStyle(server.querySelector(".num")).color : null,
    };
  };
  const push = (body) => streams[streams.length - 1].dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(body) }));
  const seeded = read();
  push(Object.assign({}, snap, { build: "0.0.0+handedt" }));
  const differs = read();
  push(snap);
  const back = read();
  window.__d1 = {
    job: job.id, build: snap.build, server_version: job.server_version, streams: streams.length,
    seeded, differs, back,
    ok: seeded.server === job.server_version && seeded.note === ""
      && differs.note === "server code differs from the monitor's" && differs.noteTitle === "monitor 0.0.0+handedt"
      && back.note === "",
  };
})();
