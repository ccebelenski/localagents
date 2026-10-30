/* localagents monitor: the overview page (story 3.2; monitor spec §5 "Overview").
 *
 * The page lives on ONE server-sent events stream (/events): each `snapshot` event redraws the
 * host cards and the running-jobs table in place; the page never polls. The fleet charts, the
 * decode sparklines and the state strips' segments need history: on a snapshot the page
 * re-fetches /api/fleet/history (one call for the whole fleet) and each live endpoint's
 * /api/endpoints/<name>/history (for its `state` series; the strip's current word is the
 * snapshot's own `probe_state`, never a point of the series) at most once every 5 s. While a
 * detail page is open the overview is hidden and fetches nothing; it fetches once on return.
 *
 * The JSON is the contract in docs/superpowers/specs/2026-10-29-monitor-design.md §5a; every
 * field read here is named there (tests/test_monitor_page.py checks it). The objects are read
 * through a few fixed names: `snap` (the snapshot), `host`, `ep` (an endpoint), `job` (a
 * running job), `hw` (ep.hardware), `fleet` (the fleet history), `fleetEp` (one endpoint's
 * fleet series) and `hist` (an endpoint history).
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
  var HISTORY_MIN_INTERVAL_MS = 5000; // fleet and strip history: at most one fetch per 5 s
  var RECONNECT_MIN_MS = 1000;
  var RECONNECT_MAX_MS = 30000;

  // categorical slots, fixed per endpoint by its index in config order (dark-surface steps);
  // past eight the hues repeat with a dashed line, so identity never rests on colour alone
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

  function jobLink(id, name) { return h("a", { href: "#/job/" + encodeURIComponent(id), text: name }); }
  function endpointLink(name) {
    return h("a", { class: "ep-name", href: "#/endpoint/" + encodeURIComponent(name), text: name });
  }

  // --- page state -----------------------------------------------------------------------------

  var state = {
    range: DEFAULT_RANGE,
    snap: null, // the last snapshot
    fleet: null, // the last fleet history, for state.fleetRange
    fleetRange: null,
    hists: {}, // endpoint name -> its last history (for histRange)
    histRange: null,
    order: {}, // endpoint name -> index in config order (series colours)
  };

  function colourOf(name) {
    var i = state.order[name];
    return PALETTE[(i == null ? 0 : i) % PALETTE.length];
  }
  function dashOf(name) {
    var i = state.order[name];
    return i != null && i >= PALETTE.length ? [6, 4] : null;
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

  // one job as the MCP status line (status.py's `_job_row`): the mark, the name, the elapsed,
  // then the bits that are actually there joined by ` · ` — calls, files, ctx, t/s,
  // compactions — then the last activity cut short. The marks are status.py's MARK (running
  // "⟳", queued "…"): the mark is the queued word, no badge on the card row.
  function epJobRow(job) {
    var row = [h("span", { text: job.status === "queued" ? "… " : "⟳ " }),
      jobLink(job.id, job.name),
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
    var act = oneLine(job.last_activity);
    if (act) {
      trail(h("span", { class: "ep-act", title: act, text: act.length > 60 ? act.slice(0, 59) + "…" : act }));
    }
    return h("div", { class: "ep-job" }, row);
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
    // the decode tile names the window its value, sparkline and strip share (3.7q: the time
    // axis is the page's range control, and nothing else on the card says so); the sparkline
    // carries the shared-scale peak label at its right edge. The label gets a title because
    // at card width it can ellipsis before "last <range>".
    var rangeWord = state.histRange || state.range;
    var decodeLabel = "decode tok/s · last " + rangeWord;
    var spark = sparkline(ep.name, snap.now);
    var decodeTile = fig(decodeLabel, rates.gen_tps != null ? fmt.speed(rates.gen_tps) : "",
      spark ? h("span", { class: "spark-wrap" }, [spark,
        h("span", { class: "spark-peak muted", text: "↑ " + fmt.speed(sparkScale) })]) : null);
    decodeTile.firstChild.setAttribute("title", decodeLabel);
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
    return h("div", { class: "endpoint state-live", "data-endpoint": ep.name }, [
      // the state dot (3.7f): the first thing on the head; the down/dormant kinds take the
      // same span with their variant (their CSS lives here too — see the 3.7d-f block)
      h("div", { class: "ep-head" }, [h("span", { class: "sdot sdot-live", "aria-hidden": "true" }),
        endpointLink(ep.name), h("span", { class: "tag", text: ep.backend }),
        ep.model ? h("span", { class: "ep-model", title: ep.model, text: shortModel(ep.model) }) : null]),
      stateStrip(ep, snap.now),
      fieldGrid(snap, ep),
      rates.reason ? h("div", { class: "ep-note muted", text: "speeds: " + rates.reason }) : null,
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
        h("span", { class: "ep-seen", text: ep.last_seen != null ? "last seen " + fmt.ago(ep.last_seen, snap.now) : "" })]),
      ep.error && !quiet ? h("div", { class: "ep-error", text: ep.error }) : null,
      // the id beside the name makes the sentence clickable; the name alone stays plain text
      ep.dropped_job ? h("div", { class: "ep-jobs" }, ["running when it dropped: ",
        ep.dropped_job_id ? jobLink(ep.dropped_job_id, ep.dropped_job) : ep.dropped_job]) : null,
    ]);
  }

  function dormantEndpoint(ep) {
    var seen = ep.last_seen != null ? "last seen " + fmt.date(ep.last_seen) : "never seen";
    // the state dot (3.7c lane contract) is the first child of this one-line element; it has no
    // ep-head of its own; lane B styles .sdot
    return h("div", { class: "endpoint state-dormant", "data-endpoint": ep.name },
      [h("span", { class: "sdot sdot-" + ep.state, "aria-hidden": "true" }),
       endpointLink(ep.name), " ", h("span", { text: ep.backend + ", " + seen })]);
  }

  // 3.7a (operator 2026-10-29: "I would not display cards that are dormant and never seen, as
  // they are just clutter"): a never-seen dormant endpoint is not drawn, except on a down node,
  // whose card names every endpoint on it (3.7c). render() counts the hidden ones under a line.
  function neverSeenOn(host, ep) {
    return ep.state === "dormant" && ep.last_seen == null && host.state !== "down";
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
    var note = nodeDown ? "node unreachable" : host.state === "unknown" ? "node unknown" : "";
    if (nodeDown) {
      // the card's one alert is the probe's sentence (every endpoint on a down node reports the
      // box unreachable): the first endpoint's that has one
      var said = host.endpoints.filter(function (ep) { return ep.error; })[0];
      if (said) note = said.error;
    }
    var body = host.endpoints.map(function (ep) {
      if (ep.state === "live") return liveEndpoint(snap, ep);
      if (ep.state === "down") return downEndpoint(snap, ep, nodeDown);
      return neverSeenOn(host, ep) ? null : dormantEndpoint(ep);
    }).filter(Boolean);
    if (!body.length && !nodeDown) return null; // 3.7a: a host with nothing drawn
    return h("div", { class: cls, "data-host": host.name }, [
      h("div", { class: "host-head" }, [h("span", { class: "host-name", text: host.name }),
        note ? h("span", { class: "host-note", text: note }) : null]),
    ].concat(body));
  }

  // --- the Recent jobs log (story 3.7g) ---------------------------------------------------------

  var JOB_COLUMNS = ["job", "status", "model", "endpoint", "elapsed", "turns", "tool calls", "tool errors",
    "context", "speed", "last activity"];
  var NUMERIC = { elapsed: 1, turns: 1, "tool calls": 1, "tool errors": 1, speed: 1 };
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

  function jobRow(job) {
    var ctx = null;
    if (job.ctx_pct != null) {
      var w = Math.max(0, Math.min(100, job.ctx_pct));
      ctx = h("span", { title: fmt.int(job.ctx_tokens) + " / " + fmt.int(job.context) + " tokens" }, [
        h("span", { class: "bar" + (w >= 80 ? " high" : "") }, [h("span", { style: "width:" + w + "%" })]),
        h("span", { class: "num", text: fmt.pct(job.ctx_pct) }),
      ]);
    }
    // 3.7g: no queued badge here — the status pill says it. Compaction and server-gone badges
    // stay: they carry something the pill cannot.
    var nameCell = [jobLink(job.id, job.name)];
    if (job.compactions > 0) {
      nameCell.push(h("span", { class: "badge", text: fmt.int(job.compactions) + " compaction" + (job.compactions === 1 ? "" : "s") }));
    }
    if (job.server_live === false) {
      nameCell.push(h("span", { class: "badge", title: "the job's MCP server no longer beats: nothing is running it", text: "server gone" }));
    }
    return h("tr", { "data-job": job.id }, [
      h("td", null, nameCell),
      h("td", null, [statusPill(job.status)]),
      h("td", { class: "cut", title: job.model || "", text: job.model || "" }),
      h("td", null, [job.endpoint ? endpointLink(job.endpoint) : null]),
      h("td", { class: "n", text: fmt.dur(job.elapsed_s) }),
      // 3.7h: a live job's turn count is 0 until the first assistant turn lands, and a plain 0
      // reads like a counter that never ran — a muted dash until the number is a figure
      h("td", { class: "n" }, [job.status === "running" && !job.num_turns
        ? h("span", { class: "muted", text: "–" })
        : h("span", { text: fmt.int(job.num_turns) })]),
      h("td", { class: "n", text: fmt.int(job.tool_calls) }),
      // 3.7i: the badge is a link — the count is tool results that came back as errors, and
      // the job page is where they are listed by tool
      h("td", { class: "n" }, [job.errors > 0
        ? h("a", { class: "badge err", href: "#/job/" + encodeURIComponent(job.id),
          title: COLUMN_TITLES["tool errors"], text: fmt.int(job.errors) })
        : h("span", { text: fmt.int(job.errors) })]),
      h("td", null, [ctx]),
      h("td", { class: "n", text: job.gen_tps != null ? fmt.speed(job.gen_tps) + " tok/s" : "" }),
      // 3.7g: no "current tool" column — on a running row it was always the same line as the
      // last activity, and on a finished row it is history; the activity cell below holds the
      // full text in its title
      h("td", { class: "cut", title: job.last_activity || "", text: oneLine(job.last_activity) }),
    ]);
  }

  function oneLine(text) {
    if (!text) return "";
    return String(text).split("\n")[0];
  }

  function jobsTable(snap) {
    // 3.7g: the snapshot's order is the log's contract (live jobs by start, then the newest
    // finished first) — no client-side re-sort, which used to scramble the finished tail
    var jobs = snap.jobs || [];
    if (!jobs.length) {
      return h("div", { class: "table-wrap" }, [h("div", { class: "empty", text: "no jobs yet" })]);
    }
    var head = h("tr", null, JOB_COLUMNS.map(function (c) {
      return h("th", { class: NUMERIC[c] ? "n" : null, title: COLUMN_TITLES[c] || null, text: c });
    }));
    // the note spells out the row order (3.7g: the table now holds finished rows too, and the
    // order carries meaning). A "show all" link like Strata's log has waits for an all-jobs
    // page — the server keeps the full history, but no route lists it yet, so the note is plain
    return h("div", null, [
      h("div", { class: "table-wrap" }, [h("table", null, [h("thead", null, [head]),
        h("tbody", null, jobs.map(jobRow))])]),
      h("div", { class: "jobs-note muted", text: "running and queued first, then the last 10 finished" }),
    ]);
  }

  // --- render: the snapshot into the DOM, in place -------------------------------------------

  function render(snap) {
    state.snap = snap;
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
    byId("updated").textContent = "snapshot " + fmt.time(snap.now);
    var cards = [];
    var hidden = [];
    snap.hosts.forEach(function (host) {
      host.endpoints.forEach(function (ep) {
        if (neverSeenOn(host, ep)) hidden.push(ep.name); // config order: hosts then endpoints
      });
      var card = hostCard(snap, host);
      if (card) cards.push(card);
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
    byId("hosts").replaceChildren.apply(byId("hosts"), cards);
    byId("jobs-body").replaceChildren(jobsTable(snap));
  }

  // --- history: the fleet route and each live endpoint's history, at most once per 5 s -------

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

  function fetchFleet(range) {
    return getJSON("/api/fleet/history?range=" + encodeURIComponent(range));
  }

  // story 3.7p: one call for the fleet grid and every live endpoint's own history (the server
  // classifies live exactly as the snapshot does), replacing the fleet call plus one per
  // endpoint. fetchFleet/fetchHistory stay for the endpoint page.
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

  // ask for fresh history; a call inside 5 s of the last fetch is deferred to the 5 s mark
  // (one timer, however many snapshots arrive meanwhile)
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
      state.fleet = body.fleet;
      state.fleetRange = range;
      state.hists = body.endpoints || {};
      state.histRange = range;
      charts.update(state.fleet);
      if (state.snap) render(state.snap);
    }).catch(function (e) {
      showBanner("fleet history: " + e.message);
    }).then(function () {
      refresh.inflight = false;
      if (range !== state.range) refresh.last = 0;
      if (refresh.again || range !== state.range) { refresh.again = false; scheduleHistory(false); }
    });
  }

  // --- the fleet charts (uPlot) ---------------------------------------------------------------

  var CHARTS = [
    { key: "gen_tps", id: "chart-gen", title: "decode speed (tok/s)" },
    { key: "prompt_tps", id: "chart-prompt", title: "prompt speed (tok/s)" },
    { key: "occupancy", id: "chart-occ", title: "occupancy (slots busy or requests running)" },
  ];

  var charts = {
    plots: {}, // key -> uPlot instance
    names: "", // the series (endpoint names) the plots were built for

    width: function () {
      var box = byId("fleet-charts");
      var cols = getComputedStyle(box).gridTemplateColumns.split(" ").length || 1;
      var w = Math.floor((box.clientWidth - 12 * (cols - 1)) / cols) - 18;
      return Math.max(w, 200);
    },

    options: function (title, names) {
      var axis = { stroke: "#898781", grid: { stroke: "#2c2c2a", width: 1 }, ticks: { stroke: "#2c2c2a", width: 1 } };
      return {
        width: charts.width(),
        height: 180,
        scales: { x: { time: true } },
        axes: [
          axis,
          Object.assign({}, axis, { size: 48, values: function (u, splits) { return splits.map(fmt.axis); } }),
        ],
        series: [{ label: "time", value: function (u, t) { return fmt.date(t); } }].concat(names.map(function (n) {
          return {
            label: n,
            stroke: colourOf(n),
            width: 2,
            dash: dashOf(n) || undefined,
            spanGaps: false,
            points: { show: false },
            value: function (u, v) { return fmt.axis(v); },
          };
        })),
        legend: { live: true },
      };
    },

    update: function (fleet) {
      var box = byId("fleet-charts");
      var note = byId("fleet-note");
      // spec §5: the charts draw and legend the LIVE endpoints only, in config order (the
      // snapshot's); one that drops out of live leaves the legend on the next fleet fetch
      var names = liveNames(state.snap);
      if (!fleet.t || !fleet.t.length || !names.length) {
        charts.destroy();
        note.textContent = !names.length ? "no live endpoint to chart" : "no probes in the last " + state.range;
        note.hidden = false;
        return;
      }
      note.hidden = true;
      var key = names.join("\u0000");
      if (key !== charts.names) {
        charts.destroy();
        charts.names = key;
        CHARTS.forEach(function (c) {
          var wrap = h("div", { class: "chart", id: c.id }, [h("h3", { text: c.title })]);
          box.appendChild(wrap);
          charts.plots[c.key] = new uPlot(charts.options(c.title, names), charts.data(fleet, c.key, names), wrap);
        });
        return;
      }
      CHARTS.forEach(function (c) { charts.plots[c.key].setData(charts.data(fleet, c.key, names)); });
    },

    data: function (fleet, key, names) {
      return [fleet.t].concat(names.map(function (n) {
        var fleetEp = fleet.endpoints[n];
        return (fleetEp && fleetEp[key]) || fleet.t.map(function () { return null; });
      }));
    },

    destroy: function () {
      Object.keys(charts.plots).forEach(function (k) { charts.plots[k].destroy(); });
      charts.plots = {};
      charts.names = "";
      byId("fleet-charts").replaceChildren();
    },

    resize: function () {
      var w = charts.width();
      Object.keys(charts.plots).forEach(function (k) { charts.plots[k].setSize({ width: w, height: 180 }); });
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

  function setConnected(up) {
    var c = byId("conn");
    c.className = "conn " + (up ? "conn-up" : "conn-down");
    byId("conn-label").textContent = up ? "live" : "reconnecting";
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
      charts.resize();
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
    var resizeTimer = null;
    window.addEventListener("resize", function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(charts.resize, 150);
    });
    connect();
  }

  var pages = { job: null, endpoint: null, teardown: null };

  // --- 3.7k: the one requests-table renderer --------------------------------------------------
  //
  // Both detail pages draw their request log through this: the endpoint page's requests across
  // jobs (endpoint.js) and a job page's own requests (job.js). The columns are the operator's
  // log look with the two rates Strata derives from the same facts: time, a status pill, job
  // (omitted on a job page, where every row is the one job), path, prompt (tokens.input),
  // reused (tokens.cache_read), output, prefill t/s, decode t/s, duration, error. The rows are
  // the endpoint route's (newest first, with job_id/job_name) or the job detail's (time order,
  // no job fields); neither is re-sorted here. `opts.job === false` drops the job column;
  // `opts.empty` is the sentence an empty list draws (each page keeps its own).
  //
  // The rates are blank, never a placeholder number, when their inputs are missing:
  //   prefill t/s = (input + cache_creation) / ((ms − gen_ms) / 1000), when ms > gen_ms and
  //                 input + cache_creation > 0 (a non-stream request may have no gen_ms);
  //   decode t/s  = output / (gen_ms / 1000), when gen_ms > 0 and output > 0.
  // The brief wrote Strata's prefill as "input − cache_read", but that is Strata's model, where
  // its input is the WHOLE prompt: our `tokens.input` excludes cache reads (derive._PROMPT_SUM
  // is input + cache_read + cache_creation), so the subtraction went negative on 148 of the
  // seed's 152 requests and the column read blank. Strata's processed count is
  // `prompt_tokens − cached` (registry.py:589), i.e. the tokens this request actually
  // prefilled: input + cache_creation here. The brief's literal subtraction was corrected on
  // the data model, not as a display choice (2026-10-30).
  var REQ_NUMERIC = { prompt: 1, reused: 1, output: 1, "prefill t/s": 1, "decode t/s": 1, duration: 1 };
  var ERROR_CUT = 60; // the error cell's chars; the full sentence rides in its title

  // the reads are spelled `req.tokens.<field>` so the §5a walk sees every field name (an
  // alias would hide them from the gate)
  function prefillRate(req) {
    if (!req.tokens) return null;
    if (typeof req.tokens.input !== "number" || typeof req.tokens.cache_creation !== "number") return null;
    if (typeof req.ms !== "number" || typeof req.gen_ms !== "number") return null;
    var prefill = req.tokens.input + req.tokens.cache_creation; // the tokens actually processed
    if (req.ms <= req.gen_ms || prefill <= 0) return null;
    return prefill / ((req.ms - req.gen_ms) / 1000);
  }

  function decodeRate(req) {
    if (!req.tokens) return null;
    if (typeof req.tokens.output !== "number" || typeof req.gen_ms !== "number") return null;
    if (req.gen_ms <= 0 || req.tokens.output <= 0) return null;
    return req.tokens.output / (req.gen_ms / 1000);
  }

  // the error cell's sentence: the error, else "context exceeded" when the request overran
  // the window; "" when clean (the empty string a clean export writes is clean too)
  function requestError(req) {
    if (req.error != null && req.error !== "") return String(req.error);
    if (req.context_exceeded === true) return "context exceeded";
    return "";
  }

  function requestsTable(rows, opts) {
    opts = opts || {};
    rows = rows || [];
    if (!rows.length) return h("div", { class: "empty", text: opts.empty || "no requests" });
    var withJob = opts.job !== false;
    var columns = ["time", "status"];
    if (withJob) columns.push("job");
    columns = columns.concat(["path", "prompt", "reused", "output", "prefill t/s", "decode t/s", "duration", "error"]);
    var head = h("tr", null, columns.map(function (c) {
      return h("th", { class: REQ_NUMERIC[c] ? "n" : null, text: c });
    }));
    var body = rows.map(function (req) {
      var tokens = req.tokens || {};
      // the cache cells read through req.tokens directly (the §5a walk sees a field only on a
      // chain rooted at `req`; the display alias above would hide these two)
      var cacheRead = req.tokens ? req.tokens.cache_read : null;
      var cacheCreation = req.tokens ? req.tokens.cache_creation : null;
      var error = requestError(req);
      var bad = typeof req.http_status === "number" && (req.http_status < 200 || req.http_status >= 300);
      if (req.error != null && req.error !== "") bad = true;
      var cls = [];
      if (bad) cls.push("req-err");
      if (req.context_exceeded === true) cls.push("req-ctx");
      var prefill = prefillRate(req);
      var decode = decodeRate(req);
      var cells = [h("td", { text: fmt.time(req.t) })];
      if (req.http_status == null) {
        cells.push(h("td", null, [h("span", { class: "pill", title: "no status", text: "–" })]));
      } else {
        var ok = req.http_status >= 200 && req.http_status < 300;
        cells.push(h("td", null, [h("span", { class: "pill " + (ok ? "pill-ok" : "pill-error"),
          title: String(req.http_status), text: ok ? "ok" : "error" })]));
      }
      if (withJob) {
        cells.push(h("td", null, [req.job_id ? h("a", { href: "#/job/" + encodeURIComponent(req.job_id),
          text: req.job_name || req.job_id }) : null]));
      }
      cells.push(
        h("td", { class: "cut", title: req.path || "", text: req.path || "" }),
        h("td", { class: "n", text: fmt.int(tokens.input) }),
        h("td", { class: "n", title: "cache read " + fmt.int(cacheRead) + ", cache creation "
          + fmt.int(cacheCreation), text: fmt.int(cacheRead) }),
        h("td", { class: "n", text: fmt.int(tokens.output) }),
        h("td", { class: "n", text: prefill == null ? "" : fmt.speed(prefill) }),
        h("td", { class: "n", text: decode == null ? "" : fmt.speed(decode) }),
        h("td", { class: "n", text: typeof req.ms === "number" ? fmt.speed(req.ms / 1000) + " s" : "" }),
        h("td", { class: "cut", title: error || null,
          text: error.length > ERROR_CUT ? error.slice(0, ERROR_CUT) : error })
      );
      return h("tr", { class: cls.length ? cls.join(" ") : null }, cells);
    });
    return h("div", { class: "table-wrap" }, [h("table", { class: "requests-table" },
      [h("thead", null, [head]), h("tbody", null, body)])]);
  }

  window.localagentsMonitor = {
    pages: pages,
    h: h,
    fmt: fmt,
    render: render,
    connect: connect,
    fetchHistory: fetchHistory,
    fetchFleet: fetchFleet,
    charts: charts,
    range: range,
    state: state,
    requestsTable: requestsTable, // 3.7k: the one renderer both request logs draw through
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
