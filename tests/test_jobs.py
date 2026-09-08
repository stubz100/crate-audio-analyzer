"""Tests for the Recompute tab's engine (spec §9.6, Phase 8) — no Qt.

What the button runs is `recompute_attributes`; these pin the folder-scope
gate, the two modes, the stop flag, the empty-scope refusal and the
missing-model-stack path against a fake encoder.
"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf
from test_embedding import FakeEncoder

from crate.db import open_db
from crate.jobs import RecomputeSettings, recompute_attributes
from crate.scanner import scan_library

SR = 22050


def _clicks(times_s, duration_s):
    y = np.zeros(int(SR * duration_s))
    burst = int(0.02 * SR)
    for i, t in enumerate(times_s):
        s = int(t * SR)
        y[s : s + burst] += np.random.default_rng(i).normal(0, 0.5, burst)
    return np.clip(y, -1, 1).astype("float32")


@pytest.fixture()
def library(tmp_path):
    """Four files in three folders; `B_x` vs `Bax` guards the LIKE escaping."""
    lib = tmp_path / "lib"
    for folder in ("A", "B_x", "Bax"):
        (lib / folder).mkdir(parents=True)
    sf.write(lib / "A" / "loop.wav", _clicks([i * 0.5 for i in range(8)], 4.0), SR)
    sf.write(lib / "A" / "hit.wav", _clicks([0.0], 0.4), SR)
    sf.write(lib / "B_x" / "loop2.wav", _clicks([i * 0.5 for i in range(6)], 3.0), SR)
    sf.write(lib / "Bax" / "other.wav", _clicks([0.0, 0.7], 1.5), SR)
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    yield conn, lib
    conn.close()


def _count(conn, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def _analysed(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT s.filename FROM analysis a JOIN samples s ON s.id = a.sample_id"
        )
    }


def test_scope_gates_every_expensive_stage(library):
    conn, lib = library

    report = recompute_attributes(
        conn, RecomputeSettings(scope=(str(lib / "A"),)), encoder=FakeEncoder()
    )

    assert report.analysis.analyzed == 2
    assert report.segmentation.samples_segmented + report.segmentation.one_shots_skipped == 2
    assert report.embedding.samples_embedded == 2
    assert not report.stopped and not report.notes
    assert _analysed(conn) == {"loop.wav", "hit.wav"}
    assert _count(conn, "SELECT COUNT(*) FROM embedding") == 2
    text = report.format()
    assert "[analysis]" in text and "[segmentation]" in text and "[embedding]" in text


def test_an_underscore_in_a_scope_folder_is_not_a_wildcard(library):
    conn, lib = library

    report = recompute_attributes(
        conn, RecomputeSettings(scope=(str(lib / "B_x"),)), encoder=FakeEncoder()
    )

    assert report.analysis.analyzed == 1
    assert _analysed(conn) == {"loop2.wav"}       # not Bax/other.wav


def test_changed_only_then_force_full(library):
    conn, lib = library
    settings = RecomputeSettings(scope=(str(lib),))

    first = recompute_attributes(conn, settings, encoder=FakeEncoder())
    assert first.analysis.analyzed == 4 and first.embedding.samples_embedded == 4

    second = recompute_attributes(conn, settings, encoder=FakeEncoder())
    assert second.analysis.analyzed == 0 and second.embedding.samples_embedded == 0

    full = recompute_attributes(
        conn, RecomputeSettings(scope=(str(lib),), force_full=True), encoder=FakeEncoder()
    )
    assert full.analysis.analyzed == 4 and full.embedding.samples_embedded == 4


def test_stop_ends_the_stage_and_skips_the_rest(library):
    conn, lib = library
    polls: list[int] = []

    def stop() -> bool:
        polls.append(1)
        return len(polls) > 1                      # first file goes through

    report = recompute_attributes(
        conn, RecomputeSettings(scope=(str(lib),)), should_stop=stop, encoder=FakeEncoder()
    )

    assert report.stopped and report.analysis.stopped and report.analysis.analyzed == 1
    assert report.segmentation is None and report.embedding is None
    assert "[segmentation] not run" in report.format()
    assert _count(conn, "SELECT COUNT(*) FROM analysis") == 1   # committed work kept


def test_empty_scope_is_refused_before_anything_runs(library):
    conn, lib = library
    with pytest.raises(ValueError):
        recompute_attributes(conn, RecomputeSettings(), encoder=FakeEncoder())
    assert _count(conn, "SELECT COUNT(*) FROM analysis") == 0


class _NoModelStack:
    """What `ClapEncoder` does on a machine without the `ml` extra."""

    logit_scale = 1.0

    def embed_text(self, texts):
        raise ImportError("No module named 'torch'")

    def embed_audio(self, clips, sr):
        raise ImportError("No module named 'torch'")


def test_missing_model_stack_skips_embedding_with_a_note(library):
    conn, lib = library

    report = recompute_attributes(
        conn, RecomputeSettings(scope=(str(lib),)), encoder=_NoModelStack()
    )

    assert report.analysis.analyzed == 4 and report.segmentation is not None
    assert report.embedding is None and not report.stopped
    assert any("uv sync --extra ml" in note for note in report.notes)
    assert "[embedding] not run" in report.format()


class _BrokenModel:
    """A model stack that is installed but cannot load — no checkpoint in the
    cache and no network, the first-run failure mode."""

    logit_scale = 1.0

    def embed_text(self, texts):
        raise OSError("checkpoint not in cache and the hub is unreachable")

    def embed_audio(self, clips, sr):
        raise OSError("checkpoint not in cache and the hub is unreachable")


def test_a_failing_model_load_keeps_the_earlier_stages_in_the_report(library):
    conn, lib = library

    report = recompute_attributes(
        conn, RecomputeSettings(scope=(str(lib),)), encoder=_BrokenModel()
    )

    assert report.analysis.analyzed == 4 and report.segmentation is not None
    assert report.embedding is None and not report.stopped
    assert any("embedding stage failed" in note and "OSError" in note for note in report.notes)
    assert "[analysis]" in report.format() and "[embedding] not run" in report.format()

