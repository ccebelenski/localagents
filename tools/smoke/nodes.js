fetch("/api/snapshot").then(r => r.json()).then(snap => {
  // a hand-edited copy of the seeded snapshot: dgx1's node down, a fourth host whose node is unknown,
  // and no jobs (the empty table); rendered through the page's own render()
  snap.hosts[1].state = "down";
  snap.hosts.push({ name: "ai9", state: "unknown", dormant: false, endpoints: [Object.assign({}, snap.hosts[1].endpoints[0], { name: "ai9-x", state: "down", error: "timed out" })] });
  snap.jobs = [];
  localagentsMonitor.render(snap);
})
