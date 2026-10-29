"""Story 1.5a: the status line reads the shared database. Servers keep their mcp_servers row
alive (heartbeat / updated=started) and write jobs, requests and tool calls; the renderer
merges the live servers' jobs into one line. The rendered strings are pinned: the table swap
must not change what the status bar shows."""

import os
import subprocess
import sys
import time
from pathlib import Path

from localagents import db, jobs, status

MODEL = "unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS"


def _owner():
    """What _open records as this process's owner (story 1.9 item 9): the walk to a ``claude``
    ancestor when one is above us, the parent pid when there is none. Tests that hand an
    owner to render() use it so they pass in either environment."""
    return db.session_pid() or os.getppid()


def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return jobs.JobStore()


def _new(store, **kw):
    kw.setdefault("task", "t")
    kw.setdefault("model", MODEL)
    kw.setdefault("endpoint", "llamacpp")
    kw.setdefault("base_url", "http://x:8080")
    kw.setdefault("backend", "llama.cpp")
    kw.setdefault("host", "local")
    kw.setdefault("cwd", "/tmp")
    kw.setdefault("run_cwd", "/tmp")
    kw.setdefault("isolation", "none")
    return store.new(**kw)


def test_short_model_and_elapsed():
    assert status.short_model("unsloth/Qwen3.8-Flash-Next-GGUF:UD-IQ4_XS") == "Qwen3.8-Flash-Next"
    assert status.short_model("GLM-5.3-Flash-EXL3") == "GLM-5.3-Flash"
    assert status.short_model("deepseek-ai/DeepSeek-V4-Flash-0731") == "DeepSeek-V4-Flash-0731"
    assert status.fmt_elapsed(45) == "45s"
    assert status.fmt_elapsed(78) == "1m18s"
    assert status.fmt_elapsed(3 * 3600 + 7 * 60) == "3h07m"


def test_publish_and_render_running_job(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    now = time.time()
    job.status, job.started_at = "running", now - 78
    for i in range(4):
        job.record_tool_call(now - 40 + i, "Bash", f"b{i}", "ls")
    job.changed()
    srv = store.conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (store.server_id,)).fetchone()
    assert srv["pid"] == os.getpid() and srv["owner"] == _owner()
    me = _owner()  # what _open records as owner for this process
    assert status.render(now=now, owner=me) == "local: ⟳ Qwen3.8-Flash-Next 1m18s 4 calls"
    # this job retires off the line; a second one shows the single forms
    job.status, job.finished_at = "succeeded", now - 3600
    job.changed()
    job2 = _new(store)
    job2.status, job2.started_at = "running", now - 78
    job2.record_tool_call(now - 20, "Edit", "e1", "/tmp/a.py")
    job2.changed()
    assert status.render(now=now, owner=me) == "local: ⟳ Qwen3.8-Flash-Next 1m18s 1 call 1 file"


def test_finished_jobs_linger_then_drop(monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    job.status, job.started_at, job.finished_at = "succeeded", 1000.0, 1180.0
    job.changed()
    assert status.render(now=1200.0, owner=_owner()) == "local: ✓ Qwen3.8-Flash-Next 3m00s"
    # collect filters by `now`, so the linger boundary moves with the clock, not with a re-publish
    assert status.render(now=1180.0 + status.KEEP_FINISHED_S + 1) == ""


def test_null_lookup_ids_still_render(monkeypatch, tmp_path):
    """Story 1.7 item 3: collect LEFT JOINs models/endpoints — a job whose model_id or
    endpoint_id is NULL (a lookup that failed on another server, say) still shows on the line,
    the model as `?` and with no endpoint load segment, instead of vanishing."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    now = time.time()
    job.status, job.started_at = "running", now - 12
    job.changed()
    store.conn.execute("UPDATE jobs SET model_id = NULL, endpoint_id = NULL WHERE id = ?", (job.id,))
    assert status.render(now=now, owner=_owner()) == "local: ⟳ ? 12s"


def test_lost_job_has_a_mark(monkeypatch, tmp_path):
    """Story 1.7 item 1: `lost` has a status mark, so render_entry never prints `?` for a job
    the lost rule swept (story 1.5a wrote the status; MARK never grew its glyph)."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    now = time.time()
    job.status, job.started_at, job.finished_at = "lost", now - 45, now - 5
    job.changed()
    assert status.render(now=now, owner=_owner()) == "local: † Qwen3.8-Flash-Next 40s"


def test_dead_server_jobs_are_dropped(monkeypatch, tmp_path):
    """Liveness is heartbeat first, pid second: a live pid whose row stopped beating 61 s ago
    is not live, and neither is one marked stopped. The row itself is history and stays."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    now = time.time()
    job.status, job.started_at = "running", now - 30
    job.changed()
    assert status.render(now=now, owner=_owner()) == "local: ⟳ Qwen3.8-Flash-Next 30s"
    store.conn.execute("UPDATE mcp_servers SET updated = ? WHERE id = ?", (now - 61, store.server_id))
    assert status.render(now=now, owner=_owner()) == ""
    store.conn.execute("UPDATE mcp_servers SET updated = ?, stopped = ? WHERE id = ?", (now, now, store.server_id))
    assert status.render(now=now, owner=_owner()) == ""
    assert store.conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] == 1


def test_remove_deletes_own_file(monkeypatch, tmp_path):
    """"remove" is close(): stopped set, the server's jobs vanish from the line, and close is
    idempotent."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    now = time.time()
    job.status, job.started_at = "running", now - 30
    job.changed()
    assert status.render(now=now, owner=_owner()) == "local: ⟳ Qwen3.8-Flash-Next 30s"
    store.close()
    row = store.conn.execute("SELECT stopped FROM mcp_servers WHERE id = ?", (store.server_id,)).fetchone()
    assert row["stopped"] is not None
    assert status.render(now=now, owner=_owner()) == ""
    store.close()
    assert status.render(now=now, owner=_owner()) == ""


def test_statusline_command_is_light(tmp_path):
    """It must not import the server stack: it runs on every status-bar refresh.

    Story 1.7 item 8: the subprocess runs with XDG_STATE_HOME and LOCALAGENTS_DB pointed at
    this test's tmp_path — never the developer's real database (an inherited LOCALAGENTS_DB
    used to send it there), and any file it ever creates must live under tmp_path."""
    state = tmp_path / "state"
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.argv=['localagents','statusline']; import localagents; localagents.main(); "
         "print(sorted(m for m in sys.modules if m in ('mcp','httpx','uvicorn','localagents.server')))"],
        # absolute: a relative "src" broke the test when pytest ran from another directory
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent / "src",
        env={**os.environ, "XDG_STATE_HOME": str(state), "LOCALAGENTS_DB": str(state / "localagents.db")},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "[]"
    # the renderer never creates the database: nothing was written anywhere, real or tmp
    assert not state.exists()


def test_render_without_a_database_is_an_empty_line(monkeypatch, tmp_path):
    """A machine with no database yet: print the empty line, create no file (or directory)."""
    monkeypatch.setenv("LOCALAGENTS_DB", str(tmp_path / "nope" / "localagents.db"))
    assert status.render() == ""
    assert not (tmp_path / "nope").exists()


def test_a_corrupt_pid_is_not_a_live_server(monkeypatch, tmp_path):
    """Review of 1.7, finding 9: os.kill(2**40, 0) raises OverflowError, which pid_alive let out:
    the status bar printed a traceback (render promises it never does). A corrupt pid is not a
    live process: the server drops off the line."""
    from localagents import db

    assert db.pid_alive(2**40) is False and db.pid_alive(2**40, strict=True) is False
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    job.status, job.started_at = "running", time.time() - 5
    job.changed()
    store.conn.execute("UPDATE mcp_servers SET pid = ? WHERE id = ?", (2**40, store.server_id))
    assert status.render(owner=_owner()) == ""


def test_render_swallows_a_pid_error_too(monkeypatch, tmp_path):
    def overflow(now=None, conn=None):
        raise OverflowError("signed integer is greater than maximum")

    monkeypatch.setattr(status, "collect", overflow)
    assert status.render() == ""


def test_other_sessions_jobs_fold_into_a_count(monkeypatch, tmp_path):
    now = time.time()
    mine_store = _store(monkeypatch, tmp_path)
    mine = _new(mine_store)
    mine.status, mine.started_at = "running", now - 30
    mine.changed()
    mine_store.record_endpoint_load({"llamacpp": "1/1 slots"})
    # a second live server (same pid is fine for the liveness check) owned by another session
    other = jobs.JobStore()
    other.conn.execute("UPDATE mcp_servers SET owner = 4242 WHERE id = ?", (other.server_id,))
    o1 = _new(other, model="x/GLM-5.3-Flash-EXL3", endpoint="dgx1", base_url="http://y:8000")
    o1.status, o1.started_at = "running", now - 5
    o1.changed()
    o2 = _new(other, model="x/GLM-5.3-Flash-EXL3", endpoint="dgx1", base_url="http://y:8000")
    o2.status, o2.started_at, o2.finished_at = "succeeded", now - 50, now - 10
    o2.changed()
    other.record_endpoint_load({"dgx1": "2 run 1 wait kv 34%"})
    assert status.render(now=now, owner=_owner()) == "local: ⟳ Qwen3.8-Flash-Next 30s · llamacpp 1/1 slots · +2 other"
    assert status.render(now=now, owner=4242) == "local: ✓ GLM-5.3-Flash 40s · ⟳ GLM-5.3-Flash 5s · dgx1 2 run 1 wait kv 34% · +1 other"
    # no identifiable Claude Code parent: everything is ours, no endpoint load shown for non-running
    monkeypatch.setattr(status, "my_owner", lambda: None)
    assert status.render(now=now).count("·") == 4 and "+" not in status.render(now=now)


def test_render_lines_is_one_header_per_endpoint_one_row_per_job(monkeypatch, tmp_path):
    """Story 1.11 item 4: one header per endpoint of this session (first-seen order of the
    entries, which collect returns sorted by started_at), one indented row per job, a
    trailing line for other sessions' jobs. A running job shows calls, files, ctx, t/s,
    compactions (> 0) and its last activity; a finished one mark, model, elapsed, files and
    its compaction count (the trailing indicator — ruling on the 1.11 review) — while
    render_entry shows neither compactions nor activity (the one-line tests pin that it did
    not move)."""
    store = _store(monkeypatch, tmp_path)
    now = time.time()

    a1 = _new(store, endpoint="ai1", base_url="http://ai1:8080", model="x/Qwen3.8-27B", context=100000)
    a1.status, a1.started_at = "running", now - 240
    for i in range(12):
        a1.record_tool_call(now - 200 + i, "Bash", f"b{i}", "ls")
    a1.last_activity = "Bash: uv run pytest -q"
    a1.compactions = 2
    a1.changed()
    a1.record_request({"t": now - 10, "path": "/v1/messages", "status": 200, "ms": 3000,
                       "usage": {"input_tokens": 41000, "output_tokens": 100}})

    a2 = _new(store, endpoint="ai1", base_url="http://ai1:8080", model="x/Qwen3.8-27B")
    a2.status, a2.started_at, a2.finished_at = "succeeded", now - 150, now - 30
    a2.record_tool_call(now - 140, "Edit", "e1", "/a.py")
    a2.record_tool_call(now - 139, "Edit", "e2", "/b.py")
    a2.compactions = 3
    a2.changed()
    store.record_endpoint_load({"ai1": "1/1 slots"})

    d1 = _new(store, endpoint="dgx1-a", base_url="http://dgx1a:8000", model="x/deepseek-v4", context=100000)
    d1.status, d1.started_at = "running", now - 120
    for i in range(3):
        d1.record_tool_call(now - 100 + i, "Bash", f"c{i}", "make")
    d1.changed()
    d1.record_request({"t": now - 5, "path": "/v1/messages", "status": 200, "ms": 3000,
                       "usage": {"input_tokens": 12000, "output_tokens": 120}})
    store.record_endpoint_load({"dgx1-a": "1 run kv 31%"})

    other = jobs.JobStore()
    other.conn.execute("UPDATE mcp_servers SET owner = 4242 WHERE id = ?", (other.server_id,))
    o1 = _new(other, model="x/GLM-5.3-Flash-EXL3", endpoint="dgx1", base_url="http://y:8000")
    o1.status, o1.started_at = "running", now - 5
    o1.changed()
    o2 = _new(other, model="x/GLM-5.3-Flash-EXL3", endpoint="dgx1", base_url="http://y:8000")
    o2.status, o2.started_at, o2.finished_at = "succeeded", now - 50, now - 10
    o2.changed()

    me = _owner()
    assert status.render_lines(now=now, owner=me) == [
        "local ai1 · 1/1 slots",
        "  ⟳ Qwen3.8-27B 4m00s · 12 calls · ctx 41% · 33 t/s · 2 compactions · Bash: uv run pytest -q",
        "  ✓ Qwen3.8-27B 2m00s · 2 files · 3 compactions",
        "local dgx1-a · 1 run kv 31%",
        "  ⟳ deepseek-v4 2m00s · 3 calls · ctx 12% · 40 t/s",
        "+2 jobs in other sessions",
    ]
    # the one-line form is unchanged by the refactor: the same data renders as before
    # (render_entry's bits are space-joined, endpoint loads and the +N tail as today)
    assert status.render(now=now, owner=me) == (
        "local: ⟳ Qwen3.8-27B 4m00s 12 calls ctx 41% 33 t/s · ✓ Qwen3.8-27B 2m00s 2 files · "
        "⟳ deepseek-v4 2m00s 3 calls ctx 12% 40 t/s · ai1 1/1 slots · dgx1-a 1 run kv 31% · +2 other"
    )


def test_render_lines_puts_jobs_without_an_endpoint_under_local_question(monkeypatch, tmp_path):
    """A job whose endpoint_id is NULL renders under `local ?` (the model as ? too), and the
    trailing line is singular for one other job: `+1 job in another session`."""
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    job = _new(store)
    job.status, job.started_at = "running", now - 30
    job.changed()
    store.conn.execute("UPDATE jobs SET model_id = NULL, endpoint_id = NULL WHERE id = ?", (job.id,))

    other = jobs.JobStore()
    other.conn.execute("UPDATE mcp_servers SET owner = 4242 WHERE id = ?", (other.server_id,))
    o1 = _new(other, model="x/GLM-5.3-Flash-EXL3", endpoint="dgx1", base_url="http://y:8000")
    o1.status, o1.started_at = "running", now - 5
    o1.changed()
    assert status.render_lines(now=now, owner=_owner()) == [
        "local ?",
        "  ⟳ ? 30s",
        "+1 job in another session",
    ]


def test_render_lines_is_empty_and_never_raises(monkeypatch, tmp_path):
    """Nothing to show yields [], and the same exception set render swallows (it yields the
    empty line, which the operator's script -n-tests) yields [] instead of a traceback."""
    store = _store(monkeypatch, tmp_path)
    assert status.render_lines(now=time.time(), owner=_owner()) == []

    def overflow(now=None, conn=None):
        raise OverflowError("signed integer is greater than maximum")

    monkeypatch.setattr(status, "collect", overflow)
    assert status.render_lines() == []


def test_statusline_prints_lines_and_one_line_flag(monkeypatch, tmp_path):
    """Story 1.11 item 5: `localagents statusline` prints render_lines() joined by newlines
    (header + one row per job); `--one-line` prints render() as before; the empty case
    prints exactly one empty line (the operator's script -n-tests it), as render() did."""
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    dbpath = state / "localagents.db"
    monkeypatch.setenv("LOCALAGENTS_DB", str(dbpath))
    store = jobs.JobStore()
    job = _new(store)
    job.status, job.started_at = "running", time.time() - 35
    job.changed()

    src = Path(__file__).resolve().parent.parent / "src"
    env = {**os.environ, "XDG_STATE_HOME": str(state), "LOCALAGENTS_DB": str(dbpath)}

    def run_cli(one_line: bool = False, env2: dict | None = None):
        args = "['localagents', 'statusline'" + (", '--one-line'" if one_line else "") + "]"
        return subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.argv={args}; import localagents; localagents.main()"],
            capture_output=True, text=True, cwd=src, env=env2 or env,
        )

    out = run_cli()
    assert out.returncode == 0, out.stderr
    assert out.stdout == "local llamacpp\n  ⟳ Qwen3.8-Flash-Next 35s\n"
    out = run_cli(one_line=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "local: ⟳ Qwen3.8-Flash-Next 35s\n"

    # the empty case: no database at all — exactly one empty line, nothing created
    state2 = tmp_path / "state2"
    env2 = {**os.environ, "XDG_STATE_HOME": str(state2), "LOCALAGENTS_DB": str(state2 / "localagents.db")}
    out = run_cli(env2=env2)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "\n"
    assert not state2.exists()


def test_collect_carries_the_compactions_count(monkeypatch, tmp_path):
    """Story 1.11 item 3: the entry carries the row's compactions count (the job row already
    comes from SELECT j.*); the line renders it in item 4's rows."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store)
    now = time.time()
    job.status, job.started_at = "running", now - 30
    job.compactions = 2
    job.changed()
    entries, _ = status.collect(now=now, conn=store.conn)
    assert len(entries) == 1
    assert entries[0]["compactions"] == 2


def test_load_summary_shapes():
    assert status.load_summary({"slots": {"total": 2, "busy": 1, "free": 1}}) == "1/2 slots"
    assert status.load_summary({"load": {"requests_running": 2, "requests_waiting": 1, "kv_cache_usage": 0.337}}) == "2 run 1 wait kv 34%"
    assert status.load_summary({"load": {"requests_running": 0, "requests_waiting": 0, "kv_cache_usage": 0.0}}) == "0 run kv 0%"
    assert status.load_summary({"up": True, "models": ["m"]}) is None
    assert status.load_summary({"slots": {"total": 4}}) is None  # llama.cpp without /slots enabled


def test_shim_usage_feeds_context_and_speed(monkeypatch, tmp_path):
    """The shim's _log records one request row per backend response; the line derives ctx fill
    and speed from the rows alone (story 1.5b: the in-memory note_usage fields are gone)."""
    store = _store(monkeypatch, tmp_path)
    job = _new(store, context=131072)
    now = time.time()
    job.status, job.started_at = "running", now - 10
    job.changed()
    for t, ms, gen_ms, usage in (
        (1, 3000, None, {"input_tokens": 1000, "cache_read_input_tokens": 39000, "output_tokens": 300}),
        (2, 9000, 3000, {"output_tokens": 300}),  # prompt processing excluded from the speed figure
        (3, 4000, None, {"input_tokens": 2000, "cache_read_input_tokens": 40000, "output_tokens": 200}),
        (4, 5, None, None),  # a count_tokens call: a null-usage row must not move either figure
    ):
        rec = {"t": now + t, "path": "/v1/messages", "status": 200, "ms": ms, "usage": usage}
        if gen_ms is not None:
            rec["gen_ms"] = gen_ms
        job.record_request(rec)
    assert status.render(owner=_owner()) == "local: ⟳ Qwen3.8-Flash-Next 10s ctx 32% 75 t/s"


def test_my_owner_walks_to_a_claude_process_or_none():
    owner = status.my_owner()
    if owner is not None:  # running under Claude Code's Bash tool
        comm = open(f"/proc/{owner}/comm").read().strip()
        # native binary, or an npm install running as node with a claude entry (story 1.9a row 5)
        assert comm == "claude" or (
            comm == "node"
            and any(os.path.basename(a) == "claude"
                    for a in open(f"/proc/{owner}/cmdline").read().split("\0") if a)
        )


def test_my_owner_is_the_dbs_shared_walk(monkeypatch):
    """Story 1.9 item 9: my_owner is db.session_pid with no argument — the row and the line
    finally compute the same thing, by construction."""
    monkeypatch.setattr(db, "session_pid", lambda start_pid=None: 9999)
    assert status.my_owner() == 9999


def test_open_records_the_walked_session_pid(monkeypatch, tmp_path):
    """The server row's owner is the walk's answer — the pid of the Claude Code session the
    server runs under — falling back to the parent pid when there is no claude ancestor
    (story 1.9 item 9). Under `uv run localagents` the parent is uv, so the two views used
    never to match and the session's own jobs rendered as "+N other"."""
    monkeypatch.setattr(db, "session_pid", lambda start_pid=None: 4242)
    store = _store(monkeypatch, tmp_path)
    srv = store.conn.execute("SELECT pid, owner FROM mcp_servers WHERE id = ?", (store.server_id,)).fetchone()
    assert srv["pid"] == os.getpid() and srv["owner"] == 4242
    monkeypatch.setattr(db, "session_pid", lambda start_pid=None: None)
    other = jobs.JobStore()
    srv = other.conn.execute("SELECT owner FROM mcp_servers WHERE id = ?", (other.server_id,)).fetchone()
    assert srv["owner"] == os.getppid()
