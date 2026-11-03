Read the CSV files under `data/` and write `summary.json` in the project root holding the
number of data rows (headers excluded), the total units, and the units per region:

    {"rows": <int>, "units": <int>, "regions": {<region>: <int>, ...}}

Scratch files go under `t/` in the project, never `/tmp`.
