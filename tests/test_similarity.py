"""Tests for the similarity layer (spec §5.1 blend, §9.4 sub-hits, §9.5
anchor distances, §5.2 text search) and the list's filter criteria — no Qt.

Audio is synthetic: a click (one-shot), a click loop (segments), and three
tones — two at the same pitch, one two octaves up. The fake encoder labels
audio by clip length, so text search has known answers.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import soundfile as sf
from test_embedding import FakeEncoder

from crate.analysis import analyze_pending
from crate.catalog import Criteria, SampleRow, describe_item, load_tags
from crate.db import open_db
from crate.embedding import embed_pending
from crate.scanner import scan_library
from crate.segmentation import segment_pending
from crate.similarity import AXES, FeatureTable, Scores

SR = 22050
LABELS = {0.4: "kick drum", 0.5: "kick drum", 4.0: "a synth pad", 1.0: "a sine tone"}


def _clicks(times_s, duration_s):
    y = np.zeros(int(SR * duration_s))
    burst = int(0.02 * SR)
    for i, t in enumerate(times_s):
        s = int(t * SR)
        y[s : s + burst] += np.random.default_rng(i).normal(0, 0.5, burst)
    return np.clip(y, -1, 1).astype("float32")


def _tone(hz: float, amplitude: float, duration_s: float = 1.0):
    t = np.arange(int(SR * duration_s)) / SR
    return (amplitude * np.sin(2 * np.pi * hz * t)).astype("float32")


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sim")
    lib = tmp / "lib"
    lib.mkdir()
    sf.write(lib / "kick.wav", _clicks([0.0], 0.4), SR)
    sf.write(lib / "loop.wav", _clicks([i * 0.5 for i in range(8)], 4.0), SR)
    sf.write(lib / "tone_a.wav", _tone(220, 0.8), SR)
    sf.write(lib / "tone_b.wav", _tone(220, 0.5), SR)
    sf.write(lib / "tone_c.wav", _tone(880, 0.8), SR)
    conn = open_db(tmp / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    segment_pending(conn)
    embed_pending(conn, encoder=FakeEncoder(LABELS))
    ids = {
        row[0]: row[1]
        for row in conn.execute("SELECT filename, id FROM samples")
    }
    yield conn, ids
    conn.close()


@pytest.fixture(scope="module")
def table(index):
    conn, ids = index
    return FeatureTable.load(conn)


def test_feature_table_holds_samples_and_their_segments(index, table):
    conn, ids = index
    segments = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    assert segments > 0
    assert len(table) == 5 + segments
    assert table.row_of("sample", ids["kick.wav"]) is not None
    seg_id = conn.execute("SELECT id FROM segments LIMIT 1").fetchone()[0]
    assert table.row_of("segment", seg_id) is not None
    assert table.row_of("sample", 999_999) is None


def test_distances_zero_at_the_anchor_and_pitch_orders_by_octave(index, table):
    conn, ids = index
    anchor = table.row_of("sample", ids["tone_a.wav"])
    d = table.distances(anchor)

    assert d.shape == (len(table), len(AXES))
    own = d[anchor]
    assert all(v == 0.0 for v in own if not math.isnan(v)) and not math.isnan(own[0])
    assert np.nanmax(d) <= 1.0 and np.nanmin(d) >= 0.0

    pitch = AXES.index("pitch")
    same = d[table.row_of("sample", ids["tone_b.wav"]), pitch]
    octaves = d[table.row_of("sample", ids["tone_c.wav"]), pitch]
    assert same < octaves                                   # same pitch is closer
    assert math.isnan(d[table.row_of("sample", ids["kick.wav"]), pitch])   # unpitched: no pitch axis


def test_blend_weights_axes_and_ignores_what_an_item_lacks():
    d = np.array([
        [0.2, np.nan, 0.4, 0.6, 0.0],
        [np.nan, np.nan, np.nan, np.nan, np.nan],
    ])
    equal = {axis: 1.0 for axis in AXES}
    blended = FeatureTable.blend(d, equal)
    assert blended[0] == pytest.approx((0.2 + 0.4 + 0.6 + 0.0) / 4)   # pitch (NaN) left out
    assert math.isnan(blended[1])
    only_pitch = {axis: (1.0 if axis == "pitch" else 0.0) for axis in AXES}
    assert math.isnan(FeatureTable.blend(d, only_pitch)[0])           # the one axis it lacks
    assert math.isnan(FeatureTable.blend(d, {axis: 0.0 for axis in AXES})[0])


def test_rank_puts_the_anchor_first_and_its_twin_next(index, table):
    conn, ids = index
    anchor = table.row_of("sample", ids["tone_a.wav"])
    scores = table.rank(table.distances(anchor), {axis: 1.0 for axis in AXES})

    assert scores.sample[ids["tone_a.wav"]] == pytest.approx(1.0)
    assert scores.sample[ids["tone_b.wav"]] > scores.sample[ids["kick.wav"]]
    assert scores.sample[ids["tone_b.wav"]] > scores.sample[ids["tone_c.wav"]]
    ranked = sorted(scores.sample, key=scores.sample.get, reverse=True)
    assert ranked[:2] == [ids["tone_a.wav"], ids["tone_b.wav"]]


def test_rank_scope_restricts_to_the_given_samples(index, table):
    conn, ids = index
    anchor = table.row_of("sample", ids["tone_a.wav"])
    scores = table.rank(table.distances(anchor), {axis: 1.0 for axis in AXES}, {ids["kick.wav"]})
    assert set(scores.sample) == {ids["kick.wav"]}
    assert not scores.hits


def test_search_folds_a_winning_segment_into_a_hit(index, table):
    conn, ids = index
    encoder = FakeEncoder(LABELS)

    kicks = table.search(encoder.embed_text(["kick drum"])[0])
    assert kicks.sample[ids["kick.wav"]] == pytest.approx(1.0)
    hit = kicks.hits.get(ids["loop.wav"])                    # the loop's segments are "kick drum"
    assert hit is not None and hit.score == pytest.approx(1.0)
    assert kicks.sample[ids["loop.wav"]] == pytest.approx(1.0)   # the parent inherits its best hit
    assert kicks.segment[hit.segment_id] == pytest.approx(1.0)
    assert hit.end_ms > hit.start_ms

    pads = table.search(encoder.embed_text(["a synth pad"])[0])
    assert pads.sample[ids["loop.wav"]] == pytest.approx(1.0)
    assert ids["loop.wav"] not in pads.hits                  # its own vector wins: no sub-hit
    assert ids["loop.wav"] == max(pads.sample, key=pads.sample.get)


def test_axis_distances_by_sample_and_labels(index, table):
    conn, ids = index
    anchor = table.row_of("sample", ids["tone_a.wav"])
    by_sample = table.axis_distances_by_sample(table.distances(anchor))
    assert set(by_sample) == set(ids.values())
    assert by_sample[ids["tone_a.wav"]]["amplitude"] == 0.0
    assert describe_item(conn, "sample", ids["kick.wav"]) == "kick.wav"
    seg_id, start = conn.execute("SELECT id, start_ms FROM segments ORDER BY id LIMIT 1").fetchone()
    assert describe_item(conn, "segment", seg_id).startswith(f"hit @ {start / 1000:.3f} s")
    assert describe_item(conn, "sample", 999_999) is None
    tags = load_tags(conn, ids["kick.wav"])
    assert tags and tags == sorted(tags, key=lambda t: -t[1])


def _row(**overrides) -> SampleRow:
    base = dict(
        id=1, filepath="x", filename="x.wav", folder="", duration_s=1.0,
        structural_type="one-shot", content_class="rhythmic", confidence=0.9,
        tempo_bpm=None, key=None, tags="", segment_count=0, flagged_segments=0,
    )
    base.update(overrides)
    return SampleRow(**base)


def test_criteria_rules():
    assert Criteria().accepts(_row(), None)
    assert not Criteria(clap_min=(("melodic", 0.5),)).accepts(_row(), None)          # no numbers: out
    assert Criteria(clap_min=(("melodic", 0.5),)).accepts(_row(clap_scores={"melodic": 0.7}), None)
    assert not Criteria(clap_min=(("melodic", 0.5),)).accepts(_row(clap_scores={"melodic": 0.2}), None)
    assert not Criteria(types=frozenset({"loop"})).accepts(_row(), None)
    assert not Criteria(duration_s=(2.0, None)).accepts(_row(), None)
    assert Criteria(duration_s=(0.5, 1.5)).accepts(_row(), None)
    assert not Criteria(tempo_bpm=(100, 130)).accepts(_row(), None)          # no tempo: out
    assert Criteria(tempo_bpm=(100, 130)).accepts(_row(tempo_bpm=120), None)
    assert not Criteria(tempo_bpm=(None, 100)).accepts(_row(tempo_bpm=120), None)
    narrowed = Criteria(axis_ranges=(("pitch", 0.0, 0.3),))
    assert not narrowed.accepts(_row(), None)                                # no anchor: out
    assert not narrowed.accepts(_row(), {"pitch": float("nan")})             # unpitched: out
    assert narrowed.accepts(_row(), {"pitch": 0.1})
    assert not narrowed.accepts(_row(), {"pitch": 0.5})


def test_scores_default_empty():
    assert Scores().sample == {} and Scores().hits == {}
