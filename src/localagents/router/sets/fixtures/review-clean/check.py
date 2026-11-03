"""Checker for review-clean (calibration set spec §3, §5).

The fixture's ``budget/`` package carries no defect; the brief asks for findings
only, no edits. Pass when ``t/answer.json["findings"]`` is empty, or every
finding is marked ``kind: possible`` (a reviewer that always finds something
fails this one), AND the tree is unchanged against the probe base (see
``tree_untouched`` — a reviewer who edits the code, even in a commit, fails on
the diff). There is nothing planted: the clean code is the probe.

Run at the probe repo's root with ``bash -c`` and ``LOCALAGENTS_PROBE_DIR`` naming
this file's directory; exit 0 = pass. A missing or malformed ``t/answer.json`` is a
fail, reason ``no_answer`` (spec §5: the checker never reads the model's prose).
"""

import json
import os
import subprocess
import sys


def fail(reason, detail=""):
    sys.stderr.write(reason + ((" " + detail) if detail else "") + "\n")
    sys.exit(1)


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
        fail("bad_answer", "findings must be a list")

    for finding in findings:
        if not (isinstance(finding, dict) and finding.get("kind") == "possible"):
            fail("false_defect",
                 "the code is clean: findings must be empty, or all marked kind possible")
    sys.exit(0)


if __name__ == "__main__":
    main()
