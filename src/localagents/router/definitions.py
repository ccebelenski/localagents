"""The router's dimension definitions (docs/superpowers/specs/2026-11-02-router-design.md §5):
a versioned JSON document, the seven questions d3 asks of every brief, and the acceptance
thresholds per question type.

The document text shipped here is a DRAFT owned by the calibration set spec
(docs/superpowers/specs/2026-11-02-calibration-set-design.md): its Appendix A is the question
wording, its §4 the complexity rubric, and its iteration is what edits this text — a saved
version in ``dimension_definitions`` is what the scorecard cells and verdicts cite with
``defs_version``, so the shipped draft is only the seed, never the live truth."""

import copy
import json
import sqlite3
import time
from dataclasses import dataclass

from localagents import db

# the seven dimensions, in the order d3's request carries them (router-design spec §5)
DIMENSIONS = ("software_dev", "language", "tool_use", "computer_use", "complexity", "specification",
              "context_load")
BANDS = ("0", "1", "2", "3", "4")
# the acceptance bar per question type (calibration set spec; the spike's confidence field and
# top-option probability behave differently, so the bar is per type)
THRESHOLDS = {"choice": 0.5, "score": 0.4, "noul": 0.2}

# the shipped draft (calibration set spec Appendix A; the language one-liners and complexity's
# question line are the draft wording, the calibration spec owns all of it)
DEFAULT_DOCUMENT: dict[str, dict] = {
    "software_dev": {
        "type": "score",
        "instruction": "How much code must the agent write or change?",
        "criteria": {
            "0": "none",
            "1": "a line or a config value",
            "2": "one function or one file",
            "3": "several files or a new module",
            "4": "a component or a system",
        },
    },
    "language": {
        "type": "choice",
        "instruction": "Which programming language does the brief require work in?",
        "criteria": {
            "python": "Python (.py)",
            "javascript": "JavaScript (.js, .jsx; Node, browsers)",
            "typescript": "TypeScript (.ts, .tsx)",
            "go": "Go (.go)",
            "rust": "Rust (.rs, Cargo)",
            "java": "Java (.java, Maven or Gradle)",
            "c": "C (.c, .h)",
            "cpp": "C++ (.cpp, .hpp)",
            "shell": "shell scripting (bash, sh, zsh) as the deliverable, not a tool driven from another language",
            "none": "no particular language, or prose/ops only",
        },
    },
    "tool_use": {
        "type": "score",
        "instruction": "How much must the agent drive tools (shell, tests, git, probes) rather than read and write text?",
        "criteria": {
            "0": "none",
            "1": "run one command",
            "2": "run and read tests or builds",
            "3": "investigate across several tools and iterate",
            "4": "orchestrate a multi-step procedure with checks",
        },
    },
    "computer_use": {
        "type": "score",
        "instruction": "Must the agent operate a live system?",
        "criteria": {
            "0": "no",
            "1": "read a running service's state",
            "2": "drive a service or browser through its protocol",
            "3": "operate a node or deployment",
            "4": "several systems in concert",
        },
    },
    "complexity": {
        "type": "score",
        "instruction": "How much scope and judgement does the brief carry?",
        "criteria": {
            "0": "a command and a one-line report",
            "1": "one function or one file with one clear change",
            "2": "one module with several parts, or state to keep straight",
            "3": "several files, or a design the brief does not spell out",
            "4": "several components with open design questions",
        },
    },
    "specification": {
        "type": "score",
        "instruction": "How much does the brief leave open?",
        "criteria": {
            "0": "fully specified, exact edits",
            "1": "clear intent, obvious choices",
            "2": "some choices left to the agent",
            "3": "the design is the agent's",
            "4": "open questions the agent must settle and justify",
        },
    },
    "context_load": {
        "type": "score",
        "instruction": "How much must the agent hold at once?",
        "criteria": {
            "0": "one small file",
            "1": "a few files",
            "2": "a module",
            "3": "a subsystem",
            "4": "the whole project",
        },
    },
}


def validate(document) -> None:
    """The document's shape: all seven dimensions, each an entry with the type the spec pins
    for it (``choice`` for language, ``score`` for the other six — §5), a non-empty
    instruction and a non-empty criteria. A ``score`` entry's criteria are keyed exactly by the
    five bands; a ``choice`` entry's by its options, ``none`` among them. Raises ValueError
    naming the missing or malformed key."""
    if not isinstance(document, dict):
        raise ValueError(f"the document is a mapping of dimension to entry, got {type(document).__name__}")
    missing = [d for d in DIMENSIONS if d not in document]
    if missing:
        raise ValueError(f"the document is missing the dimensions: {', '.join(missing)}")
    unknown = sorted(set(document) - set(DIMENSIONS))
    if unknown:
        raise ValueError(f"the document carries dimensions the router does not ask: {', '.join(unknown)}")
    for name in DIMENSIONS:
        entry = document[name]
        if not isinstance(entry, dict):
            raise ValueError(f"{name}: an entry is a mapping, got {type(entry).__name__}")
        # the spec pins each dimension's kind (§5): language is the one choice question
        # (its answer is an option), the other six score (their answer is a band). A
        # revision that reshaped one would move every scorecard cell citing it
        expected_type = "choice" if name == "language" else "score"
        if entry.get("type") != expected_type:
            raise ValueError(f"{name}: type is '{expected_type}', got {entry.get('type')!r}")
        instruction = entry.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError(f"{name}: the instruction is a non-empty string")
        criteria = entry.get("criteria")
        if not isinstance(criteria, dict) or not criteria:
            raise ValueError(f"{name}: criteria is a non-empty mapping")
        if not all(isinstance(k, str) and k and isinstance(v, str) and v.strip() for k, v in criteria.items()):
            raise ValueError(f"{name}: every criterion is a non-empty string under a non-empty key")
        if entry["type"] == "score":
            if set(criteria) != set(BANDS):
                raise ValueError(f"{name}: a score's criteria are keyed exactly by the bands 0..4")
        elif "none" not in criteria:
            # language is neutral when none or uncertain (router-design §5), so none must be askable
            raise ValueError(f"{name}: a choice over languages carries 'none'")


def questions(document: dict) -> dict:
    """The document in d3's request shape: ``{name: {type, instructions, criteria}}``, the
    seven in ``DIMENSIONS`` order. The stored entry is keyed ``instruction`` (it is the
    definitions text); the request field d3 takes is ``instructions``. A ``choice`` sends its
    criteria as the option→definition mapping; a ``score`` sends an ORDERED LIST of level texts,
    band 0 first — d3 answers a dict-shaped score with ``invalid_question: score criteria must
    be an ordered list of 2 to 10 levels`` (the first live scoring of real briefs, 2026-11-02),
    and its answer's ``probabilities`` come back keyed by the level's index as a string."""
    out = {}
    for name in DIMENSIONS:
        entry = document[name]
        criteria = entry["criteria"]
        if entry["type"] == "score":
            criteria = [criteria[b] for b in BANDS]
        else:
            criteria = dict(criteria)
        out[name] = {"type": entry["type"], "instructions": entry["instruction"], "criteria": criteria}
    return out


@dataclass
class Definitions:
    version: int
    document: dict


def load(conn: sqlite3.Connection) -> Definitions:
    """The newest definitions version; an empty table seeds ``DEFAULT_DOCUMENT`` as version 1.
    The seed runs under ``BEGIN IMMEDIATE`` and re-checks, so two servers opening a fresh
    store at once insert the seed exactly once."""
    row = conn.execute("SELECT version, document FROM dimension_definitions"
                       " ORDER BY version DESC LIMIT 1").fetchone()
    if row is not None:
        return Definitions(row["version"], json.loads(row["document"]))
    with db.immediate(conn):
        row = conn.execute("SELECT version, document FROM dimension_definitions"
                           " ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO dimension_definitions (version, document, created)"
                         " VALUES (1, ?, ?)", (json.dumps(DEFAULT_DOCUMENT), time.time()))
            return Definitions(1, copy.deepcopy(DEFAULT_DOCUMENT))
    return Definitions(row["version"], json.loads(row["document"]))


def save(conn: sqlite3.Connection, document: dict) -> Definitions:
    """Validate, then append the next version and return it. Nothing is written when the
    document is malformed (validate first, outside any transaction)."""
    validate(document)
    with db.immediate(conn):
        version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM dimension_definitions").fetchone()[0]
        conn.execute("INSERT INTO dimension_definitions (version, document, created)"
                     " VALUES (?, ?, ?)", (version, json.dumps(document), time.time()))
    return Definitions(version, copy.deepcopy(document))
