"""Story 2.3 item 2: a down-recent endpoint "names a job that was running on it when it
dropped" (monitor spec §4). ``running_job_at`` is a read-only query over the MCP server's
``jobs`` table; the jobs here are seeded through ``JobStore.new`` + ``changed()`` over a real
temp database (conftest points LOCALAGENTS_DB at this test's directory)."""
import pytest

from localagents import jobs
from localagents.monitor import jobs as monitor_jobs

T = 1793288065.0


@pytest.fixture
def seeded():
    """Two endpoints: ai1 carries a finished-before-T job and one running across T; ai2
    carries only a lost job whose finished_at is after T."""
    store = jobs.JobStore()

    def new(endpoint):
        return store.new(task="t", model="x/Qwen3.8-27B", endpoint=endpoint,
                         base_url=f"http://{endpoint}:8080", backend="strata", host=endpoint,
                         cwd="/tmp", run_cwd="/tmp", isolation="none")

    a = new("ai1")  # finished before T
    a.status, a.started_at, a.finished_at = "succeeded", T - 100, T - 50
    a.changed()
    b = new("ai1")  # running across T
    b.status, b.started_at = "running", T - 10
    b.changed()
    d = new("ai2")  # lost, finished_at after T
    d.status, d.started_at, d.finished_at = "lost", T - 5, T + 10
    d.changed()
    return store.conn, a, b, d


def test_the_running_job_is_returned(seeded):
    """started_at <= T and finished_at NULL: the job running across T is named; the one
    finished before T is not."""
    conn, a, b, _ = seeded
    row = monitor_jobs.running_job_at(conn, a.endpoint_id, T)
    assert row is not None and row["id"] == b.id and row["status"] == "running"


def test_no_job_running_yields_none(seeded):
    """A time before anything started: none."""
    conn, a, _, _ = seeded
    assert monitor_jobs.running_job_at(conn, a.endpoint_id, T - 200) is None


def test_a_lost_job_finished_after_t_still_counts(seeded):
    """A lost job whose finished_at is after T was still running when the endpoint dropped:
    it is named."""
    conn, _, _, d = seeded
    row = monitor_jobs.running_job_at(conn, d.endpoint_id, T)
    assert row is not None and row["id"] == d.id and row["status"] == "lost"
