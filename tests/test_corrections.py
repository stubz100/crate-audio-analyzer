"""Phase 11 — the correction workflow (spec §4, §8, §11). No Qt.

What is asserted is the contract the panel and the stages rely on: a
correction is written with its facet's own flag and the other facet stays
free; re-analysis and re-classification honour exactly that flag; a reset
hands the facet back to the machine's own call from what the index already
holds; and the curated tags are the user's, case-insensitively, and never
the machine's.
"""

from __future__ import annotations

import pytest
from test_embedding import FakeEncoder, _clicks, _write

from crate.analysis import analyze_pending
from crate.catalog import load_sample, load_samples
from crate.corrections import (
    CONTENT_CLASSES,
    STRUCTURAL_TYPES,
    add_tag,
    all_tags,
    automatic_structural_type,
    load_classification,
    load_user_tags,
    remove_tag,
    reset_content_class,
    reset_structural_type,
    set_content_class,
    set_structural_type,
    suggested_tags,
)
from crate.db import open_db
from crate.embedding import embed_pending, reclassify
from crate.scanner import scan_library
from crate.segmentation import segment_pending

LABELS = {0.4: "a drum hit", 4.0: "a melodic loop"}


@pytest.fixture()
def indexed(tmp_path):
    """A 0.4 s hit and a 4.0 s eight-hit loop: scanned, analysed, segmented,
    embedded with the fake encoder — so every sample has a class, a type,
    four class scores and its chips."""
    lib = tmp_path / "lib"
    lib.mkdir()
    _write(lib / "hit.wav", _clicks([0.0], 0.4))
    _write(lib / "loop.wav", _clicks([i * 0.5 for i in range(8)], 4.0))
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    segment_pending(conn)
    embed_pending(conn, encoder=FakeEncoder(LABELS))
    ids = {r[0]: r[1] for r in conn.execute("SELECT filename, id FROM samples")}
    yield conn, ids
    conn.close()


def _flags(conn, sample_id):
    return tuple(conn.execute(
        "SELECT provenance, content_class_confirmed, structural_type_confirmed, is_user_confirmed "
        "FROM classification WHERE sample_id = ?", (sample_id,),
    ).fetchone())


def _segment_classes(conn, sample_id) -> set[str | None]:
    return {
        r[0] for r in conn.execute(
            "SELECT c.content_class FROM segment_classification c JOIN segments g ON g.id = c.segment_id "
            "WHERE g.sample_id = ?", (sample_id,),
        )
    }


# --- Facet A ---


def test_set_content_class_writes_its_flag_and_reaches_the_segments(indexed):
    conn, ids = indexed
    loop = ids["loop.wav"]
    assert _segment_classes(conn, loop), "the loop should have classified segments to inherit"
    assert set_content_class(conn, [loop], "vocal") == 1
    assert load_classification(conn, loop).content_class == "vocal"
    assert _flags(conn, loop) == ("manual", 1, 0, 1)
    assert _segment_classes(conn, loop) == {"vocal"}
    with pytest.raises(ValueError):
        set_content_class(conn, [loop], "drums")


def test_a_corrected_class_survives_reclassification_and_leaves_the_type_free(indexed):
    conn, ids = indexed
    hit = ids["hit.wav"]
    set_content_class(conn, [hit], "other")

    summary = reclassify(conn, encoder=FakeEncoder(LABELS))
    assert summary.protected == 1 and summary.classified == 1
    assert load_classification(conn, hit).content_class == "other"

    analysed = analyze_pending(conn, reanalyze=True)          # Facet B is not frozen by a Facet A correction
    assert analysed.skipped_confirmed == 0
    assert _flags(conn, hit) == ("manual", 1, 0, 1)            # ... and re-analysis did not loosen it


def test_reset_content_class_returns_to_claps_own_call(indexed):
    conn, ids = indexed
    hit = ids["hit.wav"]
    before = load_classification(conn, hit)
    assert before.content_class in CONTENT_CLASSES and before.class_scores
    set_content_class(conn, [hit], "vocal")
    assert reset_content_class(conn, [hit]) == 1
    after = load_classification(conn, hit)
    assert after.content_class == before.content_class == before.best_class
    assert not after.content_class_confirmed
    assert _flags(conn, hit) == ("automatic", 0, 0, 0)
    assert _segment_classes(conn, hit) <= {before.content_class}

    reset_content_class(conn, [hit], confidence_threshold=1.01)   # nothing is that sure: flagged
    flagged = load_classification(conn, hit)
    assert flagged.content_class is None and flagged.best_class == before.best_class


# --- Facet B ---


def test_set_structural_type_writes_its_flag(indexed):
    conn, ids = indexed
    hit = ids["hit.wav"]
    assert set_structural_type(conn, [hit], "loop") == 1
    assert load_classification(conn, hit).structural_type == "loop"
    assert _flags(conn, hit) == ("manual", 0, 1, 1)
    with pytest.raises(ValueError):
        set_structural_type(conn, [hit], "phrase")


def test_a_corrected_type_survives_reanalysis_and_leaves_the_class_free(indexed):
    conn, ids = indexed
    hit = ids["hit.wav"]
    set_structural_type(conn, [hit], "loop")

    analysed = analyze_pending(conn, reanalyze=True)
    assert analysed.skipped_confirmed == 1
    assert load_classification(conn, hit).structural_type == "loop"

    summary = reclassify(conn, encoder=FakeEncoder(LABELS))     # Facet A is not frozen by a Facet B correction
    assert summary.protected == 0
    assert _flags(conn, hit) == ("manual", 0, 1, 1)


def test_reset_structural_type_returns_to_the_rule(indexed):
    conn, ids = indexed
    loop = ids["loop.wav"]
    before = load_classification(conn, loop).structural_type
    assert before in STRUCTURAL_TYPES
    assert automatic_structural_type(conn, loop) == before
    set_structural_type(conn, [loop], "one-shot")
    assert reset_structural_type(conn, [loop]) == 1
    after = load_classification(conn, loop)
    assert after.structural_type == before and not after.structural_type_confirmed
    assert _flags(conn, loop) == ("automatic", 0, 0, 0)


def test_resetting_one_facet_keeps_the_other_protected(indexed):
    conn, ids = indexed
    hit = ids["hit.wav"]
    set_content_class(conn, [hit], "vocal")
    set_structural_type(conn, [hit], "multi-hit")
    assert _flags(conn, hit) == ("manual", 1, 1, 1)
    reset_content_class(conn, [hit])
    assert _flags(conn, hit) == ("manual", 0, 1, 1)
    assert load_classification(conn, hit).structural_type == "multi-hit"
    reset_structural_type(conn, [hit])
    assert _flags(conn, hit) == ("automatic", 0, 0, 0)


def test_corrections_apply_to_every_sample_given(indexed):
    conn, ids = indexed
    both = [ids["hit.wav"], ids["loop.wav"]]
    assert set_content_class(conn, both, "other") == 2
    assert {load_classification(conn, s).content_class for s in both} == {"other"}
    assert add_tag(conn, both, "field recording") == 2


# --- the curated tags ---


def test_tags_are_curated_case_insensitively_and_forgotten_when_unused(indexed):
    conn, ids = indexed
    hit, loop = ids["hit.wav"], ids["loop.wav"]
    assert add_tag(conn, [hit, loop], "Kick Drum") == 2
    assert add_tag(conn, [hit], "kick drum") == 0                 # already there, whatever the case
    assert load_user_tags(conn, hit) == ["Kick Drum"]             # spelled the way it was first written
    assert all_tags(conn) == [("Kick Drum", 2)]
    assert remove_tag(conn, [hit], "KICK DRUM") == 1
    assert all_tags(conn) == [("Kick Drum", 1)]
    assert remove_tag(conn, [loop], "kick drum") == 1
    assert all_tags(conn) == []                                   # no sample carries it: forgotten
    assert remove_tag(conn, [loop], "kick drum") == 0
    with pytest.raises(ValueError):
        add_tag(conn, [hit], "   ")


def test_suggested_tags_are_the_machines_minus_the_curated(indexed):
    conn, ids = indexed
    hit = ids["hit.wav"]
    suggested = suggested_tags(conn, hit, limit=3)
    assert len(suggested) == 3 and suggested == sorted(suggested, key=lambda p: -p[1])
    first = suggested[0][0]
    add_tag(conn, [hit], first.upper())                            # promoted, in any case
    assert first not in [name for name, _ in suggested_tags(conn, hit, limit=3)]
    assert len(suggested_tags(conn, hit, limit=3)) == 3           # the next one moved up


def test_list_rows_carry_the_curated_tags_and_the_flags(indexed):
    conn, ids = indexed
    hit, loop = ids["hit.wav"], ids["loop.wav"]
    add_tag(conn, [hit], "punchy")
    add_tag(conn, [hit], "Acoustic")
    set_structural_type(conn, [hit], "multi-hit")
    row = load_sample(conn, hit)
    assert row is not None and row.user_tags == "Acoustic, punchy"
    assert row.type_confirmed and not row.class_confirmed
    assert row.structural_type == "multi-hit"
    rows = {r.id: r for r in load_samples(conn)}
    assert rows[hit].user_tags == "Acoustic, punchy" and rows[loop].user_tags == ""
    assert not rows[loop].type_confirmed
    assert load_sample(conn, 10_000) is None
