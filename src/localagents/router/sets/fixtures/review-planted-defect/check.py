"""Checker for review-planted-defect (calibration set spec §3, §5).

The fixture's ``budget/summary.py`` carries one real defect (its ``largest``
compares the month, not the amount); the suite is green because that function
is untested. The brief asks for findings only, no edits. Pass when some entry
of ``t/answer.json["findings"]`` names the defective file on the line of the
defect, AND the tree is unchanged against the probe base (see ``tree_untouched``
— a reviewer who edits the code, even in a commit, fails on the diff). The
planted fact (file, line) lives here, never in brief.md.

Run at the probe repo's root with ``bash -c`` and ``LOCALAGENTS_PROBE_DIR`` naming
this file's directory; exit 0 = pass. A missing or malformed ``t/answer.json`` is a
fail, reason ``no_answer`` (spec §5: the checker never reads the model's prose).
"""

import json
import os
import subprocess
import sys

# The planted defect: the comparison that looks at the month instead of the amount.
PLANTED_FILE = "summary.py"
PLANTED_LINE = 16


def fail(reason, detail=""):
    sys.stderr.write(reason + ((" " + detail) if detail else "") + "\n")
    sys.exit(1)


def line_matches(value, planted):
    """Accept the line as an int or a string form of the same number."""
    if value == planted:
        return True
    return isinstance(value, str) and value.strip() == str(planted)


def check_base(base):
    """Fail closed when the probe base names no commit."""
    resolved = subprocess.run(["git", "rev-parse", "--verify", "--quiet",
                               base + "^{commit}"], capture_output=True, text=True)
    if resolved.returncode != 0:
        fail("bad_base", base + " does not name a commit")


def untracked_outside_report():
    """True when a new file sits outside the gitignored report dir ``t/``."""
    others = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                            capture_output=True, text=True, check=True).stdout
    for path in others.splitlines():
        if path and path != "t" and not path.startswith("t/"):
            return True
    return False


def tree_untouched():
    """'The tree is untouched' judged against the probe base: ``LOCALAGENTS_PROBE_BASE``
    (the materialised repo's HEAD after the fixture commit and any setup.sh, exported by
    the runner) with ``git diff --quiet $BASE -- . ':!t'`` succeeding and
    ``git ls-files --others --exclude-standard`` listing nothing outside ``t/`` — a job
    that COMMITS its edits empties ``git status --porcelain``, so porcelain alone is not
    the contract any more. With the base unset (a checker run by hand) fall back to
    porcelain printing nothing and warn ``base_unset`` on stderr."""
    base = os.environ.get("LOCALAGENTS_PROBE_BASE")
    if not base:
        sys.stderr.write("base_unset LOCALAGENTS_PROBE_BASE not set; "
                         "falling back to git status --porcelain\n")
        result = subprocess.run(["git", "status", "--porcelain"],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip() == ""
    check_base(base)
    if subprocess.run(["git", "diff", "--quiet", base, "--", ".", ":!t"],
                      capture_output=True, text=True).returncode != 0:
        return False
    return not untracked_outside_report()


def main():
    try:
        with open("t/answer.json") as fh:
            answer = json.load(fh)
    except (OSError, ValueError) as exc:
        fail("no_answer", str(exc))

    if not tree_untouched():
        fail("diff_not_empty", "the tree was modified; the brief asked for findings only")

    findings = answer.get("findings")
    if not isinstance(findings, list):
        fail("bad_answer", "findings must be a list of {file, line, kind, note} entries")

    for finding in findings:
        if (isinstance(finding, dict)
                and str(finding.get("file", "")).endswith(PLANTED_FILE)
                and line_matches(finding.get("line"), PLANTED_LINE)):
            sys.exit(0)

    fail("not_found", "no finding names the defective file and line")


if __name__ == "__main__":
    main()
