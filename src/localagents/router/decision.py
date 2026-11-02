"""The d3 decision-model client (docs/superpowers/specs/2026-11-02-router-design.md §4 steps
1-2, §6, §14): resolve a pointer brief, ask d3 one ``system_one`` question set about the text,
and hand back a ``BriefScores`` — values, confidences, the axes the confidence bars dropped,
the truncation mark. No routing lives here: selection (§6) reads a BriefScores and decides,
and an undetermined axis is neutral there, so an answer below its bar is absent from ``values``
rather than guessed at."""

import os
import pathlib
import re
from dataclasses import dataclass

import httpx

from localagents.router.definitions import BANDS, THRESHOLDS, Definitions, questions

GUIDANCE = "name a model, or check the d3 server"
# a pointer brief ("cat <path> and follow it"): the path is the run of non-space after cat
_POINTER = re.compile(r"^\s*cat\s+(\S+)")


@dataclass
class BriefScores:
    """One brief read by d3. ``values`` holds only the determined axes — an int band for a
    score, the option string for a choice, a bool for a noul (none of the seven asks noul,
    but the shape is supported). ``confidences`` covers every parseable answer, determined
    or not: the ticket cites them (§4 step 2), undetermined ones included, to say why.
    ``raw`` is the server's answers dict as received — the score float and the per-option
    probabilities the ticket carries live there."""

    values: dict[str, int | str]
    confidences: dict[str, float]
    undetermined: set[str]
    truncated: bool
    raw: dict
    defs_version: int


class DecisionUnavailable(Exception):
    """d3 could not score the brief. ``reason`` is down (connection error), slow (timeout)
    or bad_response (non-200, or a body without answers); §14 turns it into a refusal of
    ``auto`` with the guidance, never a silently-guessed ticket."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason

    def to_dict(self) -> dict:
        return {"error": "model_unavailable", "message": str(self), "reason": self.reason,
                "guidance": GUIDANCE}


class BriefUnresolved(Exception):
    """A pointer brief whose file is missing or unreadable: §14 refuses the job entirely."""

    def __init__(self, path, message: str):
        super().__init__(message)
        self.path = str(path)

    def to_dict(self) -> dict:
        return {"error": "brief_unresolved", "message": str(self), "path": self.path}


def _clean_pointer_path(raw: str) -> str:
    """The path out of a pointer line, as people actually type it: wrapped in backticks or
    quotes, trailed by the punctuation of the sentence it was written in ("cat `briefs/
    job.md`, and follow it"), and ``~/`` for home. The quote strip and the punctuation strip
    feed each other (a comma sits outside the closing backtick), so they run together until
    the text stops shrinking; home is expanded last, on what is left."""
    path = raw
    while True:
        trimmed = path.rstrip(",.;:")
        if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in "`\"'":
            trimmed = trimmed[1:-1]
        if trimmed == path:
            break
        path = trimmed
    return os.path.expanduser(path)


def resolve_brief(task: str, cwd: str) -> str:
    """A pointer brief ("cat `<path>` and follow it", the pointer on its first non-empty line,
    at most three non-empty lines total) is replaced by the file's text — the spike: pointer
    text as stored drops d3 from 85% to 65%. The record keeps the pointer itself as ``task``.
    The path survives being wrapped in backticks or quotes, trailed by sentence punctuation,
    and being written with ``~/`` (``_clean_pointer_path``). Relative paths read against
    ``cwd``; a missing or unreadable file is BriefUnresolved.
    Any other brief is returned unchanged (a long brief that happens to open with "cat " is
    prose, not a pointer)."""
    lines = [line for line in task.splitlines() if line.strip()]
    if lines and len(lines) <= 3:
        match = _POINTER.match(lines[0])
        if match:
            path = pathlib.Path(_clean_pointer_path(match.group(1)))
            if not path.is_absolute():
                path = pathlib.Path(cwd) / path
            try:
                return path.read_text()
            except OSError as e:
                raise BriefUnresolved(
                    path,
                    f"the pointer brief names {path}, which cannot be read "
                    f"({e.strerror or type(e).__name__})",
                ) from e
    return task


def _as_float(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _top_band(probabilities) -> int | None:
    """The band with the highest probability, as an int. A tie goes to the higher band:
    under-scoring sends a hard brief to a weak model, over-scoring only wastes capacity —
    and an over-scored simple brief still loses to nothing, it just pays a bigger model."""
    if not isinstance(probabilities, dict):
        return None
    bands = [(p, int(b)) for b in BANDS if (p := _as_float(probabilities.get(b))) is not None]
    if not bands:
        return None
    return max(bands)[1]


def _read_answer(name, answer):
    """One d3 answer as ``(value, confidence, type)``, or None when the answer carries no
    usable shape (absent, not a dict, an unknown type, a missing field). The answer's own
    ``type`` field is what parses it — the recorded shapes are self-describing, and a type
    the document never asked for is still parseable — and it is the threshold's key too.
    A noul answer carries no confidence field: its distance from the coin flip,
    ``abs(noul - 0.5) * 2``, is.

    An answer that carries ``error`` is the server refusing this question (a shape it would
    not answer), not an answer missing: that is bad_response with the server's sentence,
    raised, because an unparseable answer is silently undetermined and a refusal is the
    document's failure — §14: never a silently-guessed ticket."""
    if not isinstance(answer, dict):
        return None
    if "error" in answer:
        message = f"d3 refused the question {name}: {answer['error']}"
        if answer.get("message"):
            message += f": {answer['message']}"
        raise DecisionUnavailable("bad_response", message)
    atype = answer.get("type")
    if atype == "score":
        band = _top_band(answer.get("probabilities"))
        confidence = _as_float(answer.get("confidence"))
        if band is None or confidence is None:
            return None
        return band, confidence, atype
    if atype == "choice":
        choice = answer.get("choice")
        confidence = _as_float(answer.get("confidence"))
        if not isinstance(choice, str) or not choice or confidence is None:
            return None
        return choice, confidence, atype
    if atype == "noul":
        probability = _as_float(answer.get("noul"))
        if probability is None:
            return None
        return probability >= 0.5, abs(probability - 0.5) * 2, atype
    return None


async def score(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    text: str,
    defs: Definitions,
    thresholds: dict[str, float] = THRESHOLDS,
    timeout_s: float = 20.0,
    max_tokens: int = 24_000,
) -> BriefScores:
    """One ``POST {url}/v1/systemone`` with the document's question set and the brief as state
    (a brief longer than ``max_tokens`` tokens — approximated at four characters each — is
    scored on its first that many characters and marked ``truncated``, §4 step 2). An answer
    whose confidence is below its type's bar joins ``undetermined`` and is left out of
    ``values`` (§6: an undetermined axis is neutral); so does a language of ``none``. A missing
    or unparseable answer is undetermined too — no signal, contribute nothing. Raises
    DecisionUnavailable: down for a connection error, slow for an httpx timeout (a
    ConnectTimeout is httpx-wise a timeout, so it reads slow; it is usually a dead box, but
    guessing down from a silent port is the registry probe's job, not this client's),
    bad_response for a non-200 or a body without answers."""
    limit = max_tokens * 4
    truncated = len(text) > limit
    try:
        response = await client.post(
            f"{url.rstrip('/')}/v1/systemone",
            json={"model": model, "state": text[:limit] if truncated else text,
                  "questions": questions(defs.document)},
            timeout=timeout_s,
        )
    except httpx.TimeoutException as e:
        raise DecisionUnavailable("slow", f"the d3 server at {url} did not answer within {timeout_s:g} s") from e
    except httpx.ConnectError as e:
        raise DecisionUnavailable("down", f"cannot reach the d3 server at {url}") from e
    except httpx.HTTPError as e:
        raise DecisionUnavailable("bad_response", f"the d3 request failed: {type(e).__name__}: {e}") from e
    if response.status_code != 200:
        # the d3 server answers its own failures in JSON (model_error with the exception's
        # name and text, state too long with the limit): carry what it said into the message
        # — the operator reads this dict, and "answered 500" alone says nothing — rather
        # than leaving them to guess at a bare status code
        message = f"the d3 server at {url} answered {response.status_code}"
        try:
            err = response.json()
        except ValueError:
            err = None
        if isinstance(err, dict) and "error" in err:
            message += f": {err['error']}"
            if err.get("message"):
                message += f": {err['message']}"
        raise DecisionUnavailable("bad_response", message)
    try:
        body = response.json()
    except ValueError as e:
        raise DecisionUnavailable("bad_response", f"the d3 server at {url} sent a body that is not JSON") from e
    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, dict):
        raise DecisionUnavailable("bad_response", f"the d3 server at {url} sent no answers")

    values: dict[str, int | str] = {}
    confidences: dict[str, float] = {}
    undetermined: set[str] = set()
    for name in questions(defs.document):
        read = _read_answer(name, answers.get(name))
        if read is None:
            undetermined.add(name)
            continue
        value, confidence, atype = read
        confidences[name] = confidence
        if confidence < thresholds.get(atype, 0.0):
            undetermined.add(name)
        elif name == "language" and value == "none":
            # language is neutral when none or uncertain (§6), so none never scores as a value
            undetermined.add(name)
        else:
            values[name] = value
    return BriefScores(values, confidences, undetermined, truncated, answers, defs.version)
