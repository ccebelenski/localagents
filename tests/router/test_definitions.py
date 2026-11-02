"""router 1 (docs/superpowers/specs/2026-11-02-router-design.md §5): the dimension definitions
document — the shipped draft, its validation, d3's request shape, and the versioned storage in
``dimension_definitions``. The draft text itself is owned by the calibration set spec
(2026-11-02-calibration-set-design.md, Appendix A and the §4 complexity rubric)."""

import pytest

from localagents.router import definitions


def test_default_document_validates_and_has_the_seven_dimensions():
    definitions.validate(definitions.DEFAULT_DOCUMENT)  # the shipped draft must pass its own check
    assert set(definitions.DEFAULT_DOCUMENT) == {
        "software_dev", "language", "tool_use", "computer_use", "complexity", "specification",
        "context_load",
    }
    assert definitions.THRESHOLDS == {"choice": 0.5, "score": 0.4, "noul": 0.2}


def test_questions_shape_is_what_d3_takes():
    q = definitions.questions(definitions.DEFAULT_DOCUMENT)
    assert q["complexity"]["type"] == "score"
    # d3 takes a score's criteria as an ordered list of levels, band 0 first (not a dict: the
    # live server answers invalid_question for a dict)
    assert isinstance(q["complexity"]["criteria"], list) and len(q["complexity"]["criteria"]) == 5
    assert q["complexity"]["criteria"] == [definitions.DEFAULT_DOCUMENT["complexity"]["criteria"][b] for b in "01234"]
    assert q["language"]["type"] == "choice"
    assert isinstance(q["language"]["criteria"], dict) and "none" in q["language"]["criteria"]
    for name, entry in q.items():
        assert entry["instructions"].strip(), name
        assert entry["criteria"], name
        texts = entry["criteria"] if isinstance(entry["criteria"], list) else list(entry["criteria"].values())
        assert all(d.strip() for d in texts), name


def test_load_seeds_version_1_and_save_increments(conn):
    d1 = definitions.load(conn)
    assert d1.version == 1 and d1.document == definitions.DEFAULT_DOCUMENT
    # the table carries it now, and a second load answers the same version (no reseeding)
    assert definitions.load(conn).version == 1
    doc = {name: dict(entry) for name, entry in definitions.DEFAULT_DOCUMENT.items()}
    d2 = definitions.save(conn, doc)
    assert d2.version == 2 and d2.document == doc
    assert definitions.load(conn).version == 2


def test_save_rejects_a_document_missing_a_dimension(conn):
    doc = {k: v for k, v in definitions.DEFAULT_DOCUMENT.items() if k != "complexity"}
    with pytest.raises(ValueError, match="complexity"):
        definitions.save(conn, doc)
    # the rejection wrote nothing: not even the seed
    assert conn.execute("SELECT COUNT(*) FROM dimension_definitions").fetchone()[0] == 0
