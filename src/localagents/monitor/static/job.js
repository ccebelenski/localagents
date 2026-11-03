/* localagents monitor: the job detail page (story 3.4; monitor spec §5 "Job detail").
 *
 * Registered through app.js's pages.job hook: on #/job/<id> it fetches /api/jobs/<id> once
 * (the §5a job detail: the summary dict plus requests, tool_call_log and the transcript tail),
 * draws into the detail element, and returns the teardown app.js calls before the next route.
 * A running job's header refreshes from each snapshot's jobs[] entry (elapsed, turns, tool
 * calls, last tool; a server_live of false marks it lost, spec §6; a snapshot `build` other than
 * the job's server_version reads "server code differs", story 1.14) — for that the page keeps
 * its own /events subscription, closed by the teardown; the overview's stream keeps drawing
 * while the page is open.
 *
 * The pure parts the browser smoke checks (timelineMarks, contextFill, toolColour) live on
 * window.localagentsMonitor.jobPage. The route bodies are read through fixed names: `detail`
 * (the job detail body), `snap` (a snapshot), `req` (a request row), `tc` (a tool call),
 * `job` (a snapshot jobs[] entry) — tests/test_monitor_page.py checks they name only §5a
 * fields.
 *
 * No framework, no build step: plain DOM built through h(), which sets text with textContent,
 * so a job name, an error sentence or the transcript tail from the database is never parsed
 * as HTML.
 */
(function () {
  "use strict";

  var M = window.localagentsMonitor;
  var h = M.h;
  var fmt = M.fmt;
  var SVGNS = "http://www.w3.org/2000/svg";

  // the status bar's marks (status.py MARK): one character per status
  var MARK = { queued: "…", running: "⟳", succeeded: "✓", failed: "✗", timeout: "⏱", cancelled: "∅", lost: "†" };
  var STATUS_CLASSES = {
    queued: "status-queued", running: "status-running", succeeded: "status-succeeded",
    failed: "status-failed", timeout: "status-timeout", cancelled: "status-cancelled", lost: "status-lost",
  };
  // tool mark colours: deterministic per tool name over the overview's palette; an error mark
  // is the critical colour, never a tool colour
  var TOOL_PALETTE = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
  var ERROR_COLOUR = "#d03b3b"; // app.css --critical
  // the metrics delta's seven columns in the page's own display order — processed, cached,
  // cache hit, generated, prompt t/s, generate t/s, spec acceptance — not derive's order,
  // which puts generated first. Each carries its label and format: a fraction as a whole
  // percentage, a speed in t/s, token counts as integers. The page walks the object in this
  // order, so a column §5a does not name by key still shows labelled (or by its raw name
  // when the order gains a new one); a Python test pins the SET of keys here to
  // derive._METRICS_COLUMNS, so a column added to the delta is caught even unseen here
  var METRICS = [
    ["prompt_tokens_processed", "processed", function (v) { return fmt.int(v); }],
    ["prompt_tokens_cached", "cached", function (v) { return fmt.int(v); }],
    ["cache_hit_ratio", "cache hit", function (v) { return fmt.frac(v); }],
    ["generated_tokens", "generated", function (v) { return fmt.int(v); }],
    ["prompt_tps", "prompt", function (v) { return fmt.speed(v) + " t/s"; }],
    ["generate_tps", "generate", function (v) { return fmt.speed(v) + " t/s"; }],
    ["spec_decode_acceptance", "spec acceptance", function (v) { return fmt.frac(v); }],
  ];

  // --- the pure parts (exported on jobPage for the browser smoke) ---------------------------

  // the job's duration window: started (or created, when it never started) to finished, or the
  // given clock while it runs; null when it cannot be bounded
  function span(detail, now) {
    var t0 = detail.started_at != null ? detail.started_at : detail.created_at;
    var t1 = detail.finished_at != null ? detail.finished_at : now;
    if (t0 == null || t1 == null || t1 <= t0) return null;
    return { t0: t0, t1: t1 };
  }

  // one mark per tool call at its time, 0..1 across the job's duration
  function timelineMarks(detail, now) {
    var s = span(detail, now);
    var out = [];
    if (!s || !detail.tool_call_log) return out;
    detail.tool_call_log.forEach(function (tc) {
      if (tc.t == null) return;
      out.push({
        x: Math.max(0, Math.min(1, (tc.t - s.t0) / (s.t1 - s.t0))),
        tool: tc.tool,
        error: tc.is_error === true,
      });
    });
    return out;
  }

  // the context fill over the marks: each /v1/messages request with a usage, its prompt
  // tokens (input + cache read + cache creation) over the window, in time order; a request
  // without a usage keeps the line where it was. It is drawn against the same clock as the
  // marks — the caller passes the very `now` it gave timelineMarks, so a browser whose clock
  // lies cannot slide the fill out from under the marks (story 3.4a)
  function contextFill(detail, now) {
    var s = span(detail, now);
    var out = [];
    if (!s || !detail.context || !detail.requests) return out;
    detail.requests.forEach(function (req) {
      if (req.path !== "/v1/messages" || req.t == null || !req.tokens) return;
      if (req.tokens.input == null && req.tokens.cache_read == null && req.tokens.cache_creation == null) return;
      var tokens = (req.tokens.input || 0) + (req.tokens.cache_read || 0) + (req.tokens.cache_creation || 0);
      out.push({
        x: Math.max(0, Math.min(1, (req.t - s.t0) / (s.t1 - s.t0))),
        pct: tokens / detail.context,
      });
    });
    return out;
  }

  function toolColour(tool) {
    var n = 0;
    for (var i = 0; i < tool.length; i++) n = (n * 31 + tool.charCodeAt(i)) >>> 0;
    return TOOL_PALETTE[n % TOOL_PALETTE.length];
  }

  M.jobPage = { timelineMarks: timelineMarks, contextFill: contextFill, toolColour: toolColour };

  // --- DOM helpers --------------------------------------------------------------------------

  // a labelled figure, or nothing when the value is absent (never a placeholder number)
  function fig(label, text) {
    if (text == null || text === "") return null;
    return h("span", { class: "fig" }, [h("span", { class: "label", text: label }),
      h("span", { class: "num", text: text })]);
  }

  function oneLine(text) {
    if (text == null) return "";
    return String(text).split("\n")[0];
  }

  // select an element's text: the copy button's fallback when the clipboard is missing or the
  // write is refused (story 3.4a)
  function selectText(el) {
    var sel = window.getSelection && window.getSelection();
    if (!sel) return;
    var range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
  }

  // the job's root for relativising touched paths, the status bar's rule (status._job_root):
  // the worktree's path when the job runs in one, else its cwd
  function jobRoot(detail) {
    var wt = detail.worktree;
    if (wt && typeof wt.path === "string" && wt.path) return wt.path;
    return detail.cwd;
  }

  function relPath(detail, p) {
    var root = jobRoot(detail);
    if (root && p.indexOf(root + "/") === 0) return p.slice(root.length + 1);
    return p;
  }

  // --- the page: registered as pages.job, drawing into the detail element -------------------

  M.pages.job = function (container, id, state) {
    var detail = null; // the /api/jobs/<id> body (§5a job detail)
    var lost = false; // set by the first snapshot that says server_live is false (§6)
    var live = {}; // the header figures a snapshot refreshes in place, by summary key
    var stream = null;
    var dead = false;

    container.replaceChildren(h("div", { class: "empty", text: "loading the job…" }));

    fetch("/api/jobs/" + encodeURIComponent(id), { cache: "no-store" })
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error((body && body.message) || ("HTTP " + r.status + " from /api/jobs/" + id));
          return body;
        });
      })
      .then(function (d) {
        if (dead) return;
        detail = d;
        draw();
        applySnapshot(state.snap); // the on-connect snapshot may have beaten the fetch
      })
      .catch(function (e) {
        if (dead) return;
        container.replaceChildren(h("div", { class: "empty" }, [e.message]));
      });

    // a running job's header refreshes from each snapshot's jobs[] entry (elapsed, turns,
    // tool calls, last tool); server_live false means the record says running but nothing is
    // running it (spec §6: shown lost). Applied from the overview's last snapshot once the
    // detail arrives, then from each snapshot the page's own stream pushes
    function applySnapshot(snap) {
      if (!detail || !snap) return;
      applyBuild(snap); // before the jobs[] lookup: a job past the log's tail still compares
      var job = null;
      (snap.jobs || []).forEach(function (j) { if (j.id === detail.id) job = j; });
      if (!job) return;
      if (job.server_live === false && !lost) setLost();
      if (live.elapsed_s) live.elapsed_s.textContent = fmt.dur(job.elapsed_s);
      if (live.num_turns) live.num_turns.textContent = fmt.int(job.num_turns);
      if (live.tool_calls) live.tool_calls.textContent = fmt.int(job.tool_calls);
      if (live.last_tool) live.last_tool.textContent = oneLine(job.last_tool);
    }

    stream = new EventSource("/events");
    stream.addEventListener("snapshot", function (e) {
      if (!detail || dead) return;
      var snap;
      try { snap = JSON.parse(e.data); } catch (err) { return; }
      applySnapshot(snap);
    });

    // story 1.14: the "server" figure's muted note — the job's server ran other code than this
    // monitor runs (their build stamps differ). Said only when both stamps are known: an
    // unknown stamp is not a difference
    function applyBuild(snap) {
      var note = live.buildNote;
      if (!note) return;
      var differs = snap.build != null && snap.build !== "" && snap.build !== detail.server_version;
      note.textContent = differs ? "server code differs from the monitor's" : "";
      note.title = differs ? "monitor " + snap.build : "";
    }

    function setLost() {
      lost = true;
      if (live.mark) {
        live.mark.className = "status " + STATUS_CLASSES.lost;
        live.mark.textContent = (MARK.lost || "") + " lost";
      }
      if (live.note) live.note.textContent = "lost: " + (detail.error || "its server stopped beating");
    }

    // the server's clock when the overview's stream has one (a skewed browser clock cannot
    // lie about a running job's duration), else the browser's
    function nowClock() {
      return state.snap && state.snap.now != null ? state.snap.now : Date.now() / 1000;
    }

    function draw() {
      lost = detail.status === "lost";
      live = {};
      var root = h("div", { class: "job-detail" }, [
        header(),
        gauge(),
        usageMetrics(),
        filesSection(),
        timelineSection(),
        byToolSection(),
        requestsSection(),
        transcriptSection(),
      ]);
      container.replaceChildren(root);
    }

    // the context gauge: ctx_tokens over the window as a bar with both numbers and the
    // percentage; absent when either is null
    function gauge() {
      if (detail.ctx_tokens == null || !detail.context) return null;
      var pct = 100 * detail.ctx_tokens / detail.context;
      var w = Math.max(0, Math.min(100, pct));
      return h("div", { class: "gauge" }, [
        h("span", { class: "label", text: "context" }),
        h("span", { class: "gauge-bar" + (w >= 80 ? " high" : "") }, [h("span", { style: "width:" + w.toFixed(1) + "%" })]),
        h("span", { class: "num", text: fmt.int(detail.ctx_tokens) + " / " + fmt.int(detail.context) + " tokens, " + fmt.pct(pct) }),
      ]);
    }

    // the four usage counters and the metrics delta's seven values when the runner recorded
    // one ("metrics not recorded" when null)
    function usageMetrics() {
      var usage = detail.usage || {};
      var figs = [
        fig("input tokens", fmt.int(usage.input_tokens)),
        fig("output tokens", fmt.int(usage.output_tokens)),
        fig("cache read", fmt.int(usage.cache_read_input_tokens)),
        fig("cache creation", fmt.int(usage.cache_creation_input_tokens)),
      ].filter(Boolean);
      if (detail.metrics) {
        METRICS.forEach(function (m) {
          var v = detail.metrics[m[0]];
          if (v != null) figs.push(fig(m[1], m[2](v)));
        });
        // a column the label order does not name yet: shown by its raw name, never dropped
        Object.keys(detail.metrics).forEach(function (k) {
          if (METRICS.some(function (m) { return m[0] === k; })) return;
          if (detail.metrics[k] != null) figs.push(fig(k, fmt.int(detail.metrics[k])));
        });
      } else {
        figs.push(h("span", { class: "muted", text: "metrics not recorded" }));
      }
      return h("div", { class: "usage" }, [h("h3", { text: "Usage and metrics" }), h("div", { class: "fig-row" }, figs)]);
    }

    // the files touched, relative to the job's root when the path starts with it (the status
    // bar's rule), the full path as the tooltip
    function filesSection() {
      if (!detail.files_touched || !detail.files_touched.length) return null;
      return h("div", { class: "files" }, [h("h3", { text: "Files touched" })].concat(
        detail.files_touched.map(function (p) {
          return h("div", { class: "file", title: p, text: relPath(detail, p) });
        })
      ));
    }

    // the tool-call timeline: an inline SVG across the job's duration (started to finished,
    // or to now), one mark per call at its time coloured by tool, errors red, the context
    // fill drawn over it as a faint line, and a legend
    function timelineSection() {
      var now = nowClock();
      var marks = timelineMarks(detail, now);
      var fill = contextFill(detail, now);
      if (!marks.length && !fill.length) return null;
      var W = 720, H = 44;
      var svg = document.createElementNS(SVGNS, "svg");
      svg.setAttribute("class", "timeline-svg");
      svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", marks.length + " tool calls across the job's duration" +
        (fill.length ? ", with the context fill" : ""));
      // the context fill over the marks: a faint line, 0 at the bottom to the window at the
      // top (a fill past the window clamps to the top)
      if (fill.length) {
        var d = "", pen = false;
        fill.forEach(function (pt) {
          var x = 1 + pt.x * (W - 2);
          var y = H - 14 - Math.max(0, Math.min(1, pt.pct)) * (H - 20);
          d += (pen ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
          pen = true;
        });
        var line = document.createElementNS(SVGNS, "path");
        line.setAttribute("d", d);
        line.setAttribute("fill", "none");
        line.setAttribute("stroke", "var(--ink-2)");
        line.setAttribute("stroke-width", "1");
        line.setAttribute("opacity", "0.5");
        svg.appendChild(line);
      }
      // one mark per call at its time: a tick at the bottom, coloured by tool, errors red;
      // the marks and the log share their order (the null-time entries are skipped by both)
      var i = 0;
      // the log is guarded as an absent field here too (3.9D): timelineMarks returns [] without
      // it, so a context fill from the requests alone reaches these loops with nothing to walk
      (detail.tool_call_log || []).forEach(function (tc) {
        if (tc.t == null) return;
        var mk = marks[i++];
        if (!mk) return;
        var rect = document.createElementNS(SVGNS, "rect");
        rect.setAttribute("class", mk.error ? "timeline-mark error" : "timeline-mark");
        rect.setAttribute("x", (1 + mk.x * (W - 2)).toFixed(1));
        rect.setAttribute("y", String(H - 12));
        rect.setAttribute("width", "2");
        rect.setAttribute("height", "12");
        rect.setAttribute("fill", mk.error ? ERROR_COLOUR : toolColour(tc.tool));
        var title = document.createElementNS(SVGNS, "title");
        title.textContent = fmt.time(tc.t) + " " + tc.tool + (tc.is_error ? " (error)" : "") +
          (tc.input ? ": " + tc.input : "");
        rect.appendChild(title);
        svg.appendChild(rect);
      });
      // the legend: one swatch per tool, first-seen order
      var seen = {};
      var legend = [];
      (detail.tool_call_log || []).forEach(function (tc) {
        if (seen[tc.tool]) return;
        seen[tc.tool] = true;
        legend.push(h("span", { class: "legend-item" }, [
          h("span", { class: "legend-swatch", style: "background:" + toolColour(tc.tool) }),
          tc.tool,
        ]));
      });
      return h("div", { class: "timeline" }, [
        h("h3", { text: "Tool calls" }),
        svg,
        h("div", { class: "timeline-legend" }, legend),
      ]);
    }

    // the breakdown by tool from tool_counts (tool, calls, errors), busiest first
    function byToolSection() {
      var counts = detail.tool_counts;
      if (!counts) return null;
      var tools = Object.keys(counts).sort(function (a, b) {
        var d = (counts[b].calls || 0) - (counts[a].calls || 0);
        return d !== 0 ? d : (a < b ? -1 : a > b ? 1 : 0);
      });
      var rows = tools.map(function (tool) {
        var c = counts[tool];
        return h("tr", null, [
          h("td", null, [h("span", { class: "legend-swatch", style: "background:" + toolColour(tool) }), tool]),
          h("td", { class: "n", text: fmt.int(c.calls) }),
          h("td", { class: "n" }, [(c.errors || 0) > 0 ? h("span", { class: "badge err", text: fmt.int(c.errors) })
            : h("span", { text: fmt.int(c.errors) })]),
        ]);
      });
      var head = h("tr", null, [h("th", { text: "tool" }), h("th", { class: "n", text: "calls" }),
        h("th", { class: "n", text: "errors" })]);
      return h("div", { class: "by-tool" }, [h("h3", { text: "By tool" }),
        h("div", { class: "table-wrap" }, [h("table", null, [h("thead", null, [head]), h("tbody", null, rows)])])]);
    }

    // the request log (3.7k): the same shared renderer as the endpoint page, without the job
    // column (every row is this job; the route rows carry no job fields), in the server's time
    // order (not re-sorted). The client-side totals line below stays this page's own: the ms
    // sum skips nulls, errors count a non-null, non-empty error (3.2c row 2); it is drawn as
    // the endpoint page's `.totals` figs, since the shared table's columns no longer align
    // with the old twelve-cell footer (cache creation and ms have no column of their own).
    function requestsSection() {
      var reqs = detail.requests || [];
      if (!reqs.length) return null;
      var tot = { ms: 0, input: 0, output: 0, cache_read: 0, cache_creation: 0, errors: 0 };
      reqs.forEach(function (req) {
        var t = req.tokens || {};
        tot.ms += req.ms || 0;
        tot.input += t.input || 0;
        tot.output += t.output || 0;
        tot.cache_read += t.cache_read || 0;
        tot.cache_creation += t.cache_creation || 0;
        // 3.2c row 2: '' is clean, as the row's own cell already treats it; 3.7v: a recorded
        // /api/hello handshake is not an error either, exactly as the shared renderer marks
        // the row and as the header's "request errors" figure counts it
        if (req.path !== "/api/hello" && req.error != null && req.error !== "") tot.errors++;
      });
      var totals = h("div", { class: "totals" }, [
        h("span", { class: "fig" }, [h("span", { class: "label", text: "requests" }), h("span", { class: "num", text: fmt.int(reqs.length) })]),
        h("span", { class: "fig" }, [h("span", { class: "label", text: "tokens in" }), h("span", { class: "num", text: fmt.int(tot.input) })]),
        h("span", { class: "fig" }, [h("span", { class: "label", text: "out" }), h("span", { class: "num", text: fmt.int(tot.output) })]),
        h("span", { class: "fig" }, [h("span", { class: "label", text: "cache read" }), h("span", { class: "num", text: fmt.int(tot.cache_read) })]),
        h("span", { class: "fig" }, [h("span", { class: "label", text: "cache creation" }), h("span", { class: "num", text: fmt.int(tot.cache_creation) })]),
        h("span", { class: "fig" }, [h("span", { class: "label", text: "time" }), h("span", { class: "num", text: fmt.dur(tot.ms / 1000) })]),
        h("span", { class: "fig" + (tot.errors > 0 ? " err" : "") }, [h("span", { class: "label", text: "errors" }), h("span", { class: "num", text: fmt.int(tot.errors) })]),
      ]);
      return h("div", { class: "job-requests" }, [h("h3", { text: "Requests" }), totals,
        // the detail's clock (nowClock: the snapshot's now, the browser's as fallback) feeds
        // the shared renderer's day-stamped time column (3.7-fold-4)
        M.requestsTable(reqs, { job: false, now: nowClock() })]);
    }

    // the transcript tail in a <pre> (the user text, textContent only) with the log directory
    // as selectable text in a <code> plus a copy button — never a link into the local
    // filesystem, dead in a page served over http (story 3.4a) — or the sentence saying why
    // there is no tail
    function transcriptSection() {
      if (detail.transcript_tail == null) {
        if (!detail.transcript_note) return null;
        return h("div", { class: "transcript" }, [h("h3", { text: "Transcript" }),
          h("p", { class: "muted", text: detail.transcript_note })]);
      }
      var els = [h("h3", { text: "Transcript" }),
        h("pre", { class: "transcript-tail", text: detail.transcript_tail })];
      if (detail.log_dir) {
        var code = h("code", { class: "log-dir", text: detail.log_dir });
        var btn = h("button", { type: "button", class: "copy-path", text: "copy path" });
        // the clipboard when the browser offers one (a plain-http page may not, and the write
        // can still be refused); otherwise select the path for the user to copy themselves
        var btnDone = function (ok) { btn.textContent = ok ? "copied" : "select it"; };
        btn.addEventListener("click", function () {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(detail.log_dir).then(
              function () { btnDone(true); },
              function () { selectText(code); btnDone(false); });
          } else {
            selectText(code); btnDone(false);
          }
        });
        els.push(h("div", { class: "transcript-dir" }, [code, btn]));
      }
      return h("div", { class: "transcript" }, els);
    }

    // a header figure a snapshot refreshes in place (a running job's elapsed, turns, tool
    // calls and last tool move as the job goes); registered under its summary key
    function liveFig(label, key, text) {
      var num = h("span", { class: "num", text: text == null || text === "" ? "" : text });
      live[key] = num;
      return h("span", { class: "fig" }, [h("span", { class: "label", text: label }), num]);
    }

    // the header's "request errors" figure (3.7v): the rows the shared renderer marks req-err,
    // counted through its own rule (app.js requestFailed: a status outside 2xx or a non-empty
    // error, the /api/hello handshake excepted — 3.11a made it one function). 0 draws as "0".
    function requestErrors(reqs) {
      return (reqs || []).filter(function (req) { return M.requestFailed(req); }).length;
    }

    // story 1.14: the build stamp of the MCP server that ran the job, with the muted note
    // applyBuild fills from each snapshot's ``build``; absent when the stamp is unknown
    function serverFig() {
      var f = fig("server", detail.server_version);
      if (!f) return null;
      var note = h("span", { class: "muted build-note" });
      live.buildNote = note;
      f.appendChild(note);
      return f;
    }

    function header() {
      var status = lost ? "lost" : detail.status;
      var mark = h("span", {
        class: "status " + (STATUS_CLASSES[status] || "status-unknown"),
        text: (MARK[status] || "") + " " + status,
      });
      live.mark = mark;
      // the note, filled when the job is (or becomes) lost, or the error sentence when the
      // job ended with one; empty until then
      var note = h("div", { class: "job-note" });
      live.note = note;
      if (lost) note.textContent = "lost: " + (detail.error || "its server stopped beating");
      else if (detail.error) note.textContent = detail.error;
      var figs = [
        fig("created", fmt.date(detail.created_at)),
        fig("started", fmt.date(detail.started_at)),
        fig("finished", fmt.date(detail.finished_at)),
        liveFig("elapsed", "elapsed_s", fmt.dur(detail.elapsed_s)),
        liveFig("turns", "num_turns", fmt.int(detail.num_turns)),
        liveFig("tool calls", "tool_calls", fmt.int(detail.tool_calls)),
        // 3.7v (the operator: a job showing one error next to a column saying two): the two
        // figures count different things, so the header says both by their own names — the
        // overview's tool errors (tool results flagged is_error, derive.error_count) and the
        // request table's failures, handshake rows excluded (see requestErrors)
        fig("tool errors", fmt.int(detail.errors)),
        fig("request errors", fmt.int(requestErrors(detail.requests))),
        detail.compactions > 0 ? fig("compactions", fmt.int(detail.compactions)) : null,
        liveFig("last tool", "last_tool", oneLine(detail.last_tool)),
        serverFig(),
      ].filter(Boolean);
      return h("div", { class: "job-header" }, [
        h("div", { class: "job-head" }, [
          // 3.10f-2: the job's one key is its UUID; this header is the one place that shows it
          // in full — the page's subject (the overview's links title it, the tables show the short form)
          h("span", { class: "job-name", text: detail.id }),
          mark,
          // router 8: a calibration probe says so beside its status mark
          detail.calibration ? h("span", { class: "tag cal", text: "cal" }) : null,
          detail.model ? h("span", { class: "tag", text: detail.model }) : null,
          detail.endpoint ? h("a", { class: "ep-name", href: "#/endpoint/" + encodeURIComponent(detail.endpoint), text: detail.endpoint }) : null,
        ]),
        h("div", { class: "fig-row" }, figs),
        detail.task ? h("pre", { class: "job-task", text: detail.task }) : null,
        note,
        h("div", { class: "job-nav" }, [h("a", { href: "#/", text: "← back to the overview" })]),
      ]);
    }

    return function () {
      dead = true;
      if (stream) stream.close();
    };
  };
})();
