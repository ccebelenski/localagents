"""Story 2.1: every capture under tests/fixtures/monitor/ is listed in manifest.json,
the listed byte count matches the file, and each row's parser runs over the file's
bytes without raising and returns something non-empty.

The manifest is the contract between the captures and the Epic 2 parsers: every
parser is tested against a capture, never a typed document. A capture the parser
cannot read is a finding recorded in the manifest (`note`), not a fixture to fix.

`fixture_parsers` arrives with step 4 of the story; rows before then (and error
rows) carry no `parser`, and the parser loop only runs over rows that do.
"""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

MONITOR = Path(__file__).parent / "fixtures" / "monitor"


def _rows() -> list[dict]:
    return json.loads((MONITOR / "manifest.json").read_text())


def _on_disk() -> set[str]:
    """Files under tests/fixtures/monitor/, manifest and curl sidecars excluded."""
    return {
        str(p.relative_to(MONITOR))
        for p in MONITOR.rglob("*")
        if p.is_file() and p.name != "manifest.json" and not p.name.endswith(".headers")
    }


def test_every_fixture_is_listed_and_parses():
    rows = _rows()
    assert rows, "manifest lists no captures"

    # an error row names no file (the capture failed); every other row must have one
    listed = {r["path"] for r in rows if "error" not in r}
    missing = listed - _on_disk()
    unlisted = _on_disk() - listed
    assert not missing, f"manifest rows with no file: {sorted(missing)}"
    assert not unlisted, f"files not listed in the manifest: {sorted(unlisted)}"

    for row in rows:
        if "error" in row:
            assert "status" not in row and "bytes" not in row, f"{row['path']}: error row also has status/bytes"
            assert row["error"], f"{row['path']}: empty error sentence"
            continue
        f = MONITOR / row["path"]
        assert f.is_file(), f"{row['path']}: listed but not a file"
        data = f.read_bytes()
        assert len(data) == row["bytes"], f"{row['path']}: manifest says {row['bytes']} bytes, file is {len(data)}"

    try:
        from fixture_parsers import PARSERS
    except ImportError:
        pytest.skip("fixture_parsers arrives with story step 4")
    for row in rows:
        if "error" in row or "parser" not in row:
            continue
        out = PARSERS[row["parser"]](data := (MONITOR / row["path"]).read_bytes())
        assert out, f"{row['path']}: parser {row['parser']!r} returned nothing"


def _row_digest(out: object) -> str:
    """Story 2.1b: the sha256 of a parser's result — of the text itself for a
    text result, of `json.dumps(parsed, sort_keys=True, default=str)` for a
    document — so the pin is the VALUES, not a count."""
    payload = out.encode() if isinstance(out, str) else json.dumps(out, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def _digest_problems(rows: list[dict], root: Path) -> list[str]:
    """Story 2.1b: every non-error row pins `digest`, the sha256 of its parser's
    result over the file's bytes at `root`, so a value that survives a
    byte-count edit (a truncated fixture with `bytes` retuned, or a value
    swapped in place at the same length) still fails the manifest. `root`
    takes the fixture tree's parent so a mutated tmp_path copy can be run
    through the same check without touching the real fixtures."""
    from fixture_parsers import PARSERS

    problems = []
    for row in rows:
        if "error" in row:
            continue
        if "parser" not in row:
            problems.append(f"{row['path']}: non-error row names no parser")
        elif "digest" not in row:
            problems.append(f"{row['path']}: no `digest` pinned in the manifest")
            continue
        out = PARSERS[row["parser"]]((root / row["path"]).read_bytes())
        digest = _row_digest(out)
        if digest != row["digest"]:
            problems.append(
                f"{row['path']}: parser {row['parser']!r} digests to {digest}, manifest pins {row['digest']}"
            )
    return problems


def test_each_row_pins_what_its_parser_returns():
    """Story 2.1b: every non-error row pins `digest`, the sha256 of its parser's
    result (of the text itself for a text result), so a fixture truncated with
    its `bytes` edited to match — or a value swapped in place at the same byte
    length — still fails the manifest."""
    try:
        assert _digest_problems(_rows(), MONITOR) == []
    except ImportError:
        pytest.skip("fixture_parsers arrives with story step 4")


def test_a_value_change_moves_the_digest(tmp_path):
    """Story 2.1b: the review's construction D — a parsed value swapped in
    place at the same byte length, so the manifest's `bytes` and the sidecar's
    Content-Length still agree — must fail on the digest line. The edit: a
    mapped vLLM sample in the metrics capture, `1.0` -> `2.0`."""
    try:
        from fixture_parsers import PARSERS
    except ImportError:
        pytest.skip("fixture_parsers arrives with story step 4")
    root = tmp_path / "monitor"
    shutil.copytree(MONITOR, root)
    f = root / "vllm" / "dgx1-a_metrics_1.txt"
    old = b'vllm:num_requests_running{engine="0",model_name="DeepSeek-V4.1-Flash-1017"} 1.0\n'
    new = b'vllm:num_requests_running{engine="0",model_name="DeepSeek-V4.1-Flash-1017"} 2.0\n'
    data = f.read_bytes()
    assert data.count(old) == 1
    mutated = data.replace(old, new)
    assert len(mutated) == len(data)  # same length: the byte and CL checks survive
    f.write_bytes(mutated)
    rows = _rows()
    row = next(r for r in rows if r["path"] == "vllm/dgx1-a_metrics_1.txt")
    assert len(mutated) == row["bytes"]
    assert _sidecar_problems(rows, root) == []
    # but the digest moved
    assert _digest_problems(rows, root) == [
        f"vllm/dgx1-a_metrics_1.txt: parser 'prometheus' digests to "
        f"{_row_digest(PARSERS['prometheus'](mutated))}, manifest pins {row['digest']}"
    ]


def _content_length(sidecar: Path) -> int | None:
    """The Content-Length of the LAST header block in a curl `-D` sidecar —
    a redirect's block comes first and its Content-Length names the body that
    was never captured; the captured body is the last block's — or None if the
    server sent none. Header names are case-insensitive across engines."""
    cl = None
    for line in sidecar.read_text().splitlines()[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "content-length":
            cl = int(value.strip())
    return cl


def _sidecar_problems(rows: list[dict], root: Path) -> list[str]:
    """Story 2.1a: every row with an http:// url has its `.headers` sidecar — the
    `path` with the extension replaced — including the error row, whose capture
    has headers but no body. When the sidecar carries Content-Length, that of
    its last header block (a redirect's block comes first) is the body's size:
    the row's `bytes`, or 0 for the error row.

    The error row needs no explicit `headers` field: it names a `path` like every
    other row, and `with_suffix('.headers')` reproduces the name curl gave it
    (vllm/dgx1-a_health_1.headers), so the rule is uniform. An error row that
    never produced a file names no `path` to derive its sidecar from; that is a
    problem reported, not a KeyError raised.
    """
    problems = []
    for row in rows:
        if not row["url"].startswith("http://"):
            continue  # the db export row was never an HTTP capture
        if "error" in row and "path" not in row:
            problems.append(f"{row['url']}: error row has no path to derive its sidecar from")
            continue
        sidecar = root / Path(row["path"]).with_suffix(".headers")
        if not sidecar.is_file():
            problems.append(f"{row['path']}: no sidecar at {sidecar.relative_to(root)}")
            continue
        cl = _content_length(sidecar)
        if cl is not None and cl != row.get("bytes", 0):
            problems.append(
                f"{row['path']}: sidecar Content-Length {cl}, manifest bytes {row.get('bytes', 0)}"
            )
    return problems


def test_every_http_row_has_its_headers_sidecar():
    assert _sidecar_problems(_rows(), MONITOR) == []


def test_missing_sidecar_is_caught(tmp_path):
    """A copy of the tree with one sidecar deleted must fail the check —
    the real fixtures are never touched."""
    root = tmp_path / "monitor"
    shutil.copytree(MONITOR, root)
    (root / "vllm" / "dgx1-a_metrics_1.headers").unlink()
    assert _sidecar_problems(_rows(), root) == [
        "vllm/dgx1-a_metrics_1.txt: no sidecar at vllm/dgx1-a_metrics_1.headers"
    ]


def test_wrong_content_length_is_caught(tmp_path):
    root = tmp_path / "monitor"
    shutil.copytree(MONITOR, root)
    f = root / "vllm" / "dgx1-a_models_1.headers"
    f.write_text(f.read_text().replace("content-length: 500", "content-length: 501"))
    assert _sidecar_problems(_rows(), root) == [
        "vllm/dgx1-a_models_1.json: sidecar Content-Length 501, manifest bytes 500"
    ]


def test_error_row_without_path_is_a_problem(tmp_path):
    """Story 2.1b: a failed capture that never produced a file names no `path`
    to derive its sidecar from; the check reports the gap instead of raising a
    KeyError."""
    (tmp_path / "manifest.json").write_text(json.dumps([
        {
            "endpoint": "ai9",
            "backend": "strata",
            "url": "http://10.0.0.99:8080/props",
            "captured_at": "2026-10-29T15:34:25Z",
            "error": "connection refused",
        }
    ]))
    rows = json.loads((tmp_path / "manifest.json").read_text())
    assert _sidecar_problems(rows, tmp_path) == [
        "http://10.0.0.99:8080/props: error row has no path to derive its sidecar from"
    ]


def test_content_length_comes_from_the_last_header_block(tmp_path):
    """Story 2.1b: a redirect's block comes first in the sidecar and its
    Content-Length names the body that was never captured; the captured body
    is the last block's, so its Content-Length is the row's size."""
    sidecar = tmp_path / "capture.headers"
    sidecar.write_text(
        "HTTP/1.1 301 Moved Permanently\n"
        "location: http://10.0.0.70:8000/version\n"
        "content-length: 123\n"
        "\n"
        "HTTP/1.1 200 OK\n"
        "content-type: application/json\n"
        "content-length: 456\n"
    )
    assert _content_length(sidecar) == 456
