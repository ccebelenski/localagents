// 3.9D D3: click a REAL range button and read back what the click path must redraw. The 3.7l
// probe switched ranges by calling the internal setter directly, which skips the button's click
// handler, its aria-pressed rebuild and the fetch the handler schedules — none of that was ever
// clicked. This clicks both offered extremes in sequence (15m, 300 s ticks, and 7d, one tick per
// day), waits for each click to land (state.range and histRange follow, the pressed button moves,
// the timeline redraws) rather than trusting a fixed sleep, and leaves before/after axis labels
// and pressed buttons in window.__d3 for the probe's `d3` field. No exception, no fixed sleep.
// usage: node tools/smoke/cdp.mjs 9243 http://127.0.0.1:8783/ 1400 900 t/range.png "js:$(cat tools/smoke/range.js)"
(async function () {
  var M = window.localagentsMonitor;
  var read = function () {
    return {
      axis: Array.from(document.querySelectorAll(".tl-axis span")).map(function (s) { return s.textContent; }),
      title: document.getElementById("fleet-title").textContent,
      pressed: Array.from(document.querySelectorAll("#range button"))
        .filter(function (b) { return b.getAttribute("aria-pressed") === "true"; })
        .map(function (b) { return b.textContent; }),
    };
  };
  // the click landed when the handler's redraw pressed the button AND the fetch it scheduled
  // landed (histRange follows, timeline.update ran) — the axis cannot be trusted before that
  var waitFor = async function (want) {
    for (var i = 0; i < 80; i++) {
      if (M.state.range === want && M.state.histRange === want && read().pressed.join() === want) return true;
      await new Promise(function (r) { setTimeout(r, 100); });
    }
    return false;
  };
  var step = async function (want) {
    var btn = Array.from(document.querySelectorAll("#range button"))
      .find(function (b) { return b.textContent === want; });
    if (!btn) return { want: want, found: false };
    btn.click();
    var landed = await waitFor(want);
    return Object.assign({ want: want, found: true, landed: landed }, read());
  };
  window.__d3 = { before: read(), m15: await step("15m"), d7: await step("7d") };
})().catch(function (e) { window.__d3 = { threw: String((e && e.message) || e) }; });
