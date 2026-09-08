"""Qwen2-Audio — spec §5.2 (an optional natural-language caption, node X1)
and §5.3 (its audio encoder's latent as a candidate similarity axis, node
X2). Phase 5 (2026-09-08) is the spike that measures both on this CPU.

The model is `Qwen/Qwen2-Audio-7B-Instruct`: a Whisper-large-v3-style audio
encoder (32 layers, d = 1280, 30-s windows at 16 kHz, 25 frames/s after its
2× average pool), a linear projector into the language model's 4096-d
space, and a 7B Qwen2 decoder. Everything runs in bf16 on the CPU — no
quantisation, this machine has the memory — and nothing here loads until
`QwenAudio.load()` is called: the `ml` extra is optional (spec §12).

Several latents are kept side by side, because there is no documented
"correct" pooling the way there is for CLAP (§5.3), and speaker identity
in Whisper-style encoders tends to sit in the earlier layers:

- `enc_L{n}_mean` / `enc_L{n}_stats` — encoder layer n (before the 2× pool),
  mean over the frames the clip actually occupies, or mean ‖ std (the
  statistics pooling of speaker-verification models), L2-normalised;
- `enc_last_mean` / `enc_last_stats` — the encoder's output (after its pool
  and layer norm), pooled the same two ways;
- `proj_mean` — the projector's output, the language model's view of the
  audio, mean over the occupied frames.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from .db import ids_clause, now_iso, scope_clause

log = logging.getLogger(__name__)

QWEN_CHECKPOINT = "Qwen/Qwen2-Audio-7B-Instruct"
QWEN_SAMPLE_RATE = 16_000
QWEN_MAX_SECONDS = 30.0
FRAMES_PER_SECOND = 25          # encoder output rate after the 2× average pool
MAX_FRAMES = int(QWEN_MAX_SECONDS * FRAMES_PER_SECOND)   # 750
LAYER_TAPS = (4, 8, 12, 16, 24)   # encoder layers tapped (of 32)


def latent_variant_names(taps: tuple[int, ...] = LAYER_TAPS) -> tuple[str, ...]:
    names: list[str] = []
    for n in taps:
        names += [f"enc_L{n}_mean", f"enc_L{n}_stats"]
    return tuple(names + ["enc_last_mean", "enc_last_stats", "proj_mean"])


LATENT_VARIANTS = latent_variant_names()
SOURCE_CAPTION = "qwen2audio-caption"   # text_tags.source_model (spec §8)
CAPTION_PROMPT = "Describe this sound in one sentence."   # plainer wording: "as a sound designer would" pulled a sung note into synth vocabulary


@dataclass
class CaptionResult:
    text: str
    seconds: float
    prompt_tokens: int
    new_tokens: int

    @property
    def tokens_per_second(self) -> float:
        return self.new_tokens / self.seconds if self.seconds > 0 else 0.0


@dataclass
class LatentResult:
    vectors: dict[str, np.ndarray]   # variant -> unit vector (float32)
    seconds: float                   # encoder + projector time


def load_audio_for_qwen(path) -> tuple[np.ndarray, int] | None:
    """Decode at the model's rate, mono. None when the file cannot be read."""
    import librosa

    try:
        return librosa.load(str(path), sr=QWEN_SAMPLE_RATE, mono=True)
    except Exception as exc:  # noqa: BLE001 - reported by the caller
        log.debug("decode failed: %s (%s: %s)", path, type(exc).__name__, exc)
        return None


def clip_for_qwen(y: np.ndarray, sr: int) -> np.ndarray:
    """The first 30 s, float32 — deterministic, like `crop_for_clap`."""
    limit = int(QWEN_MAX_SECONDS * sr)
    y = np.asarray(y, dtype=np.float32)
    return y[:limit] if y.size > limit else y


def valid_frames(seconds: float, frames_per_second: int = FRAMES_PER_SECOND, cap: int = MAX_FRAMES) -> int:
    """How many encoder frames a clip of `seconds` occupies; the rest of the
    30-s window is padding (silence) and is left out of the pooling."""
    return max(1, min(cap, int(round(seconds * frames_per_second))))


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector)) or 1.0
    return (vector / norm).astype(np.float32)


def masked_mean(frames: np.ndarray, n_valid: int) -> np.ndarray:
    """Mean over the first `n_valid` frames of (frames, dim), L2-normalised."""
    frames = np.asarray(frames, dtype=np.float32)
    n_valid = max(1, min(n_valid, frames.shape[0]))
    return _unit(frames[:n_valid].astype(np.float64).mean(axis=0))


def masked_stats(frames: np.ndarray, n_valid: int) -> np.ndarray:
    """Mean ‖ standard deviation over the first `n_valid` frames (2 × dim),
    L2-normalised — statistics pooling, what speaker-verification models
    pool with, since a voice is as much its variation as its average."""
    frames = np.asarray(frames, dtype=np.float32)
    n_valid = max(1, min(n_valid, frames.shape[0]))
    block = frames[:n_valid].astype(np.float64)
    return _unit(np.concatenate([block.mean(axis=0), block.std(axis=0)]))


class QwenAudio:
    """The model, loaded on first use. `latents` runs the audio tower and the
    projector only (the cost of a latent axis over the library); `caption`
    runs the whole thing, decoder included."""

    def __init__(self, checkpoint: str = QWEN_CHECKPOINT, layer_taps: tuple[int, ...] = LAYER_TAPS) -> None:
        self._checkpoint = checkpoint
        self._layer_taps = layer_taps
        self._model = None
        self._processor = None
        self.load_seconds = 0.0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoProcessor, Qwen2AudioForConditionalGeneration

        torch.set_grad_enabled(False)
        started = time.perf_counter()
        log.info("loading %s (bf16, CPU)", self._checkpoint)
        self._processor = AutoProcessor.from_pretrained(self._checkpoint)
        self._model = Qwen2AudioForConditionalGeneration.from_pretrained(
            self._checkpoint, dtype=torch.bfloat16, low_cpu_mem_usage=True
        ).eval()
        self.load_seconds = time.perf_counter() - started
        log.info("loaded in %.0f s", self.load_seconds)

    # --- the pieces ---

    @property
    def _inner(self):
        return getattr(self._model, "model", self._model)

    @property
    def audio_tower(self):
        return self._inner.audio_tower

    @property
    def projector(self):
        return self._inner.multi_modal_projector

    def _features(self, clip: np.ndarray, sr: int):
        extractor = self._processor.feature_extractor
        return extractor(clip, sampling_rate=sr, return_tensors="pt")["input_features"]

    def latents(self, clip: np.ndarray, sr: int) -> LatentResult:
        """The candidate vectors for one clip (≤ 30 s): one encoder pass,
        the tapped layers read through forward hooks."""
        import torch

        self.load()
        clip = clip_for_qwen(clip, sr)
        seconds = clip.size / sr
        started = time.perf_counter()
        features = self._features(clip, sr).to(torch.bfloat16)
        layers = self.audio_tower.layers
        captured: dict[int, torch.Tensor] = {}

        def grabber(index: int):
            def grab(_module, _inputs, output):
                captured[index] = output[0] if isinstance(output, tuple) else output
            return grab

        handles = [layers[n].register_forward_hook(grabber(n)) for n in self._layer_taps if n < len(layers)]
        try:
            out = self.audio_tower(features)
        finally:
            for handle in handles:
                handle.remove()
        last = out.last_hidden_state[0].float().numpy()                        # (750, 1280)
        projected = self.projector(out.last_hidden_state)[0].float().numpy()   # (750, 4096)
        n = valid_frames(seconds)
        vectors: dict[str, np.ndarray] = {}
        for index, states in captured.items():
            frames = states[0].float().numpy()                                 # (1500, 1280), before the pool
            vectors[f"enc_L{index}_mean"] = masked_mean(frames, 2 * n)
            vectors[f"enc_L{index}_stats"] = masked_stats(frames, 2 * n)
        vectors["enc_last_mean"] = masked_mean(last, n)
        vectors["enc_last_stats"] = masked_stats(last, n)
        vectors["proj_mean"] = masked_mean(projected, n)
        return LatentResult(vectors, time.perf_counter() - started)

    def caption(
        self, clip: np.ndarray, sr: int, prompt: str = CAPTION_PROMPT, max_new_tokens: int = 48
    ) -> CaptionResult:
        """One sentence about the clip from the whole model — encoder,
        projector and the 7B decoder, greedy."""
        import torch

        self.load()
        clip = clip_for_qwen(clip, sr)
        conversation = [
            {
                "role": "user",
                "content": [
                    {"type": "audio", "audio_url": "clip.wav"},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        text = self._processor.apply_chat_template(conversation, add_generation_prompt=True, tokenize=False)
        inputs = self._processor(text=text, audio=[clip], sampling_rate=sr, return_tensors="pt")
        if "input_features" in inputs:
            inputs["input_features"] = inputs["input_features"].to(torch.bfloat16)
        prompt_tokens = int(inputs["input_ids"].shape[1])
        started = time.perf_counter()
        generated = self._model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        seconds = time.perf_counter() - started
        new_ids = generated[:, prompt_tokens:]
        text_out = self._processor.batch_decode(new_ids, skip_special_tokens=True)[0].strip()
        return CaptionResult(text_out, seconds, prompt_tokens, int(new_ids.shape[1]))


# --- node X1: captions into the index (spec §5.2, §7) ------------------------


class Captioner(Protocol):
    def caption(self, clip: np.ndarray, sr: int) -> CaptionResult: ...


@dataclass
class CaptionSummary:
    captioned: int = 0
    failed: int = 0
    stopped: bool = False          # stopped by request; what was written is kept
    model_seconds: float = 0.0     # time inside the model (decode excluded)
    elapsed_s: float = 0.0
    error_samples: list[str] = field(default_factory=list)

    def format(self) -> str:
        each = f" ({self.model_seconds / self.captioned:.1f} s each in the model)" if self.captioned else ""
        lines = [f"captioned {self.captioned} samples{each} | failed {self.failed}"]
        lines.extend(f"  ! {s}" for s in self.error_samples)
        if self.stopped:
            lines.append("stopped by request; every caption written so far is kept")
        lines.append(f"elapsed: {self.elapsed_s:.1f}s")
        return "\n".join(lines)


def caption_pending(
    conn: sqlite3.Connection,
    captioner: Captioner | None = None,
    limit: int | None = None,
    recaption: bool = False,
    progress_every: int = 10,
    scope: Sequence[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    sample_ids: Sequence[int] | None = None,
) -> CaptionSummary:
    """One Qwen2-Audio sentence per sample that has none, or whose content
    changed since it was written (`text_tags`, `source_model` =
    'qwen2audio-caption', one row per sample). Opt-in and scope-sized by
    design: measured 2026-09-08 at ~10 s per file on a 16-core CPU.

    `captioner` defaults to the real model; tests pass a fake. Its `load`,
    if it has one, runs before the loop so a missing model stack fails the
    stage once, loudly, instead of once per file. `should_stop` is polled
    before each sample; a per-file failure is counted and skipped.
    """
    summary = CaptionSummary()
    started = time.perf_counter()
    sql = (
        "SELECT s.id, s.filepath FROM samples s "
        "LEFT JOIN text_tags t ON t.sample_id = s.id AND t.source_model = ? "
        "WHERE s.duration_s IS NOT NULL"
    )
    params: list = [SOURCE_CAPTION]
    if not recaption:
        sql += " AND (t.id IS NULL OR t.created_at IS NULL OR s.content_changed_at > t.created_at)"
    ids_sql, ids_params = ids_clause(sample_ids)    # "Caption this sample": these and nothing else
    scope_sql, scope_params = scope_clause(scope)
    sql += ids_sql + scope_sql + " GROUP BY s.id ORDER BY s.id"
    params += ids_params + scope_params
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    worklist = conn.execute(sql, params).fetchall()
    total = len(worklist)
    log.info("captions: %d samples to caption", total)
    if not worklist:
        summary.elapsed_s = time.perf_counter() - started
        return summary

    captioner = captioner or QwenAudio()
    load = getattr(captioner, "load", None)
    if callable(load):
        load()                                      # outside the per-file isolation: fail once
    for n, (sample_id, filepath) in enumerate(worklist):
        if should_stop is not None and should_stop():
            summary.stopped = True
            log.info("captioning stopped by request after %d of %d", n, total)
            break
        try:
            loaded = load_audio_for_qwen(filepath)
            if loaded is None:
                raise ValueError("decode failed")
            y, sr = loaded
            result = captioner.caption(y, sr)
            conn.execute(
                "DELETE FROM text_tags WHERE sample_id = ? AND source_model = ?", (sample_id, SOURCE_CAPTION)
            )
            conn.execute(
                "INSERT INTO text_tags (sample_id, tag_or_caption, source_model, score, created_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (sample_id, result.text, SOURCE_CAPTION, now_iso()),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - per-file isolation
            conn.rollback()
            summary.failed += 1
            log.warning("caption failed: %s (%s: %s)", filepath, type(exc).__name__, exc)
            if len(summary.error_samples) < 5:
                summary.error_samples.append(f"{type(exc).__name__}: {filepath}")
            continue
        summary.captioned += 1
        summary.model_seconds += result.seconds
        if progress_every and summary.captioned % progress_every == 0:
            log.info(
                "captioned %d/%d (%.1f s/sample in the model): %s",
                summary.captioned, total, summary.model_seconds / summary.captioned, result.text[:70],
            )
    summary.elapsed_s = time.perf_counter() - started
    return summary
