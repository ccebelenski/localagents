"""router 3 (docs/superpowers/specs/2026-11-02-router-design.md §4 steps 1-2, §6, §14): the d3
decision-model client — recorded systemone answers mapped to brief scores, the confidence rule
that marks an axis undetermined, truncation of an over-long brief, pointer-brief resolution, and
the model_unavailable / brief_unresolved responses. No routing here: selection (§6) reads a
BriefScores and decides. The d3 server is a MockTransport; the answer shapes are the recorded
d3-mini fixture (tests/fixtures/router/d3_answers.json)."""

import asyncio
import copy
import json
import pathlib

import httpx
import pytest

from localagents.router import decision, definitions

FIXTURE = json.loads((pathlib.Path(__file__).resolve().parent.parent
                      / "fixtures" / "router" / "d3_answers.json").read_text())
URL = "http://d3.test:8080"


def _defs(version=3):
    return definitions.Definitions(version, copy.deepcopy(definitions.DEFAULT_DOCUMENT))


def _score_answer(confidence, probabilities=None):
    """The fixture's `difficulty` shape (top band "3", score float 3.03...), re-banded."""
    answer = copy.deepcopy(FIXTURE["answers"]["difficulty"])
    answer["confidence"] = confidence
    if probabilities is not None:
        answer["probabilities"] = probabilities
    return answer


def _choice_answer(confidence, choice):
    answer = copy.deepcopy(FIXTURE["answers"]["kind"])
    answer["confidence"] = confidence
    answer["choice"] = choice
    return answer


def _score(client, text="fix the flaky test", defs=None, **kw):
    async def drive():
        try:
            return await decision.score(client, URL, "d3-mini", text, defs or _defs(), **kw)
        finally:
            await client.aclose()
    return asyncio.run(drive())


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _served(answers):
    return _client(lambda request: httpx.Response(200, json={"answers": answers, "usage": {}}))


def test_score_maps_recorded_answers_to_values_and_undetermined():
    noul = copy.deepcopy(FIXTURE["answers"]["read_only"])  # 0.026: confidence 0.948 > 0.2
    answers = {
        # the fixture's score shape, confidence lifted above the score bar: the top band as an int
        "software_dev": _score_answer(0.55),
        # the shape's own confidence (0.307) sits below the score bar 0.4: undetermined
        "tool_use": _score_answer(0.30701448295194933),
        # the fixture's choice shape, lifted above the choice bar: the option string
        "language": _choice_answer(0.9, "python"),
        "computer_use": _score_answer(0.6, {"0": 0.05, "1": 0.45, "2": 0.2, "3": 0.2, "4": 0.1}),
        # a 2-vs-3 tie goes to the higher band: under-scoring sends a hard brief to a weak model
        "complexity": _score_answer(0.6, {"0": 0.1, "1": 0.05, "2": 0.35, "3": 0.35, "4": 0.15}),
        # the fixture's noul shape against a score question: the answer's own type is what
        # parses; noul 0.026 is far from yes, and its derived confidence 0.948 clears the bar
        "specification": noul,
        "context_load": _score_answer(0.6, {"0": 0.5, "1": 0.2, "2": 0.1, "3": 0.1, "4": 0.1}),
    }
    scores = _score(_served(answers), defs=_defs(version=7))

    assert scores.values == {"software_dev": 3, "language": "python", "computer_use": 1,
                             "complexity": 3, "specification": False, "context_load": 0}
    assert isinstance(scores.values["software_dev"], int) and not isinstance(scores.values["software_dev"], bool)
    assert scores.undetermined == {"tool_use"}
    # the bar is recorded even for the axis it dropped (the ticket cites confidences, §4 step 2)
    assert scores.confidences["tool_use"] == pytest.approx(0.30701448295194933)
    assert scores.confidences["specification"] == pytest.approx(abs(0.02602121156390871 - 0.5) * 2)
    assert scores.defs_version == 7
    assert not scores.truncated
    # the score float and per-option probabilities ride in raw for the ticket
    assert scores.raw["software_dev"]["score"] == 3.0347104087718186


def test_language_none_is_undetermined():
    answers = {name: _score_answer(0.8) for name in definitions.DIMENSIONS}
    answers["language"] = _choice_answer(0.95, "none")
    scores = _score(_served(answers))
    # none or uncertain language is neutral (§6): undetermined, out of values, confidence kept
    assert "language" in scores.undetermined
    assert "language" not in scores.values
    assert scores.confidences["language"] == 0.95


def test_long_brief_is_truncated_and_marked():
    seen = {}

    def handler(request):
        seen["state"] = json.loads(request.content)["state"]
        return httpx.Response(200, json={"answers": {}, "usage": {}})

    scores = _score(_client(handler), "x" * 100_001, max_tokens=25_000)
    assert len(seen["state"]) == 100_000
    assert scores.truncated is True

    scores = _score(_client(handler), "x" * 99_999, max_tokens=25_000)
    assert scores.truncated is False


def test_request_wire_shape_is_what_the_server_reads():
    """The POSTed body is the wire contract (spec §4 step 1): exactly the three fields the
    server route requires, the question set straight from the document the scores will be
    recorded against, and a score's criteria as an ordered list of the five level texts —
    a dict there is refused by the live server (invalid_question, the first live scoring of
    real briefs), which would make every axis silently undetermined."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"answers": {}, "usage": {}})

    defs = _defs(version=9)
    _score(_client(handler), text="fix the flaky test", defs=defs)

    body = seen["body"]
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == "d3-mini"
    assert body["state"] == "fix the flaky test"
    assert body["questions"] == definitions.questions(defs.document)
    for name, q in body["questions"].items():
        if q["type"] == "score":
            assert isinstance(q["criteria"], list) and len(q["criteria"]) == 5, name
            assert all(isinstance(text, str) and text for text in q["criteria"]), name


def test_connect_error_is_model_unavailable_down():
    def handler(request):
        raise httpx.ConnectError("Connection refused", request=request)

    with pytest.raises(decision.DecisionUnavailable) as excinfo:
        _score(_client(handler))
    assert excinfo.value.reason == "down"
    d = excinfo.value.to_dict()
    assert d["error"] == "model_unavailable"
    assert d["reason"] == "down"
    assert d["guidance"] == "name a model, or check the d3 server"
    assert d["message"]


def test_timeout_is_model_unavailable_slow():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(decision.DecisionUnavailable) as excinfo:
        _score(_client(handler), timeout_s=1.5)
    assert excinfo.value.reason == "slow"
    assert excinfo.value.to_dict()["error"] == "model_unavailable"


def test_non_200_is_bad_response():
    with pytest.raises(decision.DecisionUnavailable) as excinfo:
        _score(_client(lambda request: httpx.Response(503, text="nope")))
    assert excinfo.value.reason == "bad_response"
    assert excinfo.value.to_dict()["message"] == f"the d3 server at {URL} answered 503"
    # a 200 whose body carries no answers is the same failure
    with pytest.raises(decision.DecisionUnavailable) as excinfo:
        _score(_client(lambda request: httpx.Response(200, json={"usage": {}})))
    assert excinfo.value.reason == "bad_response"


def test_answer_with_error_is_bad_response():
    """A question the server refused (its answer carries ``error``) is not an answer
    missing: silently marking that axis undetermined would hide a broken document, so the
    refusal rides out as bad_response with the server's sentence."""
    answers = {name: _score_answer(0.8) for name in definitions.DIMENSIONS}
    answers["tool_use"] = {"error": "invalid_question", "message": "criteria must be five levels"}
    with pytest.raises(decision.DecisionUnavailable) as excinfo:
        _score(_served(answers))
    assert excinfo.value.reason == "bad_response"
    assert excinfo.value.to_dict()["message"] == (
        "d3 refused the question tool_use: invalid_question: criteria must be five levels")

    # a refusal with no sentence still names the question and the error, no dangling colon
    answers = {name: _score_answer(0.8) for name in definitions.DIMENSIONS}
    answers["language"] = {"error": "bad_request"}
    with pytest.raises(decision.DecisionUnavailable) as excinfo:
        _score(_served(answers))
    assert excinfo.value.to_dict()["message"] == "d3 refused the question language: bad_request"


def test_json_error_body_is_reported_in_the_message():
    """The server answers its own failures in JSON (d3serve's model_error 500); the client
    carries the error and the server's sentence into the message the operator reads, rather
    than a bare status code."""
    def handler(request):
        return httpx.Response(500, json={"error": "model_error", "type": "RuntimeError",
                                         "message": "CUDA out of memory"})

    with pytest.raises(decision.DecisionUnavailable) as excinfo:
        _score(_client(handler))
    assert excinfo.value.reason == "bad_response"
    assert excinfo.value.to_dict()["message"] == (
        f"the d3 server at {URL} answered 500: model_error: CUDA out of memory")

    # an error body without a message sentence still names the error, with no dangling colon
    with pytest.raises(decision.DecisionUnavailable) as excinfo:
        _score(_client(lambda request: httpx.Response(413, json={"error": "state too long"})))
    assert excinfo.value.to_dict()["message"] == f"the d3 server at {URL} answered 413: state too long"


def test_resolve_brief_reads_a_pointer(tmp_path):
    (tmp_path / "briefs").mkdir()
    (tmp_path / "briefs" / "job.md").write_text("do the thing\nquietly\n")
    assert decision.resolve_brief("cat briefs/job.md and follow it", str(tmp_path)) == "do the thing\nquietly\n"
    # three non-empty lines is still a pointer; an absolute path needs no cwd
    brief = f"cat {tmp_path / 'briefs' / 'job.md'}\nthen follow it\nand report"
    assert decision.resolve_brief(brief, "/nonexistent-cwd") == "do the thing\nquietly\n"


def test_resolve_brief_leaves_a_normal_brief_alone(tmp_path):
    (tmp_path / "notes.txt").write_text("decoy\n")
    long_brief = "cat notes.txt is not what I mean\n" + "\n".join(f"line {i}" for i in range(10))
    assert decision.resolve_brief(long_brief, str(tmp_path)) == long_brief
    plain = "fix the flaky test in tests/test_probe.py"
    assert decision.resolve_brief(plain, str(tmp_path)) == plain


def test_resolve_brief_accepts_comma_backticks_quotes_and_tilde(tmp_path, monkeypatch):
    """How operators write the pointer: the path wrapped in backticks or quotes, trailed by
    the sentence's own comma, and with ~/ for home. All four reach the same file."""
    home = tmp_path / "home"
    (home / "briefs").mkdir(parents=True)
    (home / "briefs" / "job.md").write_text("do the thing\n")
    monkeypatch.setenv("HOME", str(home))  # expanduser is environment-read on posix
    for form in (
        "cat `briefs/job.md`, and follow it",  # backticks, comma outside them
        "cat 'briefs/job.md' and follow it",   # single quotes
        'cat "briefs/job.md" and follow it',   # double quotes
        "cat ~/briefs/job.md and follow it",   # home, expanded after the strips
    ):
        assert decision.resolve_brief(form, str(home)) == "do the thing\n", form


def test_resolve_brief_missing_file_is_an_error(tmp_path):
    with pytest.raises(decision.BriefUnresolved) as excinfo:
        decision.resolve_brief("cat nope/gone.md and follow it", str(tmp_path))
    d = excinfo.value.to_dict()
    assert d["error"] == "brief_unresolved"
    assert d["path"] == str(tmp_path / "nope" / "gone.md")
    assert d["path"].endswith("gone.md")
