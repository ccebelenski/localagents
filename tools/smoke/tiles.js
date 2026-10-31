// P3 (commit 9c41b21, the tiles): the endpoint page at #/endpoint/ai1. Read the seeded tiles,
// then hand-render a 200-character served model and host.fallback "refused" (driven through
// localagentsMonitor.render -> the endpoint page's onSnapshot hook re-draws the detail from
// state.snap), and read both. Run at 1400x900 and 760x900 (cdp.mjs sets the viewport).
(async () => {
  const L = localagentsMonitor;
  let real = L.state.snap;
  const t0 = Date.now();
  while (!real && Date.now() - t0 < 6000) { await new Promise(r => setTimeout(r, 100)); real = L.state.snap; }
  if (!real) { window.__d1 = { error: "no snapshot after 6 s" }; return; }
  const q = (s) => Array.from(document.querySelectorAll(s));
  const readTiles = (label) => {
    const tv = q('.tile-value').map(e => ({ text: e.textContent, sw: e.scrollWidth, cw: e.clientWidth, overflow: e.scrollWidth > e.clientWidth }));
    const modelVal = document.querySelector('.tile[data-tile="model"] .tile-value');
    return {
      label,
      tileLabels: q('.tile-label').map(e => e.textContent),
      groups: q('.tier-group').map(g => ({ heading: g.querySelector('h3') ? g.querySelector('h3').textContent : null, tiles: g.querySelectorAll('.tile').length })),
      headings: q('.tier-group h3').map(e => e.textContent),
      hostClause: (document.querySelector('.endpoint-detail .ep-head .muted') || {}).textContent || null,
      state: (document.querySelector('.endpoint-detail .ep-state') || {}).textContent || null,
      title: (document.querySelector('.endpoint-detail .ep-title') || {}).textContent || null,
      tileValues: tv,
      modelTile: modelVal ? { len: modelVal.textContent.length, sw: modelVal.scrollWidth, cw: modelVal.clientWidth, overflow: modelVal.scrollWidth > modelVal.clientWidth } : null,
      anyValueOverflow: tv.some(t => t.overflow),
      sparkCount: q('.tile .spark').length,
      sparkSvg: q('.tile .spark svg').length,
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      hScroll: document.documentElement.scrollWidth > window.innerWidth,
      tileMinWidth: (function () { const e = document.querySelector('.tile'); return e ? getComputedStyle(e).minWidth : null; })(),
    };
  };
  const seeded = readTiles("seeded");
  // hand-render: a 200-char served model and the port-22 fallback, through render() -> onSnapshot
  const copy = JSON.parse(JSON.stringify(real));
  copy.hosts.forEach(h => h.endpoints.forEach(e => { if (e.name === "ai1") { e.model = "M".repeat(200); h.fallback = "refused"; } }));
  L.render(copy);
  const hand = readTiles("hand-200model-fallback-refused");
  window.__d1 = { width: window.innerWidth, height: window.innerHeight, seeded, hand };
  return "ok";
})();
