# Give the note store persistence

`notes` (see `README.md`) keeps its notes in memory; they are gone when the
process exits. Give the store persistence so the notes survive a restart.

There are two reasonable designs for where the notes live on disk, and this
brief does not choose between them:

- `jsonfile` — the notes live in a single JSON file.
- `sqlite` — the notes live in a SQLite database file.

Choose one of the two and add `save(store, path)` and `load(path)` to
`notes/store.py`, using the design you chose: `save` writes a `Store`'s notes
to ``path``, and `load` reads them back into a new `Store` with the same note
ids. Add a unittest for the new functions in `tests_notes.py`, and make the
suite pass: `python3 -m unittest discover -q`.

Then create `t/` if it does not exist and write `t/answer.json` with this shape:

```json
{"decision": null, "reason": ""}
```

Set `decision` to the name of the design you chose (`jsonfile` or `sqlite`),
and `reason` to one or two sentences explaining the trade-off that decided it
for you.
