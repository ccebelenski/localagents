"""Story 2.1: every capture under tests/fixtures/monitor/ is listed in manifest.json,
the listed byte count matches the file, and each row's parser runs over the file's
bytes without raising and returns something non-empty.

The manifest is the contract between the captures and the Epic 2 parsers: every
parser is tested against a capture, never a typed document. A capture the parser
cannot read is a finding recorded in the manifest (`note`), not a fixture to fix.

`fixture_parsers` arrives with step 4 of the story; rows before then (and error
rows) carry no `parser`, and the parser loop only runs over rows that do.
"""
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


def test_each_row_pins_what_its_parser_returns():
    """Story 2.1a: every non-error row pins `parsed`, the size of its parser's
    result (entry count for a dict/list, newline count for text), so a fixture
    truncated with its `bytes` edited to match still fails the manifest."""
    rows = _rows()
    try:
        from fixture_parsers import PARSERS
    except ImportError:
        pytest.skip("fixture_parsers arrives with story step 4")
    for row in rows:
        if "error" in row:
            continue
        assert "parser" in row, f"{row['path']}: non-error row names no parser"
        assert "parsed" in row, f"{row['path']}: no `parsed` count pinned in the manifest"
        out = PARSERS[row["parser"]]((MONITOR / row["path"]).read_bytes())
        n = out.count("\n") if isinstance(out, str) else len(out)
        assert n == row["parsed"], (
            f"{row['path']}: parser {row['parser']!r} yields {n}, manifest pins {row['parsed']}"
        )


def _content_length(sidecar: Path) -> int | None:
    """The Content-Length value in a curl `-D` sidecar (status line first), or
    None if the server sent none. Header names are case-insensitive across engines."""
    for line in sidecar.read_text().splitlines()[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "content-length":
            return int(value.strip())
    return None


def _sidecar_problems(rows: list[dict], root: Path) -> list[str]:
    """Story 2.1a: every row with an http:// url has its `.headers` sidecar — the
    `path` with the extension replaced — including the error row, whose capture
    has headers but no body. When the sidecar carries Content-Length, that value
    is the body's size: the row's `bytes`, or 0 for the error row.

    The error row needs no explicit `headers` field: it names a `path` like every
    other row, and `with_suffix('.headers')` reproduces the name curl gave it
    (vllm/dgx1-a_health_1.headers), so the rule is uniform.
    """
    problems = []
    for row in rows:
        if not row["url"].startswith("http://"):
            continue  # the db export row was never an HTTP capture
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
