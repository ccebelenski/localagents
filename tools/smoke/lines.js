// 3.7u-fix (U1/U2): the long lines a card never met. A hand-edited copy of the seeded snapshot
// whose dormant endpoint carries a 200-char name, whose down endpoint a 300-char error sentence
// and a 200-char dropped-job name, rendered through the page's own render() (seeded -> lines),
// measuring synchronously after each render (the next SSE push overwrites the hand-edited DOM).
// The rule under test: every one-line text element on a host card is one line whatever its text —
// nowrap, pinned to one line box, scrolling in its own barless box when the card cannot fit it —
// and the floor's `need` counts every one of them. The numbers land in window.__lines; cdp.mjs
// prints them as `lines`.
fetch("/api/snapshot").then(r => r.json()).then(seed => {
  const M = localagentsMonitor;
  const rep = (unit, n) => unit.repeat(Math.ceil(n / unit.length)).slice(0, n);
  const DORMANT = rep("wide-dormant-endpoint-", 200);
  const ERROR = rep("engine unreachable: connection refused after ", 300);
  // deliberately one unbreakable token: at 3.7u a token wider than the card spilled past the
  // card's border and scrolled the whole page (U2: documentElement.scrollWidth 1692 > 885)
  const DROPPED = rep("wideDroppedJobNameXy", 200);
  const q = (s) => Array.from(document.querySelectorAll(s));
  const LINES = ".host .ep-head, .host .ep-job-figs, .host .ep-act, .host .ep-line";
  const measure = (tag) => {
    return {
      tag,
      need: M.cardLayout ? M.cardLayout.need : null,
      floor: M.cardLayout ? M.cardLayout.floor : null,
      // the page never scrolls sideways
      pageScroll: document.documentElement.scrollWidth + "/" + document.documentElement.clientWidth,
      // every card line: content no taller than one line box of its own font, no scrollbar
      // stolen from its box (offsetHeight minus clientHeight is exactly its own borders —
      // a dormant line carries a 1 px top border), and the overflows class exactly where it
      // scrolls
      lineTall: q(LINES).every(e => {
        const s = getComputedStyle(e);
        const inner = e.getBoundingClientRect().height
          - parseFloat(s.borderTopWidth) - parseFloat(s.borderBottomWidth)
          - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom);
        // tolerance: the head's pin (+1 px, 3.7u's device for the state dot's line box) and a
        // flex child's margin-top — the down head is +4 px taller than one line box because
        // .ep-seen has always carried margin-top: 4 px; pre-existing and text-independent
        const kids = Array.from(e.children).reduce((m, c) =>
          Math.max(m, parseFloat(getComputedStyle(c).marginTop) || 0), 0);
        return inner <= parseFloat(s.lineHeight) + 1 + kids + 0.01;
      }),
      bars: q(LINES).filter(e => {
        const s = getComputedStyle(e);
        return e.offsetHeight - e.clientHeight
          > parseFloat(s.borderTopWidth) + parseFloat(s.borderBottomWidth) + 0.01;
      }).map(e => e.className + " bar " + (e.offsetHeight - e.clientHeight) + "px"),
      overflowsClass: q(".host .overflows").map(e => e.className),
      overflowing: q(LINES).filter(e => e.scrollWidth > e.clientWidth).map(e => e.className),
      // the three long lines before and after: the dormant line, the down card's error, its
      // dropped-job line — heights and whether the whole string is in textContent
      dormantH: q(".endpoint.state-dormant").map(e => e.getBoundingClientRect().height),
      errorH: q(".state-down .ep-error").map(e => e.getBoundingClientRect().height),
      droppedH: q(".ep-jobs.ep-line").map(e => e.getBoundingClientRect().height),
      hasDormant: q(".endpoint.state-dormant").some(e => e.textContent.indexOf(DORMANT) >= 0),
      hasError: q(".state-down .ep-error").some(e => e.textContent === ERROR),
      hasDropped: q(".ep-jobs.ep-line").some(e => e.textContent.indexOf(DROPPED) >= 0),
      liveHeights: q(".endpoint.state-live").map(e => e.getBoundingClientRect().height),
    };
  };
  const out = [measure("seeded")];
  const wide = JSON.parse(JSON.stringify(seed));
  wide.hosts.forEach(h => h.endpoints.forEach(ep => {
    if (ep.state === "dormant") ep.name = DORMANT;
    if (ep.state === "down") { ep.error = ERROR; ep.dropped_job = DROPPED; }
  }));
  M.render(wide);
  out.push(measure("lines"));
  window.__lines = out;
})
