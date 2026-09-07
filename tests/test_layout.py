"""Tests for node G — the map layout (spec §7, §8, §9.6) — no Qt.

UMAP is the `map` extra and its first call compiles for tens of seconds, so
these run on the PCA stand-in (deterministic, picklable, fast). One opt-in
test (`CRATE_REAL_UMAP=1`) fits the real thing.
"""

from __future__ import annotations

import os
import pickle

import numpy as np
import pytest
import soundfile as sf
from test_embedding import FakeEncoder

from crate.analysis import analyze_pending
from crate.db import open_db
from crate.embedding import embed_pending
from crate.layout import (
    LayoutSettings,
    PcaReducer,
    fit_layout,
    load_current_layout,
    place_anchor,
)
from crate.scanner import scan_library
from crate.segmentation import segment_pending
from crate.similarity import AXES, FeatureTable

SR = 22050
EQUAL = {axis: 1.0 for axis in AXES}
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
    tmp = tmp_path_factory.mktemp("layout")
    lib = tmp / "lib"
    (lib / "A").mkdir(parents=True)
    (lib / "B").mkdir()
    sf.write(lib / "A" / "kick.wav", _clicks([0.0], 0.4), SR)
    sf.write(lib / "A" / "loop.wav", _clicks([i * 0.5 for i in range(8)], 4.0), SR)
    sf.write(lib / "A" / "tone_a.wav", _tone(220, 0.8), SR)
    sf.write(lib / "B" / "tone_b.wav", _tone(220, 0.5), SR)
    sf.write(lib / "B" / "tone_c.wav", _tone(880, 0.8), SR)
    conn = open_db(tmp / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    segment_pending(conn)
    embed_pending(conn, encoder=FakeEncoder(LABELS))
    ids = {row[0]: row[1] for row in conn.execute("SELECT filename, id FROM samples")}
    yield conn, ids, tmp, lib
    conn.close()


def test_fit_layout_places_every_sample_in_scope_and_becomes_current(index):
    conn, ids, tmp, lib = index

    summary = fit_layout(
        conn, LayoutSettings(EQUAL, scope_description="whole index"), tmp / "models",
        reducer=PcaReducer(),
    )

    assert summary.placed == 5 and summary.reducer == "pca" and summary.layout_id > 0
    assert not summary.notes                                   # injected PCA is not a fallback
    info, positions = load_current_layout(conn)
    assert info is not None and info.id == summary.layout_id
    assert set(positions) == set(ids.values()) and info.weights == EQUAL and info.reducer == "pca"
    assert (tmp / "models" / f"layout_{summary.layout_id}.pkl").exists()

    def dist(a, b):
        return float(np.hypot(positions[a][0] - positions[b][0], positions[a][1] - positions[b][1]))

    assert dist(ids["tone_a.wav"], ids["tone_b.wav"]) < dist(ids["tone_a.wav"], ids["kick.wav"])


def test_a_second_fit_replaces_the_current_layout_and_keeps_the_old_rows(index):
    conn, ids, tmp, lib = index
    first, _ = load_current_layout(conn)

    summary = fit_layout(
        conn, LayoutSettings(EQUAL, scope=(str(lib / "A"),), scope_description="folder A"),
        tmp / "models", reducer=PcaReducer(),
    )

    assert summary.placed == 3
    info, positions = load_current_layout(conn)
    assert info.id != first.id and info.scope_description == "folder A" and len(positions) == 3
    assert conn.execute("SELECT COUNT(*) FROM map_layout WHERE is_current = 1").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM map_position").fetchone()[0] == 8   # 5 + 3, history kept


def test_place_anchor_transforms_one_sample_into_the_current_layout(index):
    conn, ids, tmp, lib = index
    info, positions = load_current_layout(conn)               # folder A: tone_c is not in it
    assert ids["tone_c.wav"] not in positions

    placed = place_anchor(conn, "sample", ids["tone_c.wav"])

    info2, positions2 = load_current_layout(conn)
    assert placed.layout_id == info.id and info2.id == info.id
    assert ids["tone_c.wav"] in positions2 and len(positions2) == 4
    place_anchor(conn, "sample", ids["tone_c.wav"])           # again: updated in place
    assert len(load_current_layout(conn)[1]) == 4

    seg_id = conn.execute(
        "SELECT id FROM segments WHERE sample_id = ? LIMIT 1", (ids["loop.wav"],)
    ).fetchone()[0]
    assert place_anchor(conn, "segment", seg_id).sample_id == ids["loop.wav"]   # its parent

    before = positions[ids["tone_a.wav"]]                      # already in the fit: same spot
    again = place_anchor(conn, "sample", ids["tone_a.wav"])
    assert again.x == pytest.approx(before[0], abs=1e-6) and again.y == pytest.approx(before[1], abs=1e-6)


def test_layout_refusals_and_stop(index, tmp_path):
    conn, ids, tmp, lib = index
    with pytest.raises(ValueError):
        fit_layout(conn, LayoutSettings({axis: 0.0 for axis in AXES}), tmp_path, reducer=PcaReducer())
    with pytest.raises(ValueError):                            # too few samples in scope
        fit_layout(conn, LayoutSettings(EQUAL, scope=(str(lib / "nowhere"),)), tmp_path, reducer=PcaReducer())

    current, _ = load_current_layout(conn)
    stopped = fit_layout(conn, LayoutSettings(EQUAL), tmp_path, reducer=PcaReducer(), should_stop=lambda: True)
    assert stopped.stopped and "nothing written" in stopped.format()
    assert load_current_layout(conn)[0].id == current.id      # untouched

    with pytest.raises(LookupError):
        place_anchor(conn, "segment", 999_999)
    empty = open_db(tmp_path / "empty.db")
    with pytest.raises(ValueError):                            # no layout yet
        place_anchor(empty, "sample", 1)
    assert load_current_layout(empty) == (None, {})
    empty.close()


def test_weighted_matrix_reflects_the_weights(index):
    conn, ids, tmp, lib = index
    table = FeatureTable.load(conn)
    rows = table.sample_rows()

    full = table.weighted_matrix(rows, EQUAL)
    only_clap = table.weighted_matrix(rows, {"conceptual": 1.0})

    assert full.shape[0] == rows.size and only_clap.shape[1] == 32      # the fake encoder's DIM
    assert full.shape[1] == only_clap.shape[1] + 5 + 1 + 33 + 4
    assert not np.isnan(full).any()
    with pytest.raises(ValueError):
        table.weighted_matrix(rows, {})


def test_pca_reducer_round_trips_through_pickle():
    matrix = np.random.default_rng(0).normal(size=(20, 6))
    reducer = PcaReducer()
    xy = reducer.fit_transform(matrix)
    again = pickle.loads(pickle.dumps(reducer))
    assert xy.shape == (20, 2) and np.allclose(again.transform(matrix), xy)


@pytest.mark.skipif(not os.environ.get("CRATE_REAL_UMAP"), reason="set CRATE_REAL_UMAP=1 to fit real UMAP")
def test_real_umap_fit_and_transform(index, tmp_path):
    conn, ids, tmp, lib = index
    summary = fit_layout(conn, LayoutSettings(EQUAL), tmp_path)
    assert summary.reducer == "umap" and summary.placed == 5
    placed = place_anchor(conn, "sample", ids["kick.wav"])
    assert np.isfinite([placed.x, placed.y]).all()
