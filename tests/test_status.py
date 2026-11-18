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
    mine = _admitted(mine_store)  # a session, as dispatch.new_job writes one (status-sessions)
    mine.status, mine.started_at = "running", now - 30
    mine.changed()
    mine_store.record_endpoint_load({"llamacpp": status.load_sample(1, 1, 0, 1)})
    # a second live server (same pid is fine for the liveness check) owned by another session
    other = jobs.JobStore()
    other.conn.execute("UPDATE mcp_servers SET owner = 4242 WHERE id = ?", (other.server_id,))
    o1 = _admitted(other, model="x/GLM-5.3-Flash-EXL3", endpoint="dgx1", base_url="http://y:8000")
    o1.status, o1.started_at = "running", now - 5
    o1.changed()
    o2 = _new(other, model="x/GLM-5.3-Flash-EXL3", endpoint="dgx1", base_url="http://y:8000")
    o2.status, o2.started_at, o2.finished_at = "succeeded", now - 50, now - 10
    o2.changed()
    other.record_endpoint_load({"dgx1": status.load_sample(2, 2, 1, 1)})
    assert status.render(now=now, owner=_owner()) == "local: ⟳ Qwen3.8-Flash-Next 30s · llamacpp 1/1 sessions · +2 other"
    assert status.render(now=now, owner=4242) == (
        "local: ✓ GLM-5.3-Flash 40s · ⟳ GLM-5.3-Flash 5s · dgx1 1/2 sessions +1 foreign 1 wait · +1 other")
    # no identifiable Claude Code parent: everything is ours, no endpoint load shown for non-running
    monkeypatch.setattr(status, "my_owner", lambda: None)
    # (the `+N other` tail is absent; `+1 foreign` is the dgx1 header's own token)
    assert status.render(now=now).count("·") == 4 and " other" not in status.render(now=now)


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

    a1 = _admitted(store, endpoint="ai1", base_url="http://ai1:8080", model="x/Qwen3.8-27B", context=100000)
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
    store.record_endpoint_load({"ai1": status.load_sample(1, 0, 0, 1)})

    d1 = _admitted(store, endpoint="dgx1-a", base_url="http://dgx1a:8000", model="x/deepseek-v4", context=100000)
    d1.status, d1.started_at = "running", now - 120
    for i in range(3):
        d1.record_tool_call(now - 100 + i, "Bash", f"c{i}", "make")
    d1.changed()
    d1.record_request({"t": now - 5, "path": "/v1/messages", "status": 200, "ms": 3000,
                       "usage": {"input_tokens": 12000, "output_tokens": 120}})
    store.record_endpoint_load({"dgx1-a": "1 run kv 31%"})  # an older server's text: no capacity

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
        "local ai1 · 1/1 sessions",
        "  ⟳ Qwen3.8-27B 4m00s · 12 calls · ctx 41% · 33 t/s · 2 compactions · Bash: uv run pytest -q",
        "  ✓ Qwen3.8-27B 2m00s · 2 files · 3 compactions",
        "local dgx1-a · 1 session",
        "  ⟳ deepseek-v4 2m00s · 3 calls · ctx 12% · 40 t/s",
        "+2 jobs in other sessions",
    ]
    # the one-line form is unchanged by the refactor: the same data renders as before
    # (render_entry's bits are space-joined, endpoint loads and the +N tail as today)
    assert status.render(now=now, owner=me) == (
        "local: ⟳ Qwen3.8-27B 4m00s 12 calls ctx 41% 33 t/s · ✓ Qwen3.8-27B 2m00s 2 files · "
        "⟳ deepseek-v4 2m00s 3 calls ctx 12% 40 t/s · ai1 1/1 sessions · dgx1-a 1 session · +2 other"
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


def test_display_activity_is_one_line_relative_with_the_cut_after():
    """Story 1.11b: display_activity is the row's pure view of a raw last_activity: every run
    of whitespace (a multi-line Bash command included) collapses to one space, paths under
    the job's root lose that prefix (every occurrence), and the cut to MAX_ACTIVITY happens
    AFTER relativising so the file name survives where the raw cut ate the budget on the
    worktree path. None stays None."""
    assert status.display_activity(None, None) is None
    multi = 'Bash: python3 -c "\nimport json\nx = 1\n"'
    assert status.display_activity(multi, None) == 'Bash: python3 -c " import json x = 1 "'
    root = "/home/u/.local/state/localagents/worktrees/claudelocalmcp/wt1"
    assert status.display_activity(f"Read: {root}/tests/test_x.py", root) == "Read: tests/test_x.py"
    # every occurrence of the root; a path outside it untouched; a rootless view untouched too
    assert status.display_activity(f"Grep: {root}/a {root}/b", root) == "Grep: a b"
    assert status.display_activity("Read: /elsewhere/a.py", root) == "Read: /elsewhere/a.py"
    # the raw string is well past 60 before the name; without the root it is cut there —
    # the complaint's behavior — while the relative one is short and the name survives
    raw = f"Read: {root}/tests/test_x.py"
    assert len(raw) > status.MAX_ACTIVITY
    assert status.display_activity(raw, None) == raw[:status.MAX_ACTIVITY]
    raw = f"Read: {root}/tests/deep/nested/test_x.py"
    assert len(raw) > status.MAX_ACTIVITY
    assert status.display_activity(raw, root) == "Read: tests/deep/nested/test_x.py"
    # and even a relative overflow is still cut to the limit
    assert len(status.display_activity("Read: " + "d" * 200, None)) == status.MAX_ACTIVITY


def test_row_activity_is_one_line_relative_to_the_root(monkeypatch, tmp_path):
    """Story 1.11b: collect keeps the RAW last_activity and the job's root (the worktree's
    ``path`` when there is one, else cwd) on the entry, and the row renders the activity as
    one line with paths relative to that root — the multi-line Bash command no longer breaks
    the row into bar lines, and the worktree prefix no longer eats the 60 characters."""
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    root = str(tmp_path / "wt")

    wt = _new(store, endpoint="ai1", base_url="http://ai1:8080", model="x/Qwen3.8-27B")
    wt.status, wt.started_at = "running", now - 30
    wt.last_activity = f'Read: {root}/tests/test_x.py\nBash: python3 -c "\nimport json\n"'
    wt.worktree = {"repo": "/repo", "path": root, "root": root, "branch": "b", "base": "m"}
    wt.changed()

    plain = _new(store, endpoint="ai1", base_url="http://ai1:8080", model="x/Qwen3.8-27B",
                 cwd=str(tmp_path / "repo"))
    plain.status, plain.started_at = "running", now - 20
    plain.last_activity = f"Read: {plain.cwd}/app.py"
    plain.changed()

    entries, _ = status.collect(now=now, conn=store.conn)
    by_id = {e["id"]: e for e in entries}
    assert by_id[wt.id]["last_activity"] == wt.last_activity  # raw: the cut moved to the row
    assert by_id[wt.id]["root"] == root  # the worktree JSON's path
    assert by_id[plain.id]["root"] == str(tmp_path / "repo")  # else the job's cwd
    assert status.render_lines(now=now, owner=_owner()) == [
        "local ai1",
        "  ⟳ Qwen3.8-27B 30s · Read: tests/test_x.py Bash: python3 -c \" import json \"",
        "  ⟳ Qwen3.8-27B 20s · Read: app.py",
    ]


def test_load_summary_shapes():
    assert status.load_summary({"slots": {"total": 2, "busy": 1, "free": 1}}) == "1/2 slots"
    assert status.load_summary({"load": {"requests_running": 2, "requests_waiting": 1, "kv_cache_usage": 0.337}}) == "2 run 1 wait kv 34%"
    assert status.load_summary({"load": {"requests_running": 0, "requests_waiting": 0, "kv_cache_usage": 0.0}}) == "0 run kv 0%"
    assert status.load_summary({"up": True, "models": ["m"]}) is None
    # ruling B (2026-10-29): the prober's load copy carries only the series the document has,
    # so a vLLM probe over a gauge-less document reaches the summary with an empty load
    assert status.load_summary({"up": True, "load": {}}) is None
    assert status.load_summary({"slots": {"total": 4}}) is None  # llama.cpp without /slots enabled


def test_status_line_shows_queued_count(monkeypatch, tmp_path):
    """Router 14a (design §7 "Exposed": the status line shows the queue per real endpoint): a
    ``q<n>`` token after the endpoint's own load when n jobs wait in OUR queue there, distinct
    from the server-side ``wait`` count. Waiting means an entry not yet admitted whose job is
    still queued, from any live session; an admitted entry is a session, not a queued job.
    The sampler's stored text carries no token: the line counts the queue when it renders."""
    from localagents.router import queue

    assert status.QUEUE_LIVE_S == jobs.SERVER_LIVE_S  # restated: status.py never imports jobs
    assert status.load_summary({"slots": {"total": 1, "busy": 1}}, queued=2) == "1/1 slots q2"
    assert status.load_summary({"slots": {"total": 1, "busy": 1}, "load": {"requests_waiting": 3}},
                               queued=1) == "1/1 slots 3 wait q1"
    assert status.load_summary({"load": {"requests_running": 2, "requests_waiting": 1,
                                         "kv_cache_usage": 0.337}}, queued=2) == "2 run 1 wait kv 34% q2"
    assert status.load_summary({"up": True, "models": ["m"]}, queued=2) == "q2"
    assert status.load_summary({"up": True, "models": ["m"]}, queued=0) is None

    store = _store(monkeypatch, tmp_path)
    now = time.time()
    running = _new(store, created_at=now - 30)
    running.status, running.started_at = "running", now - 30
    running.changed()
    queue.enqueue(store.conn, running.id, running.endpoint_id, store.server_id, "normal", forced=True)
    waiting = _new(store, created_at=now - 10)
    queue.enqueue(store.conn, waiting.id, waiting.endpoint_id, store.server_id, "normal")
    admitted = _new(store, created_at=now - 8)  # admitted, its task not started yet: a session
    queue.enqueue(store.conn, admitted.id, admitted.endpoint_id, store.server_id, "normal", forced=True)
    store.record_endpoint_load({"llamacpp": "1/1 slots 1 wait"})
    other = jobs.JobStore()  # another live session's job waits in the same queue
    other.conn.execute("UPDATE mcp_servers SET owner = 4242 WHERE id = ?", (other.server_id,))
    theirs = _new(other, created_at=now - 5)
    queue.enqueue(other.conn, theirs.id, theirs.endpoint_id, other.server_id, "background")

    me = _owner()
    # status-sessions: the admitted job (8s) holds a session and lists before the waiting one
    # (10s); the old-format sample gives no capacity, so the two sessions show without one
    assert status.render(now=now, owner=me) == (
        "local: ⟳ Qwen3.8-Flash-Next 30s · … Qwen3.8-Flash-Next 8s · … Qwen3.8-Flash-Next 10s · "
        "llamacpp 2 sessions 1 wait q2 · +1 other")
    assert status.render_lines(now=now, owner=me)[0] == "local llamacpp · 2 sessions 1 wait q2"

    # no load row (the sampler has not written one): the queue alone still shows, and an
    # endpoint whose jobs of ours are only queued gets its segment on the one-line form too
    store.conn.execute("DELETE FROM endpoint_load")
    running.status, running.finished_at = "succeeded", now - 1
    running.changed()
    line = status.render(now=now, owner=me)
    assert line.endswith(" · llamacpp 1 session q2 · +1 other"), line  # the admitted one's
    assert status.render_lines(now=now, owner=me)[0] == "local llamacpp · 1 session q2"


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


def test_status_collect_excludes_calibration_jobs_from_counts(monkeypatch, tmp_path):
    """router 8 (spec §10 "Transparent"): a calibration probe is a job row shown like any
    job, with `cal` beside its status mark, but it is not traffic — it adds nothing to the
    other-sessions job count, while its own t/s bit still shows on its row."""
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    mine = _new(store)
    mine.status, mine.started_at = "running", now - 30
    mine.record_request({"t": now - 10, "path": "/v1/messages", "status": 200, "ms": 3000,
                         "usage": {"input_tokens": 41000, "output_tokens": 100}})
    mine.changed()
    cal = _new(store, calibration=True, priority="background")
    cal.status, cal.started_at = "running", now - 20
    cal.record_request({"t": now - 8, "path": "/v1/messages", "status": 200, "ms": 3000,
                        "usage": {"input_tokens": 4000, "output_tokens": 120}})
    cal.changed()

    # another session: one ordinary job (traffic) and one calibration probe (not traffic)
    other = jobs.JobStore()
    other.conn.execute("UPDATE mcp_servers SET owner = 4242 WHERE id = ?", (other.server_id,))
    o1 = _new(other, model="x/GLM-5.3-Flash-EXL3", endpoint="dgx1", base_url="http://y:8000")
    o1.status, o1.started_at = "running", now - 5
    o1.changed()
    ocal = _new(other, model="x/GLM-5.3-Flash-EXL3", endpoint="dgx1", base_url="http://y:8000",
                calibration=True, priority="background")
    ocal.status, ocal.started_at = "running", now - 4
    ocal.changed()

    entries, _ = status.collect(now=now, conn=store.conn)
    by_id = {e["id"]: e for e in entries}
    assert by_id[cal.id]["calibration"] is True
    assert by_id[ocal.id]["calibration"] is True
    assert by_id[mine.id]["calibration"] is False

    assert status.render(now=now, owner=_owner()) == (
        "local: ⟳ Qwen3.8-Flash-Next 30s 33 t/s · ⟳ cal Qwen3.8-Flash-Next 20s 40 t/s · +1 other")
    assert status.render_lines(now=now, owner=_owner()) == [
        "local llamacpp",
        "  ⟳ Qwen3.8-Flash-Next 30s · 33 t/s",
        "  ⟳ cal Qwen3.8-Flash-Next 20s · 40 t/s",
        "+1 job in another session",
    ]


def test_every_status_has_a_mark_and_the_monitor_legend_is_the_same_map():
    """Router 11: ``dropped`` joins the statuses, so the mark map covers every status the
    jobs CHECK admits (queued, running and every terminal one) and the monitor's browser copy
    (``job.js``'s ``var MARK``, "the status bar's marks (status.py MARK)") says the same thing
    character for character."""
    import json
    import re

    assert set(status.MARK) == {"queued", "running", *jobs.TERMINAL_STATUSES}
    js = (Path(status.__file__).parent / "monitor" / "static" / "job.js").read_text()
    literal = re.search(r"var MARK = (\{[^}]*\});", js).group(1)
    legend = json.loads(re.sub(r"(\w+):", r'"\1":', literal))
    assert legend == status.MARK


# --- sessions, not slots (status-sessions story) ------------------------------------------------


def _admitted(store, **kw):
    """A job holding an admitted (forced) queue entry: a session, the way the server writes one
    (router/dispatch.py new_job: the entry goes in with the job)."""
    from localagents.router import queue

    job = _new(store, **kw)
    queue.enqueue(store.conn, job.id, job.endpoint_id, store.server_id, "normal", forced=True)
    return job


def test_header_counts_the_running_session_not_the_idle_slot(monkeypatch, tmp_path):
    """The operator's live line (2026-11-15): ``local ai1 · 0/1 slots q2`` beside a running
    job. The slot reading is instantaneous (idle while the session runs a tool); admission
    counts sessions, so the header does too. The stored sample here is the old text a server
    on today's code writes: it carries no session capacity, so the header shows our one
    session without a denominator, never the slot reading."""
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    job = _admitted(store, endpoint="ai1", base_url="http://ai1:8080")
    job.status, job.started_at = "running", now - 60
    job.changed()
    store.record_endpoint_load({"ai1": "0/1 slots"})
    assert status.render_lines(now=now, owner=_owner())[0] == "local ai1 · 1 session"


def test_a_long_queued_job_prints_below_the_running_job_it_waits_behind(monkeypatch, tmp_path):
    """The operator's live line: a job queued 2h39m ago printed ABOVE the running job, because
    collect sorted every row by started_at and a queued job's falls back to created_at.
    Running rows come first; queued rows follow in queue order."""
    from localagents.router import queue

    store = _store(monkeypatch, tmp_path)
    now = time.time()
    waiting = _new(store, endpoint="ai1", base_url="http://ai1:8080", created_at=now - 9540)
    queue.enqueue(store.conn, waiting.id, waiting.endpoint_id, store.server_id, "normal", now=now - 9540)
    running = _admitted(store, endpoint="ai1", base_url="http://ai1:8080", created_at=now - 3120)
    running.status, running.started_at = "running", now - 3116
    running.changed()
    rows = status.render_lines(now=now, owner=_owner())
    assert rows[1:] == ["  ⟳ Qwen3.8-Flash-Next 51m56s", "  … Qwen3.8-Flash-Next 2h39m"], rows


def test_the_operators_snippet_renders_sessions_and_queue_order(monkeypatch, tmp_path):
    """The operator's live snippet (2026-11-15), rebuilt: ai1 runs one session with two jobs
    queued behind it; dgx1-a runs one with one queued. With the sampler's new sample the
    header is our live sessions over the session capacity plus our queue (``1/1 sessions
    q2``), and the rows are the running one first, then the queued ones in queue order. The
    one-line form shows the same sessions text for each endpoint segment."""
    from localagents.router import queue

    store = _store(monkeypatch, tmp_path)
    now = time.time()
    qwen = "unsloth/qwen3.8-flash-next-unsloth-ud-q4_k_xl"
    deep = "DeepSeek-V4.1-Flash-1017"

    def waiting(endpoint, model, age):
        job = _new(store, endpoint=endpoint, base_url=f"http://{endpoint}:8080", model=model,
                   created_at=now - age)
        queue.enqueue(store.conn, job.id, job.endpoint_id, store.server_id, "normal", now=now - age)
        return job

    waiting("ai1", qwen, 2 * 3600 + 39 * 60)
    run1 = _admitted(store, endpoint="ai1", base_url="http://ai1:8080", model=qwen, created_at=now - 3200)
    run1.status, run1.started_at = "running", now - (51 * 60 + 56)
    run1.changed()
    waiting("ai1", qwen, 13 * 60)
    run2 = _admitted(store, endpoint="dgx1-a", base_url="http://dgx1-a:8080", model=deep,
                     created_at=now - 900)
    run2.status, run2.started_at = "running", now - (13 * 60 + 25)
    run2.changed()
    waiting("dgx1-a", deep, 8 * 60 + 38)
    # what the sampler writes now: ai1 one slot, idle between requests; dgx1-a a pooled box of
    # two sessions with no request generating
    store.record_endpoint_load({"ai1": status.load_sample(1, 0, 0, 1),
                                "dgx1-a": status.load_sample(2, 0, 0, 1)})

    me = _owner()
    assert status.render_lines(now=now, owner=me) == [
        "local ai1 · 1/1 sessions q2",
        "  ⟳ qwen3.8-flash-next-unsloth-ud-q4_k_xl 51m56s",
        "  … qwen3.8-flash-next-unsloth-ud-q4_k_xl 2h39m",
        "  … qwen3.8-flash-next-unsloth-ud-q4_k_xl 13m00s",
        "local dgx1-a · 1/2 sessions q1",
        "  ⟳ DeepSeek-V4.1-Flash-1017 13m25s",
        "  … DeepSeek-V4.1-Flash-1017 8m38s",
    ]
    assert status.render(now=now, owner=me) == (
        "local: ⟳ qwen3.8-flash-next-unsloth-ud-q4_k_xl 51m56s · ⟳ DeepSeek-V4.1-Flash-1017 13m25s · "
        "… qwen3.8-flash-next-unsloth-ud-q4_k_xl 2h39m · … qwen3.8-flash-next-unsloth-ud-q4_k_xl 13m00s · "
        "… DeepSeek-V4.1-Flash-1017 8m38s · ai1 1/1 sessions q2 · dgx1-a 1/2 sessions q1")


def test_foreign_use_shows_only_when_present():
    """``+N foreign`` is ``Capacity.admits``' arithmetic, ``max(0, running - ours)``: the
    server's live requests beyond ours. A new sample carries ours from its own tick, so a
    request count 10 s old is never set against a session count from now (a session that
    just ended must not turn its own last request into a foreign one); an old-format sample
    has no sample-time count and uses the live one."""
    s = status.read_sample
    assert status.sessions_text(s(status.load_sample(1, 1, 0, 1)), 1) == "1/1 sessions"
    assert status.sessions_text(s(status.load_sample(2, 2, 0, 1)), 1) == "1/2 sessions +1 foreign"
    assert status.sessions_text(s(status.load_sample(2, 3, 0, 1)), 1) == "1/2 sessions +2 foreign"
    # between requests ours is a session and no request runs: nothing foreign, never negative
    assert status.sessions_text(s(status.load_sample(1, 0, 0, 1)), 1) == "1/1 sessions"
    # the sample's tick saw our one request; our session has since ended: still not foreign
    assert status.sessions_text(s(status.load_sample(1, 1, 0, 1)), 0) == "0/1 sessions"
    # an old-format sample: the live count is the only one there is
    assert status.sessions_text(s("1/1 slots"), 1) == "1 session"
    assert status.sessions_text(s("2 run kv 3%"), 1) == "1 session +1 foreign"
    assert status.sessions_text(s("1/1 slots"), 0) == "+1 foreign"


def test_server_wait_stays_only_when_nonzero_and_kv_and_slots_no_longer_headline():
    """The server's own queue depth stays (strata and vLLM), only when non-zero; the slot
    reading, the bare ``N run`` and ``kv N%`` no longer appear in the header, from either
    sample shape."""
    s = status.read_sample
    assert status.sessions_text(s(status.load_sample(1, 1, 2, 1)), 1, 3) == "1/1 sessions 2 wait q3"
    assert status.sessions_text(s(status.load_sample(1, 1, 0, 1)), 1, 3) == "1/1 sessions q3"
    assert status.sessions_text(s("1/1 slots 2 wait"), 1) == "1 session 2 wait"
    assert status.sessions_text(s("1 run 1 wait kv 34%"), 1) == "1 session 1 wait"
    for text in ("0/1 slots", "1/1 slots 2 wait", "0 run kv 0%", "2 run 1 wait kv 34%"):
        line = status.sessions_text(s(text), 1) or ""
        assert "slots" not in line and "run" not in line and "kv" not in line, (text, line)


def test_a_read_that_could_not_see_never_says_free():
    """No capacity at render time — no fresh sample, the endpoint down (the sampler writes
    sessions null), an old-format sample, or a sample this reader cannot trust — shows the
    session count without a denominator, and nothing when it is zero: never ``0/N
    sessions`` for an endpoint the sampler could not read."""
    down = status.read_sample(status.load_sample(None, 0, 0, 1))
    assert down == {"sessions": None, "running": 0, "waiting": 0, "ours": 1}
    for sample in (None, down, status.read_sample("0/1 slots"), status.read_sample("0 run kv 0%")):
        assert status.sessions_text(sample, 1) == "1 session", sample
        assert status.sessions_text(sample, 2) == "2 sessions", sample
        assert status.sessions_text(sample, 0) is None, sample
        assert status.sessions_text(sample, 0, 2) == "q2", sample
    # untrusted text reads as no sample at all, never raises
    for text in (None, "", "{", "{}", "[1]", '{"v": 1, "sessions": 1, "running": 0, "waiting": 0, "ours": 0}',
                 '{"v": 2, "sessions": true, "running": 0, "waiting": 0, "ours": 0}',
                 '{"v": 2, "sessions": -1, "running": 0, "waiting": 0, "ours": 0}',
                 '{"v": 2, "sessions": 1, "running": "0", "waiting": 0, "ours": 0}',
                 '{"v": 2, "sessions": 1, "running": 0, "waiting": 0}',
                 '{"v": 2, "sessions": 1.5, "running": 0, "waiting": 0, "ours": 0}',
                 "q2", "busy", "slots 1/1"):
        assert status.read_sample(text) is None, text


def test_read_sample_reads_every_old_load_summary_shape():
    """The old format's definition is load_summary: the reader takes the live requests (busy
    slots, or vLLM's ``run``) and the server's wait from each of its shapes, and never a
    capacity or a sample-time count of ours (an old sample has neither)."""
    cases = [
        ({"slots": {"total": 2, "busy": 1}}, 1, 0),
        ({"slots": {"total": 1, "busy": 1}, "load": {"requests_waiting": 3}}, 1, 3),
        ({"load": {"requests_running": 2, "requests_waiting": 1, "kv_cache_usage": 0.337}}, 2, 1),
        ({"load": {"requests_running": 0, "requests_waiting": 0, "kv_cache_usage": 0.0}}, 0, 0),
        ({"load": {"requests_running": 12}}, 12, 0),
    ]
    for probe, running, waiting in cases:
        text = status.load_summary(probe)
        assert status.read_sample(text) == {"sessions": None, "running": running,
                                            "waiting": waiting, "ours": None}, text


def test_old_format_and_stale_samples_in_the_table(monkeypatch, tmp_path):
    """Other server versions write the old text into the same endpoint_load table, and the
    sampler only probes endpoints of RUNNING sessions, so a header may have an old sample, a
    stale one (older than 60 s: ignored), or none. Each renders without a denominator."""
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    job = _admitted(store, endpoint="ai1", base_url="http://ai1:8080")
    job.status, job.started_at = "running", now - 30
    job.changed()
    me = _owner()
    assert status.render_lines(now=now, owner=me)[0] == "local ai1 · 1 session"  # no sample yet
    store.record_endpoint_load({"ai1": "1/1 slots 2 wait"})
    assert status.render_lines(now=now, owner=me)[0] == "local ai1 · 1 session 2 wait"
    store.record_endpoint_load({"ai1": status.load_sample(1, 0, 0, 1)})
    assert status.render_lines(now=now, owner=me)[0] == "local ai1 · 1/1 sessions"
    # the same sample written 61 s ago: stale, so the capacity is no longer known
    store.conn.execute("UPDATE endpoint_load SET updated = ?", (now - 61,))
    assert status.render_lines(now=now, owner=me)[0] == "local ai1 · 1 session"
    # the endpoint went down: the sampler writes no capacity
    store.record_endpoint_load({"ai1": status.load_sample(None, 0, 0, 1)})
    assert status.render_lines(now=now, owner=me)[0] == "local ai1 · 1 session"
    # a corrupt row is no sample, and the line still renders
    store.conn.execute("UPDATE endpoint_load SET load = '{not json'")
    assert status.render_lines(now=now, owner=me)[0] == "local ai1 · 1 session"


def test_session_count_is_the_queues_occupancy(monkeypatch, tmp_path):
    """The header's session count is router/queue.py's occupancy set, restated (status.py
    never imports the queue): the two FROM/liveness fragments are pinned equal to queue.py's,
    and over a store with every kind of entry the per-name counts equal queue.occupancy per
    endpoint: admitted running and admitted-not-started count, forced included; a waiting
    entry, a finished job, a job with no entry and an entry whose owner stopped or stopped
    beating do not."""
    from localagents.router import queue

    assert status._LIVE_FROM == queue.LIVE_FROM
    assert status._LIVE == queue._LIVE
    assert status.QUEUE_LIVE_S == queue.SERVER_LIVE_S

    store = _store(monkeypatch, tmp_path)
    now = time.time()
    a_run = _admitted(store, endpoint="a", base_url="http://a:1")
    a_run.status, a_run.started_at = "running", now - 30
    a_run.changed()
    _admitted(store, endpoint="a", base_url="http://a:1")  # admitted, task not started
    w = _new(store, endpoint="a", base_url="http://a:1")
    queue.enqueue(store.conn, w.id, w.endpoint_id, store.server_id, "normal")  # waiting
    done = _admitted(store, endpoint="a", base_url="http://a:1")
    done.status, done.started_at, done.finished_at = "succeeded", now - 50, now - 5
    done.changed()
    bare = _new(store, endpoint="a", base_url="http://a:1")  # no entry: counted by nothing
    bare.status, bare.started_at = "running", now - 10
    bare.changed()
    b_run = _admitted(store, endpoint="b", base_url="http://b:1")
    b_run.status, b_run.started_at = "running", now - 30
    b_run.changed()
    gone = jobs.JobStore()  # a server that stopped beating: its session no longer counts
    g = _admitted(gone, endpoint="b", base_url="http://b:1")
    g.status, g.started_at = "running", now - 30
    g.changed()
    gone.conn.execute("UPDATE mcp_servers SET updated = ? WHERE id = ?", (now - 120, gone.server_id))
    stopped = jobs.JobStore()
    st = _admitted(stopped, endpoint="b", base_url="http://b:1")
    st.status, st.started_at = "running", now - 30
    st.changed()
    stopped.conn.execute("UPDATE mcp_servers SET stopped = ? WHERE id = ?", (now, stopped.server_id))

    got = {r["name"]: r["n"] for r in store.conn.execute(status._SESSIONS_SQL, (now - status.QUEUE_LIVE_S,))}
    want = {name: queue.occupancy(store.conn, [eid], now)
            for name, eid in (("a", a_run.endpoint_id), ("b", b_run.endpoint_id))}
    assert got == want == {"a": 2, "b": 1}


def test_queued_rows_follow_queue_order(monkeypatch, tmp_path):
    """Queued rows print in queue order (router/queue.py ``_WAITING_SQL``'s ORDER BY: priority
    rank, arrival, the entry's rowid), after the running and finished rows: an urgent job that
    arrived last prints first among the waiting, and the waiting order equals
    queue.waiting's. An admitted job whose task has not started holds a session and prints
    ahead of every waiting one; a queued job with no entry prints last."""
    from localagents.router import queue

    store = _store(monkeypatch, tmp_path)
    now = time.time()
    kw = {"endpoint": "ai1", "base_url": "http://ai1:8080"}

    def wait(priority, age, model):
        job = _new(store, model=model, created_at=now - age, **kw)
        queue.enqueue(store.conn, job.id, job.endpoint_id, store.server_id, priority, now=now - age)
        return job

    bg = wait("background", 500, "x/bg-old")
    n1 = wait("normal", 400, "x/normal-old")
    n2 = wait("normal", 300, "x/normal-new")
    ur = wait("urgent", 100, "x/urgent-late")
    tie = wait("normal", 300, "x/normal-tie")  # same arrival as n2: the entry's rowid breaks it
    bare = _new(store, model="x/no-entry", created_at=now - 9999, **kw)
    adm = _admitted(store, model="x/admitted", created_at=now - 5, **kw)
    fin = _admitted(store, model="x/finished", created_at=now - 600, **kw)
    fin.status, fin.started_at, fin.finished_at = "succeeded", now - 590, now - 20
    fin.changed()
    run = _admitted(store, model="x/running", created_at=now - 60, **kw)
    run.status, run.started_at = "running", now - 50
    run.changed()

    entries, _ = status.collect(now=now, conn=store.conn)
    order = [e["id"] for e in entries]
    assert order == [fin.id, run.id, adm.id, ur.id, n1.id, n2.id, tie.id, bg.id, bare.id]
    waiting_ids = [e["id"] for e in entries if e["status"] == "queued" and e["id"] not in (adm.id, bare.id)]
    assert waiting_ids == [r["job_id"] for r in queue.waiting(store.conn, ur.endpoint_id, now)]
    assert [line.split()[1] for line in status.render_lines(now=now, owner=_owner())[1:]] == [
        "finished", "running", "admitted", "urgent-late", "normal-old", "normal-new",
        "normal-tie", "bg-old", "no-entry"]


def test_collect_on_a_long_history_is_fast(monkeypatch, tmp_path):
    """status.py's promise: under 100 ms. A store with 3000 finished jobs, each keeping the
    admitted entry it ran under (entries persist, ruling 184), plus a running session and a
    waiting job: collect stays well inside the budget (generous here: CI boxes vary)."""
    from localagents.router import queue

    store = _store(monkeypatch, tmp_path)
    now = time.time()
    eid = _new(store).endpoint_id
    c = store.conn
    c.execute("BEGIN")
    for i in range(3000):
        c.execute("INSERT INTO jobs (id, mcp_server_id, endpoint_id, status, created_at, started_at,"
                  " finished_at, updated) VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?)",
                  (f"h{i}", store.server_id, eid, i, i, i + 1, i + 1))
        c.execute("INSERT INTO queue_entries (job_id, endpoint_id, owner_server, priority_rank,"
                  " enqueued, admitted) VALUES (?, ?, ?, 1, ?, ?)", (f"h{i}", eid, store.server_id, i, i))
    c.execute("COMMIT")
    run = _admitted(store)
    run.status, run.started_at = "running", now - 30
    run.changed()
    w = _new(store)
    queue.enqueue(store.conn, w.id, w.endpoint_id, store.server_id, "normal")
    store.record_endpoint_load({"llamacpp": status.load_sample(1, 0, 0, 1)})
    t0 = time.perf_counter()
    entries, loads = status.collect(now=now)
    took = time.perf_counter() - t0
    assert loads == {"llamacpp": "1/1 sessions q1"} and len(entries) == 3  # with the eid probe job
    assert took < 0.1, took


def test_render_lines_scoped_to_opencode_sessions(monkeypatch, tmp_path):
    """Epic 6.12: OpenCode's status block scopes by the jobs' owner_session — the open session
    and its relays — not by the server's owner pid; everything else folds into +N as ever."""
    store = _store(monkeypatch, tmp_path)
    now = time.time()
    rows = []
    for sess in ("ses_main", "ses_relay", "ses_elsewhere", None):
        j = _new(store, endpoint="ai9", base_url="http://ai9:8080", model="x/Qwen3.8-27B")
        j.status, j.started_at = "running", now - 60
        j.changed()
        store.conn.execute("UPDATE jobs SET owner_session = ? WHERE id = ?", (sess, j.id))
        rows.append(j)
    lines = status.render_lines(now=now, sessions=frozenset({"ses_main", "ses_relay"}))
    assert lines[0] == "local ai9" and len([ln for ln in lines if ln.startswith("  ⟳")]) == 2
    assert lines[-1] == "+2 jobs in other sessions"
    assert status.render_lines(now=now, sessions=frozenset({"ses_none"})) == ["+4 jobs in other sessions"]
