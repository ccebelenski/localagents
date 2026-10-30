// 3.7y (operator 2026-10-30: "ai9 is 'down' yes, but it's also 'never seen'. Perhaps it
// shouldn't have a card in that state?"): a never-seen endpoint is hidden on a down node too,
// so a down node with nothing left to draw has no card, and the hidden line names the node's
// state. Three hand renders in ONE script, the DOM read synchronously after each into
// window.__d1.cases[] (cdp.mjs reports __d1 as the `d1` field of its AFTER_JSON line):
//   (a) ai9, node down, its one endpoint never seen -> no card, the hidden line names it
//       "ai9 (node down)" (the seeded push carries the same error sentence, so the node's
//       alert stays in words even with no card);
//   (b) the same host with last_seen 3 days before snap.now -> the card is back: the node-down
//       border, the head note "down" (3.7x), one seen-dormant endpoint line ("last seen …");
//   (c) from (b), render (a)'s world again -> the card disappears between two renders: the
//       3.9D D1 transition (the old element leaves #hosts before the map forgets it), the
//       hidden line returns, renders.full advanced, and no exception was thrown.
fetch("/api/snapshot").then(function (r) { return r.json(); }).then(function (snap) {
  var ERR = "10.0.0.62:8080 timed out (host off, or a firewall dropping packets)";
  var world = function (lastSeen) {
    var w = JSON.parse(JSON.stringify(snap));
    w.hosts.push({ name: "ai9", state: "down", dormant: false, fallback: null, fallback_t: null,
      endpoints: [{ name: "ai9", backend: "llama.cpp", base_url: "http://10.0.0.62:8080",
        host: "ai9", state: "dormant", last_seen: lastSeen, last_active: null, error: ERR }] });
    return w;
  };
  var render = function (w) {
    try {
      localagentsMonitor.render(w);
      return null;
    } catch (e) {
      return (e && e.name ? e.name : "error") + ": " + (e && e.message ? e.message : String(e));
    }
  };
  var read = function (label, threw) {
    var card = document.querySelector('#hosts .host[data-host="ai9"]');
    var hiddenEl = document.getElementById("hosts-hidden");
    var note = card && card.querySelector(".host-note");
    var ep = card && card.querySelector(".endpoint");
    return {
      label: label,
      threw: threw,
      hosts: Array.prototype.map.call(document.querySelectorAll("#hosts .host"), function (e) { return e.dataset.host; }),
      hiddenShown: !hiddenEl.hidden,
      hiddenLine: hiddenEl.textContent || null,
      ai9Card: !!card,
      ai9Classes: card ? card.className : null,
      ai9Note: note ? note.textContent : null,
      ai9NoteTitle: note ? note.getAttribute("title") : null,
      ai9Ep: ep ? { cls: ep.className, text: ep.textContent.replace(/\s+/g, " ").trim() } : null,
      renders: Object.assign({}, localagentsMonitor.renders),
    };
  };
  var d1 = window.__d1 = { threw: null, base: read("seeded world, no ai9"), cases: [] };
  d1.cases.push(read("(a) down node, one never-seen endpoint", render(world(null))));
  d1.cases.push(read("(b) last_seen 3 days before snap.now", render(world(snap.now - 259200))));
  d1.cases.push(read("(c) render (a)'s world again: the card disappears", render(world(null))));
  // the headline throw, if any, also rides the top-level field (nodes.js shape)
  d1.threw = d1.cases.map(function (c) { return c.threw; }).filter(Boolean).join("; ") || null;
});
