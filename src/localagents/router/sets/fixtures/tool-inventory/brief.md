# Take inventory of the archive tree

The `tree/` directory holds the archive service's files. Count what is there: the
number of files under `tree/` per extension (the extension is the part of the file
name after the last dot, lowercased, without the dot), and the number of lines
anywhere under `tree/` that contain `RETRY` — an exact, case-sensitive substring; a
line counts once no matter how many times it contains it.

When you are done, create a `t/` directory if one does not exist yet and write
`t/answer.json` containing exactly one JSON object with the keys `by_extension`: a
mapping from extension (no dot) to file count, and `matching_lines`: the number of
lines containing `RETRY`. For example:
`{"by_extension": {"txt": 1}, "matching_lines": 1}`.
