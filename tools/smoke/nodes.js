fetch("/api/snapshot").then(r => r.json()).then(snap => {
  // a hand-edited copy of the seeded snapshot: dgx1's node down, a fourth host whose node is unknown,
  // and no jobs (the empty table); rendered through the page's own render()
  snap.hosts[1].state = "down";
  snap.hosts.push({ name: "ai9", state: "unknown", dormant: false, endpoints: [Object.assign({}, snap.hosts[1].endpoints[0], { name: "ai9-x", state: "down", error: "timed out" })] });
  snap.jobs = [];
  localagentsMonitor.render(snap);
  // 3.9D D1 (fold-3 review): the transition the review measured — from that world, one render
  // where one card's only drawn endpoint becomes hidden (dormant, last_seen null, host not
  // down). The rebuild detaches the old element and hostCard() returns null for the host;
  // before the fix the host stayed in the old-cards map, so the gone-host loop threw
  // NotFoundError removing the already-detached node and the render aborted mid-way.
  // Measured synchronously into window.__d1 — the next SSE push rewrites the hand-edited DOM.
  var read = function () {
    return {
      hosts: Array.prototype.map.call(document.querySelectorAll("#hosts .host"), function (e) { return e.dataset.host; }),
      hiddenLine: (document.getElementById("hosts-hidden") || {}).textContent || null,
      hiddenShown: !document.getElementById("hosts-hidden").hidden,
      renders: Object.assign({}, localagentsMonitor.renders),
    };
  };
  var card = Array.prototype.filter.call(document.querySelectorAll("#hosts .host"), function (e) {
    return e.querySelectorAll(".endpoint").length === 1 && !e.classList.contains("node-down");
  })[0];
  var d1 = window.__d1 = { threw: null, target: null, before: null, after: null };
  if (!card) { d1.threw = "no single-endpoint card to hide"; return; }
  var epName = card.querySelector(".endpoint").dataset.endpoint;
  var copy = JSON.parse(JSON.stringify(snap));
  var t = copy.hosts.filter(function (h) { return h.name === card.dataset.host; })[0];
  var ep = t.endpoints.filter(function (e) { return e.name === epName; })[0];
  ep.state = "dormant";
  ep.last_seen = null;
  d1.target = { host: card.dataset.host, endpoint: epName };
  d1.before = read();
  try {
    localagentsMonitor.render(copy);
  } catch (e) {
    d1.threw = (e && e.name ? e.name : "error") + ": " + (e && e.message ? e.message : String(e));
  }
  d1.after = read();
});
