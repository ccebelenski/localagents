// 3.7u: the wide snapshot. A hand-edited copy of the seeded snapshot whose running job carries a
// 200-char name and a 300-char last_activity and whose live endpoint serves a 120-char model id
// (no "/" or ":" in it, so the card's short form — status.py's short_model — is the whole id),
// rendered through the page's own render() in the order seeded -> wide -> seeded, measuring
// synchronously after each render (the next SSE push overwrites the hand-edited DOM). The
// numbers land in window.__u; cdp.mjs prints them as `u`.
fetch("/api/snapshot").then(r => r.json()).then(seed => {
  const M = localagentsMonitor;
  const rep = (unit, n) => unit.repeat(Math.ceil(n / unit.length)).slice(0, n);
  const NAME = rep("wide-job-name-", 200);
  const MODEL = rep("Wide-Model-Id-", 120);
  const ACT = rep("Bash: uv run pytest -q tests/test_monitor_page.py ", 300);
  const q = (s) => Array.from(document.querySelectorAll(s));
  const sel = ".host .ep-head, .host .ep-model, .host .ep-active, .host .ep-job-figs, .host .ep-act, .host .figures .fig .label, .host .figures .fig .num";
  const measure = (tag) => {
    const hosts = document.getElementById("hosts");
    const cols = getComputedStyle(hosts).gridTemplateColumns;
    return {
      tag,
      floor: M.cardLayout ? M.cardLayout.floor : null,
      hostsWidth: hosts.clientWidth,
      gridCols: cols, columns: cols.split(" ").length,
      liveHeights: q(".endpoint.state-live").map(e => e.getBoundingClientRect().height),
      hostBoxes: q(".host").map(e => e.dataset.host + " " + e.getBoundingClientRect().width.toFixed(1) + "x" + e.getBoundingClientRect().height.toFixed(3)),
      jobLines: q(".ep-job").map(e => ({ h: e.getBoundingClientRect().height,
        figsH: e.querySelector(".ep-job-figs").getBoundingClientRect().height,
        actH: e.querySelector(".ep-act").getBoundingClientRect().height,
        lineHeight: getComputedStyle(e.querySelector(".ep-act")).lineHeight })),
      overflow: q(sel).filter(e => e.scrollWidth > e.clientWidth).map(e => e.className + " " + e.scrollWidth + ">" + e.clientWidth),
      checked: q(sel).length,
      // a line scrolling past the cap: its box's height minus its client height is the bar
      scrollbars: q(".host .ep-head, .host .ep-job-figs, .host .ep-act").filter(e => e.offsetHeight > e.clientHeight)
        .map(e => e.className + " bar " + (e.offsetHeight - e.clientHeight) + "px, box " + e.getBoundingClientRect().height),
      raised: M.cardLayout ? M.cardLayout.raised : null,
      pageEllipsis: q(sel).filter(e => /…\s*$/.test(e.textContent)).map(e => e.className),
      fullModel: q(".ep-model").some(e => e.textContent === MODEL),
      fullAct: q(".ep-act").some(e => e.textContent === ACT),
      fullName: q(".ep-job-figs").some(e => e.textContent.indexOf(NAME) >= 0),
      pageScroll: document.documentElement.scrollWidth + "/" + document.documentElement.clientWidth,
    };
  };
  const out = [measure("seeded")];
  const wide = JSON.parse(JSON.stringify(seed));
  wide.hosts.forEach(h => h.endpoints.forEach(ep => { if (ep.state === "live") ep.model = MODEL; }));
  wide.jobs.forEach(j => { if (j.status === "running") { j.name = NAME; j.last_activity = ACT; } });
  M.render(wide);
  out.push(measure("wide"));
  M.render(JSON.parse(JSON.stringify(seed)));
  out.push(measure("seeded-again"));
  window.__u = out;
})
