"""router 4: the live d3 contract, against a running ``localagents d3 serve``. Skipped unless
LOCALAGENTS_D3_URL points at one (the unit tests are the default; the router entry would
probe exactly this URL's /health, so this is the contract it depends on).

The unit tests fake one noul question; this asks what the router actually sends (§4 step 1):
one system_one call with the default definitions document's seven questions. The shapes that
broke once already live are the shapes asserted here — a score answering with probabilities
keyed by level index "0".."4" (the criteria go up as an ordered list: the first live scoring
of real briefs found d3 refuses a dict-shaped score), and the choice answer carrying its
pick. An answer carrying ``error`` is the server refusing a question the client would only
notice as silently-undetermined; here it fails loudly."""

import os

import httpx
import pytest

from localagents.router import definitions

# d3-mini answers in under a second on a GPU (the spike); on a CPU box one brief is
# minutes, so the timeout is generous — and the test is skipped without a server anyway
TIMEOUT_S = 600


@pytest.mark.live
def test_the_default_question_set_answers_in_the_shapes_the_client_parses():
    url = os.environ.get("LOCALAGENTS_D3_URL")
    if not url:
        pytest.skip("LOCALAGENTS_D3_URL is not set (no running d3 server)")
    base = url.rstrip("/")
    asked = definitions.questions(definitions.DEFAULT_DOCUMENT)
    with httpx.Client() as c:
        r = c.get(base + "/health", timeout=10)
        assert r.status_code == 200
        health = r.json()
        assert health["ok"] is True
        r = c.post(base + "/v1/systemone", timeout=TIMEOUT_S, json={
            "model": health.get("model"),
            "state": "Write a python function that reverses a string.",
            "questions": asked,
        })
        assert r.status_code == 200
        answers = r.json()["answers"]
        assert set(answers) == set(asked)
        for name, answer in answers.items():
            assert "error" not in answer, f"{name}: {answer}"
            if asked[name]["type"] == "score":
                # exactly the five level probabilities, keyed by index as a string
                assert set(answer["probabilities"]) == {"0", "1", "2", "3", "4"}, name
            else:  # the document's one choice dimension: language
                assert isinstance(answer["choice"], str) and answer["choice"], name
