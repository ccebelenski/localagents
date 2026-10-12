"""A blocking wait must emit MCP progress notifications over the real protocol.

Claude Code aborts a tool call that is silent for its idle window (30 min on stdio), and
backgrounds a call after two minutes; both rely on these notifications actually leaving the
server, which an in-process call of the tool function can't show. This drives the MCPServer
over in-memory streams with the real ClientSession.
"""


import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from localagents import jobs, server


def _job():
    return server.store.new(
        task="t", model="m", endpoint="e", base_url="http://e:8080",
        cwd="/tmp", run_cwd="/tmp", isolation="none",
    )


def test_wait_job_sends_progress_notifications(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "PROGRESS_INTERVAL_S", 0.05)
    job = _job()
    seen = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async def main():
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            low = server.mcp._lowlevel_server
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    low.run, server_streams[0], server_streams[1],
                    low.create_initialization_options(), True,
                )
                async with ClientSession(*client_streams) as cs:
                    await cs.initialize()

                    async def finish():
                        await anyio.sleep(0.3)
                        job.status = "succeeded"
                        job._done.set()

                    tg.start_soon(finish)
                    res = await cs.call_tool(
                        "wait_job", {"job_id": job.id, "timeout_s": 5}, progress_callback=on_progress
                    )
                tg.cancel_scope.cancel()
            return res

    res = anyio.run(main)
    assert not res.is_error
    assert res.structured_content["status"] == "succeeded"
    assert len(seen) >= 3, seen
    assert all("turns" in (m or "") for _, _, m in seen)
