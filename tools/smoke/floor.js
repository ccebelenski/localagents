// 3.7u-fix P-1 (Opus review of the page folds): the column floor's oracle, measured independently
// of raiseCardFloor. For every card line the need is the line's TEXT, never the room its card gives
// it: a block line's text extent (a Range over its contents: the rects are not clipped by the
// line's own scroll box), a flex row's items summed (each at its content width — a shrunk scroller
// reports it as scrollWidth — plus the row's column gaps; the free space a row's auto margin or
// space-between hands out is not text), each plus the chrome between the line's box and the card's
// edge (the line's box is block-level: card width minus its width). A line that is itself a flex
// item (.ep-seen in a down head, .host-note in the host head) is counted through its row. The field
// grid is counted as app.js counts it (columns x the widest tile text, plus the gaps). Evaluated as
// an expression: returns the JSON string, and leaves it in window.__floor.
(function () {
  var M = window.localagentsMonitor;
  var hostsEl = document.getElementById("hosts");
  var firstCard = hostsEl.querySelector(".host");
  var each = function (list, fn) { Array.prototype.forEach.call(list, fn); };
  var px = function (v) { return parseFloat(v) || 0; };
  var lines = [];
  var seen = [];
  each(hostsEl.children, function (card) {
    var cardW = card.getBoundingClientRect().width;
    each(card.querySelectorAll(".ep-head, .ep-job-figs, .ep-act, .ep-line"), function (line) {
      var box = line;
      var parentStyle = getComputedStyle(line.parentNode);
      if (/flex/.test(parentStyle.display) && line.parentNode !== card) box = line.parentNode;
      if (seen.indexOf(box) >= 0) return;
      seen.push(box);
      var s = getComputedStyle(box);
      var own = px(s.paddingLeft) + px(s.paddingRight) + px(s.borderLeftWidth) + px(s.borderRightWidth);
      var text = 0;
      if (/flex/.test(s.display)) {
        var kids = Array.prototype.filter.call(box.children, function (k) { return getComputedStyle(k).display !== "none"; });
        kids.forEach(function (k) { text += Math.max(k.getBoundingClientRect().width, k.scrollWidth); });
        text += Math.max(0, kids.length - 1) * px(s.columnGap);
      } else {
        var r = document.createRange();
        r.selectNodeContents(box);
        text = r.getBoundingClientRect().width;
      }
      var chrome = cardW - box.getBoundingClientRect().width;
      lines.push({ host: card.dataset.host, cls: box.className.replace(/ ?overflows/, ""), text: Math.round(text * 10) / 10,
        need: Math.round((text + own + chrome) * 10) / 10 });
    });
    each(card.querySelectorAll(".figures"), function (grid) {
      var gs = getComputedStyle(grid);
      var cols = gs.gridTemplateColumns.split(" ").length;
      var w = 0;
      each(grid.querySelectorAll(".fig > .label, .fig > .num, .fig > .kv"), function (t) {
        var r = document.createRange(); r.selectNodeContents(t);
        w = Math.max(w, r.getBoundingClientRect().width);
      });
      lines.push({ host: card.dataset.host, cls: "figures x" + cols, text: w,
        need: Math.round((cols * w + (cols - 1) * px(gs.columnGap) + cardW - grid.getBoundingClientRect().width) * 10) / 10 });
    });
  });
  lines.sort(function (a, b) { return b.need - a.need; });
  var oracle = lines.length ? Math.ceil(lines[0].need) : 0;
  var out = {
    w: window.innerWidth,
    floor: M.cardLayout.floor, need: M.cardLayout.need,
    oracle: oracle, widest: lines.length ? lines[0].host + " " + lines[0].cls : null,
    // the gates (app.js rounds up; 1 px of sub-pixel slack either way): the last measurement is
    // the text's need, and — on a page whose texts never shrank (the seeded world, no hand
    // render; floor.mjs) — the floor, a high-water mark, is that need too, never a window width
    needIsText: Math.abs(M.cardLayout.need - oracle) <= 1,
    floorIsText: Math.abs(M.cardLayout.floor - oracle) <= 1,
    columns: getComputedStyle(hostsEl).gridTemplateColumns.split(" ").length,
    cols: getComputedStyle(hostsEl).gridTemplateColumns,
    // 3.10g (F4 fold): the first card's rendered width. The 1.5x floor cap is a card rule, so
    // a released override (a later top-level `.host { max-width: none; }`) widens this while
    // the floor — the text's need — does not move. floor.mjs fails a step above 760 px whose
    // card exceeds the cap.
    cardWidth: firstCard ? Math.round(firstCard.getBoundingClientRect().width * 10) / 10 : null,
    hostsWidth: hostsEl.clientWidth,
    page: document.documentElement.scrollWidth + "/" + document.documentElement.clientWidth,
    top: lines.slice(0, 4),
  };
  window.__floor = out;
  return JSON.stringify(out);
})()
