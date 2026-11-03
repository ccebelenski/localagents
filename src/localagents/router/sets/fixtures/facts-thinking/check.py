"""facts-thinking checker: the interval implementation passes the fixture's tests."""

import subprocess


def main() -> int:
    tests = subprocess.run(["python3", "-m", "unittest", "-q", "tests_intervals"],
                           capture_output=True, text=True)
    if tests.returncode != 0:
        print("tests failed")
        print(tests.stderr[-2000:])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
