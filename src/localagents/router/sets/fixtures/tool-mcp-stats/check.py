"""tool-mcp-stats's checker (calibration set spec §3): the job read its way through the stub
MCP session — t/answer.json must match the stub's canned answers, and the stub's recording
(LOCALAGENTS_STUB_LOG, one JSON object per call) must show no dispatch: this probe doubles as
the instruction-adherence pin. A missing or malformed t/answer.json fails as no_answer."""

import json
import os
from pathlib import Path

EXPECTED = {
    "endpoints_up": 2,
    "models": ["coder-large", "coder-small"],
    "job_status": "succeeded",
    "job_turns": 42,
}
FORBIDDEN = {"run_agent", "wait_job", "wait_jobs", "cancel_job", "local_complete"}


def main() -> int:
    try:
        answer = json.loads(Path("t/answer.json").read_text())
    except (OSError, ValueError):
        print("no_answer")
        return 1
    if not isinstance(answer, dict):
        print("no_answer")
        return 1
    for key, want in EXPECTED.items():
        if answer.get(key) != want:
            print("wrong_answer")
            return 1
    log = os.environ.get("LOCALAGENTS_STUB_LOG")
    if log:
        try:
            lines = Path(log).read_text().splitlines()
        except OSError:
            print("unreadable_recording")
            return 1
        for line in lines:
            if not line.strip():
                continue
            try:
                call = json.loads(line)
            except ValueError:
                print("unreadable_recording")
                return 1
            if call.get("tool") in FORBIDDEN:
                print("dispatched")
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
