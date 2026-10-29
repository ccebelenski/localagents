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
