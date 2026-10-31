/* localagents monitor: the endpoint detail page (story 3.3; monitor spec §5 "Endpoint detail").
 *
 * Registered as window.localagentsMonitor.pages.endpoint (app.js calls it on #/endpoint/<name>
 * with the #detail element and its page state, and calls the returned teardown before the next
 * route). The page draws, for one endpoint:
 *
 *   - a header: name, backend, host, base_url, state, last seen, the down sentence and the job
 *     that was running when it dropped, a link back to the overview;
 *   - stat tiles, each a current figure and a sparkline over the range (spec §5's tier table;
 *     3.10d: the tiers stay as the tiles' ordering and the stylesheet's border colours, but
 *     the page draws no tier-N heading — the groups after the first carry a plain noun).
 *     A tile with no source is ABSENT: a tile drawn from a history series exists only
 *     when the history response carries that series (§5a: absent keys mean no source);
 *   - the state strip over the range (the history's `state` series);
 *   - the request table with its totals line (/api/endpoints/<name>/requests);
 *   - the jobs that ran on it in the range: the history body's §5a job rows, drawn through
 *     app.js's one jobs-table renderer with the endpoint column dropped (3.10b) — and its
 *     restart and outage events, a section only when the range holds one (3.10b).
 *
 * Redraw. The page registers with app.js's snapshot hook (localagentsMonitor.onSnapshot: the
 * listener runs at the end of every render(), the skip path included — 3.9D D4 replaced the
 * MutationObserver on #updated, whose text moved to #conn-label in 3.7r and went dead), and it
 * watches #range (range.draw() rebuilds it on every range change) through a MutationObserver:
 * no timer, no polling. On a snapshot the header and the tiles' current figures are redrawn
 * from state.snap; the history and the request table are fetched on open, on a range change,
 * and on a snapshot only when the last fetch is 30 s old or more (never per snapshot).
 *
 * The JSON is §5a's (docs/superpowers/specs/2026-10-29-monitor-design.md); the route bodies are
 * read through fixed names, as app.js does: `snap` (the snapshot), `host`, `ep` (the snapshot's
 * endpoint), `hw` (ep.hardware), `rates` (ep.rates), `hist` (the endpoint history; its `jobs`
 * arrive as §5a rows that app.js's shared renderer reads), `ev` (one of hist.events),
 * `reqs` (the requests body), `req` (one of its rows) and
 * `totals` (reqs.totals). tests/test_monitor_page.py checks every field read through them is a
 * §5a name. Text goes in through h() (textContent), never as HTML.
 */
(function () {
  "use strict";

  var M = window.localagentsMonitor;
  if (!M || !M.pages) return; // app.js not loaded: nothing to register with
  var h = M.h;
  var fmt = M.fmt;

  var REFETCH_MIN_MS = 30000; // history and requests: at most once per 30 s on snapshots
  var SVG_NS = "http://www.w3.org/2000/svg";

  // the state strip's colours: app.js's STATE_FIXED / STATE_OTHER (not exported), copied so the
  // strip here and the overview's colour a state the same way
  var STATE_FIXED = { idle: "#4a4945", busy: "#3987e5" };
  var STATE_OTHER = ["#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"];

  function stateColour(s) {
    if (STATE_FIXED[s]) return STATE_FIXED[s];
    var n = 0;
    for (var i = 0; i < s.length; i++) n = (n * 31 + s.charCodeAt(i)) >>> 0;
    return STATE_OTHER[n % STATE_OTHER.length];
  }

  function rangeSeconds(r) {
    var m = /^([0-9]+)([mhd])$/.exec(r || "");
    if (!m) return 3600;
    return Number(m[1]) * { m: 60, h: 3600, d: 86400 }[m[2]];
  }

  function withUnit(text, unit) { return text === "" ? "" : text + " " + unit; }

  // --- the tier table (spec §5) ---------------------------------------------------------------
  //
  // One definition per tile. `series`: the history key the tile's sparkline is drawn from, and
  // the tile's presence: the tile exists only when hist carries that key. `now(ep, hw, rates)`:
  // the current figure from the snapshot (null when the snapshot has none, then the series'
  // newest value stands in, marked as such). `show(v, ep, hw)`: the figure as text. Tiles with
  // no series (model, context) exist when their snapshot value does (3.10d dropped the
  // state and node tiles: the header already says both, port-22 fallback note included).

  function cacheHitNow(rates) {
    if (!rates || rates.cached_tps == null || rates.prompt_tps == null) return null;
    var total = rates.prompt_tps + rates.cached_tps;
    return total > 0 ? rates.cached_tps / total : null;
  }

  function gibOf(v, total) {
    var t = fmt.gib(v);
    return t === "" ? "" : t + (total != null ? " / " + fmt.gib(total) : "");
  }

  // Strata's `_mb` hardware fields are megabytes per sample over a window Strata does not
  // document (the 3.3 review's ruling): the tiles read `MB`, never a rate, until it does
  var PER_SAMPLE = "per Strata sample; the sample window is not documented";

  var TILES = [
    // tier 0: every endpoint
    { tier: 0, key: "latency_ms", label: "probe latency", series: "latency_ms",
      now: function (ep) { return ep.latency_ms; },
      show: function (v) { return withUnit(fmt.int(v), "ms"); } },
    // tier 1: llama.cpp --metrics, vLLM, Strata (counter deltas)
    { tier: 1, key: "prompt_tps", label: "prompt speed", series: "prompt_tps",
      now: function (ep, hw, rates) { return rates ? rates.prompt_tps : null; },
      show: function (v) { return withUnit(fmt.speed(v), "tok/s"); } },
    { tier: 1, key: "gen_tps", label: "decode speed", series: "gen_tps",
      now: function (ep, hw, rates) { return rates ? rates.gen_tps : null; },
      show: function (v) { return withUnit(fmt.speed(v), "tok/s"); } },
    { tier: 1, key: "cache_hit", label: "cache hit", series: "cache_hit",
      now: function (ep, hw, rates) { return cacheHitNow(rates); },
      show: function (v) { return fmt.frac(v); } },
    // tier 2: llama.cpp --slots, vLLM, Strata
    { tier: 2, key: "slots_busy", label: "slots busy", series: "slots_busy",
      now: function (ep) { return ep.slots ? ep.slots.busy : null; },
      show: function (v, ep) {
        var t = fmt.int(v);
        return t !== "" && ep && ep.slots && ep.slots.total != null ? t + " / " + fmt.int(ep.slots.total) : t;
      } },
    { tier: 2, key: "requests_running", label: "requests running", series: "requests_running",
      now: function (ep) { return ep.requests ? ep.requests.running : null; },
      show: function (v) { return fmt.int(v); } },
    { tier: 2, key: "requests_waiting", label: "requests waiting", series: "requests_waiting",
      now: function (ep) { return ep.requests ? ep.requests.waiting : null; },
      show: function (v) { return fmt.int(v); } },
    { tier: 2, key: "kv_cache_usage", label: "KV fill", series: "kv_cache_usage",
      now: function (ep) { return ep.kv_cache_usage; },
      show: function (v) { return fmt.frac(v); } },
    // tier 3: Strata today (the probe's hardware row)
    { tier: 3, key: "gpu_util", label: "GPU", series: "gpu_util",
      now: function (ep, hw) { return hw ? hw.gpu_util : null; },
      show: function (v) { return fmt.pct(v); } },
    { tier: 3, key: "gpu_mem_used", label: "VRAM", series: "gpu_mem_used",
      now: function (ep, hw) { return hw ? hw.gpu_mem_used : null; },
      show: function (v, ep, hw) { return gibOf(v, hw ? hw.gpu_mem_total : null); } },
    { tier: 3, key: "gpu_temp", label: "GPU temp", series: "gpu_temp",
      now: function (ep, hw) { return hw ? hw.gpu_temp : null; },
      show: function (v) { return withUnit(fmt.int(v), "°C"); } },
    { tier: 3, key: "gpu_power", label: "GPU power", series: "gpu_power",
      now: function (ep, hw) { return hw ? hw.gpu_power : null; },
      show: function (v, ep, hw) {
        var t = withUnit(fmt.int(v), "W");
        return t !== "" && hw && hw.gpu_power_limit != null ? t + " / " + fmt.int(hw.gpu_power_limit) + " W" : t;
      } },
    { tier: 3, key: "gpu_pcie_rx_mb", label: "PCIe rx", series: "gpu_pcie_rx_mb", hint: PER_SAMPLE,
      now: function (ep, hw) { return hw ? hw.gpu_pcie_rx_mb : null; },
      show: function (v) { return withUnit(fmt.speed(v), "MB"); } },
    { tier: 3, key: "gpu_pcie_tx_mb", label: "PCIe tx", series: "gpu_pcie_tx_mb", hint: PER_SAMPLE,
      now: function (ep, hw) { return hw ? hw.gpu_pcie_tx_mb : null; },
      show: function (v) { return withUnit(fmt.speed(v), "MB"); } },
    { tier: 3, key: "cpu", label: "CPU", series: "cpu",
      now: function (ep, hw) { return hw ? hw.cpu : null; },
      show: function (v) { return fmt.pct(v); } },
    { tier: 3, key: "ram_used", label: "RAM", series: "ram_used",
      now: function (ep, hw) { return hw ? hw.ram_used : null; },
      show: function (v, ep, hw) { return gibOf(v, hw ? hw.ram_total : null); } },
    { tier: 3, key: "disk_read_mb", label: "disk read", series: "disk_read_mb", hint: PER_SAMPLE,
      now: function (ep, hw) { return hw ? hw.disk_read_mb : null; },
      show: function (v) { return withUnit(fmt.speed(v), "MB"); } },
    { tier: 3, key: "disk_write_mb", label: "disk write", series: "disk_write_mb", hint: PER_SAMPLE,
      now: function (ep, hw) { return hw ? hw.disk_write_mb : null; },
      show: function (v) { return withUnit(fmt.speed(v), "MB"); } },
  ];

  function newest(ts, ys) {
    for (var i = ys.length - 1; i >= 0; i--) if (ys[i] != null) return { t: ts[i], v: ys[i] };
    return null;
  }

  // tilesFor(ep, hist) -> [{tier, key, label, value, series, note, hint}]: the tiles whose source
  // is present, in tier order. `ep` is the snapshot's endpoint (or null before the first
  // snapshot), `hist` the endpoint history (or null before it arrives). `series` is {t, y} for
  // the sparkline, or null for a figure with no history series. `note` says where a figure came
  // from when it is not the current snapshot's; `hint` (the tile's title) qualifies its unit,
  // or is null. 3.10d: no endpoint-state and no node tile — the header says both.
  function tilesFor(ep, hist) {
    var out = [];
    var hw = ep ? ep.hardware : null;
    var rates = ep ? ep.rates : null;
    // the snapshot's own figures with no history series: present when the snapshot has them
    if (ep && ep.model) out.push({ tier: 0, key: "model", label: "served model", value: ep.model, series: null, note: null });
    if (ep && ep.context != null) {
      out.push({ tier: 0, key: "context", label: "context window", value: fmt.int(ep.context) + " tokens", series: null, note: null });
    }
    TILES.forEach(function (d) {
      if (!hist || !Array.isArray(hist[d.series]) || !Array.isArray(hist.t)) return; // no source: absent
      var ys = hist[d.series];
      var cur = ep ? d.now(ep, hw, rates) : null;
      var note = null;
      var value = cur != null ? d.show(cur, ep, hw) : "";
      if (value === "") {
        var last = newest(hist.t, ys);
        if (last) {
          value = d.show(last.v, ep, hw);
          note = "newest in range, " + fmt.time(last.t);
        }
      }
      out.push({ tier: d.tier, key: d.key, label: d.label, value: value, series: { t: hist.t, y: ys }, note: note,
        hint: d.hint || null });
    });
    out.sort(function (a, b) { return a.tier - b.tier; }); // stable: definition order within a tier
    return out;
  }

  // --- drawing --------------------------------------------------------------------------------

  // an inline-SVG sparkline of y over [t0, t0 + span]; a null is a gap; nothing when no value
  function sparkline(series, t0, span, label) {
    var ys = series.y, ts = series.t;
    var min = Infinity, max = -Infinity;
    for (var i = 0; i < ys.length; i++) {
      if (ys[i] == null || ts[i] < t0) continue;
      if (ys[i] < min) min = ys[i];
      if (ys[i] > max) max = ys[i];
    }
    if (min === Infinity) return null;
    if (min > 0) min = 0; // a zero baseline for non-negative figures
    var W = 160, H = 32;
    var d = "", pen = false;
    for (var j = 0; j < ys.length; j++) {
      var v = ys[j];
      if (v == null || ts[j] < t0) { pen = false; continue; }
      var x = ((ts[j] - t0) / span) * W;
      var y = H - 1 - (max > min ? ((v - min) / (max - min)) * (H - 2) : 0);
      d += (pen ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
      pen = true;
    }
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "spark");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label);
    var path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "var(--accent)");
    path.setAttribute("stroke-width", "1.5");
    path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(path);
    return svg;
  }

  // the tier-N classes stay on the tiles for the stylesheet's border colours (the class test
  // pins them); the HEADINGS are plain nouns since 3.10d — the tiers are the
  // spec's data-source grouping, not page prose. The first group carries no heading at all.
  var TIER_CLASS = ["tier-0", "tier-1", "tier-2", "tier-3"];
  var GROUP_HEAD = [null, "speeds", "occupancy", "hardware"];

  function tileEl(tile, t0, span, range) {
    return h("div", { class: "tile " + TIER_CLASS[tile.tier], "data-tile": tile.key, title: tile.hint || null }, [
      h("div", { class: "tile-label", text: tile.label }),
      h("div", { class: "tile-value num", title: tile.value, text: tile.value }),
      tile.series ? sparkline(tile.series, t0, span, tile.label + " over the last " + range) : null,
      tile.note ? h("div", { class: "tile-note muted", text: tile.note }) : null,
    ]);
  }

  // the state strip: hist.state over [t0, now] as coloured segments, each titled with its state
  function stateStrip(hist, t0, span, now) {
    if (!hist || !Array.isArray(hist.state) || !hist.t.length) return null;
    var strip = h("div", { class: "strip", role: "img", "aria-label": "state over the range" });
    var seen = {};
    for (var i = 0; i < hist.t.length; i++) {
      var s = hist.state[i];
      if (s == null) continue;
      var j = i;
      while (j + 1 < hist.t.length && hist.state[j + 1] === s) j++;
      var start = Math.max(hist.t[i], t0);
      var end = j + 1 < hist.t.length ? hist.t[j + 1] : now;
      if (end > t0) {
        seen[s] = true;
        strip.appendChild(h("span", {
          style: "left:" + (((start - t0) / span) * 100).toFixed(2) + "%;width:"
            + Math.max(((end - start) / span) * 100, 0.3).toFixed(2) + "%;background:" + stateColour(s),
          title: s + " " + fmt.time(hist.t[i]) + "–" + fmt.time(end),
        }));
      }
      i = j;
    }
    var legend = Object.keys(seen).map(function (s) {
      return h("span", { class: "key" }, [h("span", { class: "swatch", style: "background:" + stateColour(s) }), s]);
    });
    return h("div", { class: "ep-strip" }, [
      h("div", { class: "strip-row" }, [strip, h("span", { class: "strip-now", text: hist.state[hist.state.length - 1] || "" })]),
      legend.length ? h("div", { class: "strip-legend muted" }, legend) : null,
    ]);
  }

  // a time cell: time of day inside the last 24 h of `now`, else date and time
  function when(t, now) {
    if (t == null) return "";
    return now != null && now - t < 86400 ? fmt.time(t) : fmt.date(t);
  }

  // 3.10f-2: the job link is app.js's exported jobLink (short id, full id in the title) — the
  // local (id, name) builder is gone with the name keys

  function table(columns, rows, numeric) {
    var head = h("tr", null, columns.map(function (c) { return h("th", { class: numeric[c] ? "n" : null, text: c }); }));
    return h("table", null, [h("thead", null, [head]), h("tbody", null, rows)]);
  }

  // the log look (3.7k): the one shared renderer, with the job column (this page's log
  // spans jobs), this page's empty sentence, and the page's clock for the day-stamped
  // time column (3.7-fold-4: the snapshot's now, via nowOf()). 3.10b: the heading is a noun
  // phrase with the filter — no parentheses of explanation; the totals line is unchanged.
  function requestsSection(reqs, range, name, now) {
    var body;
    if (!reqs) {
      body = h("div", { class: "empty", text: "loading" });
    } else {
      body = M.requestsTable(reqs.requests, { empty: "no requests in this range", now: now });
    }
    var totalsLine = null;
    if (reqs && reqs.totals) {
      var totals = reqs.totals;
      var shown = reqs.requests.length < totals.count ? " (newest " + fmt.int(reqs.requests.length) + " listed)" : "";
      totalsLine = h("div", { class: "totals" }, [
        h("span", { class: "fig" }, [h("span", { class: "label", text: "requests" }), h("span", { class: "num", text: fmt.int(totals.count) + shown })]),
        h("span", { class: "fig" }, [h("span", { class: "label", text: "tokens in" }), h("span", { class: "num", text: fmt.int(totals.input_tokens) })]),
        h("span", { class: "fig" }, [h("span", { class: "label", text: "out" }), h("span", { class: "num", text: fmt.int(totals.output_tokens) })]),
        h("span", { class: "fig" }, [h("span", { class: "label", text: "cache read" }), h("span", { class: "num", text: fmt.int(totals.cache_read_tokens) })]),
        h("span", { class: "fig" }, [h("span", { class: "label", text: "cache creation" }), h("span", { class: "num", text: fmt.int(totals.cache_creation_tokens) })]),
        h("span", { class: "fig" }, [h("span", { class: "label", text: "time" }), h("span", { class: "num", text: fmt.dur(totals.ms / 1000) })]),
        h("span", { class: "fig" + (totals.errors > 0 ? " err" : "") }, [h("span", { class: "label", text: "errors" }), h("span", { class: "num", text: fmt.int(totals.errors) })]),
      ]);
    }
    return h("section", { class: "requests" }, [h("h2", { text: "Requests · " + name + " · last " + range }), totalsLine, body]);
  }

  // 3.10b (the operator: "that table should be the same table from the primary page, just
  // filtered to this endpoint - no need to have two different tables - and the code should be
  // re-used"): the history body's jobs are now full §5a job rows in the overview's log order,
  // so this section draws through app.js's one jobs-table renderer with the endpoint column
  // omitted (every row is this endpoint) and the filter spelled in the heading and the note.
  // No slice/reverse: the server's order is the log's contract (3.7g).
  function jobsSection(hist, range, name, now) {
    var body = hist && Array.isArray(hist.jobs)
      ? M.jobsTable(hist.jobs, { id: "jobs-endpoint", omit: ["endpoint"], now: now,
        empty: "no jobs ran on it in this range",
        note: "jobs that ran on " + name + " in the last " + range })
      : h("div", { class: "empty", text: "loading" });
    return h("section", { class: "ep-jobs-list" }, [h("h2", { text: "Jobs · " + name + " · last " + range }), body]);
  }

  // 3.10b (the operator: "What is 'events'? I have none."): the collector writes an event only
  // on an up/down transition, so most ranges hold none — then the section is absent, not an
  // empty table with an explaining sentence. Before the history arrives there is no event to
  // show either, so it stays absent until one lands. With events, the heading says what they
  // are, and the draw (drawTables) tolerates the null.
  function eventsSection(hist, range, name, now) {
    var evs = hist && Array.isArray(hist.events) && hist.events.length ? hist.events.slice().reverse() : null; // newest first
    if (!evs) return null;
    var body = h("div", { class: "table-wrap" }, [table(["time", "kind", "detail"], evs.map(function (ev) {
      return h("tr", { class: "ev-" + (ev.kind || "") }, [
        h("td", { text: when(ev.t, now) }),
        h("td", { text: ev.kind || "" }),
        h("td", { class: "wrap", text: ev.detail || "" }),
      ]);
    }), {})]);
    return h("section", { class: "events" }, [h("h2", { text: "Restarts and outages · " + name + " · last " + range }), body]);
  }

  // the snapshot's endpoint called `name`, with its host; nulls when there is no snapshot yet or
  // the name is not in it
  function find(snap, name) {
    if (!snap || !snap.hosts) return { host: null, ep: null };
    for (var i = 0; i < snap.hosts.length; i++) {
      var host = snap.hosts[i];
      for (var k = 0; k < host.endpoints.length; k++) {
        if (host.endpoints[k].name === name) return { host: host, ep: host.endpoints[k] };
      }
    }
    return { host: null, ep: null };
  }

  function header(name, snap, host, ep, hist, err) {
    var back = h("a", { class: "back", href: "#/", text: "← overview" });
    if (!ep) {
      return h("div", { class: "ep-detail-head" }, [back, h("h2", { class: "ep-title" }, [h("span", { text: name })]),
        h("div", { class: "muted", text: err || (snap ? "no endpoint named " + name + " in the snapshot" : "waiting for the first snapshot") })]);
    }
    var seen = ep.last_seen == null ? "never seen up"
      : ep.state === "live" ? "last seen " + fmt.ago(ep.last_seen, snap.now)
        : "last seen " + fmt.date(ep.last_seen) + " (" + fmt.ago(ep.last_seen, snap.now) + ")";
    var dropped = null;
    if (ep.state === "down" && ep.dropped_job_id) {
      // 3.10f-2: the entry carries the job's id, so the sentence links without consulting the
      // range's history for a match
      dropped = h("div", { class: "ep-dropped" }, ["running when it dropped: ",
        M.jobLink(ep.dropped_job_id)]);
    }
    // the card the endpoint sits under: the endpoint's own `host` (a page that re-sorts needs
    // it beside the endpoint), the containing host object as the fallback; the object's node
    // state goes in the parentheses when it is present, and the port-22 fallback answer rides
    // along when there is one (3.10d: the node tile is gone — this is its note, one muted clause)
    var hostName = ep.host != null ? ep.host : host ? host.name : null;
    // what the engine last served here, while the live pair (model/context) is null
    var lastServed = null;
    if ((ep.state === "down" || ep.state === "dormant") && ep.last_model != null) {
      lastServed = h("div", { class: "ep-note muted", text: "last served " + ep.last_model
        + (ep.last_context != null ? ", " + fmt.int(ep.last_context) + " tokens" : "") });
    }
    return h("div", { class: "ep-detail-head endpoint state-" + ep.state, "data-endpoint": ep.name }, [
      back,
      h("div", { class: "ep-head" }, [
        h("h2", { class: "ep-title", text: ep.name }),
        h("span", { class: "tag", text: ep.backend }),
        h("span", { class: "ep-state", text: ep.state }),
        hostName != null ? h("span", { class: "muted", text: "host " + hostName + (host && (host.state || host.fallback)
          ? " (" + [host.state, host.fallback ? "port 22: " + host.fallback : null].filter(Boolean).join(" · ") + ")"
          : "") }) : null,
        h("span", { class: "ep-url", text: ep.base_url }),
      ]),
      h("div", { class: "ep-seen muted", text: seen + (ep.state === "live" && ep.jobs_running ? "; " + fmt.int(ep.jobs_running) + " job" + (ep.jobs_running === 1 ? "" : "s") + " running" : "") }),
      ep.state === "down" && ep.error ? h("div", { class: "ep-error", text: ep.error }) : null,
      lastServed,
      dropped,
      ep.rates && ep.rates.reason && ep.state === "live" ? h("div", { class: "ep-note muted", text: "speeds: " + ep.rates.reason }) : null,
    ]);
  }

  // --- the page -------------------------------------------------------------------------------

  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) throw new Error((body && body.message) || ("HTTP " + r.status + " from " + url));
        return body;
      });
    });
  }

  function fetchRequests(name, range) {
    return getJSON("/api/endpoints/" + encodeURIComponent(name) + "/requests?range=" + encodeURIComponent(range));
  }

  function endpointPage(container, name, state) {
    var page = {
      dead: false,
      hist: null, reqs: null, range: null, // the last answers and the range they were asked for
      err: null,
      lastFetch: 0, inflight: false, again: false,
      observers: [],
      off: null, // 3.9D D4: the snapshot hook's remover, called in teardown
    };
    var root = h("div", { class: "endpoint-detail" });
    var headBox = h("div");
    var tilesBox = h("div");
    var tablesBox = h("div");
    root.appendChild(headBox);
    root.appendChild(tilesBox);
    root.appendChild(tablesBox);
    container.replaceChildren(root);

    function nowOf() {
      if (state.snap && state.snap.now != null) return state.snap.now;
      if (page.hist && page.hist.t && page.hist.t.length) return page.hist.t[page.hist.t.length - 1];
      return Date.now() / 1000;
    }

    // the header and the tiles: current figures from state.snap, series from the last history
    function drawCurrent() {
      var snap = state.snap;
      var found = find(snap, name);
      headBox.replaceChildren(header(name, snap, found.host, found.ep, page.hist, page.err));
      var range = page.range || state.range;
      var span = rangeSeconds(range);
      var now = nowOf();
      var t0 = now - span;
      var tiles = tilesFor(found.ep, page.hist);
      var groups = [];
      [0, 1, 2, 3].forEach(function (tier) {
        var mine = tiles.filter(function (tile) { return tile.tier === tier; });
        if (!mine.length) return;
        // 3.10d: no heading above the first group, a plain noun above the rest
        groups.push(h("div", { class: "tier-group" }, [tier === 0 ? null : h("h3", { text: GROUP_HEAD[tier] }),
          h("div", { class: "tiles" }, mine.map(function (tile) { return tileEl(tile, t0, span, range); }))]));
      });
      var parts = [
        page.err && found.ep ? h("div", { class: "ep-error", text: page.err }) : null,
        h("div", { class: "tier-groups" }, groups),
        stateStrip(page.hist, t0, span, now),
      ].filter(Boolean); // replaceChildren would print a null as the text "null"
      tilesBox.replaceChildren.apply(tilesBox, parts);
    }

    // the tables: from the last history and requests answers
    function drawTables() {
      var range = page.range || state.range;
      var now = nowOf();
      if (page.err && !page.hist && !page.reqs) { // nothing ever arrived: the header says why
        tablesBox.replaceChildren();
        return;
      }
      // 3.10b: the events section is null when the range holds no event (and until the
      // history arrives); replaceChildren would print a null as the text "null", so the list
      // is filtered first, as drawCurrent does for its parts.
      tablesBox.replaceChildren.apply(tablesBox, [
        // the short lists first: the request table can run to 1000 rows
        jobsSection(page.hist, range, name, now),
        eventsSection(page.hist, range, name, now),
        requestsSection(page.reqs, range, name, now),
      ].filter(Boolean));
    }

    function draw() {
      if (page.dead) return;
      drawCurrent();
      drawTables();
    }

    function fetchAll() {
      if (page.dead) return;
      if (page.inflight) { page.again = true; return; }
      page.inflight = true;
      page.lastFetch = Date.now();
      var range = state.range;
      Promise.all([M.fetchHistory(name, range), fetchRequests(name, range)]).then(function (bodies) {
        if (page.dead || range !== state.range) return; // stale: the range changed meanwhile
        page.hist = bodies[0];
        page.reqs = bodies[1];
        page.range = range;
        page.err = null;
        draw();
      }).catch(function (e) {
        if (page.dead) return;
        page.err = e.message;
        draw();
      }).then(function () {
        page.inflight = false;
        if (page.dead) return;
        if (page.again || range !== state.range) { page.again = false; fetchAll(); }
      });
    }

    // a snapshot arrived (app.js's render() reached its end, hook or no skip): redraw the
    // current figures; re-fetch the series and tables only when the last fetch is REFETCH_MIN_MS old
    function onSnapshot() {
      if (page.dead) return;
      drawCurrent();
      if (Date.now() - page.lastFetch >= REFETCH_MIN_MS) fetchAll();
    }

    // the range control was redrawn (app.js's range.draw on every change): re-fetch on a new range
    function onRange() {
      if (page.dead) return;
      if (state.range !== page.range) fetchAll();
    }

    function watch(id, fn) {
      var el = document.getElementById(id);
      if (!el || typeof MutationObserver === "undefined") return;
      var obs = new MutationObserver(fn);
      obs.observe(el, { childList: true, characterData: true, subtree: true });
      page.observers.push(obs);
    }

    // 3.9D D4: the snapshot half of liveness is the hook, not a dead element's mutations; the
    // range half stays the observer — #range is real (range.draw rebuilds it) and app.js has no
    // range-change event, so building one here would be a second mechanism for one button.
    page.off = M.onSnapshot(onSnapshot);
    watch("range", onRange);
    draw();
    fetchAll();

    return function teardown() {
      page.dead = true;
      if (page.off) page.off();
      page.observers.forEach(function (obs) { obs.disconnect(); });
      page.observers = [];
      container.replaceChildren();
    };
  }

  M.endpointPage = { tilesFor: tilesFor, TILES: TILES };
  M.pages.endpoint = endpointPage;
})();
