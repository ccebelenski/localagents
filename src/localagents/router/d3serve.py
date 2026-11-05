"""``localagents d3 serve`` (router design §3, the d3 server row): the package's own small
server for a pinned Decision 3.0 model (vllm-sr/d3-mini by default, a Qwen3.5 backbone with a
decision readout), started like the monitor (``localagents monitor``). ``GET /health`` is the
router entry's probe (§8: d3 down is ``model_unavailable``); ``POST /v1/systemone`` answers
typed questions over ``AutoModel.system_one`` (the interface and shapes: docs/spikes/
2026-11-01-d3-categorizer.md, the fixture's shapes: tests/fixtures/router/d3_answers.json).

torch and transformers are the router extra (``pip install 'localagents[router]'``) and are
imported only in ``load_model`` — the server itself (starlette, uvicorn) is a core dependency,
so a box without the extra still starts and answers /health, with the install line on stderr
and the error on /health until it gets one.
"""

from __future__ import annotations

import argparse
import sys
import threading
from contextlib import asynccontextmanager
from typing import Any, Callable

import starlette.applications
import starlette.concurrency
import starlette.responses
import starlette.routing

# vllm-sr/d3-mini, main as of 2026-11-02: the router scores with this checkpoint, so the
# revision is pinned — the model's answers are what the calibration set measures, and a
# drifting checkpoint would move them out from under the scorecard
D3_REVISION = "df99bf4ca4418bc6908ee68e9b7479af1f3b8800"
# d3 takes the brief as text: a state longer than this is refused with 413. 60,000 is what a
# live server on this class of GPU answers up to — above it the model's own forward pass fails,
# so the limit moves here where the client can read it (the client scores longer briefs
# truncated — the router entry's decision_max_tokens, §12)
D3_MAX_CHARS = 60_000

# the sentence a box without the router extra gets, on stderr from the CLI and on /health
# from the server, so the operator reads the same words in both places
NO_EXTRA = ("torch/transformers not installed; install the router extra: "
            "pip install 'localagents[router]'")

# one request at a time: the model is not concurrent, so every system_one call takes this
# lock. It is module-level: one process serves one model, as the CLI starts it
_LOCK = threading.Lock()


def _call_model(model, state: str, questions) -> dict:
    """The model's one call, behind the lock (the threadpool runs it, one at a time)."""
    with _LOCK:
        return model.system_one(state=state, questions=questions)


def load_model(model_id: str, device: str, revision: str = D3_REVISION):
    """``AutoModel.from_pretrained(model_id, trust_remote_code=True, revision=...)`` moved to
    ``device`` — no dtype: the checkpoint refuses one other than None (the spike). torch and
    transformers come from the router extra; a box without it gets one sentence and exit 2,
    never a traceback."""
    try:
        import torch  # noqa: F401 — the extra's presence; the model's own .to does the move
        from transformers import AutoModel
    except ImportError:
        print(f"localagents d3: {NO_EXTRA}", file=sys.stderr)
        raise SystemExit(2)
    return AutoModel.from_pretrained(model_id, trust_remote_code=True, revision=revision).to(device)


def _model_or_error(request):
    """(The loaded model, None), or (None, the (status, body) the caller answers with while
    it is not): 503 while still loading, or 503 with the loader's error once it has failed
    (the operator reads it where the router entry reads it)."""
    model = request.app.state.model
    if model is not None:
        return model, None
    error = request.app.state.model_error
    if error is None:
        return None, (503, {"ok": False, "loading": True})
    return None, (503, {"ok": False, "loading": False, "error": error})


def app(model_loader: Callable[[], Any], model_id: str, device: str) -> starlette.applications.Starlette:
    """The server. On startup ``model_loader()`` runs in a thread and its result is kept;
    ``GET /health`` answers 503 ``{"ok": false, "loading": true}`` until it does, then 200
    with the id and device the server was started for. A loader that fails (no extra, a bad
    revision) keeps the server up and reports the error on /health.

    ``model_id`` and ``device`` are extra arguments, not attributes of the loaded object: the
    checkpoint object is not asked to report them (an AutoModel does not carry them), and the
    report is what the operator asked for — the CLI's ``--model``/``--device``. The loaded
    object is used only for its ``system_one``.
    """

    def _load(application: starlette.applications.Starlette) -> None:
        try:
            application.state.model = model_loader()
        except SystemExit:
            # the loader's missing-extra exit (load_model prints the sentence to stderr and
            # exits 2 when run from a shell): /health carries the same sentence, not
            # "SystemExit: 2" (operator, live 2026-11-04)
            application.state.model_error = NO_EXTRA
        except BaseException as e:  # a bad revision, no GPU: the error is recorded, not a
            # traceback out of a daemon thread
            application.state.model_error = f"{type(e).__name__}: {e}"

    @asynccontextmanager
    async def _lifespan(application: starlette.applications.Starlette):
        threading.Thread(target=_load, args=(application,), daemon=True).start()
        yield

    def health(request):
        model, refusal = _model_or_error(request)
        if model is None:
            status, body = refusal
            return starlette.responses.JSONResponse(body, status_code=status)
        return starlette.responses.JSONResponse({"ok": True, "model": model_id, "device": device})

    async def systemone(request):
        model, refusal = _model_or_error(request)
        if model is None:
            status, body = refusal
            return starlette.responses.JSONResponse(body, status_code=status)
        try:
            body = await request.json()
        except Exception:
            return starlette.responses.JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(body, dict) or "state" not in body or "questions" not in body:
            return starlette.responses.JSONResponse({"error": "missing state or questions"},
                                                    status_code=400)
        state = body["state"]
        if not isinstance(state, str):
            return starlette.responses.JSONResponse({"error": "state must be a string"},
                                                    status_code=400)
        if len(state) > D3_MAX_CHARS:
            return starlette.responses.JSONResponse({"error": "state too long", "max_chars": D3_MAX_CHARS},
                                                    status_code=413)
        # the handler is async because request.json() is; the model call goes to the
        # threadpool explicitly, so the event loop never blocks on it. An exception inside
        # system_one (a CUDA fault, an answer the serializer chokes on) is this request's
        # JSON 500, not the server's death: _call_model's lock released when the call raised,
        # and the client reports what the server said (decision.py turns the body into the
        # message) instead of guessing at an empty 500
        try:
            result = await starlette.concurrency.run_in_threadpool(_call_model, model, state,
                                                                   body["questions"])
        except Exception as e:  # noqa: BLE001 — one bad answer must not take the server down
            return starlette.responses.JSONResponse(
                {"error": "model_error", "type": type(e).__name__, "message": str(e)[:500]},
                status_code=500)
        return starlette.responses.JSONResponse(result)

    server = starlette.applications.Starlette(
        routes=[
            starlette.routing.Route("/health", health),
            starlette.routing.Route("/v1/systemone", systemone, methods=["POST"]),
        ],
        lifespan=_lifespan,
    )
    server.state.model = None
    server.state.model_error = None
    return server


def _port(text: str) -> int:
    n = int(text)
    if not 1 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"port must be in 1..65535, got {n}")
    return n


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="localagents d3",
        description="serve a pinned Decision 3.0 model (default vllm-sr/d3-mini) on "
                    "/health and /v1/systemone")
    ap.add_argument("command", choices=["serve"],
                    help="serve (the only command; none or an unknown one is a usage error)")
    ap.add_argument("--model", default="vllm-sr/d3-mini", help="the checkpoint (an HF id)")
    ap.add_argument("--revision", default=D3_REVISION,
                    help="the checkpoint revision to pin (a git sha; default the one shipped)")
    ap.add_argument("--device", default="cuda:0", help="the device to load on (default cuda:0)")
    ap.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    ap.add_argument("--port", type=_port, default=8766, help="port to serve on (default 8766)")
    return ap.parse_args(argv)


def main(argv: list[str]) -> None:
    a = parse_args(argv)
    server = app(lambda: load_model(a.model, a.device, a.revision), a.model, a.device)
    import uvicorn

    print(f"localagents d3: http://{a.host}:{a.port}/ · {a.model} on {a.device}", flush=True)
    uvicorn.run(server, host=a.host, port=a.port, log_level="warning", access_log=False)
