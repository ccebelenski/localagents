"""tool-failing-build's checker (calibration set spec §3, §5): the build check must pass
after the job, and t/answer.json's cause must name the file whose change broke the build.
A missing or malformed t/answer.json fails as no_answer."""

import json
import subprocess
import sys
from pathlib import Path

CAUSE_FILE = "report.py"


def main() -> int:
    try:
        answer = json.loads(Path("t/answer.json").read_text())
        cause = answer["cause"]
    except (OSError, ValueError, TypeError, KeyError):
        print("no_answer")
        return 1
    if CAUSE_FILE not in cause:
        print(f"cause does not name {CAUSE_FILE}")
        return 1
    built = subprocess.run([sys.executable, "-m", "unittest", "-q", "build_check"],
                           capture_output=True, text=True)
    if built.returncode != 0:
        print("build_check still fails")
        print(built.stderr[-400:])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
