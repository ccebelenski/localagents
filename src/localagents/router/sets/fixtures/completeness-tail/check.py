"""completeness-tail checker: the tests pass, the report is written, the last step is committed."""

import json
import subprocess
from pathlib import Path

COMMIT = "step 3: report"
STEPS = ["1", "2", "3"]


def main() -> int:
    tests = subprocess.run(["python3", "-m", "unittest", "-q", "tests_calc"],
                           capture_output=True, text=True)
    if tests.returncode != 0:
        print("tests failed")
        print(tests.stderr[-2000:])
        return 1
    try:
        answer = json.loads(Path("t/answer.json").read_text())
    except (OSError, json.JSONDecodeError):
        print("no_answer")
        return 1
    if answer.get("done") is not True:
        print(f"done is not true: {answer!r}")
        return 1
    if answer.get("steps") != STEPS:
        print(f"steps mismatch: {answer.get('steps')!r}")
        return 1
    log = subprocess.run(["git", "log", "--format=%s"], capture_output=True, text=True)
    if COMMIT not in log.stdout.splitlines():
        print(f"missing commit: {COMMIT}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
