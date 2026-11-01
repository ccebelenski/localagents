/* localagents monitor: the overview page (story 3.2; monitor spec §5 "Overview").
 *
 * The page lives on ONE server-sent events stream (/events): each `snapshot` event redraws the
 * host cards and the running-jobs table in place; the page never polls. The decode sparklines,
 * the state strips' segments and the fleet timeline (story 3.7l: one shared axis — a row per
 * live endpoint, its busy band and its job spans) need history: on a snapshot the page
 * re-fetches /api/overview/history (one call: every live endpoint's own history — its `state`
 * series feeds the strips and the bands; the strip's current word is the snapshot's own
 * `probe_state`, never a point of the series — plus the jobs overlapping the window) at most
 * once per `refresh_s` (3.7-fold-3: the interval follows the snapshot's own clock). While a
 * detail page is open the overview is hidden and fetches nothing; it fetches once on return.
 *
 * The JSON is the contract in docs/superpowers/specs/2026-10-29-monitor-design.md §5a; every
 * field read here is named there (tests/test_monitor_page.py checks it). The objects are read
 * through a few fixed names: `snap` (the snapshot), `host`, `ep` (an endpoint), `job` (a
 * running job or a timeline span), `hw` (ep.hardware) and `hist` (an endpoint history).
 * (3.7l: `fleet`/`fleetEp` were the fleet charts' names; the charts are gone, and so is the
 * charting library they needed — uPlot was dropped from the shell and the package in 3.7-fold-4
 * once no page used it.)
 *
 * No framework, no build step: plain DOM built through h(), which sets text with textContent,
 * so a job name or an error sentence from the database is never parsed as HTML.
 */
(function () {
  "use strict";

  // monitor/ranges.py RANGES and DEFAULT_RANGE (a test pins the copy)
  var RANGES = ["15m", "1h", "6h", "24h", "7d"];
  var DEFAULT_RANGE = "1h";
  var RANGE_STORE_KEY = "localagents.monitor.range";
  // 3.7-fold-3 (3.7m): the history fetch follows the snapshot's own clock — `refresh_s`, the
  // fan-out's push period — floored at 1 s so a tiny server setting cannot make the page hammer
  // it. HISTORY_MIN_INTERVAL_MS stays the default until the first snapshot sets it.
  var HISTORY_MIN_INTERVAL_DEFAULT_MS = 5000;
  var HISTORY_MIN_INTERVAL_MS = HISTORY_MIN_INTERVAL_DEFAULT_MS;
  var RECONNECT_MIN_MS = 1000;
  var RECONNECT_MAX_MS = 30000;

  // categorical slots, fixed per endpoint by its index in config order (dark-surface steps);
  // past eight the hues repeat — the fleet charts used to tell repeats apart with a dashed
  // line, the timeline never needs it because every row carries its name (3.7l)
  var PALETTE = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
  // the state strip: idle recedes, busy is the accent; any other state string (Strata's own)
  // takes one of these by a hash of its text, and every segment's title names its state
  var STATE_FIXED = { idle: "#4a4945", busy: "#3987e5" };
  var STATE_OTHER = ["#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"];

  // --- number formatting: every figure on the page goes through fmt -------------------------

  var fmt = {
    // integers with thousands separators
    int: function (v) {
      if (v == null || !isFinite(v)) return "";
      return Math.round(v).toLocaleString();
    },
    // a compact count (3.7n: the ctx tile's window, 262,144 -> "262k", 1,500 -> "1.5k";
    // under 1,000 as is); the exact fmt.int number rides in the tile's title
    compact: function (v) {
      if (v == null || !isFinite(v)) return "";
      if (Math.abs(v) < 1000) return String(Math.round(v));
      var k = v / 1000;
      return (Math.abs(k) >= 100 ? Math.round(k) : Math.round(k * 10) / 10) + "k";
    },
    // speeds (tokens per second) with one decimal
    speed: function (v) {
      if (v == null || !isFinite(v)) return "";
      return v.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    },
    // a 0..1 fraction as a whole percentage
    frac: function (v) {
      if (v == null || !isFinite(v)) return "";
      return Math.round(v * 100) + "%";
    },
    // a value already in percent (0..100)
    pct: function (v) {
      if (v == null || !isFinite(v)) return "";
      return Math.round(v) + "%";
    },
    // durations: 42s, 4m12s, 2h03m, 3d04h; with wholeHours, a whole number of hours reads as
    // hours alone (24h, 48h: the attention window, a setting in hours), never 1d00h
    dur: function (s, wholeHours) {
      if (s == null || !isFinite(s)) return "";
      s = Math.max(0, Math.floor(s));
      if (wholeHours && s >= 3600 && s % 3600 === 0) return s / 3600 + "h";
      var pad = function (n) { return n < 10 ? "0" + n : String(n); };
      if (s < 60) return s + "s";
      if (s < 3600) return Math.floor(s / 60) + "m" + pad(s % 60) + "s";
      if (s < 86400) return Math.floor(s / 3600) + "h" + pad(Math.floor((s % 3600) / 60)) + "m";
      return Math.floor(s / 86400) + "d" + pad(Math.floor((s % 86400) / 3600)) + "h";
    },
    // bytes as GiB with one decimal
    gib: function (b) {
      if (b == null || !isFinite(b)) return "";
      return (b / 1073741824).toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + " GiB";
    },
    // epoch seconds as the browser's local time of day / local date and time
    time: function (t) {
      if (t == null || !isFinite(t)) return "";
      return new Date(t * 1000).toLocaleTimeString();
    },
    date: function (t) {
      if (t == null || !isFinite(t)) return "";
      return new Date(t * 1000).toLocaleString();
    },
    // 3.7-fold-5: the timeline's tick labels. The same clock face fmt.time prints, minus the
    // seconds a fifteen-minute gridline never changes ("1:15 AM"); past 24 h the date form
    // drops the year and the seconds too ("10/28 1:30 AM") — the axis's short ticks want the
    // shortest honest time, the long forms stay for titles and the request rows
    clock: function (t) {
      if (t == null || !isFinite(t)) return "";
      return new Date(t * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    },
    dayClock: function (t) {
      if (t == null || !isFinite(t)) return "";
      return new Date(t * 1000).toLocaleDateString(undefined, { month: "numeric", day: "numeric" }) +
        " " + fmt.clock(t);
    },
    // "4m12s ago", measured on the server's clock (snap.now) so a skewed browser clock cannot lie
    ago: function (t, now) {
      if (t == null || now == null) return "";
      return fmt.dur(now - t) + " ago";
    },
    // an axis tick: compact, integers past 100
    axis: function (v) {
      if (v == null || !isFinite(v)) return "";
      return Math.abs(v) >= 100 ? fmt.int(v) : (Math.round(v * 10) / 10).toLocaleString();
    },
  };

  function rangeSeconds(r) {
    var m = /^([0-9]+)([mhd])$/.exec(r);
    if (!m) return 3600;
    return Number(m[1]) * { m: 60, h: 3600, d: 86400 }[m[2]];
  }

  // --- DOM helpers ----------------------------------------------------------------------------

  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v == null || v === false) return;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "style") el.setAttribute("style", v);
        else el.setAttribute(k, v === true ? "" : v);
      });
    }
    (children || []).forEach(function (c) {
      if (c == null || c === false || c === "") return;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return el;
  }

  function byId(id) { return document.getElementById(id); }

  // a labelled tile of the card's fixed field grid (3.7d): ALWAYS drawn, so every live card
  // holds the same twelve tiles in the same places (operator 2026-10-29: absent figures used to
  // vanish and every card packed differently — "hard to scan from card to card"). An absent
  // value is a muted dash, never a placeholder number.
  // `full` (3.7n): the complete value text when the drawn one is a shorter form (compact ctx);
  // it goes into the value's title, so an elided tile can still be read in full.
  function fig(label, text, extra, full) {
    var value = text == null || text === ""
      ? h("span", { class: "num muted", text: "–" })
      : h("span", { class: "num", text: text });
    if (full != null && full !== text) value.setAttribute("title", full);
    return h("span", { class: "fig" }, [h("span", { class: "label", text: label }), value, extra || null]);
  }

  // 3.10f-2 (ruling (n)): a job has one key, its UUID — the pages show the first eight
  // characters everywhere and hold the full id in the title (the started column of 3.10a
  // carries the time; the job page's header shows the full id, being the page's subject).
  // shortId and jobLink are exported: endpoint.js and job.js draw through the same rule.
  function shortId(id) { return String(id).slice(0, 8); }
  function jobLink(id) { return h("a", { href: "#/job/" + encodeURIComponent(id), title: id, text: shortId(id) }); }
  function endpointLink(name) {
    return h("a", { class: "ep-name", href: "#/endpoint/" + encodeURIComponent(name), text: name });
  }

  // --- page state -----------------------------------------------------------------------------

  var state = {
    range: DEFAULT_RANGE,
    snap: null, // the last snapshot
    hists: {}, // endpoint name -> its last history (for histRange)
    histRange: null,
    order: {}, // endpoint name -> index in config order (series colours)
  };

  function colourOf(name) {
    var i = state.order[name];
    return PALETTE[(i == null ? 0 : i) % PALETTE.length];
  }
  function stateColour(s) {
    if (STATE_FIXED[s]) return STATE_FIXED[s];
    var n = 0;
    for (var i = 0; i < s.length; i++) n = (n * 31 + s.charCodeAt(i)) >>> 0;
    return STATE_OTHER[n % STATE_OTHER.length];
  }

  // --- the host cards -------------------------------------------------------------------------

  // 3.7q: one y scale for every live card's sparkline — the maximum in-range gen_tps over all
  // live endpoints' own histories, floored at 1 — so a tall line on one card and a low line
  // on another compare by height across the row. Decision: a module variable computed once
  // per render before the cards are drawn; threading it as a parameter would carry it past
  // every tile that ignores it. The peak label shows it at each sparkline's right edge (the
  // same text on every card); each card's aria-label keeps its own in-range peak.
  var sparkScale = 1;

  // 3.7-fold-3 (3.7m page half): the skip comparison (serialise-minus-`now` of the last
  // rendered snapshot, tagged with the history generation so the post-history render still
  // runs), and the render counters the browser smoke reads: `full` = at least one host card
  // rebuilt, `inPlace` = a changed snapshot whose cards were all kept and only sub-blocks
  // swapped, `skipped` = a snapshot whose content equalled the last one's
  var snapSig = null;
  var historyGen = 0;
  // 3.12b level 1: `tableSkipped` counts dataTable calls whose rows hash matched the last draw
  // of that id (the table touched nothing)
  var renders = { full: 0, inPlace: 0, skipped: 0, tableSkipped: 0 };

  function computeSparkScale(snap) {
    var span = rangeSeconds(state.histRange || state.range);
    var t0 = snap.now - span;
    var max = 0;
    snap.hosts.forEach(function (host) {
      host.endpoints.forEach(function (ep) {
        if (ep.state !== "live") return;
        var hist = state.hists[ep.name];
        if (!hist || !hist.t || !hist.gen_tps) return;
        var t = hist.t;
        var ys = hist.gen_tps;
        for (var i = 0; i < t.length; i++) {
          var v = ys[i];
          if (v != null && t[i] >= t0 && v > max) max = v;
        }
      });
    });
    return max > 1 ? max : 1;
  }

  // the state strip: the endpoint history's `state` series over [now - range, now] as coloured
  // segments (a null, a down probe, is a gap); the current word is the snapshot's own
  // `probe_state` — the newest probe, fresher than the history grid the segments are drawn
  // from — so the history exists only for the segments; nothing when the history has no state
  // series, and no "now" label when the snapshot's probe_state is null (a down probe)
  function stateStrip(ep, now) {
    var hist = state.hists[ep.name];
    if (!hist || !hist.state || !hist.t || !hist.t.length) return null;
    var rangeWord = state.histRange || state.range;
    var span = rangeSeconds(rangeWord);
    var t0 = now - span;
    var strip = h("div", { class: "strip", role: "img", "aria-label": "state over the last " + rangeWord });
    var current = ep.probe_state;
    for (var i = 0; i < hist.t.length; i++) {
      var s = hist.state[i];
      if (s == null) continue;
      // a run of the same state: draw it as one segment
      var j = i;
      while (j + 1 < hist.t.length && hist.state[j + 1] === s) j++;
      var start = Math.max(hist.t[i], t0);
      var end = j + 1 < hist.t.length ? hist.t[j + 1] : now;
      if (end > t0) {
        var left = ((start - t0) / span) * 100;
        var width = Math.max(((end - start) / span) * 100, 0.3);
        strip.appendChild(h("span", {
          style: "left:" + left.toFixed(2) + "%;width:" + width.toFixed(2) + "%;background:" + stateColour(s),
          title: s + " " + fmt.time(hist.t[i]) + "–" + fmt.time(end),
        }));
      }
      i = j;
    }
    // the window the strip (and the card's sparkline) spans, named once per card at the
    // strip row's left: the time axis of a 22 px line has no ticks, so the row says it (3.7q)
    return h("div", { class: "strip-row" }, [h("span", { class: "strip-range muted", text: rangeWord }), strip,
      current ? h("span", { class: "strip-now", text: current }) : null]);
  }

  // a decode-speed sparkline from the endpoint's OWN history's gen_tps (3.7o: the same
  // state.hists entry the strip reads — the fleet grid is the union of every endpoint's probe
  // times, so a healthy endpoint reads mostly nulls there and its card showed a row of dots;
  // the fleet grid stays the fleet charts'). Inline SVG (3.7f per ANAS): it fills the tile's
  // width; the area under the line carries 12% of the line's fleet colour, so the card ties
  // itself to its line in the chart below; a real 0 is a point on the baseline (truthful idle
  // — the server sends 0.0 for a quiet interval and null only for no-data), and a null is a
  // hole — the pen lifts and the next point starts a new run. A lone point between holes shows
  // as a dot; the dots are the page's business, the server only ever sends numbers. The peak
  // and the scale are computed over the IN-RANGE points only (D3): an old spike outside the
  // window used to shrink the visible line and put a peak outside the window in the label.
  function sparkline(name, now) {
    var hist = state.hists[name];
    if (!hist || !hist.t || !hist.gen_tps) return null;
    var t = hist.t;
    var ys = hist.gen_tps;
    var span = rangeSeconds(state.histRange || state.range);
    var t0 = now - span;
    var max = 0, seen = false;
    for (var i = 0; i < t.length; i++) {
      var v = ys[i];
      if (v == null || t[i] < t0) continue;
      seen = true;
      if (v > max) max = v;
    }
    if (!seen) return null;
    // the line is drawn to the shared scale (3.7q, computed once per render); max stays this
    // endpoint's own in-range peak and is what the aria-label reports
    var scale = sparkScale;
    var runs = [], run = null;
    for (var i = 0; i < t.length; i++) {
      var v = ys[i];
      if (v == null || t[i] < t0) { run = null; continue; }
      if (!run) { run = []; runs.push(run); }
      run.push((((t[i] - t0) / span) * 100).toFixed(1) + "," + (23 - (v / scale) * 21).toFixed(1));
    }
    if (!runs.length) return null;
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", "22");
    svg.setAttribute("viewBox", "0 0 100 24");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "decode speed over the last " + (state.histRange || state.range) +
      ", peak " + fmt.speed(max) + " tok/s");
    var colour = colourOf(name);
    runs.forEach(function (pts) {
      if (pts.length > 1) {
        // fill under the line, closed to the baseline below the run's own ends
        var poly = document.createElementNS(NS, "polygon");
        poly.setAttribute("points", pts.join(" ") + " " +
          pts[pts.length - 1].split(",")[0] + ",24 " + pts[0].split(",")[0] + ",24");
        poly.setAttribute("fill", colour);
        poly.setAttribute("fill-opacity", "0.12");
        poly.setAttribute("stroke", "none");
        svg.appendChild(poly);
        var line = document.createElementNS(NS, "polyline");
        line.setAttribute("points", pts.join(" "));
        line.setAttribute("fill", "none");
        line.setAttribute("stroke", colour);
        line.setAttribute("stroke-width", "2");
        line.setAttribute("vector-effect", "non-scaling-stroke");
        svg.appendChild(line);
      } else {
        var dot = document.createElementNS(NS, "circle");
        dot.setAttribute("cx", pts[0].split(",")[0]);
        dot.setAttribute("cy", pts[0].split(",")[1]);
        dot.setAttribute("r", "1.5");
        dot.setAttribute("fill", colour);
        svg.appendChild(dot);
      }
    });
    return svg;
  }

  function jobsOn(snap, name) {
    // 3.7g: the snapshot's jobs is the whole recent log now — the card's block stays the live
    // jobs (the 3.7e contract), so filter the finished tail out again here
    return (snap.jobs || []).filter(function (job) {
      return job.endpoint === name && (job.status === "queued" || job.status === "running");
    });
  }

  // one job as the MCP status line (status.py's `_job_row`): the mark, the job's short id
  // (3.10f-2), the elapsed,
  // then the bits that are actually there joined by ` · ` — calls, files, ctx, t/s,
  // compactions. The marks are status.py's MARK (running "⟳", queued "…"): the mark is the
  // queued word, no badge on the card row.
  // 3.7-fold-5 (the coordinator's read): the status line's activity tail no longer rides the
  // figures' line — at card width the row cut after "2 files · …" and hid the ctx bar, the
  // speed and the errors, the very figures the row exists for. Two FIXED lines instead:
  // `.ep-job-figs` carries the figures; `.ep-act` is the activity's own line, muted (3.7u: both
  // drawn whole — the 60-char cut and the ellipsis are gone, the card's column floor grows to
  // the widest line instead, raiseCardFloor below), ALWAYS present — an empty activity is
  // an empty line of the same height, so the row's height is two pinned lines whatever the
  // job carries (3.7n: a card's height never depends on its values).
  function epJobRow(job) {
    var row = [h("span", { text: job.status === "queued" ? "… " : "⟳ " }),
      jobLink(job.id),
      h("span", { class: "num", text: " " + fmt.dur(job.elapsed_s) })];
    var trail = function (node) {
      row.push(h("span", { class: "muted", text: " · " }));
      row.push(node);
    };
    if (job.tool_calls) {
      trail(h("span", { text: fmt.int(job.tool_calls) + (job.tool_calls === 1 ? " call" : " calls") }));
    }
    if (job.files > 0) { // the status line's files bit (D1); `_n(files, "file")`'s form
      trail(h("span", { text: fmt.int(job.files) + (job.files === 1 ? " file" : " files") }));
    }
    if (job.ctx_pct != null) {
      var w = Math.max(0, Math.min(100, job.ctx_pct));
      trail(h("span", { class: "ep-ctx", title: fmt.int(job.ctx_tokens) + " / " + fmt.int(job.context) + " tokens" }, [
        h("span", { class: "bar" + (w >= 80 ? " high" : "") }, [h("span", { style: "width:" + w + "%" })]),
        h("span", { class: "num", text: fmt.pct(job.ctx_pct) }),
      ]));
    }
    if (job.gen_tps != null) trail(h("span", { class: "num", text: fmt.speed(job.gen_tps) + " t/s" }));
    if (job.compactions > 0) {
      trail(h("span", { class: "badge", text: fmt.int(job.compactions) + " compaction" + (job.compactions === 1 ? "" : "s") }));
    }
    // D2 (the operator: "I see an error count, but don't know what it is or means"): the
    // badge says what it counts, with 3.7i's wording so the jobs table can take it verbatim
    if (job.errors > 0) {
      trail(h("span", {
        class: "badge err",
        title: "tool results that came back as errors; the job page lists them by tool",
        text: fmt.int(job.errors) + " tool error" + (job.errors === 1 ? "" : "s"),
      }));
    }
    // 3.7u: the activity is drawn whole — no 60-char cut, no ellipsis; the card grows to hold
    // it (raiseCardFloor). The title keeps the same text for a capped card whose line scrolls.
    var act = oneLine(job.last_activity);
    return h("div", { class: "ep-job" }, [
      h("div", { class: "ep-job-figs" }, row),
      h("div", { class: "ep-act", title: act || null, text: act || "" }),
    ]);
  }

  // the card's jobs block (3.7e): always present, always the card's last child, so it never
  // moves between renders. Our jobs get one status line each; the engine being busy with
  // someone else's work (vLLM's own load, another harness) is not a listable job of ours —
  // the load tile already says busy, so the block says so too.
  function jobsBlock(snap, ep) {
    var mine = jobsOn(snap, ep.name);
    if (mine.length) return h("div", { class: "ep-jobs" }, mine.map(epJobRow));
    var busy = (ep.slots && ep.slots.busy > 0) || (ep.requests && ep.requests.running > 0);
    return h("div", { class: "ep-jobs muted", text: busy ? "busy, no local job" : "no jobs" });
  }

  // the KV tile is a meter (3.7f): the jobs table's bar (accent fill, warning at >= 80) with
  // the percent beside it — the second colour on the card is earned by the figure an operator
  // acts on. Absent usage is the ordinary dash, same as every other tile.
  function kvTile(ep) {
    if (ep.kv_cache_usage == null) return fig("KV", "");
    var w = Math.max(0, Math.min(100, ep.kv_cache_usage * 100));
    return h("span", { class: "fig" }, [
      h("span", { class: "label", text: "KV" }),
      h("span", { class: "kv" }, [
        h("span", { class: "bar" + (w >= 80 ? " high" : "") }, [h("span", { style: "width:" + w + "%" })]),
        h("span", { class: "num", text: fmt.frac(ep.kv_cache_usage) }),
      ]),
    ]);
  }

  // the load tile's text follows the MCP status line's load_summary form (status.py): `1/2
  // slots` plus ` N wait` when Strata reports a queue beside its slots, or `2 run 1 wait` for
  // a slotless engine (vLLM). The KV percent is not part of it: it is its own tile's figure
  function loadText(ep) {
    if (ep.slots) {
      return fmt.int(ep.slots.busy) + "/" + fmt.int(ep.slots.total) + " slots"
        + (ep.requests && ep.requests.waiting > 0 ? " " + fmt.int(ep.requests.waiting) + " wait" : "");
    }
    if (ep.requests) {
      return fmt.int(ep.requests.running) + " run"
        + (ep.requests.waiting > 0 ? " " + fmt.int(ep.requests.waiting) + " wait" : "");
    }
    return "";
  }

  // the card's field grid (3.7d): four columns, three rows, the same twelve tiles on every
  // live card — load | KV | decode (+sparkline) | stream / prompt | cache hit | ctx | probe /
  // GPU | VRAM | temp | power. Row 3 is Strata's hardware: llama.cpp and vLLM report none, so
  // it is four dashes today, and Epic 4.3 fills it. A tile is label above value.
  function fieldGrid(snap, ep) {
    var rates = ep.rates || {};
    var hw = ep.hardware || {};
    var cacheHit = null;
    if (rates.cached_tps != null && rates.prompt_tps != null && rates.prompt_tps + rates.cached_tps > 0) {
      cacheHit = rates.cached_tps / (rates.prompt_tps + rates.cached_tps);
    }
    // 3.7n: the unit lives in the label, the value is the bare number — the tile is always one
    // label line and one value line, so a card's height never depends on its values
    // (operator 2026-10-29: "when the prompt t/s isn't zero it overflows the size causing it
    // to wrap down a line"). gibOf is fmt.gib without the unit.
    var gibOf = function (b) {
      return (b / 1073741824).toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    };
    var vram = hw.gpu_mem_used != null
      ? gibOf(hw.gpu_mem_used) + (hw.gpu_mem_total != null ? " / " + gibOf(hw.gpu_mem_total) : "")
      : "";
    var power = hw.gpu_power != null
      ? fmt.int(hw.gpu_power) + (hw.gpu_power_limit != null ? " / " + fmt.int(hw.gpu_power_limit) : "")
      : "";
    // the decode tile's value, sparkline and strip share the page's window (3.7q: the time
    // axis is the page's range control); the sparkline carries the shared-scale peak label at
    // its right edge. 3.7-fold-5: 3.7q's two places become one — the strip row's left already
    // names the window once per card, so the label is the bare unit again. At a quarter-card
    // the longer form clipped to "decode tok/s · l…", and the title that hid the loss went
    // with it: nothing left to hide.
    var spark = sparkline(ep.name, snap.now);
    var decodeTile = fig("decode tok/s", rates.gen_tps != null ? fmt.speed(rates.gen_tps) : "",
      spark ? h("span", { class: "spark-wrap" }, [spark,
        h("span", { class: "spark-peak muted", text: "↑ " + fmt.speed(sparkScale) })]) : null);
    return h("div", { class: "figures" }, [
      fig("load", loadText(ep)),
      kvTile(ep),
      decodeTile,
      fig("stream tok/s", ep.gen_tps_live != null ? fmt.speed(ep.gen_tps_live) : ""),
      fig("prompt tok/s", rates.prompt_tps != null ? fmt.speed(rates.prompt_tps) : ""),
      fig("cache hit", fmt.frac(cacheHit)),
      fig("ctx", fmt.compact(ep.context), null, fmt.int(ep.context)),
      fig("probe ms", ep.latency_ms != null ? fmt.int(ep.latency_ms) : ""),
      fig("GPU", fmt.pct(hw.gpu_util)),
      fig("VRAM GiB", vram),
      fig("temp °C", hw.gpu_temp != null ? fmt.int(hw.gpu_temp) : ""),
      fig("power W", power),
    ]);
  }

  // the card shows the short model form the MCP status line shows (status.py's short_model):
  // last path segment, drop from the first colon, drop a trailing quantizer suffix. The full
  // served id rides in the title, and the endpoint detail page keeps the full id everywhere
  function shortModel(id) {
    var name = id.split("/").pop().split(":")[0];
    ["-GGUF", "-gguf", "-EXL3", "-exl3"].forEach(function (suffix) {
      if (name.length >= suffix.length && name.slice(-suffix.length) === suffix) {
        name = name.slice(0, -suffix.length);
      }
    });
    return name || id;
  }

  function liveEndpoint(snap, ep) {
    var rates = ep.rates || {};
    var act = activeInfo(ep, snap);
    return h("div", { class: "endpoint state-live", "data-endpoint": ep.name }, [
      // the state dot (3.7f): the first thing on the head; the down/dormant kinds take the
      // same span with their variant (their CSS lives here too — see the 3.7d-f block)
      h("div", { class: "ep-head" }, [h("span", { class: "sdot sdot-live", "aria-hidden": "true" }),
        endpointLink(ep.name), h("span", { class: "tag", text: ep.backend }),
        ep.model ? h("span", { class: "ep-model", title: ep.model, text: shortModel(ep.model) }) : null,
        // 3.7-fold-3 (3.7s page half): the last-active phrase at the right of the head line;
        // down/dormant endpoints keep their "last seen" lines and gain nothing here
        h("span", { class: "ep-active muted", title: act.title || null, text: act.text })]),
      stateStrip(ep, snap.now),
      fieldGrid(snap, ep),
      rates.reason ? h("div", { class: "ep-note ep-line muted", text: "speeds: " + rates.reason }) : null,
      jobsBlock(snap, ep),
    ]);
  }

  // `quiet`: the host card already carries the one alert (a node-down card, spec §4: "one alert
  // at the host card; endpoints greyed, not each alarming"), so the endpoint's sentence is left out
  function downEndpoint(snap, ep, quiet) {
    return h("div", { class: "endpoint state-down", "data-endpoint": ep.name }, [
      // the state dot (3.7c lane contract): first child of the head; lane B styles .sdot
      h("div", { class: "ep-head" }, [h("span", { class: "sdot sdot-" + ep.state, "aria-hidden": "true" }),
        endpointLink(ep.name), h("span", { class: "tag", text: ep.backend }),
        h("span", { class: "ep-seen ep-line", text: ep.last_seen != null ? "last seen " + fmt.ago(ep.last_seen, snap.now) : "" })]),
      ep.error && !quiet ? h("div", { class: "ep-error ep-line", text: ep.error }) : null,
      // the id beside the name makes the sentence clickable; the name alone stays plain text
      ep.dropped_job_id ? h("div", { class: "ep-jobs ep-line" }, ["running when it dropped: ",
        jobLink(ep.dropped_job_id)]) : null,
    ]);
  }

  function dormantEndpoint(ep) {
    var seen = ep.last_seen != null ? "last seen " + fmt.date(ep.last_seen) : "never seen";
    // the state dot (3.7c lane contract) is the first child of this one-line element; it has no
    // ep-head of its own; lane B styles .sdot
    return h("div", { class: "endpoint state-dormant ep-line", "data-endpoint": ep.name },
      [h("span", { class: "sdot sdot-" + ep.state, "aria-hidden": "true" }),
       endpointLink(ep.name), " ", h("span", { text: ep.backend + ", " + seen })]);
  }

  // 3.7a (operator 2026-10-29: "I would not display cards that are dormant and never seen, as
  // they are just clutter"): a never-seen dormant endpoint is not drawn. The down-node
  // exception this rule once carried is gone (3.7y, operator 2026-10-30: "ai9 is 'down' yes,
  // but it's also 'never seen'. Perhaps it shouldn't have a card in that state?"): a never-seen
  // endpoint is hidden on a down node too, and a host left with nothing to draw — down included
  // — gets no card (hostCard). render() counts the hidden ones under a line naming each node's
  // state, so the node's own alert stays visible without a card drawn for it.
  function neverSeenOn(ep) {
    return ep.state === "dormant" && ep.last_seen == null;
  }

  // 3.7b (operator 2026-10-29: "the flow isn't very logical ... taking an entirely different
  // size, shape, and form"): every host is a card of the same shape — all-dormant collapses to
  // a one-line full-width bar no more — in snapshot order, so a card never moves when its
  // state changes; nothing spans the grid.
  function hostCard(snap, host) {
    var cls = "host";
    if (host.state === "down") cls += " node-down";
    else if (host.state === "unknown") cls += " node-unknown";
    var nodeDown = host.state === "down";
    // 3.7x (operator 2026-10-30: "We dont' need the exposition on ai9 card - 'down' is a good
    // summary state"): the head's note is the state word; the probe's sentence (every endpoint
    // on a down node reports the box unreachable: the first one's) rides in the title, so a
    // ~120-character "timed out (host off, or a firewall dropping packets)" no longer sets the
    // column floor (3.7u) — the sentence stays in the snapshot and on the endpoint page
    var note = nodeDown ? "down" : host.state === "unknown" ? "unknown" : "";
    // F1 (fold of the batch review, 2026-10-30): an unknown node is an alert like a down one —
    // the same one-word note, the probe's sentence in its title, and the endpoint's error line
    // quiet (spec §4 True-now: "down" (or "unknown"); no `.ep-error` line either way)
    var alert = host.state === "down" || host.state === "unknown";
    var said = alert ? host.endpoints.filter(function (ep) { return ep.error; })[0] : null;
    var noteTitle = said ? said.error : null;
    var body = host.endpoints.map(function (ep) {
      if (ep.state === "live") return liveEndpoint(snap, ep);
      if (ep.state === "down") return downEndpoint(snap, ep, alert);
      return neverSeenOn(ep) ? null : dormantEndpoint(ep);
    }).filter(Boolean);
    if (!body.length) return null; // 3.7a: a host with nothing drawn; 3.7y: a down node included
    return h("div", { class: cls, "data-host": host.name }, [
      h("div", { class: "host-head" }, [h("span", { class: "host-name", text: host.name }),
        note ? h("span", { class: "host-note ep-line", title: noteTitle, text: note }) : null]),
    ].concat(body));
  }

  // --- the Recent jobs log (story 3.7g) ---------------------------------------------------------

  // 3.10a (the operator: "Recent jobs doesn't have a timestamp - so answering 'when' is not
  // possible from the screen... it shows 'speed' but really that's decode - where's prefill
  // speed?"): a day-stamped "started" column, and the one speed column split into the pair the
  // requests table already shows
  var JOB_COLUMNS = ["job", "status", "started", "model", "endpoint", "elapsed", "turns", "tool calls",
    "tool errors", "context", "prefill tok/s", "decode tok/s", "last activity"];
  var NUMERIC = { elapsed: 1, turns: 1, "tool calls": 1, "tool errors": 1,
    "prefill tok/s": 1, "decode tok/s": 1 };
  // 3.12a (spec §3): what KIND of filter each column takes — pick (a native select), number
  // (text, then x-y, then <n / >n), time (text, then time literals), text (substring, today's
  // search); the kind is read by dataTable, default text. UNIT is the sort key's unit, named in
  // the box's title so a typed bound can be checked against what the column holds.
  var FILTER_KIND = { status: "pick", started: "time", model: "pick", endpoint: "pick",
    elapsed: "number", turns: "number", "tool calls": "number", "tool errors": "number",
    context: "number", "prefill tok/s": "number", "decode tok/s": "number" };
  var UNIT = { elapsed: "seconds", context: "%", turns: "count", "tool calls": "count",
    "tool errors": "count", "prefill tok/s": "tokens/s", "decode tok/s": "tokens/s" };
  // 3.7i: the count is tool RESULTS that came back as errors, not a failing job — the header
  // says so, in the card badge's own words (the sentence epJobRow already carries), and a
  // non-zero count is a link: the job page lists them by tool
  var COLUMN_TITLES = { "tool errors": "tool results that came back as errors; the job page lists them by tool" };

  // 3.7g: the pill is a WORD, so the state never rests on colour alone; succeeded reads "done"
  // (a finished job in a log is done, not a report label), failed and cancelled keep their own
  // words, and the class is mechanical — pill-<status>, the seven names db.py's status check
  // allows, every one styled
  function statusPill(status) {
    var cls = "pill pill-" + status;
    return h("span", { class: cls, text: status === "succeeded" ? "done" : status });
  }

  // the day-stamped time cell both tables share (3.10a: factored out of requestsTable's time
  // column, 3.7-fold-4's rule, so the log's "started" reads like the request rows do): inside
  // 24 h of the caller's clock the clock time, past it the date and time; the title always
  // holds the full date-time
  function timeTd(t, now) {
    return h("td", {
      text: t != null && now != null && now - t < 86400 ? fmt.time(t) : fmt.date(t),
      title: t == null ? null : fmt.date(t),
    });
  }

  // 3.12c (spec §3): the local day of an epoch-seconds key as "YYYY-MM-DD", read BY HAND from
  // the Date's components (the app's rule: never Date.parse, never a locale formatter for a
  // comparison — the cells print local time, so the filter's day is local too).
  function localDay(v) {
    var d = new Date(v * 1000);
    var mo = d.getMonth() + 1, day = d.getDate();
    return d.getFullYear() + "-" + (mo < 10 ? "0" + mo : mo) + "-" + (day < 10 ? "0" + day : day);
  }

  // 3.12c (spec §3): the time filter's whole grammar, narrowed to the picker's shapes —
  // "YYYY-MM-DD" (that local day), "YYYY-MM-DD HH-HH" (that day, hours INCLUSIVE over whole
  // hours: 09-11 is 09:00:00 through 11:59:59; from > to matches nothing, as a number range
  // does) or, 3.12f (the operator: hours with no date is "any day, between these hours"),
  // "HH-HH" (EVERY day, the same inclusive whole-hour span and from > to rule; two digits
  // each). A null or non-finite key never matches; any other shape matches nothing — the
  // typed literals of 3.12a §6 (HH:MM, a - b, <a, >a, and the substring reading) are retired
  // with the box, and the panel is the state's only writer. The state stays a plain string,
  // so the smoke (or a hand-set view) can still write one.
  function timeFilterMatch(f, v) {
    if (typeof v !== "number" || !isFinite(v)) return false;
    var from, to;
    var m = /^(\d{4}-\d{2}-\d{2})(?: (\d{2})-(\d{2}))?$/.exec(f);
    if (m) {
      if (localDay(v) !== m[1]) return false;
      if (m[2] === undefined) return true;
      from = +m[2]; to = +m[3];
    } else {
      m = /^(\d{2})-(\d{2})$/.exec(f); // 3.12f: the dayless shape — every day, hours only
      if (!m) return false;
      from = +m[1]; to = +m[2];
    }
    if (from > to) return false;
    var hour = new Date(v * 1000).getHours();
    return hour >= from && hour <= to;
  }

  // 3.12d (the operator: "the columns display format 00m00s but filter is text search, so I
  // can't do (for example) <10m for a filter"): read a bound typed in the unit the column
  // holds — the unit is the sort key's, the same one the box's title names. Commas the cells
  // print go first (3.12a fold F2's line, moved in here). Then by unit: "seconds" reads the
  // duration grammar fmt.dur prints ("10m" 600, "1h30m" 5400, "90s" 90, "2h" 7200, "1d4h"
  // 100800; even a whole cell like "2h03m"); "ms" reads "800ms", "1.2 s", "2m", "1h" to
  // milliseconds; "%" strips one trailing percent. AFTER the unit reading, a bare number is
  // read only when the WHOLE remainder is one — so ">10x" no longer reads 10 the way today's
  // parseFloat did; it answers NaN and the box falls to the substring, the better reading of
  // a typo. A bare number keeps the sort key's unit: "<600" on elapsed is still seconds.
  function readNum(unit, s) {
    var t = String(s).replace(/,/g, "");
    var m;
    if (unit === "seconds") {
      // 3.12d fold: a decimal in any part ("1.5m", "0.5d") — fmt.dur never prints one, but a
      // typed shorthand that read nothing was the silent failure the review found
      m = /^(?:(\d+(?:\.\d+)?)d)?(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$/.exec(t);
      if (m && (m[1] !== undefined || m[2] !== undefined || m[3] !== undefined || m[4] !== undefined)) {
        return (+(m[1] || 0)) * 86400 + (+(m[2] || 0)) * 3600 + (+(m[3] || 0)) * 60 + (+(m[4] || 0));
      }
    } else if (unit === "ms") {
      m = /^(\d+(?:\.\d+)?)\s*(ms|s|m|h)$/.exec(t);
      if (m) {
        var mult = m[2] === "ms" ? 1 : m[2] === "s" ? 1000 : m[2] === "m" ? 60000 : 3600000;
        return +m[1] * mult;
      }
    } else if (unit === "%") {
      t = t.replace(/%$/, "");
    }
    return /^-?\d+(?:\.\d+)?$/.test(t) ? +t : NaN;
  }

  // one job row (3.10b): the cells are keyed by their JOB_COLUMNS name; the table helper draws
  // them in column order minus `omit` (the column list it is given is already filtered), so a
  // column left out leaves no empty cell behind. 3.10c: the row element itself (and its
  // data-job attribute) is the helper's — this returns the cell map.
  function jobRow(job, now) {
    var ctx = null;
    if (job.ctx_pct != null) {
      var w = Math.max(0, Math.min(100, job.ctx_pct));
      ctx = h("span", { title: fmt.int(job.ctx_tokens) + " / " + fmt.int(job.context) + " tokens" }, [
        h("span", { class: "bar" + (w >= 80 ? " high" : "") }, [h("span", { style: "width:" + w + "%" })]),
        h("span", { class: "num", text: fmt.pct(job.ctx_pct) }),
      ]);
    }
    // 3.7g: no queued badge here — the status pill says it. The compaction badge stays: a count
    // is something the pill cannot carry. The server-gone badge is gone (3.10a, the operator
    // asked its use): on a finished row it is the normal end state of every job — no
    // information — and on a live one the state belongs in the pill: the job page's word for a
    // record that says running while nothing runs it is "lost" (spec §6).
    var lost = job.server_live === false && (job.status === "running" || job.status === "queued");
    // 3.10f-2: the first cell is the short id (title: the full one); the id is the job's one key
    var idCell = [jobLink(job.id)];
    if (job.compactions > 0) {
      idCell.push(h("span", { class: "badge", text: fmt.int(job.compactions) + " compaction" + (job.compactions === 1 ? "" : "s") }));
    }
    var cells = {
      job: h("td", null, idCell),
      status: h("td", null, [statusPill(lost ? "lost" : job.status)]),
      started: timeTd(job.started_at, now),
      model: h("td", { class: "cut", title: job.model || "", text: job.model || "" }),
      endpoint: h("td", null, [job.endpoint ? endpointLink(job.endpoint) : null]),
      elapsed: h("td", { class: "n", text: fmt.dur(job.elapsed_s) }),
      turns: h("td", { class: "n" }, [
        // 3.7h: a live job's turn count is 0 until the first assistant turn lands, and a plain 0
        // reads like a counter that never ran — a muted dash until the number is a figure
        job.status === "running" && !job.num_turns
          ? h("span", { class: "muted", text: "–" })
          : h("span", { text: fmt.int(job.num_turns) })]),
      "tool calls": h("td", { class: "n", text: fmt.int(job.tool_calls) }),
      // 3.7i: the badge is a link — the count is tool results that came back as errors, and
      // the job page is where they are listed by tool
      "tool errors": h("td", { class: "n" }, [job.errors > 0
        ? h("a", { class: "badge err", href: "#/job/" + encodeURIComponent(job.id),
          title: COLUMN_TITLES["tool errors"], text: fmt.int(job.errors) })
        : h("span", { text: fmt.int(job.errors) })]),
      context: h("td", null, [ctx]),
      // 3.10a: the pair, the snapshot's own job figures (derive.gen_tps, derive.prefill_tps);
      // fmt.speed as before — the unit is in the headers now, as in the requests table
      "prefill tok/s": h("td", { class: "n", text: job.prefill_tps != null ? fmt.speed(job.prefill_tps) : "" }),
      "decode tok/s": h("td", { class: "n", text: job.gen_tps != null ? fmt.speed(job.gen_tps) : "" }),
      // 3.7g: no "current tool" column — on a running row it was always the same line as the
      // last activity, and on a finished row it is history; the activity cell below holds the
      // full text in its title
      "last activity": h("td", { class: "cut", title: job.last_activity || "", text: oneLine(job.last_activity) }),
    };
    return cells;
  }

  function oneLine(text) {
    if (!text) return "";
    return String(text).split("\n")[0];
  }

  // --- 3.10c: the one table helper — paging, sorting, per-column filters, all client-side -----
  //
  // (the operator, EPICS 3.10: "it shows me we need pagination on our tables - this one I'm
  // looking at as 889 lines, which is not useful. Pagination with local column filters and
  // sorting?" — "3.10c - yes, client side filter.") The helper works over the rows already
  // fetched: the endpoint requests route's 1000-row cap and its whole-range totals
  // (read.py's endpoint_requests) are unchanged — the table pages what is on the page.
  //
  // `columns`: [{name, numeric, title, cell(row) -> td, sortKey(row)}] — the renderers hand
  // over their existing header names and cell builders unchanged, plus a sortKey that matches
  // what the cell shows: a number where the cell is a figure, the timestamp for the time
  // cells, the pill's word for a status, the id for the id cell. `rows` the whole list in the
  // caller's order (with no sort set the table keeps it — 3.7g's log order stays the
  // contract). `opts`: `empty` the sentence an empty list draws, `note` the line under the
  // pager, `page` the page size (default 50), `class` the table element's class,
  // `rowAttrs(row)` extra attrs for each <tr> (the jobs table's data-job; the requests
  // table's req-err / req-ctx marks).
  //
  // The view lives in TABLE_STATE, a module-level map keyed by the table id — never in the
  // DOM: the overview replaces #jobs-body on every snapshot (render()) and the detail pages
  // redraw on theirs, so a push must find the sort, the filters and the page waiting. A
  // re-render from a NEW rows array applies the same state; a page now past the end clamps
  // to the last page. An input event updates the state and redraws THIS table only, through
  // the container the helper owns. 3.12b: that redraw is a patch — the box itself is never
  // replaced, so the caret and half-typed bounds stay put (the old caret save/restore, whose
  // saved value was never assigned, is gone with the rebuild).
  var TABLE_PAGE_SIZE = 50;
  var TABLE_STATE = {}; // table id -> {sort: null | {col, dir}, filters: {col: text}, page: n}
  // 3.12b (spec §3): the last draw per id — {container, hash, draw}. A container discarded by a
  // page change fails its isConnected check, so the next call is a full build; entries are
  // overwritten, never more than one per id.
  var TABLE_DOM = {};
  // 3.12a fold F1: the pick's value for a row whose sort key is "" — "any" already owns ""
  // (and setFilter deletes on it), so the blank key needs a value of its own (the empty-set
  // sign, like the page's "⌀ cancelled" pill; a non-ASCII glyph in a value is no new thing)
  var PICK_BLANK = "∅";

  function dataTable(id, columns, rows, opts) {
    opts = opts || {};
    rows = rows || [];
    var pageSize = opts.page || TABLE_PAGE_SIZE;
    var st = TABLE_STATE[id] || (TABLE_STATE[id] = { sort: null, filters: {}, page: 1 });
    // 3.12b level 1 (spec §3): a push whose rows hash the same as the last draw of this id
    // touches nothing — the hash is the whole rows array serialized plus the column names in
    // order (a different column set is a different table). The page already pays this string
    // for the whole snapshot (render's snapSig) and 120 rows is under a millisecond. The design
    // accepts that a running job's row changes every push (its elapsed, turns and last activity
    // arrive in the row).
    // 3.12b fold (the review): the caller's note and empty text are drawn from opts, so they are
    // part of what a skip would leave stale (the endpoint page's note names the range)
    // 3.12c fold F2 (the review): the table's CLOCK is drawn from opts too — the picker's
    // summary says "today"/"yesterday" by opts.now's local day, and the started cells day-stamp
    // against it. The hash carries that local DAY, so the first push of a new local day is a
    // real change and redraws once; a push inside the same day is still skipped. The redraw
    // patches the summary and the panel; it does NOT switch an unchanged row's started cell
    // from clock to date — that row's tr is kept by its signature (patchBody), so the cell
    // waits for the row's next real change (the spec's accepted exception; the Qwen review
    // of the fold proved the sentence that said otherwise false).
    var hash = JSON.stringify(rows) + "\n" + columns.map(function (c) { return c.name; }).join(",")
      + "\n" + (opts.note || "") + "\n" + (opts.empty || "")
      + "\n" + (opts.now != null && isFinite(opts.now) ? localDay(opts.now) : "");
    var prev = TABLE_DOM[id];
    if (prev && prev.container.isConnected && prev.hash === hash) {
      renders.tableSkipped++;
      return prev.container;
    }
    // level 2 (and the first build): the container persists — an entry with a connected
    // container keeps it; a page that discarded the DOM gets a new one
    var container = prev && prev.container.isConnected
      ? prev.container
      : h("div", { class: "table-block", "data-table": id });
    // the record this call updates: the last draw's entry when its container is still in the
    // document (the container above is then its container), else a fresh one
    var rec = prev && prev.container.isConnected ? prev : { container: container };

    // the filter matches what the operator sees: the cell's own text, plus every title the
    // cell carries — its own (the started cell's full date-time behind the clock; the model
    // and path cells' full text) and a child's (the id link's full uuid behind the short id;
    // the status pill's numeric http status behind "ok"). A title equal to the text adds
    // nothing, so no guard is needed; a SUPERSET is exactly the case to keep — the old
    // "shares nothing with the text" guard dropped the id and time titles the operator pastes.
    // 3.11a: the text is built ONCE per row and column for this table's life (lower-cased,
    // keyed by the row's index in `rows`): a cell is a function of its row and the caller's
    // opts, both fixed here — a push or a new list is a new dataTable call with its own cache.
    // Building the td per row per filtered column on every keystroke cost 175 ms at 1,000 rows.
    var textCache = {}; // column name -> [lower-cased filter text by row index]
    function cellText(col, i) {
      var byCol = textCache[col.name] || (textCache[col.name] = []);
      if (byCol[i] !== undefined) return byCol[i];
      var td = col.cell(rows[i]);
      var own = td.textContent;
      var titles = [];
      if (td.getAttribute("title")) titles.push(td.getAttribute("title"));
      Array.prototype.forEach.call(td.querySelectorAll("[title]"), function (el) {
        var v = el.getAttribute("title");
        if (v) titles.push(v);
      });
      var t = titles.join(" ");
      byCol[i] = (t ? own + " " + t : own).toLowerCase();
      return byCol[i];
    }

    // 3.12a: a pick column reads the sort key of EVERY row on every draw (the option values and
    // their counts), and requestsTable's sort keys run through rowMemo — uncached, a 120-row
    // table rebuilt every row's cells 240 times per draw. Like the filter text, the sort key is
    // a function of the row alone: one cache, same life (per dataTable call).
    var keyCache = {}; // column name -> [sortKey value by row index]
    function keyOf(col, i) {
      var byCol = keyCache[col.name] || (keyCache[col.name] = []);
      if (byCol[i] !== undefined) return byCol[i];
      byCol[i] = col.sortKey(rows[i]);
      return byCol[i];
    }

    // The rows that pass every column's filter, as row INDICES (the caches' key). `except` skips
    // one column's own filter (3.12a §4: the pick's option counts are faceted — how many rows
    // match the OTHER columns' filters, so a value at 0 stays listed). 3.12c fold F1: the skip
    // is by NAME, not object identity — the persistent time button outlives a push, and after
    // one the current call's columns are new objects, so an identity check let the reopened
    // panel facet its date counts and hours by its own time filter
    function filterIdx(except) {
      var idx = rows.map(function (row, i) { return i; }); // filtered by index: the cache's key
      columns.forEach(function (col) {
        if (except && col.name === except.name) return;
        var f = st.filters[col.name];
        if (!f) return;
        f = String(f);
        // 3.12a fold F2: the substring reading at the bottom matches the box AS TYPED — the
        // cells print fmt.int's thousands separators ("1,300"), so "1,3" is a real text needle
        var typed = f;
        if (col.filter === "pick") {
          // 3.12a §4: one value, matched exactly on the sort key — the box's text IS the value.
          // 3.12a fold F1: the blank key ("") is pickable under PICK_BLANK, because "any"
          // already owns "" (and setFilter deletes on it)
          idx = idx.filter(function (i) {
            var k = String(keyOf(col, i));
            return f === PICK_BLANK ? k === "" : k === f;
          });
          return;
        }
        if (col.filter === "number") {
          // 3.12a §5, in order: `x-y` an INCLUSIVE range on the sort key (the operator's
          // "absolute and inclusive endpoints"; x > y matches nothing; a row with no finite
          // figure never matches); then 3.10c's strict `>n` / `<n`; else the substring below.
          // 3.12a fold F2 (comma stripping) and 3.12d (the cell's own unit) now live in
          // readNum, which every bound goes through: the range is two tokens around the
          // hyphen, each end read by the unit ("5m-10m", "800ms-1.2s", "50%-80%"); an end
          // that reads NaN falls through, as an unparseable range always did, to the
          // comparator and then the typed substring
          // 3.12d fold (the review): a token may hold an inner space — the duration cells print
          // "1.2 s" and "800 ms", and the bound typed as the cell prints it must read; the
          // whitespace around each token is trimmed by the \s* on either side of the hyphen
          var numRange = /^\s*(-?[^-]+?)\s*-\s*(-?[^-]+?)\s*$/.exec(f);
          if (numRange) {
            var lo = readNum(col.unit, numRange[1]), hi = readNum(col.unit, numRange[2]);
            if (!isNaN(lo) && !isNaN(hi)) {
              idx = idx.filter(function (i) {
                var v = keyOf(col, i);
                return typeof v === "number" && isFinite(v) && v >= lo && v <= hi;
              });
              return;
            }
          }
          var numCmp = /^\s*([<>])\s*(.+?)\s*$/.exec(f); // the operand may hold a space ("1.2 s")
          if (numCmp) {
            var want = readNum(col.unit, numCmp[2]);
            if (!isNaN(want)) {
              var gt = numCmp[1] === ">";
              idx = idx.filter(function (i) {
                var v = keyOf(col, i);
                return typeof v === "number" && isFinite(v) && (gt ? v > want : v < want);
              });
              return;
            }
          }
        }
        if (col.filter === "time") {
          // 3.12c (spec §3): the picker's state string, matched whole by timeFilterMatch. No
          // fall-through: the typed time grammar and the substring reading of 3.12a §6 are
          // retired with the box, so a state in any other shape matches nothing.
          idx = idx.filter(function (i) { return timeFilterMatch(f, keyOf(col, i)); });
          return;
        }
        var needle = typed.toLowerCase();
        idx = idx.filter(function (i) { return cellText(col, i).indexOf(needle) >= 0; });
      });
      return idx;
    }

    // 3.12a §4: the pick's option values — every distinct String(sortKey) across ALL rows,
    // sorted as the column sorts (sortRows' comparator shape: two numbers of the same shape
    // compare numerically, everything else localeCompare). A value the state holds but the rows
    // no longer have (a push dropped it) is appended so it stays selected, listed "(0)".
    function pickValues(col) {
      var seen = Object.create(null), out = [];
      rows.forEach(function (row, i) {
        var v = String(keyOf(col, i));
        if (!Object.prototype.hasOwnProperty.call(seen, v)) { seen[v] = 1; out.push(v); }
      });
      out.sort(function (a, b) {
        // 3.12a fold F1: the blank key sorts first after "any" (it is the "none" value)
        if (a === "" || b === "") return a === b ? 0 : (a === "" ? -1 : 1);
        var re = /^-?\d+(?:\.\d+)?$/;
        if (re.test(a) && re.test(b)) return parseFloat(a) - parseFloat(b);
        return a.localeCompare(b);
      });
      // 3.12a fold F1: the state holds the UI value, the rows the data key — a held blank is ""
      var held = st.filters[col.name];
      var heldKey = held === PICK_BLANK ? "" : held;
      if (held != null && out.indexOf(String(heldKey)) < 0) out.push(String(heldKey));
      // the option values are the UI values: "" (the blank key) reads as PICK_BLANK
      return out.map(function (v) { return v === "" ? PICK_BLANK : v; });
    }

    // 3.12a §7: one write path for both controls — a kind's EMPTY value deletes its key (blank
    // text, "any"), so the state carries a filter only when one is set; any change restarts
    // the page, a filter changing the count
    function setFilter(col, value) {
      if (value == null || value === "") delete st.filters[col.name];
      else st.filters[col.name] = value;
      st.page = 1;
    }

    // 3.12b: the sort works over INDICES (filterIdx's output) and reads the sort key through
    // keyOf, the per-call cache the pick already used — the 3.12a review noticed the old
    // version sorted ROW objects through col.sortKey directly, bypassing the cache. The
    // comparator and its absent-value rule are untouched (tests/test_3_10c_fold.py F2 pins
    // the source shape).
    function sortRows(list) { // list: row INDICES
      if (!st.sort) return list; // the caller's order is the contract (3.7g)
      var col = null;
      columns.forEach(function (c) { if (c.name === st.sort.col) col = c; });
      if (!col) return list;
      var dir = st.sort.dir;
      // two finite numbers compare numerically wherever the column map calls them numeric
      // (the jobs table's "context" holds a percent the map does not mark .n); everything
      // else compares as text. A missing value — no finite figure, or a text column's null or
      // blank ("last activity" on a job with none) — is ABSENT, not small: it sorts past every
      // present value in BOTH directions, so blanks sit at the end of the page whichever way
      // the arrow points (F2 fold: the old sentinel multiplied by dir and put text blanks
      // through localeCompare, so a descending sort led with them)
      return list.slice().sort(function (a, b) {
        var x = keyOf(col, a), y = keyOf(col, b);
        var xnum = typeof x === "number" && isFinite(x), ynum = typeof y === "number" && isFinite(y);
        // present: a finite figure or a non-empty text (the xn/yn names, not numericness)
        var xn = xnum || (x != null && x !== ""), yn = ynum || (y != null && y !== "");
        if (xn !== yn) return xn ? -1 : 1;
        if (xnum && ynum) return (x - y) * dir;
        return String(x == null ? "" : x).localeCompare(String(y == null ? "" : y)) * dir;
      });
    }

    // ---- 3.12b level 2: the patch (spec §4) --------------------------------------------------
    //
    // The parts persist per id inside the container: wrap > table > thead[head, filterRow] +
    // tbody, then the pager, then the note when there is one. Every listener on a persistent
    // element (the th click, the input's input, the select's change, the filter row's focusout)
    // reads TABLE_DOM[id].draw — the slot each call overwrites below with its own draw — so the
    // OLD controls run THIS call's draw over THIS call's rows and caches; a captured draw would
    // pin a dead closure (stale rows, stale caches). The pager's buttons and the empty
    // sentence's clear link are rebuilt every draw, so they bind draw directly. No scroll
    // wrapper is ever recreated on a push: that is the operator's Firefox flash, the lost open
    // select, the lost caret and the sideways snap-back, all one mechanism. The caret
    // save/restore that used to follow replaceChildren goes: the input is never replaced, and
    // the restore never had a value to restore anyway (`caret` was never assigned).

    // the column shape as one key: names AND filter kinds. The one case the header and filter
    // CELLS are replaced is a later call whose columns differ (B2 (d)) — the thead and the two
    // rows themselves persist, only their cells swap inside them.
    var colsKey = columns.map(function (c) { return c.name + ":" + (c.filter || "text"); }).join(",");

    // the first table build (and the rebuild after the list emptied: the container then held
    // only the sentence): the whole skeleton, once
    function buildTable() {
      rec.wrap = h("div", { class: "table-wrap" });
      var tableEl = h("table", opts.class ? { class: opts.class } : null);
      rec.thead = h("thead");
      rec.head = h("tr");
      rec.filterRow = h("tr", { class: "table-filters" });
      rec.tbody = h("tbody");
      rec.pager = h("div", { class: "table-pager muted" });
      rec.note = opts.note ? h("div", { class: "jobs-note muted", text: opts.note }) : null;
      rec.thead.appendChild(rec.head);
      rec.thead.appendChild(rec.filterRow);
      tableEl.appendChild(rec.thead);
      tableEl.appendChild(rec.tbody);
      rec.wrap.appendChild(tableEl);
      var parts = [rec.wrap, rec.pager];
      if (rec.note) parts.push(rec.note);
      rec.container.replaceChildren.apply(rec.container, parts);
      rec.colsKey = null; // the fresh head/filterRow need their cells: draw's colsKey check fires
      // the filter row's ONE focusout listener (added at the first build): a select the
      // operator is holding open is never rebuilt by a draw (the open dropdown would vanish);
      // the draw marks it stale and the rebuild lands when it loses focus
      rec.filterRow.addEventListener("focusout", function (e) {
        rec.onFocusOut(e.target); // slot, set by every patch below (THIS call's counts)
      });
    }

    // the header cells and the filter cells: built on the first table build, and again only
    // when the columns' key changed — the one case the controls are replaced inside the
    // persistent thead rows (B2 (d))
    function buildHeadAndFilterRow() {
      rec.head.replaceChildren.apply(rec.head, columns.map(function (col) {
        var th = h("th", { title: col.title || null, text: col.name });
        // 3.10c: click to sort; the same header clicked again reverses
        th.addEventListener("click", function () {
          if (st.sort && st.sort.col === col.name) st.sort.dir = -st.sort.dir;
          else st.sort = { col: col.name, dir: 1 };
          TABLE_DOM[id].draw(); // the slot, not a captured draw (B2 (a))
        });
        return th;
      }));
      rec.filterRow.replaceChildren.apply(rec.filterRow, columns.map(function (col) {
        // 3.12a: the column's filter KIND (pick / number / time / text, spec §3); the steps fill
        // in the controls, and the cell carries the kind (data-filter-kind) so the drawn row
        // pins it. A spec without the field is text — every old caller keeps its filters.
        var kind = col.filter || "text";
        if (kind === "time") {
          // 3.12c (spec §4): the picker's control — a button styled as the text box was, reading
          // the column name when unset and the state's summary when set (pickSummary, one
          // function, also read by patchFilterRow every draw). Click toggles the panel (§5);
          // Enter/Space is the button's own. The time box and its title grammar are gone.
          var btn = h("button", { type: "button", class: "filter-pick", "data-filter": col.name,
            "data-filter-kind": "time" });
          pickSummary(col, btn);
          // the slot rule again (3.12b B2 (a)): this button persists across pushes, so the
          // click reads the CURRENT call's toggle through TABLE_DOM — a captured toggle would
          // open the panel over the BUILDING call's rows (the smoke caught the first draw's
          // 2 seeded jobs listing "today (2)" while the table showed 120). 3.12c fold F1: the
          // NAME goes, not this call's `col` object — the toggle resolves the current call's
          // column by it (a build-time object is not the current call's, and a push rebuilds
          // the columns)
          btn.addEventListener("click", function () {
            TABLE_DOM[id].toggleTimePanel(col.name, btn);
          });
          return h("th", { "data-filter-kind": kind }, [btn]);
        }
        if (kind === "pick") {
          // 3.12a §4: a native select, styled as the box is — "any" (value ""), then each
          // distinct value labelled "value (count)": the count is the rows the OTHER columns'
          // filters leave (filterRows minus this column, once per draw), so "done (40)" reads
          // "done (7)" as another filter narrows and a value at 0 stays listed. No free-text
          // search (the operator: "the bounded choices aren't a big list").
          var sel = h("select", { "data-filter": col.name, title: col.name });
          sel.addEventListener("change", function () { // "any" (empty) deletes the key
            setFilter(col, sel.value);
            TABLE_DOM[id].draw();
          });
          return h("th", { "data-filter-kind": kind }, [sel]);
        }
        // the title names the grammar the box reads (§5; §6 lands with the time box) so a
        // typed bound can be checked; the unit is the sort key's. 3.12d: the units the number
        // box now reads with their own grammar show that grammar AS THE CELL PRINTS IT — a
        // seconds column reads "n as 4m12s, 2h03m or seconds" because its cells print
        // "4m12s" and "2h03m"; any other unit (count, tokens/s) and no unit stay as 3.12a
        // wrote them, the bare numbers needing no shape
        var unitHint = col.unit === "seconds" ? "; n as 4m12s, 2h03m or seconds"
          : col.unit === "ms" ? "; n as 800ms, 1.2s or ms"
          : col.unit === "%" ? "; n as 50%" : "";
        var boxTitle = kind === "number"
          ? col.name + (col.unit ? ", " + col.unit : "") + " — text, x-y, <n, >n" + unitHint
          : col.name; // 3.12c: the time kind returns above — its box's grammar title is gone
        // the box's text IS the state (3.12a §2): written at build, never by a draw, so the
        // caret and any half-typed bound survive every push
        var input = h("input", { type: "text", placeholder: col.name, "data-filter": col.name,
          title: boxTitle, value: st.filters[col.name] || "" });
        input.addEventListener("input", function () {
          setFilter(col, input.value); // a blank box deletes the key (3.12a §7)
          TABLE_DOM[id].draw();
        });
        return h("th", { "data-filter-kind": kind }, [input]);
      }));
      rec.colsKey = colsKey;
    }

    // the sort marks are SET on the existing ths every draw: the class string rebuilt as today
    // from col.numeric, "sortable" and the sort, plus aria-sort
    function patchHead() {
      columns.forEach(function (col, i) {
        var th = rec.head.childNodes[i];
        var sorted = st.sort && st.sort.col === col.name;
        th.className = (col.numeric ? "n " : "") + "sortable"
          + (sorted ? (st.sort.dir === 1 ? " sort-asc" : " sort-desc") : "");
        if (sorted) th.setAttribute("aria-sort", st.sort.dir === 1 ? "ascending" : "descending");
        else th.removeAttribute("aria-sort");
      });
    }

    // a pick's options rebuilt in place — "any", then the values with their faceted counts, then
    // the state's value (a value the rows no longer have still stands listed "(0)": pickValues
    // appends it)
    function selectOptions(sel, col) {
      sel.replaceChildren();
      sel.appendChild(h("option", { value: "", text: "any" }));
      var counts = Object.create(null); // option values as keys: no __proto__ trap
      filterIdx(col).forEach(function (i) {
        var cv = String(keyOf(col, i));
        // 3.12a fold F1: the count is keyed by the OPTION value, so the blank key ("")
        // counts under PICK_BLANK and its option's figure is right
        cv = cv === "" ? PICK_BLANK : cv;
        counts[cv] = (counts[cv] || 0) + 1;
      });
      pickValues(col).forEach(function (v) {
        sel.appendChild(h("option", { value: v, text: (v === PICK_BLANK ? "(blank)" : v)
          + " (" + (counts[v] || 0) + ")" }));
      });
      sel.value = st.filters[col.name] || "";
    }

    function patchFilterRow() {
      rec.filterRow.childNodes.forEach(function (cell, i) {
        var col = columns[i];
        if (!col) return;
        if ((col.filter || "text") === "pick") {
          var sel = cell.querySelector("select");
          // B2 (c): a focused select is not touched — its counts are the only staleness and
          // the operator is holding the list open; marked for the row's focusout listener
          if (document.activeElement === sel) { sel.__stale = true; return; }
          selectOptions(sel, col);
        }
        if ((col.filter || "text") === "time") {
          // 3.12c: the summary is re-read every draw (the panel's writes, "clear filters", or
          // a hand-set state), and an OPEN panel is not closed by a push — its three selects
          // keep their elements and rebuild their options by the pick's focus rule
          pickSummary(col, cell.querySelector("button.filter-pick"));
          if (rec.panel && rec.panelCol === col.name) patchTimePanelSelects(col);
        }
        // the input box: its value was written at build and never again by a draw
      });
      // the slot the persistent focusout listener reads: THIS call's counts and pickValues
      // (3.12c: panel selects carry __panel and rebuild as the whole panel instead)
      rec.onFocusOut = function (t) {
        if (!t || !t.__stale) return;
        t.__stale = false;
        var name = t.getAttribute("data-filter");
        var col = null;
        columns.forEach(function (c) { if (c.name === name) col = c; });
        if (!col) return;
        if (t.__panel) patchTimePanelSelects(col);
        else selectOptions(t, col);
      };
    }

    // ---- 3.12c (spec §4-§5): the started picker ------------------------------------------------
    //
    // The time kind's button opens a small panel of three native selects — a DATE (any;
    // today; yesterday; each older date present, newest first; every option labelled
    // "label (count)", the count over the rows the OTHER columns' filters leave, the pick's
    // faceting) and two HOUR bounds (the hours present among the faceted rows on the chosen
    // date, or, 3.12f, among the faceted rows on EVERY day with the date "any" — the selects
    // are never disabled; "09:00" labels for from, "09:59" for to) — plus clear and done.
    // Every change writes the §3 state string through setFilter and redraws through the
    // TABLE_DOM slot (3.12b: never a captured draw — a captured closure would pin dead rows
    // and caches).
    //
    // The panel is ONE per table, a child of rec.container (the .table-block, position:
    // relative) and NEVER of rec.wrap: the wrapper's overflow-x: auto would clip an
    // absolutely positioned child or scroll it instead (spec §7). It closes on done, Escape,
    // an outside mousedown, or a second click on the control; the Escape and mousedown
    // listeners ride on document while it is open and come off when it closes. A push while
    // it is open keeps the panel element (3.12b's rule) and refreshes the counts in place.

    // the §3 state string split (or null when the state holds another shape); 3.12f's dayless
    // shape "HH-HH" reads with an empty day and the hours set
    function timeSummaryParts(f) {
      var m = /^(\d{4}-\d{2}-\d{2})(?: (\d{2})-(\d{2}))?$/.exec(f || "");
      if (m) return { day: m[1], from: m[2] || "", to: m[3] || "" };
      m = /^(\d{2})-(\d{2})$/.exec(f || "");
      return m ? { day: "", from: m[1], to: m[2] } : null;
    }

    // the time button's face (spec §4): the column name when unset; set, the summary —
    // "today"/"yesterday" judged against opts.now's local day (the clock the started cells
    // day-stamp against), else the date as the cells' own short date (toLocaleDateString of
    // the state's day: the date half of what fmt.date prints), then " 09 to 11" when hours
    // are bound — and 3.12f, for the dayless shape "HH-HH", exactly that "09 to 11" alone:
    // no day word and no leading space. The `set` class rides with a set state (ink, not
    // muted, as a typed filter's text is). One function, read at build and by patchFilterRow
    // every draw. A state outside the §3 shape (a hand-set view) reads as the raw string,
    // never a crash.
    function pickSummary(col, btn) {
      var f = st.filters[col.name];
      var set = f != null && f !== "";
      var text = col.name;
      if (set) {
        var parts = timeSummaryParts(f);
        if (parts) {
          text = "";
          if (parts.day) {
            if (opts.now != null && isFinite(opts.now)) {
              var nd = new Date(opts.now * 1000);
              if (parts.day === localDay(opts.now)) text = "today";
              else if (parts.day === localDay(new Date(nd.getFullYear(), nd.getMonth(),
                nd.getDate() - 1).getTime() / 1000)) text = "yesterday";
            }
            if (!text) {
              var p = parts.day.split("-");
              text = new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString();
            }
          }
          if (parts.from) text += (text ? " " : "") + parts.from + " to " + parts.to;
        } else {
          text = String(f);
        }
      }
      btn.textContent = text;
      if (set) btn.classList.add("set");
      else btn.classList.remove("set");
    }

    // 3.12c fold F1: the button hands its column's NAME (it persists across pushes, so its
    // own `col` object may be a dead call's); the column resolved here is THIS call's, the
    // way rec.onFocusOut resolves one. rec.panelCol stays the name.
    function toggleTimePanel(name, btn) {
      if (rec.panel) {
        var wasBtn = rec.panelBtn;
        closeTimePanel();
        if (wasBtn === btn) return; // a second click on the same control closes (spec §4)
      }
      var col = null;
      columns.forEach(function (c) { if (c.name === name) col = c; });
      if (!col) return;
      openTimePanel(col, btn);
    }

    // the state string the three selects write (spec §3): date alone is "YYYY-MM-DD"; both
    // hours "YYYY-MM-DD HH-HH"; ONE hour bound takes the day's edge for the missing side —
    // from alone reads "HH-23", to alone "00-HH" (a single handle means "from that hour to
    // the end of the day" / "the day's start to that hour", the inclusive span's other half);
    // "any" date empties the state (setFilter deletes the key). 3.12f: the hour edges hold
    // WITHOUT a date too — with the date "any" and hours set the state is the dayless shape
    // "HH-HH"; choosing a date after the hours joins it ("YYYY-MM-DD HH-HH"), choosing "any"
    // after a date keeps the hours (the operator: no limiting).
    function timePanelState(ds, fs, ts) {
      var day = ds.value;
      if (fs.value && ts.value) return (day ? day + " " : "") + fs.value + "-" + ts.value;
      if (fs.value) return (day ? day + " " : "") + fs.value + "-23";
      if (ts.value) return (day ? day + " " : "") + "00-" + ts.value;
      return day;
    }

    function openTimePanel(col, btn) {
      var panel = h("div", { class: "filter-panel" });
      var onChange = function () {
        setFilter(col, timePanelState(ds, fs, ts));
        TABLE_DOM[id].draw(); // the slot (3.12b)
      };
      var ds = h("select", { class: "fp-date", "data-filter": col.name, "data-fp": "date" });
      var fs = h("select", { class: "fp-from", "data-filter": col.name, "data-fp": "from" });
      var ts = h("select", { class: "fp-to", "data-filter": col.name, "data-fp": "to" });
      [ds, fs, ts].forEach(function (sel) {
        sel.__panel = true;
        sel.addEventListener("change", onChange);
      });
      panel.appendChild(h("span", { class: "fp-label", text: "date" }));
      panel.appendChild(ds);
      panel.appendChild(h("span", { class: "fp-label", text: "from" }));
      panel.appendChild(fs);
      panel.appendChild(h("span", { class: "fp-label", text: "to" }));
      panel.appendChild(ts);
      panel.appendChild(h("button", { type: "button", class: "pager-btn", text: "clear" }));
      panel.children[panel.children.length - 1].addEventListener("click", function () {
        setFilter(col, ""); // empties the state (spec §5); the panel re-reads it and stands by
        TABLE_DOM[id].draw();
      });
      panel.appendChild(h("button", { type: "button", class: "pager-btn", text: "done" }));
      panel.children[panel.children.length - 1].addEventListener("click", function () {
        closeTimePanel();
      });
      // the panel sits OUTSIDE the filter row, so it carries its own focusout into the same
      // slot: a select the operator is holding open is marked by a push and rebuilt here
      panel.addEventListener("focusout", function (e) { rec.onFocusOut(e.target); });
      rec.panel = panel;
      rec.panelBtn = btn;
      rec.panelCol = col.name;
      rec.container.appendChild(panel);
      // position under the cell: the button's bottom edge, left clamped inside the block,
      // measured AFTER the append (under fakedom every rect is zeros and every clamp passes)
      panel.style.position = "absolute";
      var cRect = rec.container.getBoundingClientRect();
      var bRect = btn.getBoundingClientRect();
      panel.style.top = (bRect.bottom - cRect.top) + "px";
      var left = bRect.left - cRect.left;
      var maxLeft = cRect.width - panel.getBoundingClientRect().width;
      if (maxLeft < 0) maxLeft = 0; // a block narrower than the panel: pin to the left edge
      if (left > maxLeft) left = maxLeft;
      if (left < 0) left = 0;
      panel.style.left = left + "px";
      patchTimePanelSelects(col);
      // close paths the panel itself cannot see: Escape and an outside mousedown, live on
      // document while open, off on close (spec §5)
      rec.panelKey = function (e) { if (e.key === "Escape") closeTimePanel(); };
      rec.panelDown = function (e) {
        var t = e.target;
        while (t) {
          if (t === panel || t === btn) return; // inside the panel or on its control
          t = t.parentNode;
        }
        closeTimePanel();
      };
      document.addEventListener("keydown", rec.panelKey);
      document.addEventListener("mousedown", rec.panelDown);
    }

    function closeTimePanel() {
      if (!rec.panel) return;
      document.removeEventListener("keydown", rec.panelKey);
      document.removeEventListener("mousedown", rec.panelDown);
      rec.panel.remove();
      rec.panel = null;
      rec.panelBtn = null;
      rec.panelCol = null;
    }

    // rebuild the panel's three selects from THIS call's rows and caches: labels and counts
    // per spec §5, values from the state, from/to NEVER disabled (3.12f — with the date "any"
    // their list is the hours of the faceted rows on every day). The pick's
    // focus rule (B2 (c)): a focused select is marked __stale instead — its rebuild lands at
    // the panel's focusout, never yanking an open dropdown.
    function patchTimePanelSelects(col) {
      var panel = rec.panel;
      if (!panel) return;
      var ds = panel.querySelector("select.fp-date");
      var fs = panel.querySelector("select.fp-from");
      var ts = panel.querySelector("select.fp-to");
      var held = timeSummaryParts(st.filters[col.name]) || { day: "", from: "", to: "" };
      // the DATE list is every day the ROWS hold (an option at faceted 0 is still a real
      // date — the date list is the point, unlike the pick's faceted zero); the counts, and
      // the hour list, read the rows the OTHER columns' filters leave (filterIdx(col))
      var days = Object.create(null), counts = Object.create(null), hours = Object.create(null);
      rows.forEach(function (row, i) {
        var v = keyOf(col, i);
        if (typeof v === "number" && isFinite(v)) days[localDay(v)] = 1;
      });
      filterIdx(col).forEach(function (i) {
        var v = keyOf(col, i);
        if (typeof v !== "number" || !isFinite(v)) return;
        var d = localDay(v);
        counts[d] = (counts[d] || 0) + 1;
        // 3.12f: with the date "any" the hours collect over EVERY faceted day; with one,
        // that day only, as before
        if (!held.day || d === held.day) {
          var hh = new Date(v * 1000).getHours();
          hours[(hh < 10 ? "0" : "") + hh] = 1;
        }
      });
      var today = "", yest = "";
      if (opts.now != null && isFinite(opts.now)) {
        today = localDay(opts.now);
        var nd = new Date(opts.now * 1000);
        yest = localDay(new Date(nd.getFullYear(), nd.getMonth(), nd.getDate() - 1).getTime() / 1000);
      }
      // a held date the rows no longer have (a push dropped it) stays listed "(0)", as
      // pickValues does for the pick
      if (held.day && !days[held.day]) days[held.day] = 1;
      var older = Object.keys(days).filter(function (d) { return d !== today && d !== yest; });
      older.sort(function (a, b) { return a < b ? 1 : (a > b ? -1 : 0); }); // newest first
      var dayLabel = function (d) {
        var p = d.split("-");
        return new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString();
      };
      var rebuild = function (sel, fill) {
        if (document.activeElement === sel) { sel.__stale = true; return; }
        fill(sel);
      };
      rebuild(ds, function (sel) {
        sel.replaceChildren();
        sel.appendChild(h("option", { value: "", text: "any" }));
        if (days[today]) sel.appendChild(h("option", { value: today, title: today,
          text: "today (" + (counts[today] || 0) + ")" }));
        if (days[yest]) sel.appendChild(h("option", { value: yest, title: yest,
          text: "yesterday (" + (counts[yest] || 0) + ")" }));
        older.forEach(function (d) {
          sel.appendChild(h("option", { value: d, text: dayLabel(d) + " (" + (counts[d] || 0) + ")" }));
        });
        sel.value = held.day;
      });
      // the hours present (faceted) on the chosen date — or on every day, 3.12f, with the
      // date "any" — zero-padded and ascending; a held bound the rows no longer carry stays
      // selectable (the state is the truth)
      var hourList = Object.keys(hours).sort();
      if (held.from && hourList.indexOf(held.from) < 0) hourList.push(held.from);
      if (held.to && hourList.indexOf(held.to) < 0) hourList.push(held.to);
      if (held.from || held.to) hourList.sort();
      var hourOptions = function (sel, suffix) {
        sel.replaceChildren();
        sel.appendChild(h("option", { value: "", text: "any" }));
        hourList.forEach(function (hh) {
          sel.appendChild(h("option", { value: hh, text: hh + ":" + suffix }));
        });
        // the held bound reads back whichever day the state holds (3.12f: the dayless shape
        // has empty day and set hours); the selects are never disabled. The fold of 3.12f: the
        // edge hours read back as "any" — a held from of "00" and a held to of "23". A single
        // handle already means the day's edge (timePanelState: from alone writes "HH-23", to
        // alone "00-HH"), so "00" from and "any" from are the same bound (likewise "23" and
        // "any" for to) and the state string is unchanged. Without that there is no escape from
        // the panel's own writes: from→any puts "00" in the state, the read-back showed it as a
        // handle, and the next to→any re-composed "00-23" instead of emptying the state. An
        // explicit 00:00 from or 23:59 to pick is one bound with "any": it writes the same
        // state it always did and reads back "any" the same way (the summary, which reads the
        // state, still prints "00 to 11").
        var heldHour = suffix === "00" ? held.from : held.to;
        if (heldHour === (suffix === "00" ? "00" : "23")) heldHour = ""; // the edge IS "any"
        sel.value = heldHour;
      };
      rebuild(fs, function (sel) { hourOptions(sel, "00"); });
      rebuild(ts, function (sel) { hourOptions(sel, "59"); });
    }

    // the tr's row signature: the whole row serialized (spec §7: a cell is a function of its
    // row and opts.now — §3's accepted exception). An EXPANDO on the element, not an
    // attribute: the row JSON is long
    function buildTr(row) {
      var attrs = opts.rowAttrs ? opts.rowAttrs(row) : null;
      var tr = h("tr", attrs, columns.map(function (col) { return col.cell(row); }));
      if (opts.key) tr.setAttribute("data-key", String(opts.key(row)));
      tr.__sig = JSON.stringify(row);
      return tr;
    }

    // the body reconciles row by row. With opts.key (jobsTable: job.id) a page row whose key
    // sits on an existing tr with the same signature KEEPS that element wherever it sits,
    // moved into position; without a key (requestsTable: request rows carry no id) the tr at
    // position i is kept when its signature matches, else replaced — today's wholesale
    // rebuild's work, no worse. Surplus trs are removed. An element is moved, never cloned
    // (spec §7: rowMemo's one-placement rule is untouched).
    function patchBody(pageRows) {
      var have = Array.prototype.slice.call(rec.tbody.childNodes);
      var wanted = [];
      if (opts.key) {
        var byKey = {};
        have.forEach(function (tr) {
          var k = tr.getAttribute("data-key");
          if (k != null && byKey[k] === undefined) byKey[k] = tr;
        });
        pageRows.forEach(function (row) {
          var key = String(opts.key(row));
          var old = byKey[key];
          if (old !== undefined) {
            delete byKey[key]; // one placement per key: a kept tr is never used twice
            wanted.push(old.__sig === JSON.stringify(row) ? old : buildTr(row));
          } else {
            wanted.push(buildTr(row));
          }
        });
      } else {
        pageRows.forEach(function (row, i) {
          var old = have[i];
          wanted.push(old && old.__sig === JSON.stringify(row) ? old : buildTr(row));
        });
      }
      // place wanted in order, then drop the leftovers (trs past the page rows on a shrink,
      // the tr whose row changed, and any element the state's page moved away from)
      for (var i = 0; i < wanted.length; i++) {
        var cur = rec.tbody.childNodes[i];
        if (cur !== wanted[i]) rec.tbody.insertBefore(wanted[i], cur || null);
      }
      Array.prototype.slice.call(rec.tbody.childNodes).forEach(function (tr) {
        if (wanted.indexOf(tr) < 0) tr.remove();
      });
    }

    // the pager line patched inside the same .table-pager element: the same children as today
    // ("1–50 of 889 · previous · next"; "N rows" alone when a filter-free table fits one page;
    // a filter always shows how many match out of how many fetched). Buttons are cheap and
    // carry no focus the operator loses, so they are rebuilt and bind this draw.
    function patchPager(filtered, from, pages, anyFilter) {
      var kids = [];
      if (filtered.length === 0) {
        kids.push(h("span", { text: "no rows match the filters" }));
      } else if (filtered.length <= pageSize && !anyFilter) {
        kids.push(h("span", { text: filtered.length + (filtered.length === 1 ? " row" : " rows") }));
      } else {
        kids.push(h("span", { text: (from + 1) + "–" + Math.min(from + pageSize, filtered.length) + " of " + filtered.length }));
        kids.push(h("span", { class: "pager-sep", text: " · " }));
        kids.push(pagerButton("previous", st.page <= 1, function () { st.page -= 1; draw(); }));
        kids.push(h("span", { class: "pager-sep", text: " · " }));
        kids.push(pagerButton("next", st.page >= pages, function () { st.page += 1; draw(); }));
      }
      // 3.12a §7: one link wipes every filter — at the end of the line whenever a filter is
      // set, also after the "no rows match" sentence (it is the way out of that state)
      if (anyFilter) {
        kids.push(h("span", { class: "pager-sep", text: " · " }));
        kids.push(pagerButton("clear filters", false, function () {
          Object.keys(st.filters).forEach(function (k) { delete st.filters[k]; });
          st.page = 1; // a filter changes the count; the page starts over
          draw();
        }));
      }
      rec.pager.replaceChildren.apply(rec.pager, kids);
    }

    function draw() {
      // 3.12a §7: empty values are deleted from the state, so a key IS a set filter
      var anyFilter = Object.keys(st.filters).length > 0;

      // 3.12b (spec §4, last bullet): the empty list lives INSIDE the persistent container —
      // the .empty sentence, plus the "clear filters" button after it when a filter is set, so
      // a filter can be cleared while no rows are fetched (the review: empty rows removed the
      // clear link). The button is rebuilt every draw and binds this draw directly. The table
      // parts leave with the replaceChildren: a list that empties and refills rebuilds them
      // once, one flash on a real change.
      if (!rows.length) {
        // 3.12c: a panel open over a list that just emptied goes with it — the replaceChildren
        // below drops the table parts anyway, and leaving the panel would leave its document
        // Escape/mousedown listeners over a detached element
        closeTimePanel();
        var emptyParts = [h("div", { class: "empty", text: opts.empty || "" })];
        if (anyFilter) {
          emptyParts.push(pagerButton("clear filters", false, function () {
            Object.keys(st.filters).forEach(function (k) { delete st.filters[k]; });
            st.page = 1; // a filter changes the count; the page starts over
            draw();
          }));
        }
        rec.container.replaceChildren.apply(rec.container, emptyParts);
        rec.wrap = null;
        return;
      }

      if (!rec.wrap) buildTable();
      if (rec.colsKey !== colsKey) buildHeadAndFilterRow();
      patchHead();
      patchFilterRow();

      var filtered = sortRows(filterIdx()).map(function (i) { return rows[i]; });
      var pages = Math.max(1, Math.ceil(filtered.length / pageSize));
      if (st.page > pages) st.page = pages; // a push shrank the table; clamp to the last page
      if (st.page < 1) st.page = 1;
      var from = (st.page - 1) * pageSize;
      patchBody(filtered.slice(from, from + pageSize));
      patchPager(filtered, from, pages, anyFilter);

      // the note's TEXT is set every draw (the element persists with the table parts); a note
      // appearing or going between calls rides with the table build (the note only when there
      // is one — replaceChildren prints a null argument as the text "null", the word the
      // operator saw under every requests table whose callers pass no note)
      if (opts.note && !rec.note) {
        rec.note = h("div", { class: "jobs-note muted", text: opts.note });
        rec.container.appendChild(rec.note);
      } else if (rec.note) {
        if (opts.note) rec.note.textContent = opts.note;
        else { rec.note.remove(); rec.note = null; }
      }
    }

    function pagerButton(label, disabled, onClick) {
      var b = h("button", { type: "button", class: "pager-btn", text: label });
      if (disabled) b.setAttribute("disabled", "");
      b.addEventListener("click", onClick);
      return b;
    }

    rec.hash = hash;
    rec.draw = draw; // the slot every persistent listener reads (B2 (a))
    rec.toggleTimePanel = toggleTimePanel; // 3.12c: the filter cell's button reads THIS call too
    if (typeof rec.onFocusOut !== "function") rec.onFocusOut = function () {};
    TABLE_DOM[id] = rec;
    draw();
    return rec.container;
  }

  // 3.11a: a renderer that builds a WHOLE row to answer one column (requestsTable's reqRow,
  // jobsTable's jobRow) is asked for the same row back to back — each column's td, its row
  // attributes, a sort key — and rebuilt every row's twelve cells per column (a 50-row page:
  // 7,800 tds). One entry remembers the last row's build. A td is handed out once: a second
  // ask for a column already given (a row object listed twice, the next draw) rebuilds, so no
  // element is ever placed in two rows. `row(r)` is for the non-DOM reads (attrs, sort keys).
  function rowMemo(build) {
    var last = null, out = null, used = {};
    function row(r) {
      if (r !== last) { last = r; out = build(r); used = {}; }
      return out;
    }
    function cell(r, key) {
      if (r === last && used[key]) last = null; // already placed: build fresh elements
      var built = row(r);
      used[key] = true;
      return built;
    }
    return { row: row, cell: cell };
  }

  // story 3.10b: the one jobs-table renderer, called by both pages (the operator, 3.10: "that
  // table should be the same table from the primary page, just filtered to this endpoint - no
  // need to have two different tables - and the code should be re-used"). `rows` are §5a job
  // rows in the server's order (3.7g: that order is the log's contract — live jobs by start,
  // then the newest finished first — no re-sort unless the operator clicks a header, 3.10c).
  // `opts`: `now` the clock the started cell day-stamps against (3.10a), `empty` the sentence
  // an empty list draws (each page keeps its own), `note` the line under the table spelling
  // out the row order (3.7g: the order carries meaning; a "show all" link waits for an
  // all-jobs page, so the notes stay plain), `omit` the column names to leave out (the
  // endpoint page drops "endpoint": every row is that endpoint).
  function jobsTable(rows, opts) {
    opts = opts || {};
    rows = rows || [];
    var omit = opts.omit || [];
    var columns = JOB_COLUMNS.filter(function (c) { return omit.indexOf(c) < 0; });
    // the sort keys match what the cells show (3.10c): the started cell by its timestamp, the
    // pill by the word it prints ("done" for a succeeded job, "lost" for a dead server's live
    // row), the id cell by the full id (the page shows the short one, the title holds this)
    var memo = rowMemo(function (job) { return jobRow(job, opts.now); }); // one build per row (3.11a)
    var specs = columns.map(function (c) {
      return {
        name: c, numeric: !!NUMERIC[c], title: COLUMN_TITLES[c] || null,
        filter: FILTER_KIND[c] || "text", unit: UNIT[c] || null, // 3.12a (spec §3)
        cell: function (job) { return memo.cell(job, c)[c]; },
        sortKey: function (job) { return jobSortKey(c, job); },
      };
    });
    // 3.10c fold: each page's jobs table keeps its own view — the overview is "jobs", the endpoint
    // page passes its own id — a filter typed on one never filters the other
    return dataTable(opts.id || "jobs", specs, rows, {
      empty: opts.empty || "no jobs yet",
      note: opts.note,
      now: opts.now, // 3.12c: the started picker's "today"/"yesterday" judge against this clock
      rowAttrs: function (job) { return { "data-job": job.id }; },
      // 3.12b: the body reconciles by job id (request rows carry none, so the requests table
      // stays positional) — an unchanged job's tr survives every push
      key: function (job) { return job.id; },
    });
  }

  function jobSortKey(col, job) {
    if (col === "job") return job.id == null ? "" : job.id;
    if (col === "status") {
      var lost = job.server_live === false && (job.status === "running" || job.status === "queued");
      var word = lost ? "lost" : job.status;
      return word === "succeeded" ? "done" : word;
    }
    if (col === "started") return job.started_at;
    if (col === "model") return job.model || "";
    if (col === "endpoint") return job.endpoint || "";
    if (col === "elapsed") return job.elapsed_s;
    if (col === "turns") return job.num_turns;
    if (col === "tool calls") return job.tool_calls;
    if (col === "tool errors") return job.errors;
    if (col === "context") return job.ctx_pct;
    if (col === "prefill tok/s") return job.prefill_tps;
    if (col === "decode tok/s") return job.gen_tps;
    return oneLine(job.last_activity); // "last activity"
  }

  // --- render: the snapshot into the DOM, in place -------------------------------------------

  // 3.7m page half (3.7-fold-3): a snapshot identical to the last rendered one in everything
  // but `now` — the serialise-minus-now comparison — redraws nothing. `state.snap` still takes
  // the new object (fmt.ago and the strips read its `now`). "Ago" freshness on the skip path:
  // a cheap per-push text refresh (refreshAgoTexts below) — one querySelector per endpoint, no
  // sub-block rebuilt — so the last-active and last-seen phrases keep pace with the pushes
  // without costing a render. historyGen rides in the comparison so the post-history render
  // still runs when only a history fetch changed the picture (its strips and sparklines).
  function withoutNow(snap) {
    var copy = Object.assign({}, snap);
    delete copy.now;
    return copy;
  }

  // the header's snapshot time (3.7r): the time half of the one conn phrase — while the stream
  // is up the label reads "updated HH:MM:SS" and moves with every snapshot; while it is down
  // the phrase keeps the last update's time and render does not touch it (setConnected drew it)
  function setUpdatedLine(snap) {
    if (streamUp) byId("conn-label").textContent = "updated " + fmt.time(snap.now);
  }

  // refresh the clock-relative phrases in place (the skip path): last-active on live heads,
  // last-seen on down heads; dormant lines hold an absolute date and never go stale
  function refreshAgoTexts(snap) {
    var hosts = byId("hosts");
    snap.hosts.forEach(function (host) {
      host.endpoints.forEach(function (ep) {
        var name = ep.name; // the selector is built from a local, not an `ep.` chain
        var sel = '.endpoint[data-endpoint="' + name.replace(/["\\]/g, "\\$&") + '"] ';
        var act = hosts.querySelector(sel + ".ep-active");
        if (act) {
          var info = activeInfo(ep, snap);
          act.textContent = info.text;
          if (info.title) act.setAttribute("title", info.title);
          else act.removeAttribute("title");
        }
        var seen = hosts.querySelector(sel + ".ep-seen");
        if (seen) {
          seen.textContent = ep.last_seen != null ? "last seen " + fmt.ago(ep.last_seen, snap.now) : "";
        }
      });
    });
  }

  // 3.7m page half (3.7-fold-3): what identifies a host card for the in-place update — the
  // host's own state plus its endpoint list (names and states, in order). Same signature: keep
  // the element; anything changed, or a new host: rebuild as before. A hidden never-seen
  // endpoint (hidden on every host, down included — 3.7y) can only join its card via its own
  // state change or a first sighting, both of which are in the signature, so the drawn
  // children match whenever the signatures do.
  function hostSignature(host) {
    // the DRAWN endpoints only (the review of fold-3, P1): a never-seen endpoint (hidden by
    // 3.7a on every host, 3.7y dropping the down-node exception) is not in the signature, so
    // its arrival must not force a rebuild of a card it never joins
    var parts = [host.name, host.state];
    host.endpoints.forEach(function (ep) {
      if (!neverSeenOn(ep)) parts.push(ep.name + "\x00" + ep.state);
    });
    return parts.join("\x01");
  }

  // keep one endpoint element: swap only the sub-blocks whose rendering changed. Guard: the
  // children must line up one-for-one (same count, same classes) — otherwise swap the whole
  // endpoint element, still inside the kept host card. outerHTML compare is conservative: a
  // DOM re-serialisation difference (attribute order, colour form) means one extra swap, never
  // a missed one; node identity is kept at the endpoint and host level, which is what the
  // hover state and the smoke's identity probe rest on.
  // P-2 (Opus review of the page folds, 3.7u-fix): the comparisons below read a live node as it
  // was BUILT. raiseCardFloor marks a scrolling line with the `overflows` class on the live node
  // after every render; a freshly built node never carries it, so comparing raw outerHTML or
  // className made every marked line look changed — each push replaced the whole endpoint whose
  // direct child scrolled (strip, figures and jobs with it) and the host head whose note
  // scrolled. The mark is masked from the live side (a clone with it removed, made only when a
  // mark is there); a node that is swapped in arrives unmarked and raiseCardFloor, which runs
  // after the in-place update, marks it again. Masking, rather than moving the mark to a data
  // attribute: an attribute is serialised by outerHTML too, so it would need the same masking,
  // and the class keeps the CSS and the smoke's selectors as they are.
  var OVERFLOW_MARK = "overflows";
  function builtHTML(el) {
    if (!el.classList.contains(OVERFLOW_MARK) && !el.querySelector("." + OVERFLOW_MARK)) return el.outerHTML;
    var copy = el.cloneNode(true);
    copy.classList.remove(OVERFLOW_MARK);
    Array.prototype.forEach.call(copy.querySelectorAll("." + OVERFLOW_MARK), function (e) {
      e.classList.remove(OVERFLOW_MARK);
    });
    return copy.outerHTML;
  }
  function builtClass(el) {
    if (!el.classList.contains(OVERFLOW_MARK)) return el.className;
    return Array.prototype.filter.call(el.classList, function (c) { return c !== OVERFLOW_MARK; }).join(" ");
  }
  function sameAsBuilt(liveEl, builtEl) { return builtHTML(liveEl) === builtEl.outerHTML; }

  function swapEndpointSubBlocks(oldEp, newEp) {
    if (oldEp.children.length !== newEp.children.length) return false;
    var i;
    for (i = 0; i < newEp.children.length; i++) {
      if (builtClass(oldEp.children[i]) !== newEp.children[i].className) return false;
    }
    var newKids = Array.prototype.slice.call(newEp.children);
    for (i = 0; i < newKids.length; i++) {
      if (!sameAsBuilt(oldEp.children[i], newKids[i])) oldEp.replaceChild(newKids[i], oldEp.children[i]);
    }
    return true;
  }

  // one host card, kept: head and endpoint blocks compared by position (the signature says the
  // lists match). Returns whether anything swapped.
  function updateHostInPlace(oldCard, newCard) {
    var changed = false;
    var oldHead = oldCard.querySelector(".host-head");
    var newHead = newCard.querySelector(".host-head");
    if (oldHead && newHead && !sameAsBuilt(oldHead, newHead)) {
      oldCard.replaceChild(newHead, oldHead);
      changed = true;
    }
    var oldEls = Array.prototype.slice.call(oldCard.querySelectorAll(".endpoint"));
    var newEls = Array.prototype.slice.call(newCard.querySelectorAll(".endpoint"));
    for (var i = 0; i < newEls.length; i++) {
      if (sameAsBuilt(oldEls[i], newEls[i])) continue;
      if (!swapEndpointSubBlocks(oldEls[i], newEls[i])) oldCard.replaceChild(newEls[i], oldEls[i]);
      changed = true;
    }
    return changed;
  }

  // 3.7s page half: the endpoint's last-active phrase (3.7-fold-3). Null reads "no recent
  // activity": 3.7s's record wrote "no activity in 7d", but fold-2 noticed the witness is not
  // pruned with the probes, so a retention figure in the words can be a lie; and fold-4's two
  // reviews found "no activity recorded" ambiguous for a box active 8 days ago whose probes
  // were pruned — it IS recorded, just not recent. Past 24 h the date form; the absolute time
  // rides in the title either way. The text is split from the span so a skipped render can
  // refresh just the text (below).
  function activeInfo(ep, snap) {
    if (ep.last_active == null) return { text: "no recent activity", title: "" };
    if (snap.now - ep.last_active > 86400) {
      return { text: "active " + fmt.date(ep.last_active), title: fmt.date(ep.last_active) };
    }
    return { text: "active " + fmt.ago(ep.last_active, snap.now), title: fmt.date(ep.last_active) };
  }

  // --- 3.7u: the card's column floor, a high-water mark ----------------------------------------
  // The operator (2026-10-30): "I don't like the '...' convention because it hides information -
  // either set a minimum card width that can accomodate the widest text, or dynamically size the
  // card to not truncate (and use that as min size so it can't shrink below that high water
  // mark). We get fewer cards horizontally, but more complete information."
  // After every render the page measures every card's one-line rows at their whole width — the
  // endpoint head, the job row's figures and activity lines, every other one-line text on the
  // card (the .ep-line class: the speeds note, the last-seen span, a down card's error and its
  // dropped-job line, the host note, a dormant line) and the field grid (its column count times
  // the widest tile text, plus the gaps) — each plus the card's own chrome around
  // it, and raises the #hosts grid's --card-floor to the widest need. The floor is never lowered
  // during the page's life, so a card never narrows (and the grid never re-flows) when a long
  // job ends. The stylesheet caps the floor at the grid's own width (min(--card-floor, 100%));
  // past the cap the line still wider than its card scrolls in its own fixed-height box.
  var CARD_FLOOR_MIN = 360; // app.css .hosts --card-floor (a test pins the copy)
  // floor: the high-water mark in px (what --card-floor holds); need: the last measurement's
  // widest card; raised: how many times the floor went up (the smoke reads all three)
  var cardLayout = { floor: CARD_FLOOR_MIN, need: 0, raised: 0 };

  function raiseCardFloor() {
    var hostsEl = byId("hosts");
    if (!hostsEl || !hostsEl.clientWidth) return; // behind a detail page: nothing laid out to read
    var each = function (list, fn) { Array.prototype.forEach.call(list, fn); };
    // pass 1, at the cards' own widths: the chrome around each line (the card's padding and
    // border; a down endpoint's own box) and each field grid's column count and gap. The line
    // selector here and the CSS rules that scroll those lines name one set — a test pins the
    // two lists against each other (U1: an unmeasured line wraps, an unstyled one hides).
    // P-1 (Opus review of the page folds): a line that is itself a flex item (.ep-seen in a down
    // endpoint's head, .host-note beside the host name) is measured through its ROW. Its own box
    // is only part of the row, so card width minus its width counted the row's free space (the
    // space-between, an auto margin, the room a wide card gives) as chrome: every such line
    // "needed" exactly its card's current width, and the floor followed the window up and never
    // came down (one column for the page's life after one narrowing). The row's max-content width
    // is its items' text plus its gaps; the chrome around the row is the card's own padding and
    // border (the row is block-level). app.css's .measuring rule gives each such row max-content
    // (a test pins every flex row that holds an .ep-line to that rule). `marked` keeps the
    // original lines: the overflows mark below stays on the line that scrolls.
    var items = [];
    var marked = [];
    each(hostsEl.children, function (card) {
      var cardW = card.getBoundingClientRect().width;
      var rows = [];
      each(card.querySelectorAll(".ep-head, .ep-job-figs, .ep-act, .ep-line"), function (line) {
        marked.push(line);
        var box = line;
        if (line.parentNode !== card && /flex/.test(getComputedStyle(line.parentNode).display)) box = line.parentNode;
        if (rows.indexOf(box) >= 0) return; // the row is measured once (.ep-head is a line itself)
        rows.push(box);
        items.push({ line: box, chrome: cardW - box.getBoundingClientRect().width });
      });
      each(card.querySelectorAll(".figures"), function (grid) {
        var style = getComputedStyle(grid);
        items.push({ texts: grid.querySelectorAll(".fig > .label, .fig > .num, .fig > .kv"),
          chrome: cardW - grid.getBoundingClientRect().width,
          columns: style.gridTemplateColumns.split(" ").length, gap: parseFloat(style.columnGap) || 0 });
      });
    });
    // pass 2, every measured element at its max-content width (app.css's .measuring rules): the
    // text's own width, never the width a stretched 1fr column happens to give it
    hostsEl.classList.add("measuring");
    var need = 0;
    items.forEach(function (it) {
      var w = 0;
      if (it.line) w = it.line.getBoundingClientRect().width;
      else {
        each(it.texts, function (t) { w = Math.max(w, t.getBoundingClientRect().width); });
        w = it.columns * w + (it.columns - 1) * it.gap;
      }
      need = Math.max(need, w + it.chrome);
    });
    hostsEl.classList.remove("measuring");
    cardLayout.need = Math.ceil(need);
    if (cardLayout.need > cardLayout.floor) {
      cardLayout.floor = cardLayout.need;
      cardLayout.raised++;
      hostsEl.style.setProperty("--card-floor", cardLayout.floor + "px");
    }
    // 3.7u-fix U3: the overflow affordance is a class, not a scrollbar — a bar steals height
    // from a pinned line box (measured: 10 px off a 21.75 px line). Set on each measured line
    // after the floor is applied, reading the layout the new floor produced: the band and the
    // ew-resize cursor mark exactly the lines that scroll in their own box (app.css
    // .overflows; lines that fit never wear it).
    marked.forEach(function (line) {
      line.classList.toggle("overflows", line.scrollWidth > line.clientWidth);
    });
  }

  // 3.9D D4 (fold-3 review): the detail pages' liveness hook. The endpoint page used to watch
  // #updated's text, but that phrase moved into #conn-label in 3.7r and the span went dead —
  // the page stopped redrawing and refetching on snapshots. render() calls every listener at
  // its end, on the skip path too (a clock-relative sentence moves while nothing else draws).
  // Returns the remover: a detail page drops its own listener in its teardown.
  var snapshotListeners = [];
  function onSnapshot(fn) {
    snapshotListeners.push(fn);
    return function () {
      var i = snapshotListeners.indexOf(fn);
      if (i >= 0) snapshotListeners.splice(i, 1);
    };
  }

  // 3.12b (spec §5): mount = insert only on a difference. Re-inserting the same element
  // recreates its scroll frame (the Firefox overlay-scrollbar flash the operator sees on every
  // push, the sideways snap-back), so a page mounting a persistent child checks first: if the
  // parent's child nodes are exactly `els`, in order, touch nothing; else replaceChildren.
  function mount(parent, els) {
    var kids = parent.childNodes;
    var same = kids.length === els.length;
    if (same) {
      for (var i = 0; i < els.length; i++) {
        if (kids[i] !== els[i]) { same = false; break; }
      }
    }
    if (!same) parent.replaceChildren.apply(parent, els);
  }

  function render(snap) {
    state.snap = snap;
    // 3.7-fold-3: the refresh interval comes from the snapshot (the one refresh master, decided
    // in 3.7m): history fetches follow `refresh_s`, floored at 1 s. Set on every render,
    // including a skipped one, before anything reads it.
    if (typeof snap.refresh_s === "number" && isFinite(snap.refresh_s)) {
      HISTORY_MIN_INTERVAL_MS = Math.max(1000, snap.refresh_s * 1000);
    }
    var sig = historyGen + " " + JSON.stringify(withoutNow(snap));
    if (sig === snapSig) {
      renders.skipped++;
      setUpdatedLine(snap);
      refreshAgoTexts(snap);
      raiseCardFloor(); // 3.7u: an "active …" phrase can grow by a few characters here
      snapshotListeners.slice().forEach(function (fn) { fn(); }); // 3.9D D4: pages stay live here too
      return;
    }
    snapSig = sig;
    state.order = {};
    var i = 0;
    snap.hosts.forEach(function (host) {
      host.endpoints.forEach(function (ep) { state.order[ep.name] = i++; });
    });
    sparkScale = computeSparkScale(snap); // before the cards: every sparkline draws to this scale
    var meta = [h("span", { text: "attention window " + fmt.dur(snap.window_s, true) })];
    if (snap.db_path) {
      // the database the server opened: muted, elided from the left when long (the tail names
      // the file), the full path in the title
      meta.push(h("span", { class: "db", title: snap.db_path, text: "database " + snap.db_path }));
    }
    byId("meta").replaceChildren.apply(byId("meta"), meta);
    setUpdatedLine(snap);
    // 3.7m page half (3.7-fold-3): cards are keyed by host name, not rebuilt wholesale. A card
    // whose signature (host state, endpoint names and states in order) is unchanged keeps its
    // element and its endpoint elements; only the sub-blocks whose rendering changed swap. A
    // changed list/state, or a new host, rebuilds as before; a gone host is removed; an order
    // change moves elements without rebuilding. The jobs table used to keep replaceChildren
    // ("a table, cheap, no hover state the operator loses mid-row the way cards have") —
    // 3.12b patches it in place too, and mount() inserts its container only on a difference.
    var hidden = [];
    var hostsEl = byId("hosts");
    var oldCards = {};
    Array.prototype.slice.call(hostsEl.children).forEach(function (el) { oldCards[el.dataset.host] = el; });
    var rebuilt = false;
    var wanted = [];
    snap.hosts.forEach(function (host) {
      host.endpoints.forEach(function (ep) {
        // 3.7y: the line names the node's state — a never-seen endpoint hidden on a node that
        // cannot be reached is the node's own alert, and must not read like a merely idle one
        if (!neverSeenOn(ep)) return;
        hidden.push(ep.name + (host.state === "down" ? " (node down)"
          : host.state === "unknown" ? " (node unknown)" : "")); // config order: hosts then endpoints
      });
      var el = oldCards[host.name];
      var sig = hostSignature(host);
      if (el && el.dataset.sig === sig) {
        updateHostInPlace(el, hostCard(snap, host));
      } else {
        // the review of fold-3 (DeepSeek, D1): a card whose shape changed was rebuilt but the
        // old element stayed in #hosts (2 → 6 → 12 cards in ten seconds on a state flip); the
        // old card leaves the DOM here, before the map forgets it
        if (el) hostsEl.removeChild(el);
        el = hostCard(snap, host);
        if (el) el.dataset.sig = sig;
        if (el || oldCards[host.name]) rebuilt = true; // built, replaced or gone; never-seen-and-still-seen is nothing
      }
      if (el) wanted.push(el);
      // 3.9D D1: forget the host whether or not a card was built. Forgetting only inside
      // `if (el)` left a host whose card became null (its last drawn endpoint hidden as
      // never-seen) in oldCards, so the gone-host loop below called removeChild on the node
      // this branch had already detached: NotFoundError, render aborted after snapSig was
      // set — and the skip path then never repairs it.
      delete oldCards[host.name];
    });
    Object.keys(oldCards).forEach(function (name) {
      hostsEl.removeChild(oldCards[name]);
      rebuilt = true;
    });
    var cursor = hostsEl.firstChild; // reorder to the snapshot's order, moving kept elements
    wanted.forEach(function (el) {
      if (el === cursor) cursor = cursor.nextSibling;
      else hostsEl.insertBefore(el, cursor);
    });
    var hiddenLine = byId("hosts-hidden");
    if (hidden.length) {
      hiddenLine.textContent = hidden.length + (hidden.length === 1 ? " endpoint" : " endpoints")
        + " never seen, not shown: " + hidden.join(", ");
      hiddenLine.hidden = false;
    } else {
      hiddenLine.hidden = true;
      hiddenLine.textContent = "";
    }
    raiseCardFloor(); // 3.7u: the cards are in place; the floor follows their widest line
    renders[rebuilt ? "full" : "inPlace"]++;
    // 3.10b: the overview passes the snapshot's rows and its own note; the endpoint page calls
    // the same renderer from endpoint.js. 3.12b: the renderer returns the PERSISTENT container
    // (level 1 skips unchanged rows wholesale, level 2 patches the changed ones), and mount
    // inserts it only when #jobs-body is not already exactly it — re-inserting the same element
    // recreates its scroll frame, which the host-card comment above used to accept as the
    // table's cost ("a table, cheap, no hover state the operator loses mid-row"): the table now
    // patches like the cards.
    mount(byId("jobs-body"), [jobsTable(snap.jobs, {
      now: snap.now, note: "running and queued first, then the last 10 finished" })]);
    snapshotListeners.slice().forEach(function (fn) { fn(); }); // 3.9D D4: the snapshot redraw is done
  }

  // --- history: the one batched call, at most once per refresh_s -----------------------------

  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) throw new Error((body && body.message) || ("HTTP " + r.status + " from " + url));
        return body;
      });
    });
  }

  function fetchHistory(name, range) {
    return getJSON("/api/endpoints/" + encodeURIComponent(name) + "/history?range=" + encodeURIComponent(range));
  }

  // story 3.7p: one call for the fleet grid and every live endpoint's own history (the server
  // classifies live exactly as the snapshot does), replacing the fleet call plus one per
  // endpoint. fetchHistory stays for the endpoint page; 3.7l dropped fetchFleet, whose only
  // caller was the fleet charts (the /api/fleet/history route itself serves other callers).
  function fetchOverviewHistory(range) {
    return getJSON("/api/overview/history?range=" + encodeURIComponent(range));
  }

  // `detail`: a detail page is open (route() sets it), and the hidden overview fetches nothing
  var refresh = { last: 0, timer: null, inflight: false, again: false, detail: false };

  function liveNames(snap) {
    var out = [];
    if (!snap) return out;
    snap.hosts.forEach(function (host) {
      host.endpoints.forEach(function (ep) { if (ep.state === "live") out.push(ep.name); });
    });
    return out;
  }

  // ask for fresh history; a call inside HISTORY_MIN_INTERVAL_MS (the snapshot's refresh_s,
  // 3.7-fold-3) of the last fetch is deferred to that mark (one timer, however many snapshots
  // arrive meanwhile)
  function scheduleHistory(force) {
    if (force) refresh.last = 0;
    // behind a detail page the overview's charts, sparklines and strips are not on screen: no
    // fetch (story 3.2a, C); route() asks again, forced, when the overview comes back
    if (refresh.detail) {
      if (refresh.timer) { clearTimeout(refresh.timer); refresh.timer = null; }
      return;
    }
    if (refresh.inflight) { refresh.again = true; return; }
    var wait = refresh.last + HISTORY_MIN_INTERVAL_MS - Date.now();
    if (wait > 0) {
      if (!refresh.timer) refresh.timer = setTimeout(function () { refresh.timer = null; scheduleHistory(false); }, wait);
      return;
    }
    if (refresh.timer) { clearTimeout(refresh.timer); refresh.timer = null; }
    refresh.inflight = true;
    refresh.last = Date.now();
    var range = state.range;
    fetchOverviewHistory(range).then(function (body) {
      if (range !== state.range) return; // the range changed meanwhile: this answer is stale
      // 3.7l: the batched body's `fleet` is no longer read by the overview (the route stays
      // for its own callers); the timeline draws from `body.endpoints` and `body.jobs`
      state.hists = body.endpoints || {};
      state.histRange = range;
      historyGen++; // the strips and sparklines draw from these: the snapshot alone may not
                    // have changed, but the picture has — the next render must not skip
      timeline.update(body, state.snap);
      if (state.snap) render(state.snap);
    }).catch(function (e) {
      // 3.9D: on a failed range fetch histRange stays the range the drawn charts and the
      // timeline's bars were fetched for, and the heading and axis (which read it, :1074)
      // stay with it. That is on purpose: the bars on screen belong to the old window, so
      // naming the pressed-but-unfetched range would describe geometry that is not there. The
      // pressed button (drawn at once) and this banner carry the pending switch, and the next
      // snapshot's scheduleHistory re-fetches — the split is transient while the server is.
      showBanner("fleet history: " + e.message);
    }).then(function () {
      refresh.inflight = false;
      if (range !== state.range) refresh.last = 0;
      if (refresh.again || range !== state.range) { refresh.again = false; scheduleHistory(false); }
    });
  }

  // --- the fleet timeline (story 3.7l) --------------------------------------------------------
  // One shared time axis, not three rate charts (the operator: "Fleet graphs are basically
  // useless right now in this form"; "the single timeline is better"). A row per live endpoint
  // in card order: its busy intervals as a 6 px band at the row's bottom (the state strip's
  // segment walk over its own history — idle NOT drawn, so an idle track is empty, a null a
  // gap) and its jobs as labelled bars above it, from the batched body's `jobs` rows: a box
  // idle all day reads as an empty row. Every left/width is a percentage of the window, so the
  // section needs no resize handling at all and its height depends only on the live count.

  // DESIGN's named intervals for the five offered ranges; the fallback takes the first nice
  // step that keeps the labels at seven or fewer (the server's range parsing accepts more
  // spellings than the control offers, and 4-7 labels is the DESIGN's invariant)
  var TL_TICKS = { "15m": 300, "1h": 900, "6h": 3600, "24h": 14400, "7d": 86400 };
  var TL_NICE = [300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400];

  function tickStep(span, rangeWord) {
    if (TL_TICKS[rangeWord] != null) return TL_TICKS[rangeWord];
    for (var i = 0; i < TL_NICE.length; i++) if (span / TL_NICE[i] <= 7) return TL_NICE[i];
    return 86400;
  }

  // "#3987e5" -> "rgba(57,135,229,0.25)": the bar's fill at the DESIGN's quarter opacity with
  // the ink text unaffected (an element opacity would fade the name too, and a pseudo-element
  // would hide the link's hit area behind it); the 1 px border keeps the full colour
  function fillOf(colour) {
    var n = parseInt(colour.slice(1), 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + ",0.25)";
  }

  var timeline = {
    update: function (body, snap) {
      if (!snap || !snap.hosts) return; // no rows to draw until the first snapshot lands
      var box = byId("timeline");
      if (!box) return; // the section belongs to the overview, which a route only hides
      var names = liveNames(snap);
      var rangeWord = state.histRange || state.range;
      byId("fleet-title").textContent = "Timeline · last " + rangeWord;
      if (!names.length) {
        box.replaceChildren(h("div", { class: "empty", text: "no live endpoints" }));
        return;
      }
      var span = rangeSeconds(rangeWord);
      var now = snap.now;
      var t0 = now - span;
      timeline.lastNow = now; // the clock the drawn percentages rest on (the smoke's witness)
      var pct = function (t) { return Math.max(0, Math.min(100, ((t - t0) / span) * 100)); };
      var jobs = (body && body.jobs) || [];
      var hists = (body && body.endpoints) || {};

      // gridlines and the now marker live in one absolute layer behind the rows, offset by the
      // 140 px label column (the label's padding-right is part of that 140 px, so the layer's
      // left edge is exactly the tracks' left edge)
      var lines = h("div", { class: "tl-lines" });
      var axisInner = h("div", { class: "tl-axis-inner" });
      var step = tickStep(span, rangeWord);
      var useDate = span > 86400; // DESIGN: the clock on the ticks, and the date+clock form past
      // 24 h (the 7 d range); 3.7-fold-5 took the seconds off both (the bars' titles keep fmt.time)
      for (var t = Math.ceil(t0 / step) * step; t <= now; t += step) {
        var pos = pct(t).toFixed(2) + "%";
        lines.appendChild(h("span", { class: "tl-grid", style: "left:" + pos }));
        axisInner.appendChild(h("span", { style: "left:" + pos, text: useDate ? fmt.dayClock(t) : fmt.clock(t) }));
      }
      lines.appendChild(h("span", { class: "tl-now", title: "now" }));

      var rows = h("div", { class: "tl-rows" }, [lines]);
      names.forEach(function (name) {
        var track = h("div", { class: "tl-track" });
        // the busy band: the segment walk straight out of stateStrip, minus idle
        var hist = hists[name];
        if (hist && hist.t && hist.state) {
          for (var i = 0; i < hist.t.length; i++) {
            var s = hist.state[i];
            if (s == null) continue;
            var j = i;
            while (j + 1 < hist.t.length && hist.state[j + 1] === s) j++;
            var segStart = Math.max(hist.t[i], t0);
            var segEnd = j + 1 < hist.t.length ? hist.t[j + 1] : now;
            if (s !== "idle" && segEnd > t0) {
              track.appendChild(h("span", {
                class: "tl-band",
                style: "left:" + pct(segStart).toFixed(2) + "%;width:" +
                  Math.max(((segEnd - segStart) / span) * 100, 0.3).toFixed(2) + "%;background:" + stateColour(s),
              }));
            }
            i = j;
          }
        }
        // the job bars, in two lanes per row: lane 0 is the ordinary 20 px bar; a job that
        // starts before lane 0's last bar ends takes lane 1, the 10 px bar above the band's
        // edge. A third job overlapping the same minute lands in lane 1 too (the DESIGN's
        // "at most two before they overlap visibly": a row's own slots bound the truth, and a
        // four-slot box is Epic 4 territory)
        var free = [-Infinity, -Infinity];
        jobs.filter(function (job) { return job.endpoint === name; }).forEach(function (job) {
          var running = job.finished_at == null;
          var end = running ? now : job.finished_at;
          var lane = job.started_at >= free[0] ? 0 : 1;
          free[lane] = end;
          var left = pct(job.started_at), right = pct(end);
          track.appendChild(h("a", {
            class: "tl-job" + (lane ? " tl-job-2" : "") + (running ? " tl-job-run" : ""),
            href: "#/job/" + encodeURIComponent(job.id),
            style: "left:" + left.toFixed(2) + "%;width:" + Math.max(right - left, 0.4).toFixed(2) +
              "%;background:" + fillOf(colourOf(name)) + ";border-color:" + colourOf(name),
            // 3.10f-2: the bar's label is the short id; the title keeps the full id first
            title: job.id + " · started " + fmt.time(job.started_at) +
              " · " + fmt.dur(end - job.started_at) + " · " + job.status,
            text: shortId(job.id),
          }));
        });
        rows.appendChild(h("div", { class: "tl-row" }, [
          h("div", { class: "tl-label" }, [endpointLink(name)]),
          track,
        ]));
      });
      box.replaceChildren(rows, h("div", { class: "tl-axis" }, [h("div", { class: "tl-axis-pad" }), axisInner]));
    },
  };

  // --- the range control ----------------------------------------------------------------------

  function loadRange() {
    try {
      var r = window.localStorage.getItem(RANGE_STORE_KEY);
      if (RANGES.indexOf(r) >= 0) return r;
    } catch (e) { /* storage unavailable: the default */ }
    return DEFAULT_RANGE;
  }

  function setRange(r) {
    state.range = r;
    try { window.localStorage.setItem(RANGE_STORE_KEY, r); } catch (e) { /* not remembered */ }
    range.draw();
    scheduleHistory(true);
  }

  var range = {
    draw: function () {
      var box = byId("range");
      box.replaceChildren.apply(box, RANGES.map(function (r) {
        var b = h("button", { type: "button", "aria-pressed": r === state.range ? "true" : "false", text: r });
        b.addEventListener("click", function () { if (r !== state.range) setRange(r); });
        return b;
      }));
    },
    set: setRange,
  };

  // --- the stream: server-sent events with reconnect and backoff ------------------------------

  function showBanner(text) {
    var b = byId("banner");
    b.textContent = text;
    b.hidden = false;
  }
  function clearBanner() { byId("banner").hidden = true; }

  // 3.7r: one phrase, not a status word beside a separate time. Green dot + "updated HH:MM:SS"
  // while the stream is open (the time advances with each push, so staleness shows without
  // looking away); red dot + "connection lost, last update HH:MM:SS" while reconnecting, the
  // last snapshot's time kept. No "live" in the header: the old label claimed a fleet meaning
  // it never had (operator: "Does that green dot and label 'live' actually mean anything?").
  // Before the first snapshot there is no time to show: "connecting" (stream opening or
  // already dropped with nothing yet to remember). setConnected sets the FORM (the stream's
  // up/down and its words), render's setUpdatedLine moves the TIME while the stream is up.
  var streamUp = false;

  function connPhrase() {
    if (state.snap == null) return "connecting";
    if (streamUp) return "updated " + fmt.time(state.snap.now);
    return "connection lost, last update " + fmt.time(state.snap.now);
  }

  function setConnected(up) {
    streamUp = up;
    var c = byId("conn");
    c.className = "conn " + (up ? "conn-up" : "conn-down");
    byId("conn-label").textContent = connPhrase();
  }

  var stream = { source: null, delay: RECONNECT_MIN_MS, timer: null };

  function connect() {
    if (stream.timer) { clearTimeout(stream.timer); stream.timer = null; }
    if (stream.source) stream.source.close();
    var es = new EventSource("/events");
    stream.source = es;
    es.addEventListener("open", function () {
      stream.delay = RECONNECT_MIN_MS;
      setConnected(true);
    });
    es.addEventListener("snapshot", function (e) {
      var snap;
      try { snap = JSON.parse(e.data); } catch (err) { showBanner("unreadable snapshot: " + err.message); return; }
      setConnected(true);
      clearBanner();
      render(snap);
      scheduleHistory(false);
    });
    // `error` is both the server's named event (a MessageEvent with data: the database could not
    // answer, the stream goes on) and EventSource's own connection error (no data)
    es.addEventListener("error", function (e) {
      if (e.data !== undefined && e.data !== null) {
        var msg = e.data;
        try { msg = JSON.parse(e.data).message || msg; } catch (err) { /* the raw text */ }
        showBanner(msg);
        return;
      }
      setConnected(false);
      es.close();
      if (stream.source === es) stream.source = null;
      if (!stream.timer) {
        stream.timer = setTimeout(connect, stream.delay);
        stream.delay = Math.min(stream.delay * 2, RECONNECT_MAX_MS);
      }
    });
    return es;
  }

  // --- navigation: #/job/<id> and #/endpoint/<name> are stories 3.3 and 3.4 -----------------

  function route() {
    var m = /^#\/(job|endpoint)\/(.+)$/.exec(window.location.hash);
    var detail = byId("detail");
    var overview = byId("overview");
    // the name is decoded before anything is torn down or flipped: a malformed escape
    // (#/endpoint/ai%zz) throws URIError, and the raw text stands in, so the page draws its
    // "no endpoint named" / "no job" sentence for it instead of a blank pane (story 3.2a, E;
    // on a cold load the throw used to stop start() before the stream connected)
    var name = null;
    if (m) {
      try { name = decodeURIComponent(m[2]); } catch (e) { name = m[2]; }
    }
    // the open detail page's teardown runs on EVERY route change, the overview's included
    if (pages.teardown) { try { pages.teardown(); } catch (e) { console.error(e); } pages.teardown = null; }
    var wasDetail = refresh.detail;
    refresh.detail = !!m;
    if (!m) {
      detail.hidden = true;
      overview.hidden = false;
      // 3.7u: renders behind a detail page measured nothing (no layout); measure now
      raiseCardFloor();
      // back from a detail page: the overview fetched nothing meanwhile, so fetch once now
      if (wasDetail) scheduleHistory(true);
      return;
    }
    var what = m[1] === "job" ? "Job" : "Endpoint";
    overview.hidden = true;
    detail.hidden = false;
    // a detail page registers itself as pages.job / pages.endpoint (stories 3.3 and 3.4, each
    // in its own file); it draws into `detail` and returns a teardown function, called before
    // the next route; the placeholder below stands until a page is registered
    var page = pages[m[1]];
    if (page) {
      pages.teardown = page(detail, name, state) || null;
      return;
    }
    detail.replaceChildren(
      h("p", null, [h("strong", { text: what + " " + name })]),
      h("p", { text: "detail page not built yet" }),
      h("p", null, [h("a", { href: "#/", text: "back to the overview" })])
    );
  }

  // --- start ----------------------------------------------------------------------------------

  function start() {
    state.range = loadRange();
    range.draw();
    route();
    window.addEventListener("hashchange", route);
    // 3.7l: no resize listener — the fleet charts were the only pixel-sized drawing; the
    // timeline is all percentages, so a narrower window is narrower CSS, not a redraw
    connect();
  }

  var pages = { job: null, endpoint: null, teardown: null };

  // --- 3.7k: the one requests-table renderer --------------------------------------------------
  //
  // Both detail pages draw their request log through this: the endpoint page's requests across
  // jobs (endpoint.js) and a job page's own requests (job.js). The columns are the operator's
  // log look with the two rates Strata derives from the same facts: time, a status pill, job
  // (omitted on a job page, where every row is the one job), path, prompt (the whole prompt:
  // input + cache_read + cache_creation — 3.10h(b), the API's bare input_tokens is 1 on a hot
  // prefix cache), new (input + cache_creation, what the engine processed),
  // reused (tokens.cache_read), output, prefill t/s, decode t/s, duration, error. The rows are
  // the endpoint route's (newest first, with job_id) or the job detail's (time order,
  // no job fields); neither is re-sorted here. `opts.job === false` drops the job column;
  // `opts.empty` is the sentence an empty list draws (each page keeps its own).
  //
  // The rates are blank, never a placeholder number, when their inputs are missing:
  //   prefill t/s = (input + cache_creation) / ((ms − gen_ms) / 1000), when ms > gen_ms and
  //                 input + cache_creation > 0 (a non-stream request may have no gen_ms);
  //                 a missing cache_creation counts as 0 when input is a number (3.10i: a
  //                 backend with no cache fields at all, vLLM, still gets a rate — an upper
  //                 bound, and the cell's title names it);
  //   decode t/s  = output / (gen_ms / 1000), when gen_ms > 0 and output > 0.
  // The brief wrote Strata's prefill as "input − cache_read", but that is Strata's model, where
  // its input is the WHOLE prompt: our `tokens.input` excludes cache reads (derive._PROMPT_SUM
  // is input + cache_read + cache_creation), so the subtraction went negative on 148 of the
  // seed's 152 requests and the column read blank. Strata's processed count is
  // `prompt_tokens − cached` (registry.py:589), i.e. the tokens this request actually
  // prefilled: input + cache_creation here. The brief's literal subtraction was corrected on
  // the data model, not as a display choice (2026-10-30).
  var REQ_NUMERIC = { prompt: 1, new: 1, reused: 1, output: 1, "prefill t/s": 1, "decode t/s": 1, duration: 1 };
  // 3.12a (spec §3): the requests table's filter kinds and units, same shapes as the jobs
  // table's FILTER_KIND / UNIT beside NUMERIC (the names follow REQ_NUMERIC/REQ_TITLES)
  var REQ_FILTER_KIND = { time: "time", status: "pick", path: "pick", prompt: "number", new: "number",
    reused: "number", output: "number", "prefill t/s": "number", "decode t/s": "number", duration: "number" };
  var REQ_UNIT = { duration: "ms", prompt: "tokens", new: "tokens", reused: "tokens", output: "tokens",
    "prefill t/s": "tokens/s", "decode t/s": "tokens/s" };
  // 3.10h(b): the two headers whose column names a derivation, not a stored field, explain
  // themselves (the operator: "Weird - prompt = 1?" — it was `tokens.input`, the uncached tail)
  var REQ_TITLES = { prompt: "the whole prompt: input + reused + cache creation",
                     new: "tokens the engine processed: input + cache creation" };
  var ERROR_CUT = 60; // the error cell's chars; the full sentence rides in its title

  // the reads are spelled `req.tokens.<field>` so the §5a walk sees every field name (an
  // alias would hide them from the gate)
  function prefillRate(req) {
    if (!req.tokens) return null;
    if (typeof req.tokens.input !== "number") return null;
    if (typeof req.ms !== "number" || typeof req.gen_ms !== "number") return null;
    // 3.10i (the vLLM lane: no cache fields in its usage at all): a missing cache_creation
    // counts as 0 when input is a number — the processed tokens are input alone, an UPPER
    // bound (the engine's own prefix cache is invisible); the cell's title says so
    var cc = typeof req.tokens.cache_creation === "number" ? req.tokens.cache_creation : 0;
    var prefill = req.tokens.input + cc; // the tokens actually processed
    if (req.ms <= req.gen_ms || prefill <= 0) return null;
    return prefill / ((req.ms - req.gen_ms) / 1000);
  }

  function decodeRate(req) {
    if (!req.tokens) return null;
    if (typeof req.tokens.output !== "number" || typeof req.gen_ms !== "number") return null;
    if (req.gen_ms <= 0 || req.tokens.output <= 0) return null;
    return req.tokens.output / (req.gen_ms / 1000);
  }

  // 3.10h(b) (the operator's "prompt = 1", not transient): the API's input_tokens is only the
  // uncached, un-created tail of the prompt — 1 on every row when the prefix cache is hot (the
  // engine re-evaluates the last token). The "prompt" cell is the whole prompt the request
  // carried: input + cache_read + cache_creation (derive._PROMPT_SUM's shape). A part that is
  // not a number counts as 0 when at least one part is one; none of them a number answers null
  // (the blank cell, never a 0 claiming an empty prompt).
  function promptTotal(req) {
    if (!req.tokens) return null;
    if (typeof req.tokens.input !== "number" && typeof req.tokens.cache_read !== "number"
        && typeof req.tokens.cache_creation !== "number") return null;
    return (req.tokens.input || 0) + (req.tokens.cache_read || 0) + (req.tokens.cache_creation || 0);
  }

  // "new": the tokens the engine actually processed this request — input + cache_creation, the
  // prefillRate numerator, shown so the prompt cell's total can be read against its rates;
  // blank (null) when neither number is present
  function newTokens(req) {
    if (!req.tokens) return null;
    if (typeof req.tokens.input !== "number" && typeof req.tokens.cache_creation !== "number") return null;
    return (req.tokens.input || 0) + (req.tokens.cache_creation || 0);
  }

  // the error cell's sentence: the error, else "context exceeded" when the request overran
  // the window; "" when clean (the empty string a clean export writes is clean too).
  // context_exceeded is stored TEXT — the backend's own sentence (db.py's column, the note
  // shim.py writes) — never a boolean, so "set" is a non-empty string or true; the truthy
  // read is the whole test (3.7k review, POSSIBLE; 3.7-fold-4)
  function requestError(req) {
    if (req.error != null && req.error !== "") return String(req.error);
    if (req.context_exceeded) return "context exceeded";
    return "";
  }

  // the one request-failure rule (3.11a: it was hand-copied here and in job.js's "request
  // errors" figure): a status outside 2xx, or a non-empty error text — except the /api/hello
  // handshake (3.7v: Claude Code's startup probe, recorded with the backend's 404/501, is a
  // fact about the backend's routing, never a failure of the job). The table's req-err mark
  // and the job header's count both read it; exported for job.js.
  function requestFailed(req) {
    if (req.path === "/api/hello") return false;
    if (typeof req.http_status === "number" && (req.http_status < 200 || req.http_status >= 300)) return true;
    return req.error != null && req.error !== "";
  }

  // opts: {job: show the job column (default true), empty: the empty sentence,
  // now: the server's clock the day-stamp rule runs against (3.7-fold-4: the endpoint page
  // passes the snapshot's now, the job page the detail's clock — same rule as the endpoint
  // page's when() and the jobs table: time of day inside 24 h, date and time past it, so at
  // 7d the same clock time never repeats per day; the title holds the full date always)}
  // 3.10c: the table's head, filter row, body slice, pager and note belong to the helper; this
  // hands over the same header names (REQ_NUMERIC, REQ_TITLES) and the same cell builders.
  // The id separates the two pages (the brief's `opts.job ? "job" : "endpoint"` reads inverted
  // against the code — the endpoint page passes no opts.job (undefined, defaulting to true) and
  // the job page passes false — so the test is `opts.job === false`).
  function requestsTable(rows, opts) {
    opts = opts || {};
    rows = rows || [];
    var withJob = opts.job !== false;
    var columns = ["time", "status"];
    if (withJob) columns.push("job");
    columns = columns.concat(["path", "prompt", "new", "reused", "output", "prefill t/s", "decode t/s", "duration", "error"]);
    // one request row keyed by column name (3.10c: the row assembly moved to the helper; the
    // cells, the row marks and the titles are unchanged); `word` is the status pill's text —
    // that column's sort key and filter text, exactly what the pill prints
    function reqRow(req) {
      var tokens = req.tokens || {};
      // the cache cells read through req.tokens directly (the §5a walk sees a field only on a
      // chain rooted at `req`; the display alias above would hide these two)
      var cacheRead = req.tokens ? req.tokens.cache_read : null;
      var cacheCreation = req.tokens ? req.tokens.cache_creation : null;
      var error = requestError(req);
      // 3.7v (the operator: "tool errors sometimes shows a count of 2 when the job only shows
      // 1 error returned (often the /api/hello…)" — the two figures count different things):
      // Claude Code's startup handshake, forwarded by the shim and recorded with the backend's
      // 404/501, is a fact about the backend's routing, never a failure of this job. The row
      // wears a muted pill reading "handshake" (the status rides in its title), takes no
      // req-err mark, and each caller's error count skips it (3.7t makes the shim answer 200
      // and write no row, so NEW databases hold none; the rows already in history stay).
      var handshake = req.path === "/api/hello";
      var cls = [];
      if (requestFailed(req)) cls.push("req-err"); // the one rule, the handshake excepted
      if (req.context_exceeded) cls.push("req-ctx"); // the same truthy read: a non-empty string or true
      var prefill = prefillRate(req);
      var decode = decodeRate(req);
      var prompt = promptTotal(req);
      var newTok = newTokens(req);
      // the time cell: the shared day-stamped timeTd (3.7-fold-4's rule; factored for the
      // jobs log's "started" in 3.10a)
      // the reused cell's title names both cache figures; a missing one drops its half of the
      // sentence (3.9D NOTICED: the old inline build kept the other half's words anyway —
      // null read and a present creation read "cache read , cache creation 300", both null
      // "cache read " with a trailing space)
      var cacheHalves = [];
      if (fmt.int(cacheRead) !== "") cacheHalves.push("cache read " + fmt.int(cacheRead));
      if (fmt.int(cacheCreation) !== "") cacheHalves.push("cache creation " + fmt.int(cacheCreation));
      var reusedTitle = cacheHalves.join(", ");
      // 3.10h(b): the prompt cell's title names the three parts its total adds
      // ("input 1, cache read 16,384, cache creation 3,012"); a missing part drops its half,
      // the reused title's rule
      var promptParts = [];
      if (fmt.int(tokens.input) !== "") promptParts.push("input " + fmt.int(tokens.input));
      if (fmt.int(cacheRead) !== "") promptParts.push("cache read " + fmt.int(cacheRead));
      if (fmt.int(cacheCreation) !== "") promptParts.push("cache creation " + fmt.int(cacheCreation));
      var promptTitle = promptParts.join(", ");
      // 3.10i: the prefill cell's title. A backend that reports NEITHER cache field (vLLM's
      // Anthropic shape, until --enable-prompt-tokens-details) gets a rate with cache
      // counted as 0 — the whole input over time-to-first-token — so the title names the
      // upper bound rather than a numerator the backend never confirmed. A backend that does
      // report cache gets the numerator sentence ("3,001 new tokens over 0.5 s").
      var prefillTitle = null;
      if (prefill != null) {
        var spanMs = req.ms - req.gen_ms;
        prefillTitle = typeof cacheCreation !== "number" && typeof cacheRead !== "number"
          ? "cache not reported by this backend: the rate counts the whole input"
          : fmt.int(req.tokens.input + (typeof cacheCreation === "number" ? cacheCreation : 0))
            // the duration cell's own sub-second rule, so a 39 ms window never reads "0.0 s"
            + " new tokens over " + (spanMs < 1000 ? fmt.int(spanMs) + " ms"
              : fmt.speed(spanMs / 1000) + " s");
      }
      var statusCell;
      if (handshake) {
        statusCell = h("td", null, [h("span", { class: "pill pill-muted",
          title: req.http_status == null ? "no status" : String(req.http_status), text: "handshake" })]);
      } else if (req.http_status == null) {
        statusCell = h("td", null, [h("span", { class: "pill", title: "no status", text: "–" })]);
      } else {
        var ok = req.http_status >= 200 && req.http_status < 300;
        statusCell = h("td", null, [h("span", { class: "pill " + (ok ? "pill-ok" : "pill-error"),
          title: String(req.http_status), text: ok ? "ok" : "error" })]);
      }
      var cells = {
        time: timeTd(req.t, opts.now),
        status: statusCell,
        path: h("td", { class: "cut", title: req.path || "", text: req.path || "" }),
        prompt: h("td", { class: "n", title: promptTitle || null, text: fmt.int(prompt) }),
        new: h("td", { class: "n", text: fmt.int(newTok) }),
        // the reused cell's title names both cache figures; a missing one drops its half of
        // the sentence rather than trailing a blank (fold-3 review NOTICED)
        reused: h("td", { class: "n", title: reusedTitle, text: fmt.int(cacheRead) }),
        output: h("td", { class: "n", text: fmt.int(tokens.output) }),
        "prefill t/s": h("td", { class: "n", title: prefillTitle, text: prefill == null ? "" : fmt.speed(prefill) }),
        "decode t/s": h("td", { class: "n", text: decode == null ? "" : fmt.speed(decode) }),
        // duration (3.7-fold-4): under a second in whole ms ("8 ms" — the old rule read every
        // sub-10 ms request "0.0 s"), else tenths of a second
        duration: h("td", { class: "n", text: typeof req.ms === "number"
          ? (req.ms < 1000 ? fmt.int(req.ms) + " ms" : fmt.speed(req.ms / 1000) + " s") : "" }),
        error: h("td", { class: "cut", title: error || null,
          text: error.length > ERROR_CUT ? error.slice(0, ERROR_CUT) : error }),
      };
      if (withJob) {
        // 3.10f-2: the job column is the shared short-id link (the row carries job_id only)
        cells.job = h("td", null, [req.job_id ? jobLink(req.job_id) : null]);
      }
      var word = handshake ? "handshake" : req.http_status == null ? "–"
        : (req.http_status >= 200 && req.http_status < 300 ? "ok" : "error");
      return { cells: cells, cls: cls, word: word };
    }
    // the sort keys read what the cells show (3.10c): the time cell by its timestamp, the
    // pill by its word, the id cell by the full id; the derived figures reuse the cells'
    // own helpers, so the number that sorts is the number that prints
    var memo = rowMemo(reqRow); // one build per row, not one per column (3.11a)
    var specs = columns.map(function (c) {
      return {
        name: c, numeric: !!REQ_NUMERIC[c], title: REQ_TITLES[c] || null,
        filter: REQ_FILTER_KIND[c] || "text", unit: REQ_UNIT[c] || null, // 3.12a (spec §3)
        cell: function (req) { return memo.cell(req, c).cells[c]; },
        sortKey: function (req) {
          if (c === "time") return req.t;
          if (c === "status") return memo.row(req).word;
          if (c === "job") return req.job_id == null ? "" : req.job_id;
          if (c === "path") return req.path || "";
          if (c === "prompt") return promptTotal(req);
          if (c === "new") return newTokens(req);
          if (c === "reused") return req.tokens ? req.tokens.cache_read : null;
          if (c === "output") return req.tokens ? req.tokens.output : null;
          if (c === "prefill t/s") return prefillRate(req);
          if (c === "decode t/s") return decodeRate(req);
          if (c === "duration") return req.ms;
          return requestError(req); // the error column sorts by its text (blank first)
        },
      };
    });
    // 3.12a fold F3: a caller may pass its own id (the endpoint page keys it by the endpoint
    // name), so a filter typed on one endpoint's page never filters another's; the job page
    // passes no id and keeps "requests-job" (one table there)
    return dataTable(opts.id || "requests-" + (opts.job === false ? "job" : "endpoint"), specs, rows, {
      empty: opts.empty || "no requests",
      class: "requests-table",
      now: opts.now, // 3.12c: the time picker's "today"/"yesterday" judge against this clock
      rowAttrs: function (req) {
        var cls = memo.row(req).cls;
        return { class: cls.length ? cls.join(" ") : null };
      },
    });
  }

  window.localagentsMonitor = {
    pages: pages,
    h: h,
    fmt: fmt,
    render: render,
    connect: connect,
    fetchHistory: fetchHistory,
    timeline: timeline, // 3.7l: the fleet timeline (the smoke hand-renders it with two endpoints)
    range: range,
    state: state,
    requestsTable: requestsTable, // 3.7k: the one renderer both request logs draw through
    requestFailed: requestFailed, // 3.11a: the one request-failure rule (the row mark; job.js's count)
    jobsTable: jobsTable, // 3.10b: the one renderer both jobs tables draw through
    renders: renders, // 3.7-fold-3 smoke: {full, inPlace, skipped} counts of render passes
    onSnapshot: onSnapshot, // 3.9D D4: the detail pages' snapshot hook; returns the remover
    cardLayout: cardLayout, // 3.7u smoke: {floor, need, raised} — the column floor's high-water mark
    shortId: shortId, // 3.10f-2: the one short-id rule, exported for the detail pages and the smoke
    jobLink: jobLink, // 3.10f-2: the short id as a link, full id in the title
    dataTable: dataTable, // 3.10c: the one table helper (page, sort, filter)
    TABLE_STATE: TABLE_STATE, // 3.10c: the per-table view, keyed by id — exported for the smoke
    TABLE_DOM: TABLE_DOM, // 3.12b: the last draw per id {container, hash, draw} — tests and smoke
    mount: mount, // 3.12b: replaceChildren only when the children actually differ — the detail pages
    PICK_BLANK: PICK_BLANK, // 3.12a fold F1: the pick's blank value — exported for the tests
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
