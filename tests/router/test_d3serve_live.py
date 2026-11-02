"""router 4: the live d3 contract, against a running ``localagents d3 serve``. Skipped unless
LOCALAGENTS_D3_URL points at one (the unit tests are the default; the router entry would
probe exactly this URL's /health, so this is the contract it depends on)."""

import os

import httpx
import pytest

# d3-mini answers in under a second on a GPU (the spike); on a CPU box one brief is
# minutes, so the timeout is generous — and the test is skipped without a server anyway
TIMEOUT_S = 600


@pytest.mark.live
def test_health_and_one_noul_question():
    url = os.environ.get("LOCALAGENTS_D3_URL")
    if not url:
        pytest.skip("LOCALAGENTS_D3_URL is not set (no running d3 server)")
    base = url.rstrip("/")
    with httpx.Client() as c:
        r = c.get(base + "/health", timeout=10)
        assert r.status_code == 200
        health = r.json()
        assert health["ok"] is True
        r = c.post(base + "/v1/systemone", timeout=TIMEOUT_S, json={
            "model": health.get("model"),
            "state": "Write a python function that reverses a string.",
            "questions": {"codeless": {"type": "noul",
                                       "instructions": "does the brief require the agent to write no code at all?"}},
        })
        assert r.status_code == 200
        answer = r.json()["answers"]["codeless"]
        assert answer["type"] == "noul"
        assert 0.0 <= answer["noul"] <= 1.0
