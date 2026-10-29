"""Story 2.1 test support: the parsers named in tests/fixtures/monitor/manifest.json.

Every Epic 2 parser will be tested against these captures, never a typed document,
so the manifest test runs the same callables below over the real bytes. Each callable
takes the file's bytes and returns the parsed document; a falsy return means the
parser saw nothing it understood (the manifest test treats that as a failure).

A real capture an existing registry parser cannot read is a FINDING for story 2.2,
recorded in the manifest row as `parser: json`/`text` plus a `note` — never by
editing src/ here.
"""
import json

from localagents import registry

PARSERS = {
    "prometheus": lambda b: registry.parse_metrics(b.decode()),
    "strata_metrics": lambda b: registry.parse_strata_metrics(json.loads(b)),
    "json": lambda b: json.loads(b),
    "text": lambda b: b.decode(),
}
