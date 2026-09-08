"""Phase 5 (spec §5.2 / §5.3): the pooling arithmetic and the retrieval
metrics, with no model; the real Qwen2-Audio run is opt-in at the bottom."""

from __future__ import annotations

import os

import numpy as np
import pytest

from crate.evaluation import (
    cosine_matrix,
    hits_in_top_k,
    jaccard_at_k,
    precision_at_k,
    separation,
    top_k,
)
from crate.qwen_audio import (
    MAX_FRAMES,
    QWEN_SAMPLE_RATE,
    clip_for_qwen,
    latent_variant_names,
    masked_mean,
    masked_stats,
    valid_frames,
)


def test_clip_frames_and_masked_mean():
    sr = QWEN_SAMPLE_RATE
    assert clip_for_qwen(np.zeros(sr * 45), sr).size == sr * 30           # first 30 s
    assert clip_for_qwen(np.zeros(sr * 4), sr).size == sr * 4
    assert clip_for_qwen(np.zeros(10, dtype=np.float64), sr).dtype == np.float32
    assert valid_frames(1.0) == 25 and valid_frames(30.0) == MAX_FRAMES == 750
    assert valid_frames(0.01) == 1 and valid_frames(90.0) == 750          # never empty, never past the window
    frames = np.vstack([np.ones((3, 4)), np.full((5, 4), -9.0)])         # the padding must not leak in
    v = masked_mean(frames, 3)
    assert v.dtype == np.float32 and np.allclose(v, 0.5) and np.isclose(np.linalg.norm(v), 1.0)
    assert np.allclose(masked_mean(frames, 0), masked_mean(frames, 1))    # at least one frame
    stats = masked_stats(frames, 3)
    assert stats.shape == (8,) and np.allclose(stats[4:], 0.0) and np.isclose(np.linalg.norm(stats), 1.0)
    assert latent_variant_names((4, 16)) == (
        "enc_L4_mean", "enc_L4_stats", "enc_L16_mean", "enc_L16_stats",
        "enc_last_mean", "enc_last_stats", "proj_mean",
    )


def _unit(rows):
    v = np.asarray(rows, dtype=np.float64)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_retrieval_metrics_on_planted_groups():
    # Three tight groups in 4-d, one stray item that belongs to nobody.
    rng = np.random.default_rng(0)
    centres = np.eye(4)[:3]
    vectors, labels = [], []
    for g in range(3):
        for _ in range(4):
            vectors.append(centres[g] + rng.normal(0, 0.05, 4))
            labels.append(f"g{g}")
    vectors.append(np.array([1.0, 1.0, 1.0, 1.0]))
    labels.append("stray")
    sims = cosine_matrix(_unit(vectors))
    assert np.all(np.isneginf(np.diag(sims)))                            # never its own neighbour
    assert top_k(sims, 3).shape == (13, 3)
    p = precision_at_k(sims, labels, k=3)
    assert np.all(p[:12] == 1.0) and p[12] == 0.0
    assert precision_at_k(sims, labels, k=3, queries=np.array([0, 12])).tolist() == [1.0, 0.0]
    assert separation(sims, labels) > 0.5
    assert np.all(jaccard_at_k(sims, sims, k=3) == 1.0)
    shuffled = cosine_matrix(_unit(rng.normal(size=(13, 4))))
    assert jaccard_at_k(sims, shuffled, k=3).mean() < 0.5
    targets = np.array(labels) == "g1"
    assert hits_in_top_k(sims, targets, k=3, queries=np.array([4])).tolist() == [True]   # a g1 query finds g1
    assert hits_in_top_k(sims, targets, k=3, queries=np.array([0])).tolist() == [False]  # a g0 query does not


@pytest.mark.skipif(not os.environ.get("CRATE_REAL_QWEN"), reason="set CRATE_REAL_QWEN=1 to run the real model")
def test_real_qwen_latents_and_caption():
    from crate.qwen_audio import LATENT_VARIANTS, QwenAudio

    sr = QWEN_SAMPLE_RATE
    t = np.arange(sr) / sr
    clip = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    model = QwenAudio()
    result = model.latents(clip, sr)
    assert set(result.vectors) == set(LATENT_VARIANTS)
    assert result.vectors["enc_last_mean"].shape == (1280,) and result.vectors["proj_mean"].shape == (4096,)
    assert result.vectors["enc_L8_stats"].shape == (2560,)
    assert all(np.isclose(np.linalg.norm(v), 1.0, atol=1e-3) for v in result.vectors.values())
    caption = model.caption(clip, sr)
    assert caption.text and caption.new_tokens > 0


# --- node X1: captions into the index (spec §5.2) ---


class FakeCaptioner:
    """Names a clip by its length; can be told to fail on one length."""

    def __init__(self, fail_on: float | None = None) -> None:
        self.calls = 0
        self.fail_on = fail_on

    def caption(self, clip, sr):
        from crate.qwen_audio import CaptionResult

        self.calls += 1
        seconds = clip.size / sr
        if self.fail_on is not None and abs(seconds - self.fail_on) < 0.05:
            raise RuntimeError("model hiccup")
        return CaptionResult(f"a clip of {seconds:.1f} s", 0.5, 100, 12)


def test_caption_pending_writes_one_sentence_per_sample(tmp_path):
    import soundfile as sf

    from crate.catalog import load_caption
    from crate.db import now_iso, open_db
    from crate.qwen_audio import caption_pending
    from crate.scanner import scan_library

    lib = tmp_path / "lib"
    lib.mkdir()
    sf.write(lib / "a.wav", np.zeros(8000, np.float32), 8000)      # 1.0 s
    sf.write(lib / "b.wav", np.zeros(16000, np.float32), 8000)     # 2.0 s
    conn = open_db(tmp_path / "index.db")
    scan_library(conn, lib)
    ids = {row[0]: row[1] for row in conn.execute("SELECT filename, id FROM samples")}

    fake = FakeCaptioner()
    summary = caption_pending(conn, fake)
    assert summary.captioned == 2 and summary.failed == 0 and fake.calls == 2
    assert load_caption(conn, ids["a.wav"]) == "a clip of 1.0 s"
    assert "captioned 2 samples" in summary.format()
    assert caption_pending(conn, fake).captioned == 0 and fake.calls == 2           # nothing new: idle

    conn.execute("UPDATE samples SET content_changed_at = ? WHERE filename = 'a.wav'", (now_iso(),))
    conn.commit()
    assert caption_pending(conn, fake).captioned == 1                              # the stale one only
    assert conn.execute(
        "SELECT COUNT(*) FROM text_tags WHERE source_model = 'qwen2audio-caption'"
    ).fetchone()[0] == 2                                                            # one row per sample

    failing = FakeCaptioner(fail_on=2.0)
    redo = caption_pending(conn, failing, recaption=True)
    assert redo.captioned == 1 and redo.failed == 1 and redo.error_samples
    assert load_caption(conn, ids["b.wav"]) == "a clip of 2.0 s"                   # the old one survives a failure

    stopped = caption_pending(conn, FakeCaptioner(), recaption=True, should_stop=lambda: True)
    assert stopped.stopped and stopped.captioned == 0
    assert caption_pending(conn, FakeCaptioner(), recaption=True, scope=[str(lib)], limit=1).captioned == 1
    assert load_caption(conn, 999_999) is None
    conn.close()
