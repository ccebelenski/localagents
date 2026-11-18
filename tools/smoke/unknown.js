// F1 (fold of the page-batch review, 2026-10-30; spec §4 True-now): an UNKNOWN node is an
// alert like a down node — the head's note is the one word "unknown", the probe's sentence
// rides in that note's title, and the endpoint draws no `.ep-error` line (it once set 3.7u's
// measured column floor; 3.15 fixed the floor, so the widths are read as a guard only). Two reads
// into window.__d1.cases[], the second synchronously after a hand render: the seeded world, then
// the seeded snapshot
// plus a fourth host ai9 state "unknown" whose one endpoint is state "down", last_seen 2 h
// before snap.now, and a 300-character error sentence.
//   before -> the cards' widths as the seed leaves them;
//   after  -> note text "unknown", note title exactly the sentence (noteTitleIsSentence true),
//             the sentence NOT in the card's text, no `.ep-error` child, the first card's width
//             unchanged.
fetch("/api/snapshot").then(function (r) { return r.json(); }).then(function (snap) {
  var SENT = new Array(301).join("E"); // exactly 300 characters
  var clone = function (x) { return JSON.parse(JSON.stringify(x)); };
  var read = function (label, threw) {
    var card = document.querySelector('#hosts .host[data-host="ai9"]');
    var note = card ? card.querySelector(".host-note") : null;
    var title = note ? note.getAttribute("title") : null;
    return {
      label: label,
      threw: threw,
      firstCardWidth: document.querySelector("#hosts .host").getBoundingClientRect().width,
      ai9Card: !!card,
      ai9Classes: card ? card.className : null,
      noteText: note ? note.textContent : null,
      noteTitleLen: title == null ? null : title.length,
      noteTitleIsSentence: title === SENT,
      sentenceInCardText: card ? card.textContent.indexOf(SENT) >= 0 : null,
      epErrorChild: card ? !!card.querySelector(".endpoint .ep-error") : null,
      renders: Object.assign({}, localagentsMonitor.renders),
    };
  };
  var render = function (w) {
    try { localagentsMonitor.render(w); return null; }
    catch (e) { return (e && e.name ? e.name : "error") + ": " + (e && e.message ? e.message : String(e)); }
  };
  var d1 = window.__d1 = { sentenceLen: SENT.length, cases: [] };
  d1.cases.push(read("before: seeded world", null));
  var unk = clone(snap);
  unk.hosts.push({ name: "ai9", state: "unknown", dormant: false, fallback: null, fallback_t: null,
    endpoints: [{ name: "ai9", backend: "llama.cpp", base_url: "http://10.0.0.62:8080",
      host: "ai9", state: "down", last_seen: snap.now - 7200, last_active: null, error: SENT,
      dropped_job: null, dropped_job_id: null }] });
  d1.cases.push(read("after: unknown node, 300-char error", render(unk)));
  d1.threw = d1.cases.map(function (c) { return c.threw; }).filter(Boolean).join("; ") || null;
});
