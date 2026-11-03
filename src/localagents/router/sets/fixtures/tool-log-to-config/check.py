"""tool-log-to-config's checker (calibration set spec §3): the port the gateway actually bound
is stated by its own log; the shipped config names a different one. config.json's gateway_port
and t/answer.json's gateway_port must both hold the bound port. A missing or malformed
t/answer.json fails as no_answer."""

import json
from pathlib import Path

CORRECT_PORT = 9009


def main() -> int:
    try:
        answer = json.loads(Path("t/answer.json").read_text())
        port = answer["gateway_port"]
    except (OSError, ValueError, TypeError, KeyError):
        print("no_answer")
        return 1
    try:
        config = json.loads(Path("config.json").read_text())
    except (OSError, ValueError):
        print("config.json is unreadable")
        return 1
    if config.get("gateway_port") != CORRECT_PORT:
        print("config_wrong")
        return 1
    if port != CORRECT_PORT:
        print("answer_wrong")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
