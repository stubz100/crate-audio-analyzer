"""Tests for CLAP embedding, zero-shot tags and Facet A — nodes D/C2/X/E
(spec §7, Phase 4). Everything here runs against a fake encoder; the real
model is exercised only by the opt-in smoke test at the bottom."""

from __future__ import annotations

import hashlib
import os

import numpy as np
import pytest
import soundfile as sf

from crate.analysis import analyze_pending
from crate.db import open_db
from crate.embedding import (
    CLAP_MAX_SECONDS,
    CLAP_SAMPLE_RATE,
    CLASS_PROMPTS,
    SOURCE_CLASS,
    SOURCE_TAGS,
    EmbedSettings,
    Prompts,
    blob_to_vector,
    classify,
    crop_for_clap,
    embed_pending,
    reclassify,
    vector_to_blob,
)
from crate.scanner import scan_library
from crate.segmentation import create_manual_segment, segment_pending, update_segment

SR = 22050
DIM = 32


class FakeEncoder:
    """Text → a deterministic unit vector per string. Audio → the vector of a
    prompt chosen by the clip's *duration* (tests control durations), so a
    clip can be made to "sound like" any prompt. Counts audio calls."""

    def __init__(self, labels_by_seconds: dict[float, str] | None = None, logit_scale: float = 30.0):
        self.labels = labels_by_seconds or {}
        self.logit_scale = logit_scale
        self.audio_calls = 0
        self.clips_seen = 0

    def embed_text(self, texts):
        out = []
        for t in texts:
            seed = int(hashlib.md5(t.encode()).hexdigest(), 16) % (2**32)
            v = np.random.default_rng(seed).normal(size=DIM)
            out.append(v / np.linalg.norm(v))
        return np.asarray(out, dtype=np.float32)

    def embed_audio(self, clips, sr):
        assert sr == CLAP_SAMPLE_RATE
        self.audio_calls += 1
        self.clips_seen += len(clips)
        labels = []
        for c in clips:
            seconds = round(len(c) / sr, 1)
            labels.append(self.labels.get(seconds, "an unrelated sound"))
        return self.embed_text(labels)


def _write(path, y, sr=SR):
    sf.write(path, np.asarray(y, dtype="float32"), sr, subtype="PCM_16")


def _clicks(times_s, duration_s):
    y = np.zeros(int(SR * duration_s))
    burst = int(0.02 * SR)
    for i, t in enumerate(times_s):
        s = int(t * SR)
        y[s : s + burst] += np.random.default_rng(i).normal(0, 0.5, burst)
    return np.clip(y, -1, 1)


@pytest.fixture()
def library(tmp_path):
    """A 0.4 s hit and a 4.0 s eight-hit loop, scanned, analysed, segmented."""
    lib = tmp_path / "lib"
    lib.mkdir()
    _write(lib / "hit.wav", _clicks([0.0], 0.4))
    _write(lib / "loop.wav", _clicks([i * 0.5 for i in range(8)], 4.0))
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    segment_pending(conn)
    yield lib, conn
    conn.close()


# --- pure pieces ------------------------------------------------------------------


def test_vector_blob_round_trip():
    v = np.random.default_rng(0).normal(size=DIM).astype(np.float32)
    assert np.array_equal(blob_to_vector(vector_to_blob(v)), v)
    assert blob_to_vector(vector_to_blob(v.astype(np.float64))).dtype == np.float32


def test_projected_features_accepts_both_transformers_return_shapes():
    import types

    from crate.embedding import projected_features

    class Tensor:  # the 4.x shape: the tensor itself
        def detach(self): return self
        def cpu(self): return self
        def numpy(self): return np.ones((2, 4), dtype=np.float32)

    output = types.SimpleNamespace(pooler_output=Tensor(), last_hidden_state=None)  # the 5.x shape
    assert projected_features(Tensor()).shape == (2, 4)
    assert projected_features(output).shape == (2, 4)


def test_crop_is_deterministic_first_window():
    clip = np.arange(int(CLAP_MAX_SECONDS * CLAP_SAMPLE_RATE) + 1000, dtype=np.float32)
    cropped = crop_for_clap(clip, CLAP_SAMPLE_RATE)
    assert cropped.size == int(CLAP_MAX_SECONDS * CLAP_SAMPLE_RATE)
    assert cropped[0] == 0.0                           # the first window, not a random one
    short = np.ones(100, dtype=np.float32)
    assert crop_for_clap(short, CLAP_SAMPLE_RATE).size == 100


def test_classify_assigns_the_matching_class_and_ranks_tags():
    enc = FakeEncoder()
    prompts = Prompts.build(enc)
    settings = EmbedSettings(confidence_threshold=0.5, top_k_tags=3)
    audio = enc.embed_text([CLASS_PROMPTS["vocal"][1]])[0]     # "a person singing"

    result = classify(audio, prompts, enc.logit_scale, settings)

    assert result.content_class == "vocal"
    assert result.confidence > 0.5
    assert abs(sum(result.class_scores.values()) - 1.0) < 1e-6
    assert len(result.tags) == 3 and result.tags[0][1] >= result.tags[-1][1]


def test_classify_flags_below_threshold_but_keeps_the_guess():
    enc = FakeEncoder(logit_scale=1.0)                          # flat distribution
    prompts = Prompts.build(enc)
    audio = enc.embed_text(["something"])[0]

    result = classify(audio, prompts, enc.logit_scale, EmbedSettings(confidence_threshold=0.9))

    assert result.content_class is None
    assert result.best_class in CLASS_PROMPTS
    assert 0.0 < result.confidence < 0.9


def test_tie_break_uses_harmonic_ratio_for_rhythmic_vs_melodic():
    enc = FakeEncoder()
    prompts = Prompts.build(enc)
    # Halfway between a rhythmic and a melodic prompt: a genuine tie.
    r = enc.embed_text([CLASS_PROMPTS["rhythmic"][0]])[0]
    m = enc.embed_text([CLASS_PROMPTS["melodic"][0]])[0]
    mid = (r + m) / np.linalg.norm(r + m)
    settings = EmbedSettings(confidence_threshold=0.0)

    tonal = classify(mid, prompts, enc.logit_scale, settings, harmonic_ratio=0.95)
    noisy = classify(mid, prompts, enc.logit_scale, settings, harmonic_ratio=0.05)
    unknown = classify(mid, prompts, enc.logit_scale, settings, harmonic_ratio=None)

    assert tonal.best_class == "melodic"
    assert noisy.best_class == "rhythmic"
    assert unknown.best_class in ("rhythmic", "melodic")


def test_settings_reject_nonsense():
    for kwargs in ({"confidence_threshold": 1.5}, {"batch_size": 0}, {"top_k_tags": -1}):
        with pytest.raises(ValueError):
            EmbedSettings(**kwargs)


# --- the pipeline -----------------------------------------------------------------


def test_embed_pending_writes_vectors_tags_and_facet_a(library):
    lib, conn = library
    enc = FakeEncoder({0.4: "a drum hit", 4.0: "a melodic loop"})

    summary = embed_pending(conn, encoder=enc, settings=EmbedSettings(top_k_tags=4))

    assert summary.samples_embedded == 2 and summary.failed == 0
    assert summary.classified == 2 and summary.flagged == 0
    rows = {
        r[0]: r for r in conn.execute(
            "SELECT s.filename, k.content_class, k.confidence, k.structural_type "
            "FROM classification k JOIN samples s ON s.id = k.sample_id"
        )
    }
    assert rows["hit.wav"][1] == "rhythmic" and rows["loop.wav"][1] == "melodic"
    assert rows["hit.wav"][3] == "one-shot"           # Facet B untouched
    assert all(r[2] > 0.5 for r in rows.values())
    vectors = conn.execute("SELECT vector FROM embedding WHERE model_name = 'clap'").fetchall()
    assert len(vectors) == 2 and all(blob_to_vector(v[0]).size == DIM for v in vectors)
    tags = conn.execute(
        "SELECT source_model, COUNT(*) FROM text_tags GROUP BY source_model"
    ).fetchall()
    assert dict(tags) == {SOURCE_TAGS: 8, SOURCE_CLASS: 8}      # 4 tags + all 4 class numbers per sample


def test_segments_are_embedded_when_long_enough_and_inherit_the_class(library):
    lib, conn = library
    n_segments = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    assert n_segments > 0
    longest = conn.execute("SELECT MAX(end_ms - start_ms) FROM segments").fetchone()[0]
    enc = FakeEncoder({4.0: "a drum loop"})

    summary = embed_pending(
        conn, encoder=enc, settings=EmbedSettings(min_segment_length_ms=longest + 1)
    )
    assert summary.segments_embedded == 0 and summary.segments_skipped_short == n_segments
    assert conn.execute("SELECT COUNT(*) FROM segment_embedding").fetchone()[0] == 0

    enc2 = FakeEncoder({4.0: "a drum loop"})
    summary = embed_pending(
        conn, encoder=enc2, settings=EmbedSettings(min_segment_length_ms=0), reembed=True
    )
    assert summary.segments_embedded == n_segments
    assert enc2.clips_seen == 2 + n_segments             # parents + segments, one batch each
    seg_cls = conn.execute(
        "SELECT content_class, structural_type FROM segment_classification"
    ).fetchall()
    assert len(seg_cls) == n_segments
    assert {c for c, _ in seg_cls} == {"rhythmic"} and {t for _, t in seg_cls} == {"one-shot"}


def test_embed_segments_off_skips_them_entirely(library):
    lib, conn = library
    enc = FakeEncoder({4.0: "a drum loop"})

    embed_pending(conn, encoder=enc, settings=EmbedSettings(embed_segments=False))

    assert conn.execute("SELECT COUNT(*) FROM segment_embedding").fetchone()[0] == 0
    assert enc.clips_seen == 2                             # parents only


def test_second_run_does_nothing_and_a_content_change_re_embeds(library):
    lib, conn = library
    enc = FakeEncoder({0.4: "a drum hit", 4.0: "a melodic loop"})
    embed_pending(conn, encoder=enc)
    assert embed_pending(conn, encoder=enc).samples_embedded == 0

    _write(lib / "hit.wav", _clicks([0.0], 0.6))          # new content
    scan_library(conn, lib)
    analyze_pending(conn)
    summary = embed_pending(conn, encoder=FakeEncoder({0.6: "a person singing"}))

    assert summary.samples_embedded == 1
    cls = conn.execute(
        "SELECT k.content_class FROM classification k JOIN samples s ON s.id = k.sample_id "
        "WHERE s.filename = 'hit.wav'"
    ).fetchone()[0]
    assert cls == "vocal"


def test_manual_classification_is_protected(library):
    lib, conn = library
    conn.execute(
        "UPDATE classification SET content_class = 'other', is_user_confirmed = 1 "
        "WHERE sample_id = (SELECT id FROM samples WHERE filename = 'hit.wav')"
    )
    conn.commit()

    summary = embed_pending(conn, encoder=FakeEncoder({0.4: "a drum hit", 4.0: "a melodic loop"}))

    assert summary.protected == 1 and summary.classified == 1
    kept = conn.execute(
        "SELECT k.content_class FROM classification k JOIN samples s ON s.id = k.sample_id "
        "WHERE s.filename = 'hit.wav'"
    ).fetchone()[0]
    assert kept == "other"
    # The machine layer is still written, so the suggestion is visible.
    assert conn.execute(
        "SELECT COUNT(*) FROM text_tags t JOIN samples s ON s.id = t.sample_id "
        "WHERE s.filename = 'hit.wav' AND t.source_model = ?", (SOURCE_CLASS,)
    ).fetchone()[0] == 4


def test_reclassify_uses_stored_vectors_without_touching_audio(library):
    lib, conn = library
    embed_pending(conn, encoder=FakeEncoder({0.4: "a drum hit", 4.0: "a melodic loop"}))
    enc = FakeEncoder(logit_scale=1.0)   # flat distribution: nothing reaches 0.99

    summary = reclassify(conn, encoder=enc, settings=EmbedSettings(confidence_threshold=0.99))

    assert enc.audio_calls == 0
    assert summary.flagged + summary.classified == 2
    assert summary.flagged == 2                           # threshold nobody meets
    assert conn.execute(
        "SELECT COUNT(*) FROM classification WHERE content_class IS NULL"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM classification WHERE confidence IS NOT NULL"
    ).fetchone()[0] == 2


def test_editing_a_segment_drops_its_vector(library):
    lib, conn = library
    embed_pending(conn, encoder=FakeEncoder({4.0: "a drum loop"}), settings=EmbedSettings(min_segment_length_ms=0))
    seg_id = conn.execute("SELECT id FROM segments LIMIT 1").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM segment_embedding WHERE segment_id = ?", (seg_id,)).fetchone()[0] == 1

    update_segment(conn, seg_id, start_ms=5)

    assert conn.execute("SELECT COUNT(*) FROM segment_embedding WHERE segment_id = ?", (seg_id,)).fetchone()[0] == 0
    # ... and the next run refills exactly that one, without re-embedding its parent.
    again = embed_pending(conn, encoder=FakeEncoder({4.0: "a drum loop"}), settings=EmbedSettings(min_segment_length_ms=0))
    assert (again.segments_embedded, again.samples_embedded) == (1, 0)
    assert conn.execute("SELECT COUNT(*) FROM segment_embedding WHERE segment_id = ?", (seg_id,)).fetchone()[0] == 1


def test_one_exploding_file_does_not_abort_the_run(library, monkeypatch):
    import crate.embedding as mod

    lib, conn = library
    real = mod.load_audio_for_clap

    def boom(path):
        if "loop" in str(path):
            raise RuntimeError("simulated")
        return real(path)

    monkeypatch.setattr(mod, "load_audio_for_clap", boom)

    summary = embed_pending(conn, encoder=FakeEncoder({0.4: "a drum hit"}))

    assert summary.samples_embedded == 1 and summary.failed == 1
    assert any("RuntimeError" in e for e in summary.error_samples)


def test_manual_segment_gets_a_vector_too(library):
    lib, conn = library
    sid = conn.execute("SELECT id FROM samples WHERE filename = 'loop.wav'").fetchone()[0]
    mid = create_manual_segment(conn, sid, 100, 900)

    embed_pending(conn, encoder=FakeEncoder({4.0: "a drum loop"}), settings=EmbedSettings(min_segment_length_ms=0))

    assert conn.execute("SELECT COUNT(*) FROM segment_embedding WHERE segment_id = ?", (mid,)).fetchone()[0] == 1


# --- the real model (opt-in: CRATE_REAL_CLAP=1) ------------------------------------


@pytest.mark.skipif(not os.environ.get("CRATE_REAL_CLAP"), reason="set CRATE_REAL_CLAP=1 to run the real CLAP smoke test")
def test_real_clap_places_a_drum_closer_to_a_drum_prompt(tmp_path):
    from crate.embedding import ClapEncoder

    enc = ClapEncoder()
    y = _clicks([0.0, 0.5, 1.0, 1.5], 2.0)
    y48 = np.interp(np.linspace(0, len(y), int(len(y) * 48000 / SR), endpoint=False), np.arange(len(y)), y)
    audio = enc.embed_audio([y48.astype(np.float32)], CLAP_SAMPLE_RATE)[0]
    text = enc.embed_text(["a drum hit", "a person singing"])
    assert audio.size == 512
    assert float(text[0] @ audio) > float(text[1] @ audio)


# --- 2026-09-06 quick review after Phase 4 -----------------------------------------


def test_segments_detected_after_the_parent_was_embedded_still_get_vectors(tmp_path):
    """Found by probe: a later crate-segment run (or --resegment) left segments
    without vectors forever, because only parents were on the worklist."""
    from crate.segmentation import SegmentationSettings

    lib = tmp_path / "lib"
    lib.mkdir()
    _write(lib / "loop.wav", _clicks([i * 0.5 for i in range(8)], 4.0))
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    analyze_pending(conn)
    embed_pending(conn, encoder=FakeEncoder({4.0: "a drum loop"}))     # before any segments exist
    segment_pending(conn)
    n = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    parent_before = conn.execute("SELECT embedded_at FROM embedding").fetchone()[0]

    enc = FakeEncoder({4.0: "a drum loop"})
    summary = embed_pending(conn, encoder=enc, settings=EmbedSettings(min_segment_length_ms=0))

    assert summary.segments_embedded == n
    assert summary.samples_embedded == 0                      # parent untouched ...
    assert conn.execute("SELECT embedded_at FROM embedding").fetchone()[0] == parent_before
    assert enc.clips_seen == n                                # ... and not sent to the model

    segment_pending(conn, settings=SegmentationSettings(max_segments=3), resegment=True)
    summary = embed_pending(conn, encoder=FakeEncoder(), settings=EmbedSettings(min_segment_length_ms=0))
    assert summary.segments_embedded == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM segments g WHERE NOT EXISTS "
        "(SELECT 1 FROM segment_embedding e WHERE e.segment_id = g.id)"
    ).fetchone()[0] == 0
    assert embed_pending(conn, encoder=FakeEncoder(), settings=EmbedSettings(min_segment_length_ms=0)).segments_embedded == 0
    conn.close()


def test_manual_segment_past_the_end_does_not_fail_its_parent(library):
    lib, conn = library
    sid = conn.execute("SELECT id FROM samples WHERE filename = 'loop.wav'").fetchone()[0]
    mid = create_manual_segment(conn, sid, 4100, 4400)              # entirely past a 4.0 s file

    class Strict(FakeEncoder):
        def embed_audio(self, clips, sr):
            assert all(len(c) > 0 for c in clips), "an empty clip reached the model"
            return super().embed_audio(clips, sr)

    summary = embed_pending(conn, encoder=Strict({4.0: "a drum loop"}), settings=EmbedSettings(min_segment_length_ms=0))

    assert summary.failed == 0 and summary.samples_embedded == 2
    assert conn.execute("SELECT COUNT(*) FROM segment_embedding WHERE segment_id = ?", (mid,)).fetchone()[0] == 0
    assert summary.segments_skipped_short >= 1
