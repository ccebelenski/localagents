// 3.15 (operator 2026-11-17, reversing 3.7u's measured floor): the column floor is app.css's fixed
// --card-floor and nothing else. At any window the grid's column count is the auto-fill arithmetic
// over that number and the grid's width (min(floor, 100%) per track, the 12 px gap), every card is
// at least a track wide and at most 1.5 floors above the 760 px narrow query (3.10g), and the page
// never scrolls sideways. Evaluated as an expression: returns the JSON string, and leaves it in
// window.__floor. floor.mjs runs it across window sizes; a hand render (wide.js) may run it too.
(function () {
  var FLOOR = 500; // app.css .hosts --card-floor (test_monitor_page.py pins the stylesheet's)
  var hostsEl = document.getElementById("hosts");
  var cs = getComputedStyle(hostsEl);
  var floor = parseFloat(cs.getPropertyValue("--card-floor"));
  var gap = parseFloat(cs.columnGap) || 0;
  var W = hostsEl.clientWidth;
  var track = Math.min(floor, W);
  var expectCols = window.innerWidth <= 760 ? 1 : Math.max(1, Math.floor((W + gap) / (track + gap)));
  var columns = cs.gridTemplateColumns.split(" ").length;
  var cards = Array.prototype.map.call(hostsEl.querySelectorAll(".host"), function (e) {
    return Math.round(e.getBoundingClientRect().width * 10) / 10;
  });
  var capOk = window.innerWidth <= 760 || cards.every(function (w) { return w <= floor * 1.5 + 1; });
  var minOk = cards.every(function (w) { return w >= track - 1; });
  var page = document.documentElement;
  var out = {
    w: window.innerWidth, floor: floor, hostsWidth: W, columns: columns, expectCols: expectCols,
    cards: cards,
    floorFixed: floor === FLOOR && !hostsEl.style.getPropertyValue("--card-floor"),
    columnsOk: columns === expectCols, capOk: capOk, minOk: minOk,
    pageScroll: page.scrollWidth + "/" + page.clientWidth,
    noSideways: page.scrollWidth <= page.clientWidth,
  };
  out.ok = out.floorFixed && out.columnsOk && capOk && minOk && out.noSideways;
  window.__floor = out;
  return JSON.stringify(out);
})()
