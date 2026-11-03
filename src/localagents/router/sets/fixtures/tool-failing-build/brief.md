# The report library's build is red

You are working in a small Python project. Its build check
(`python3 -m unittest -q build_check`) used to pass; today it fails. Figure out
why, make the smallest change that fixes it, and run the build check again to
confirm it is green.

When you are done, create a `t/` directory if one does not exist yet and write
`t/answer.json` containing exactly one JSON object with the key `cause`: a
one-line explanation of the breakage that names the file whose change caused
it. For example: `{"cause": "…"}`.
