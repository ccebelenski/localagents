# Review the budget package

`budget` (see `README.md`) records spend and computes summaries over it.
Review the code in `budget/` and report what you find.

This is a review, not a fix: do not edit, add or remove any file — the tree
must be left exactly as you found it.

Look for real defects (code that does not do what its docstring or its obvious
intent says) and for things that are only possibly wrong. For each finding,
note the file and the line.

Create `t/` if it does not exist and write `t/answer.json` with this shape:

```json
{"findings": []}
```

`findings` is a list; each entry has this shape:

```json
{"file": "<file name>", "line": <line number>, "kind": "defect", "note": "<what is wrong>"}
```

Set `kind` to `defect` for code that is definitely wrong, and to `possible`
for something that looks off but you are not sure about. An empty list is a
perfectly good answer if the code looks correct to you.
