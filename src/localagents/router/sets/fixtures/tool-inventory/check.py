"""tool-inventory's checker (calibration set spec §3): t/answer.json must count the shipped
tree exactly — files per extension and lines containing RETRY anywhere under tree/. The
true counts are this fixture's own constants, pinned to the shipped tree by the suite; a
missing or malformed t/answer.json fails as no_answer."""

import json
from pathlib import Path

PATTERN = "RETRY"
BY_EXTENSION = {"log": 8, "csv": 6, "md": 6, "py": 4, "sh": 3, "txt": 7, "json": 6}
MATCHING_LINES = 26


def main() -> int:
    try:
        answer = json.loads(Path("t/answer.json").read_text())
        by_extension = answer["by_extension"]
        matching = answer["matching_lines"]
    except (OSError, ValueError, TypeError, KeyError):
        print("no_answer")
        return 1
    if not isinstance(matching, int) or isinstance(matching, bool):
        print("answer_wrong")
        return 1
    if by_extension != BY_EXTENSION:
        print("wrong file counts")
        return 1
    if matching != MATCHING_LINES:
        print("wrong line count")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
