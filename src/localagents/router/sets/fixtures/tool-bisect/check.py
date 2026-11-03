"""tool-bisect's checker (calibration set spec §3): t/answer.json's commit must name the commit
that planted the regression — the one setup.sh recorded at refs/probe/answer, where log and
bisect never look. A missing or malformed t/answer.json fails as no_answer."""

import json
import re
import subprocess
from pathlib import Path

ANSWER_REF = "refs/probe/answer"
_HEX = re.compile(r"[0-9a-fA-F]{7,40}")


def main() -> int:
    try:
        answer = json.loads(Path("t/answer.json").read_text())
        commit = answer["commit"]
    except (OSError, ValueError, TypeError, KeyError):
        print("no_answer")
        return 1
    if not isinstance(commit, str) or not _HEX.fullmatch(commit):
        print("answer commit must be 7 to 40 hex characters")
        return 1
    planted = subprocess.run(["git", "rev-parse", ANSWER_REF], capture_output=True, text=True)
    if planted.returncode != 0:
        print("the planted commit was never recorded")
        return 1
    if not planted.stdout.strip().lower().startswith(commit.lower()):
        print("wrong commit")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
