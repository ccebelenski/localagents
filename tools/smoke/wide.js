// 3.15 (operator 2026-11-17, reversing 3.7u): no text widens a card. A hand-edited copy of the
// seeded snapshot carrying every long text a card can hold — a 120-char model id on the live
// endpoints (no "/" or ":", so the short form is the whole id), a 300-char activity and a figures
// line stuffed with every bit on the running job, a 300-char speeds note, a 300-char error on the
// down endpoint, a 200-char dormant endpoint name — rendered through the page's own render() in
// the order seeded -> wide -> seeded, measuring synchronously after each render (the next SSE push
// overwrites the hand-edited DOM). Gates, in window.__u.gates (cdp.mjs prints it as `u`):
//   gridWidths  at every render the column count is the auto-fill arithmetic over the fixed floor
//               and the grid's width, and every card is its track (capped at 1.5 floors above the
//               760 px query) — the window's numbers, never the text's (a vertical page scrollbar
//               that comes and goes with the taller hand render moves the grid's width, so the
//               three renders are compared against the grid, not against each other);
//   oneLine     every card line is at most one line box tall (3.7n);
//   ellipsis    every card line wider than its box is overflow: hidden + text-overflow: ellipsis;
//   titles      every long text is whole in its element's title;
//   noSideways  the page never scrolls sideways.
fetch("/api/snapshot").then(r => r.json()).then(seed => {
  const M = localagentsMonitor;
  const rep = (unit, n) => unit.repeat(Math.ceil(n / unit.length)).slice(0, n);
  const MODEL = rep("Wide-Model-Id-", 120);
  const ACT = rep("Bash: uv run pytest -q tests/test_monitor_page.py ", 300);
  const REASON = rep("the last probe has no counters and ", 300);
  const ERROR = rep("engine unreachable: connection refused after ", 300);
  const DORMANT = rep("wide-dormant-endpoint-", 200);
  const q = (s) => Array.from(document.querySelectorAll(s));
  const LINES = ".host .ep-head, .host .ep-model, .host .ep-job-figs, .host .ep-act, .host .ep-line";
  const measure = (tag) => {
    const hosts = document.getElementById("hosts");
    const page = document.documentElement;
    const lines = q(LINES);
    const cs = getComputedStyle(hosts);
    const floor = parseFloat(cs.getPropertyValue("--card-floor"));
    const gap = parseFloat(cs.columnGap) || 0;
    const W = hosts.clientWidth;
    const narrow = window.innerWidth <= 760;
    const cols = narrow ? 1 : Math.max(1, Math.floor((W + gap) / (Math.min(floor, W) + gap)));
    const track = narrow ? W : Math.min((W - (cols - 1) * gap) / cols, floor * 1.5);
    const columns = cs.gridTemplateColumns.split(" ").length;
    return {
      tag,
      columns, expectCols: cols, track: Math.round(track * 10) / 10,
      gridOk: columns === cols && q(".host").every(e => Math.abs(e.getBoundingClientRect().width - track) <= 1),
      widths: q(".host").map(e => e.dataset.host + " " + e.getBoundingClientRect().width.toFixed(1)),
      // the line's content box against one line box of its own font (the head's 1 px pin for the
      // name's underline, a flex child's margin-top — .ep-seen carries 4 px — allowed)
      tall: lines.filter(e => {
        const s = getComputedStyle(e);
        const inner = e.getBoundingClientRect().height
          - parseFloat(s.borderTopWidth) - parseFloat(s.borderBottomWidth)
          - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom);
        const kids = Array.from(e.children).reduce((m, c) =>
          Math.max(m, parseFloat(getComputedStyle(c).marginTop) || 0), 0);
        return inner > parseFloat(s.lineHeight) + 1 + kids + 0.01;
      }).map(e => e.className),
      overflowing: lines.filter(e => e.scrollWidth > e.clientWidth + 1).map(e => {
        const s = getComputedStyle(e);
        return { cls: e.className, sw: e.scrollWidth, cw: e.clientWidth,
          ellipsis: s.textOverflow === "ellipsis" && s.overflowX === "hidden",
          title: (e.getAttribute("title") || "").length };
      }),
      titles: {
        model: q(".host .ep-model").some(e => e.title === MODEL),
        act: q(".host .ep-act").some(e => e.title === ACT),
        figs: q(".host .ep-job-figs").every(e => e.title === e.textContent),
        reason: q(".host .ep-note").some(e => e.title === "speeds: " + REASON),
        error: q(".host .ep-error").some(e => e.title === ERROR),
        dormant: q(".host .endpoint.state-dormant").some(e => e.title.indexOf(DORMANT) === 0),
      },
      pageScroll: page.scrollWidth + "/" + page.clientWidth,
      noSideways: page.scrollWidth <= page.clientWidth,
    };
  };
  const out = [measure("seeded")];
  const wide = JSON.parse(JSON.stringify(seed));
  wide.hosts.forEach(h => h.endpoints.forEach(ep => {
    if (ep.state === "live") { ep.model = MODEL; ep.rates = Object.assign({}, ep.rates, { reason: REASON }); }
    if (ep.state === "down") ep.error = ERROR;
    if (ep.state === "dormant") ep.name = DORMANT;
  }));
  wide.jobs.forEach(j => {
    if (j.status !== "running") return;
    Object.assign(j, { last_activity: ACT, tool_calls: 123456789, files: 98765, compactions: 4321, errors: 5678,
      ctx_pct: 87, ctx_tokens: 228000, context: 262144, gen_tps: 1234.5 });
  });
  M.render(wide);
  out.push(measure("wide"));
  M.render(JSON.parse(JSON.stringify(seed)));
  out.push(measure("seeded-again"));
  const w = out[1];
  window.__u = {
    out,
    gates: {
      gridWidths: out.every(m => m.gridOk),
      oneLine: out.every(m => m.tall.length === 0),
      ellipsis: out.every(m => m.overflowing.every(o => o.ellipsis)),
      titles: Object.keys(w.titles).filter(k => !w.titles[k]),
      wideOverflows: w.overflowing.length,
      noSideways: out.every(m => m.noSideways),
    },
  };
})
