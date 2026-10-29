/* localagents monitor: the overview page (story 3.2; monitor spec §5 "Overview").
 *
 * The page lives on ONE server-sent events stream (/events): each `snapshot` event redraws the
 * host cards and the running-jobs table in place; the page never polls. The fleet charts, the
 * decode sparklines and the state strips need history: on a snapshot the page re-fetches
 * /api/fleet/history (one call for the whole fleet) and each live endpoint's
 * /api/endpoints/<name>/history (for its `state` series) at most once every 5 s.
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
    // durations: 42s, 4m12s, 2h03m, 3d04h
    dur: function (s) {
      if (s == null || !isFinite(s)) return "";
      s = Math.max(0, Math.floor(s));
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

  // a labelled figure, or nothing when the value is absent (never a placeholder number)
  function fig(label, text, extra) {
    if (text == null || text === "") return null;
    return h("span", { class: "fig" }, [h("span", { class: "label", text: label }),
      h("span", { class: "num", text: text }), extra || null]);
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

  // the state strip: the endpoint history's `state` series over [now - range, now] as coloured
  // segments (a null, a down probe, is a gap); nothing when the history has no state series
  function stateStrip(name, now) {
    var hist = state.hists[name];
    if (!hist || !hist.state || !hist.t || !hist.t.length) return null;
    var span = rangeSeconds(state.histRange || state.range);
    var t0 = now - span;
    var strip = h("div", { class: "strip", role: "img", "aria-label": "state over the last " + state.range });
    // the newest kept probe's state; null (nothing printed) when that probe was down
    var current = hist.state[hist.state.length - 1];
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
    return h("div", { class: "strip-row" }, [strip, h("span", { class: "strip-now", text: current || "" })]);
  }

  // a decode-speed sparkline from the fleet history's gen_tps for this endpoint (inline SVG)
  function sparkline(name, now) {
    var fleet = state.fleet;
    if (!fleet || !fleet.endpoints || !fleet.t) return null;
    var fleetEp = fleet.endpoints[name];
    if (!fleetEp || !fleetEp.gen_tps) return null;
    var ys = fleetEp.gen_tps;
    var max = 0;
    ys.forEach(function (v) { if (v != null && v > max) max = v; });
    if (!ys.some(function (v) { return v != null; })) return null;
    var W = 90, H = 22;
    var span = rangeSeconds(state.fleetRange || state.range);
    var t0 = now - span;
    var d = "", pen = false;
    for (var i = 0; i < fleet.t.length; i++) {
      var v = ys[i];
      if (v == null || fleet.t[i] < t0) { pen = false; continue; }
      var x = ((fleet.t[i] - t0) / span) * W;
      var y = H - 1 - (max > 0 ? (v / max) * (H - 2) : 0);
      d += (pen ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
      pen = true;
    }
    if (!d) return null;
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", W);
    svg.setAttribute("height", H);
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "decode speed over the last " + state.range + ", peak " + fmt.speed(max) + " tok/s");
    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", colourOf(name));
    path.setAttribute("stroke-width", "1.5");
    svg.appendChild(path);
    return svg;
  }

  function hardware(hw) {
    if (!hw) return null;
    var vram = hw.gpu_mem_used != null
      ? fmt.gib(hw.gpu_mem_used) + (hw.gpu_mem_total != null ? " / " + fmt.gib(hw.gpu_mem_total) : "")
      : "";
    var power = hw.gpu_power != null
      ? fmt.int(hw.gpu_power) + " W" + (hw.gpu_power_limit != null ? " / " + fmt.int(hw.gpu_power_limit) + " W" : "")
      : "";
    var parts = [
      fig("GPU", fmt.pct(hw.gpu_util)),
      fig("VRAM", vram),
      fig("temp", hw.gpu_temp != null ? fmt.int(hw.gpu_temp) + " °C" : ""),
      fig("power", power),
    ].filter(Boolean);
    return parts.length ? h("div", { class: "hw" }, parts) : null;
  }

  function jobsOn(snap, name) {
    return (snap.jobs || []).filter(function (job) { return job.endpoint === name; });
  }

  function liveEndpoint(snap, ep) {
    var rates = ep.rates || {};
    var occupancy = null;
    if (ep.slots) occupancy = fig("slots", fmt.int(ep.slots.busy) + "/" + fmt.int(ep.slots.total));
    else if (ep.requests) {
      occupancy = fig("requests", fmt.int(ep.requests.running) + " running / " + fmt.int(ep.requests.waiting) + " waiting");
    }
    var cacheHit = null;
    if (rates.cached_tps != null && rates.prompt_tps != null && rates.prompt_tps + rates.cached_tps > 0) {
      cacheHit = rates.cached_tps / (rates.prompt_tps + rates.cached_tps);
    }
    var figures = [
      occupancy,
      fig("KV", fmt.frac(ep.kv_cache_usage)),
      fig("decode", rates.gen_tps != null ? fmt.speed(rates.gen_tps) + " tok/s" : "", sparkline(ep.name, snap.now)),
      fig("stream", ep.gen_tps_live != null ? fmt.speed(ep.gen_tps_live) + " tok/s" : ""),
      fig("prompt", rates.prompt_tps != null ? fmt.speed(rates.prompt_tps) + " tok/s" : ""),
      fig("cache hit", fmt.frac(cacheHit)),
      fig("ctx", fmt.int(ep.context)),
      fig("probe", ep.latency_ms != null ? fmt.int(ep.latency_ms) + " ms" : ""),
    ].filter(Boolean);
    var mine = jobsOn(snap, ep.name);
    var jobsLine;
    var busy = (ep.slots && ep.slots.busy > 0) || (ep.requests && ep.requests.running > 0);
    if (ep.jobs_running > 0 || mine.length) {
      var names = [];
      mine.forEach(function (job, i) {
        if (i) names.push(", ");
        names.push(jobLink(job.id, job.name));
      });
      jobsLine = h("div", { class: "ep-jobs" }, [h("span", { class: "label muted", text: "jobs " }),
        h("span", { class: "num", text: fmt.int(ep.jobs_running) + " " })].concat(names));
    } else if (busy) {
      jobsLine = h("div", { class: "ep-jobs muted", text: "busy, no local job" });
    }
    return h("div", { class: "endpoint state-live", "data-endpoint": ep.name }, [
      h("div", { class: "ep-head" }, [endpointLink(ep.name), h("span", { class: "tag", text: ep.backend }),
        ep.model ? h("span", { class: "ep-model", text: ep.model }) : null]),
      stateStrip(ep.name, snap.now),
      figures.length ? h("div", { class: "figures" }, figures) : null,
      rates.reason ? h("div", { class: "ep-note muted", text: "speeds: " + rates.reason }) : null,
      hardware(ep.hardware),
      jobsLine,
    ]);
  }

  function downEndpoint(snap, ep) {
    return h("div", { class: "endpoint state-down", "data-endpoint": ep.name }, [
      h("div", { class: "ep-head" }, [endpointLink(ep.name), h("span", { class: "tag", text: ep.backend }),
        h("span", { class: "ep-seen", text: ep.last_seen != null ? "last seen " + fmt.ago(ep.last_seen, snap.now) : "" })]),
      ep.error ? h("div", { class: "ep-error", text: ep.error }) : null,
      ep.dropped_job ? h("div", { class: "ep-jobs", text: "running when it dropped: " + ep.dropped_job }) : null,
    ]);
  }

  function dormantEndpoint(ep, inline) {
    var seen = ep.last_seen != null ? "last seen " + fmt.date(ep.last_seen) : "never seen";
    return h(inline ? "span" : "div", { class: "endpoint state-dormant", "data-endpoint": ep.name },
      [endpointLink(ep.name), " ", h("span", { text: ep.backend + ", " + seen })]);
  }

  function hostCard(snap, host) {
    var cls = "host";
    if (host.state === "down") cls += " node-down";
    else if (host.state === "unknown") cls += " node-unknown";
    if (host.dormant) {
      // every endpoint dormant: the host collapses to one line
      var line = [h("span", { class: "host-name", text: host.name }), h("span", { text: " dormant: " })];
      host.endpoints.forEach(function (ep, i) {
        if (i) line.push(h("span", { text: "; " }));
        line.push(dormantEndpoint(ep, true));
      });
      return h("div", { class: cls + " dormant", "data-host": host.name }, [h("div", { class: "host-head" }, line)]);
    }
    var note = host.state === "down" ? "node unreachable" : host.state === "unknown" ? "node unknown" : "";
    var body = host.endpoints.map(function (ep) {
      if (ep.state === "live") return liveEndpoint(snap, ep);
      if (ep.state === "down") return downEndpoint(snap, ep);
      return dormantEndpoint(ep, false);
    });
    return h("div", { class: cls, "data-host": host.name }, [
      h("div", { class: "host-head" }, [h("span", { class: "host-name", text: host.name }),
        note ? h("span", { class: "host-note", text: note }) : null]),
    ].concat(body));
  }

  // --- the running-jobs table -----------------------------------------------------------------

  var JOB_COLUMNS = ["job", "model", "endpoint", "elapsed", "turns", "tool calls", "current tool", "errors",
    "context", "speed", "last activity"];
  var NUMERIC = { elapsed: 1, turns: 1, "tool calls": 1, errors: 1, speed: 1 };

  function jobRow(job) {
    var ctx = null;
    if (job.ctx_pct != null) {
      var w = Math.max(0, Math.min(100, job.ctx_pct));
      ctx = h("span", { title: fmt.int(job.ctx_tokens) + " / " + fmt.int(job.context) + " tokens" }, [
        h("span", { class: "bar" + (w >= 80 ? " high" : "") }, [h("span", { style: "width:" + w + "%" })]),
        h("span", { class: "num", text: fmt.pct(job.ctx_pct) }),
      ]);
    }
    var nameCell = [jobLink(job.id, job.name)];
    if (job.status === "queued") nameCell.push(h("span", { class: "badge", text: "queued" }));
    if (job.compactions > 0) {
      nameCell.push(h("span", { class: "badge", text: fmt.int(job.compactions) + " compaction" + (job.compactions === 1 ? "" : "s") }));
    }
    if (job.server_live === false) {
      nameCell.push(h("span", { class: "badge", title: "the job's MCP server no longer beats: nothing is running it", text: "server gone" }));
    }
    return h("tr", { "data-job": job.id }, [
      h("td", null, nameCell),
      h("td", { class: "cut", title: job.model || "", text: job.model || "" }),
      h("td", null, [job.endpoint ? endpointLink(job.endpoint) : null]),
      h("td", { class: "n", text: fmt.dur(job.elapsed_s) }),
      h("td", { class: "n", text: fmt.int(job.num_turns) }),
      h("td", { class: "n", text: fmt.int(job.tool_calls) }),
      h("td", { class: "cut", title: job.last_tool || "", text: job.last_tool || "" }),
      h("td", { class: "n" }, [job.errors > 0 ? h("span", { class: "badge err", text: fmt.int(job.errors) })
        : h("span", { text: fmt.int(job.errors) })]),
      h("td", null, [ctx]),
      h("td", { class: "n", text: job.gen_tps != null ? fmt.speed(job.gen_tps) + " tok/s" : "" }),
      h("td", { class: "cut", title: job.last_activity || "", text: oneLine(job.last_activity) }),
    ]);
  }

  function oneLine(text) {
    if (!text) return "";
    return String(text).split("\n")[0];
  }

  function jobsTable(snap) {
    var jobs = (snap.jobs || []).slice().sort(function (a, b) {
      return (a.started_at || 0) - (b.started_at || 0);
    });
    if (!jobs.length) return h("div", { class: "table-wrap" }, [h("div", { class: "empty", text: "no jobs running" })]);
    var head = h("tr", null, JOB_COLUMNS.map(function (c) { return h("th", { class: NUMERIC[c] ? "n" : null, text: c }); }));
    return h("div", { class: "table-wrap" }, [h("table", null, [h("thead", null, [head]),
      h("tbody", null, jobs.map(jobRow))])]);
  }

  // --- render: the snapshot into the DOM, in place -------------------------------------------

  function render(snap) {
    state.snap = snap;
    state.order = {};
    var i = 0;
    snap.hosts.forEach(function (host) {
      host.endpoints.forEach(function (ep) { state.order[ep.name] = i++; });
    });
    byId("meta").textContent = "attention window " + fmt.dur(snap.window_s);
    byId("updated").textContent = "snapshot " + fmt.time(snap.now);
    byId("hosts").replaceChildren.apply(byId("hosts"), snap.hosts.map(function (host) { return hostCard(snap, host); }));
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

  var refresh = { last: 0, timer: null, inflight: false, again: false };

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
    var names = liveNames(state.snap);
    var calls = [fetchFleet(range)].concat(names.map(function (n) {
      return fetchHistory(n, range).catch(function () { return null; });
    }));
    Promise.all(calls).then(function (bodies) {
      if (range !== state.range) return; // the range changed meanwhile: this answer is stale
      state.fleet = bodies[0];
      state.fleetRange = range;
      var hists = {};
      names.forEach(function (n, i) { if (bodies[i + 1]) hists[n] = bodies[i + 1]; });
      state.hists = hists;
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
      var names = Object.keys(fleet.endpoints || {});
      if (!fleet.t || !fleet.t.length) {
        charts.destroy();
        note.textContent = "no probes in the last " + state.range;
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
    // the open detail page's teardown runs on EVERY route change, the overview's included
    if (pages.teardown) { try { pages.teardown(); } catch (e) { console.error(e); } pages.teardown = null; }
    if (!m) {
      detail.hidden = true;
      overview.hidden = false;
      charts.resize();
      return;
    }
    var what = m[1] === "job" ? "Job" : "Endpoint";
    var name = decodeURIComponent(m[2]);
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
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
