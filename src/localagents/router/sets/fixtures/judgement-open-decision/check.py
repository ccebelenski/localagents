"""Checker for judgement-open-decision (calibration set spec §3, §5).

The brief names two reasonable designs for note persistence and asks the job to
choose one and justify it. Pass when ``t/answer.json["decision"]`` is one of the
two design names and ``reason`` is a non-empty string of at least 20 characters
(a real trade-off, not a bare label). The two names live here; the brief names
them too, because naming both designs is the point of the probe.

Run at the probe repo's root with ``bash -c`` and ``LOCALAGENTS_PROBE_DIR`` naming
this file's directory; exit 0 = pass. A missing or malformed ``t/answer.json`` is a
fail, reason ``no_answer`` (spec §5: the checker never reads the model's prose).
"""

import json
import sys

# The two designs the brief asks the job to choose between.
DECISIONS = ("jsonfile", "sqlite")
MIN_REASON_CHARS = 20


def fail(reason, detail=""):
    sys.stderr.write(reason + ((" " + detail) if detail else "") + "\n")
    sys.exit(1)


def main():
    try:
        with open("t/answer.json") as fh:
            answer = json.load(fh)
    except (OSError, ValueError) as exc:
        fail("no_answer", str(exc))

    decision = answer.get("decision")
    if not isinstance(decision, str) or decision.strip() not in DECISIONS:
        fail("bad_decision", "decision must be one of: " + ", ".join(DECISIONS))

    reason = answer.get("reason")
    if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON_CHARS:
        fail("reason_too_short",
             "reason must be a string of at least %d characters" % MIN_REASON_CHARS)
    sys.exit(0)


if __name__ == "__main__":
    main()
