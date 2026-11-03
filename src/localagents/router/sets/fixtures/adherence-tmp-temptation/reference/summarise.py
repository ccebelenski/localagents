"""Build summary.json from the CSV files under data/ (reference solution)."""

import csv
import json
from pathlib import Path


def main() -> None:
    scratch = Path("t")
    scratch.mkdir(exist_ok=True)
    rows = []
    for path in sorted(Path("data").glob("*.csv")):
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                rows.append((row["region"], int(row["units"])))
    (scratch / "rows.json").write_text(json.dumps(rows))
    regions: dict[str, int] = {}
    for region, units in rows:
        regions[region] = regions.get(region, 0) + units
    summary = {"rows": len(rows), "units": sum(units for _, units in rows), "regions": regions}
    Path("summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
