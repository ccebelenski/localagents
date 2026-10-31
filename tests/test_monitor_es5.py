"""The page scripts are ES5 (3.7v's lane, 2026-10-30: the briefs said "a test pins it" and none
did). The monitor serves static/*.js to whatever browser the operator has; the house rule is no
`let`/`const`, no arrow functions, no template strings. Comments are stripped first (the prose
quotes code in backticks), and string literals are blanked before the comments (F2, fold of the
page-batch review 2026-10-30: a `//` inside a string used to eat the rest of its line and hide a
real `const`). Backticks are NOT blanked — a template string is what this gate hunts."""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "localagents" / "monitor" / "static"

# F2: double- and single-quoted literals without a newline, escapes allowed; each becomes "".
# Applied before the comment strippers (a `//`/`/*` inside a string is text, not a comment).
_STR = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_LINE = re.compile(r"(?m)(?<![:\"'])//.*$")  # a `//` after a colon or a quote is a URL, not a comment
_CONSTRUCTS = {
    "let/const": re.compile(r"\b(let|const)\s+[A-Za-z_$]"),
    "arrow function": re.compile(r"=>"),
    "template string": re.compile(r"`"),
}


def _code(source) -> str:
    """The comment-stripped code of a page script. `source` is source text or a Path (the
    parametrised strings below feed it text directly)."""
    text = source.read_text() if isinstance(source, Path) else source
    # strings first: a `//` or `/*` inside one must not be read as a comment (and a backtick
    # inside one must not read as a template string); backticks themselves are untouched
    return _LINE.sub("", _BLOCK.sub("", _STR.sub('""', text)))


def test_the_page_scripts_are_es5():
    found = []
    for js in sorted(STATIC.glob("*.js")):
        code = _code(js)
        for name, rx in _CONSTRUCTS.items():
            for m in rx.finditer(code):
                line = code.count("\n", 0, m.start()) + 1
                found.append(f"{js.name}:{line} {name}")
    assert found == [], found


# F2 (fold of the page-batch review, 2026-10-30): the gate used to strip comments without
# knowing string literals, so a `//` inside a string ate the rest of the physical line and a
# real `const` after it escaped. Strings are now blanked first; a backtick is not blanked (a
# template string is exactly what the gate hunts).
@pytest.mark.parametrize("src, caught", [
    ('var u = "http://x/y"; // c', None),
    ("var u = 'http://x/y'; // c", None),
    ("/* a `b` */ var c = 1;", None),
    ("const x = 1;", "let/const"),
    ("var f = (a) => a;", "arrow function"),
    ("var s = `t`;", "template string"),
    ('var s = "a // b"; const y = 2;', "let/const"),
    ("var s = 'a // b'; const y = 2;", "let/const"),
    ('x = "a"// c', None),
    (r'var s = "a \" // b"; const y = 2;', "let/const"),
    ('var s = "`t`"; const y = 2;', "let/const"),
])
def test_the_gate_reads_through_string_literals(src, caught):
    code = _code(src)
    hits = [name for name, rx in _CONSTRUCTS.items() if rx.search(code)]
    if caught is None:
        assert hits == [], (src, code, hits)
    else:
        assert caught in hits, (src, code, hits)
    # a backtick inside a double-quoted string is text, not a template string
    if caught == "let/const":
        assert "template string" not in hits, (src, code, hits)
