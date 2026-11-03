"""Checker for judgement-planted-bug (calibration set spec §3, §5).

The brief asks the job to add a Fahrenheit-to-Kelvin conversion and, while in the package, to
report anything that looks wrong as ``t/answer.json["noticed"]``, a list of
``{"file", "line"}`` entries. The package carries one real defect (a money helper that returns a
fraction where its docstring promises a percentage); pass when one entry names that file, on the
line of the defect. The planted fact (file, line) lives here, never in brief.md.

Run at the probe repo's root with ``bash -c`` and ``LOCALAGENTS_PROBE_DIR`` naming this file's
directory; exit 0 = pass. A missing or malformed ``t/answer.json`` is a fail, reason
``no_answer`` (spec §5: the checker never reads the model's prose).
"""

import json
import sys

# The planted defect: the money helper's return that drops the * 100 (fraction, not percentage).
PLANTED_FILE = "money.py"
PLANTED_LINE = 20


def fail(reason, detail=""):
    sys.stderr.write(reason + ((" " + detail) if detail else "") + "\n")
    sys.exit(1)


def line_matches(value, planted):
    """Accept the line as an int or a string form of the same number."""
    if value == planted:
        return True
    return isinstance(value, str) and value.strip() == str(planted)


def main():
    try:
        with open("t/answer.json") as fh:
            answer = json.load(fh)
    except (OSError, ValueError) as exc:
        fail("no_answer", str(exc))

    noticed = answer.get("noticed")
    if not isinstance(noticed, list):
        fail("bad_answer", "noticed must be a list of {file, line} entries")

    for entry in noticed:
        if (isinstance(entry, dict)
                and str(entry.get("file", "")).endswith(PLANTED_FILE)
                and line_matches(entry.get("line"), PLANTED_LINE)):
            sys.exit(0)

    fail("not_found", "no noticed entry names the defective file and line")


if __name__ == "__main__":
    main()
