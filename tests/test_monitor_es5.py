"""The page scripts are ES5 (3.7v's lane, 2026-10-30: the briefs said "a test pins it" and none
did). The monitor serves static/*.js to whatever browser the operator has; the house rule is no
`let`/`const`, no arrow functions, no template strings. Comments are stripped first (the prose
quotes code in backticks), strings are not: a `=>` or a backtick inside a string literal would
be a false alarm worth a look rather than a construct this test misses."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "localagents" / "monitor" / "static"

_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_LINE = re.compile(r"(?m)(?<![:\"'])//.*$")  # a `//` after a colon or a quote is a URL, not a comment
_CONSTRUCTS = {
    "let/const": re.compile(r"\b(let|const)\s+[A-Za-z_$]"),
    "arrow function": re.compile(r"=>"),
    "template string": re.compile(r"`"),
}


def _code(path: Path) -> str:
    return _LINE.sub("", _BLOCK.sub("", path.read_text()))


def test_the_page_scripts_are_es5():
    found = []
    for js in sorted(STATIC.glob("*.js")):
        code = _code(js)
        for name, rx in _CONSTRUCTS.items():
            for m in rx.finditer(code):
                line = code.count("\n", 0, m.start()) + 1
                found.append(f"{js.name}:{line} {name}")
    assert found == [], found
