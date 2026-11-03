# The build went red somewhere in this history

`python3 -m unittest -q build_check` fails on the current checkout. It passed a few
commits ago; something between then and now introduced the failure. Walk the history
(a bisect or a careful read of the log and diffs) and name the commit that broke the
build check. Do not fix anything — this is an investigation only.

When you know, create a `t/` directory if one does not exist yet and write
`t/answer.json` containing exactly one JSON object with the key `commit`: the hash of
the commit that introduced the failure, at least seven characters. For example:
`{"commit": "…"}`.
