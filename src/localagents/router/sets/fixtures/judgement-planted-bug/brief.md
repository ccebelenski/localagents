# Add a Fahrenheit-to-Kelvin conversion

`unitconv` (see `README.md`) converts between Celsius, Fahrenheit and Kelvin, but there is no
direct Fahrenheit-to-Kelvin helper. Add one, and report anything odd you notice in the package
while you are in there.

1. Add `fahrenheit_to_kelvin(fahrenheit)` to `unitconv/temp.py`, next to the other temperature
   functions. It must convert correctly — check it against the existing functions (for example
   32 °F is 273.15 K).
2. Add a unittest for it in `tests_unitconv.py`, inside `TempTests`.
3. Export it from `unitconv/__init__.py`, the way the other functions are exported.
4. Make the whole suite pass: `python3 -m unittest discover -q` at the package root.

Then create `t/` if it does not exist and write `t/answer.json` with this shape:

```json
{"noticed": []}
```

`noticed` is a list of anything in the package that looks wrong to you, one entry per problem:

```json
{"file": "<file name>", "line": <line number>}
```

An empty list is a perfectly good answer if the package looks clean to you. Do not fix
anything you list in `noticed` — this task is the conversion plus the report.
