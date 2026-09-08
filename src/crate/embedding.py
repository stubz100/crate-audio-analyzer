"""CLAP embeddings, zero-shot tags and the Facet A classifier — pipeline nodes
`D`, `C2` (embedding half), `X` and `E` (spec §7), Phase 4.

* **Node D** embeds every sample with CLAP, unconditionally (spec §7: `D`
  depends only on `C`, never on `S`). The vector is the "conceptual" axis of
  §5.1 and the text-search backbone of §5.2.
* **Node C2** embeds each segment window the same way, gated by the §9.6
  settings *Embed segments* and *Min length for segment embedding* — the
  library's real cost multiplier (§3), so both are explicit.
* **Node X** turns the audio vector into zero-shot tag chips (`text_tags`,
  `source_model='clap-zeroshot'`) by cosine similarity to a fixed prompt
  vocabulary. These are the raw machine layer of §8; the curated layer is
  Phase 7's.
* **Node E, Facet A**: the four §4 content classes as prompt sets; the class
  with the highest similarity wins, a softmax over the class scores gives a
  confidence, and below the threshold the sample is **flagged rather than
  assigned** (`content_class` NULL, `confidence` kept, the best guess written
  as a `clap-class` tag so nothing is lost). Descriptor tie-break as §7 asks:
  a close Rhythmic-vs-Melodic call is settled by the HPSS harmonic ratio.
  Facet B is Phase 2's and is not touched here. Segments inherit the parent's
  class with `structural_type='one-shot'` (§6.4).

Model access goes through the tiny `Encoder` protocol so the whole pipeline
is testable with a fake; the real one is `ClapEncoder`, loaded lazily through
transformers' native `ClapModel` (spec §10 says "CLAP", the package is a
Phase 4 choice — see pyproject). Audio is decoded at CLAP's 48 kHz, and clips
longer than the model's 10 s window are cropped to their **first** 10 s —
deterministically, where the feature extractor's default would take a random
window and make embeddings non-reproducible.

Protections: a manually-confirmed classification (§11) is never overwritten —
its Facet A stays, only tags and vectors refresh. Nothing here runs on its own
(§9.6): `crate-embed` is an explicit run, and staleness is a flag.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from .db import WINDOW_METHOD, now_iso, scope_clause

log = logging.getLogger(__name__)

MODEL_NAME = "clap"                              # embedding.model_name (spec §8)
DEFAULT_CHECKPOINT = "laion/clap-htsat-unfused"  # 630k-audioset, no fusion: CPU-friendly
CLAP_SAMPLE_RATE = 48_000
CLAP_MAX_SECONDS = 10.0
EMBED_DIM = 512

SOURCE_TAGS = "clap-zeroshot"
SOURCE_CLASS = "clap-class"

CONTENT_CLASSES = ("rhythmic", "melodic", "vocal", "other")

# Facet A prompt sets (spec §4). A class scores as the BEST of its prompts —
# measured on 335 labeled library files (2026-09-06): max-over-prompts beat
# both the mean and the prompt-centroid ensemble (the latter two collapse the
# Vocal class to ~10%). Because the best-of scheme favours a class with more
# prompts, every class has the same number. Twelve per class scored 68%
# best-guess accuracy (73% after removing a mislabeled folder) against 60%
# for the original eight/six. Rhythmic 98%, Other 85%, Melodic 50%, Vocal
# 33% — short vocal chops and breaths read as percussion to CLAP; §11's
# manual corrections are the intended path for those.
CLASS_PROMPTS: dict[str, tuple[str, ...]] = {
    "rhythmic": (
        "a drum hit", "a single drum sound", "a percussion hit",
        "a percussive sound effect", "a drum loop", "a percussion loop",
        "a foley hit", "a rhythmic beat", "a hi-hat", "a cymbal",
        "a kick drum", "a snare drum",
    ),
    "melodic": (
        "a musical note played on an instrument", "a synthesizer sound",
        "a melodic loop", "a chord played on an instrument", "a bass note",
        "a piano note", "a guitar note", "a melody", "a synth lead",
        "a synth pad chord", "a plucked string note", "a keyboard melody",
    ),
    "vocal": (
        "a human voice", "a person singing", "a person speaking",
        "a vocal phrase", "a vocal shout", "a vocal sample",
        "a woman speaking", "a man speaking", "spoken words",
        "a sung vocal line", "a vocal breath", "a voice saying a word",
    ),
    "other": (
        "an ambient texture", "a drone", "a riser sound effect",
        "an impact sound effect", "a whoosh sound effect", "background ambience",
        "white noise", "a cinematic sound design texture",
        "an atmospheric pad texture", "a sci-fi ambience", "wind",
        "a sustained noise texture",
    ),
}
assert len({len(v) for v in CLASS_PROMPTS.values()}) == 1, "equal prompt counts per class"

# Node X vocabulary. Small on purpose: chips are suggestions the user accepts
# or edits, not a taxonomy.
ZERO_SHOT_TAGS: tuple[str, ...] = (
    "kick drum", "snare drum", "hi-hat", "hand clap", "tom drum", "cymbal",
    "shaker", "percussion", "808 bass", "bass", "synth lead", "synth pad",
    "pluck", "piano", "guitar", "strings", "brass", "bell", "vocal",
    "spoken word", "vocal chop", "drum loop", "melodic loop", "riser",
    "impact", "whoosh", "glitch", "noise", "texture", "drone", "ambience",
    "foley",
)
TAG_PROMPT = "the sound of {}"

# Node E tie-break (spec §7): when the top two classes are Rhythmic and
# Melodic and within this margin, HPSS decides.
TIE_BREAK_MARGIN = 0.15
TIE_BREAK_HARMONIC_HIGH = 0.7   # >= : melodic (the Phase 2 pitch gate)
TIE_BREAK_HARMONIC_LOW = 0.3    # <= : rhythmic


@dataclass
class EmbedSettings:
    """The §9.6 recompute-attributes settings this node reads."""

    embed_segments: bool = True             # §9.6 "Embed segments" (default On)
    min_segment_length_ms: int = 200        # §9.6 "Min length for segment embedding"
    confidence_threshold: float = 0.5       # node E: flag below this (4 classes → 0.25 is chance)
    top_k_tags: int = 5
    checkpoint: str = DEFAULT_CHECKPOINT
    batch_size: int = 8

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold is a probability (0..1)")
        if self.min_segment_length_ms < 0 or self.top_k_tags < 0 or self.batch_size < 1:
            raise ValueError("min_segment_length_ms/top_k_tags >= 0, batch_size >= 1")


@dataclass
class EmbedSummary:
    samples_embedded: int = 0
    segments_embedded: int = 0
    windows_stored: int = 0        # §6.4 CLAP windows of long files, kept as searchable hits
    segments_skipped_short: int = 0
    classified: int = 0            # Facet A assigned
    flagged: int = 0               # Facet A below threshold: flagged, not assigned
    protected: int = 0             # manually-confirmed classification left alone
    failed: int = 0
    stopped: bool = False          # stopped by request; what was done is kept
    elapsed_s: float = 0.0
    error_samples: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            f"embedded {self.samples_embedded} samples + {self.segments_embedded} segments "
            f"(skipped {self.segments_skipped_short} short) + {self.windows_stored} windows of long files | "
            f"Facet A assigned {self.classified}, flagged {self.flagged}, "
            f"protected {self.protected} | failed {self.failed}"
        ]
        lines.extend(f"  ! {s}" for s in self.error_samples)
        if self.stopped:
            lines.append("stopped by request; everything embedded so far is kept")
        lines.append(f"elapsed: {self.elapsed_s:.1f}s")
        return "\n".join(lines)


# --- vectors -------------------------------------------------------------------


def vector_to_blob(vector: np.ndarray) -> bytes:
    return np.ascontiguousarray(vector, dtype=np.float32).tobytes()


def blob_to_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _l2(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    norms = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.maximum(norms, 1e-12)


# --- the model -----------------------------------------------------------------


class Encoder(Protocol):
    """What the pipeline needs from a model: unit vectors for audio and text
    in one space, plus the model's logit scale for turning cosines into a
    class distribution."""

    logit_scale: float

    def embed_audio(self, clips: list[np.ndarray], sr: int) -> np.ndarray: ...
    def embed_text(self, texts: list[str]) -> np.ndarray: ...


class ClapEncoder:
    """transformers' ClapModel, loaded on first use (the import alone costs
    seconds and the checkpoint ~600 MB on first download)."""

    def __init__(self, checkpoint: str = DEFAULT_CHECKPOINT, batch_size: int = 8) -> None:
        self._checkpoint = checkpoint
        self._batch_size = batch_size
        self._model = None
        self._extractor = None
        self._tokenizer = None
        self.logit_scale = 33.0  # CLAP's learned scale is ~exp(3.5); replaced on load

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import ClapModel, ClapProcessor

        torch.set_grad_enabled(False)
        log.info("loading CLAP checkpoint %s (first use downloads it)", self._checkpoint)
        processor = ClapProcessor.from_pretrained(self._checkpoint)
        self._extractor = processor.feature_extractor
        self._tokenizer = processor.tokenizer
        self._model = ClapModel.from_pretrained(self._checkpoint).eval()
        try:
            self.logit_scale = float(self._model.logit_scale_a.exp().item())
        except Exception:  # pragma: no cover - checkpoint without the scalar
            pass

    def embed_audio(self, clips: list[np.ndarray], sr: int) -> np.ndarray:
        if sr != CLAP_SAMPLE_RATE:
            raise ValueError(f"CLAP expects {CLAP_SAMPLE_RATE} Hz audio, got {sr}")
        if not clips:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        self._ensure_loaded()
        import torch

        out: list[np.ndarray] = []
        for start in range(0, len(clips), self._batch_size):
            batch = [crop_for_clap(c, sr) for c in clips[start : start + self._batch_size]]
            inputs = self._extractor(batch, sampling_rate=sr, return_tensors="pt")
            with torch.no_grad():
                feats = self._model.get_audio_features(**inputs)
            out.append(_l2(projected_features(feats)))
        return np.concatenate(out)

    def embed_text(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        self._ensure_loaded()
        import torch

        inputs = self._tokenizer(list(texts), return_tensors="pt", padding=True)
        with torch.no_grad():
            feats = self._model.get_text_features(**inputs)
        return _l2(projected_features(feats))


def projected_features(output) -> np.ndarray:
    """The (n, dim) projected embedding from a `get_*_features` result.

    transformers 5 returns a `BaseModelOutputWithPooling` whose
    `pooler_output` is the projected vector (measured: (n, 512)); 4.x returned
    the tensor itself. Accept both.
    """
    tensor = output if hasattr(output, "cpu") else getattr(output, "pooler_output")
    return tensor.detach().cpu().numpy()


CLAP_MAX_WINDOWS = 24   # windows per file: a 16-minute ambience is sampled across, not just its start


def window_spans(n_samples: int, sr: int) -> list[tuple[int, int]]:
    """The 10-s windows that stand for a whole file, as [start, end) sample
    offsets (2026-09-07, the user's steer — the first 10 s alone
    misrepresented anything longer). One window for a clip up to 10 s;
    contiguous windows up to CLAP_MAX_WINDOWS; beyond that, that many spread
    evenly over the file. A short tail (< 1 s) is dropped when there are
    other windows. A function of the length alone, so the rows the embedding
    stage keeps for them (§6.4 `window` segments) are reproducible."""
    limit = int(CLAP_MAX_SECONDS * sr)
    if n_samples <= limit:
        return [(0, n_samples)]
    count = int(np.ceil(n_samples / limit))
    if count <= CLAP_MAX_WINDOWS:
        starts = [i * limit for i in range(count)]
    else:
        starts = np.linspace(0, n_samples - limit, CLAP_MAX_WINDOWS).astype(int).tolist()
    spans = [(s, min(n_samples, s + limit)) for s in starts]
    return [(s, e) for s, e in spans if e - s >= sr] or spans[:1]


def sample_windows(y: np.ndarray, sr: int) -> list[np.ndarray]:
    """`window_spans` applied to a buffer. Their vectors are averaged and
    re-normalised — the usual whole-clip embedding for a fixed-window model.
    Clips shorter than 10 s are repeat-padded by the feature extractor, as
    CLAP was trained."""
    return [y[s:e] for s, e in window_spans(y.size, sr)]


def needs_window_rows(duration_s: float | None) -> bool:
    """Whether a file of this length is embedded through more than one
    window — the same rule as `window_spans`, at CLAP's rate."""
    if duration_s is None or duration_s <= 0:
        return False
    return len(window_spans(int(round(duration_s * CLAP_SAMPLE_RATE)), CLAP_SAMPLE_RATE)) > 1


def crop_for_clap(clip: np.ndarray, sr: int) -> np.ndarray:
    """First CLAP_MAX_SECONDS of a clip, as float32 — deterministic, unlike the
    feature extractor's random truncation."""
    limit = int(CLAP_MAX_SECONDS * sr)
    clip = np.asarray(clip, dtype=np.float32)
    return clip[:limit] if clip.size > limit else clip


def load_audio_for_clap(path) -> tuple[np.ndarray, int] | None:
    """Decode at CLAP's rate (a separate decode from analysis' 22.05 kHz one:
    resampling that buffer up would lose everything above 11 kHz)."""
    import librosa

    try:
        return librosa.load(str(path), sr=CLAP_SAMPLE_RATE, mono=True)
    except Exception as exc:
        log.debug("decode failed: %s (%s: %s)", path, type(exc).__name__, exc)
        return None


# --- nodes X and E: from vectors to tags and a class ----------------------------


@dataclass
class Prompts:
    """Text-side vectors, computed once per run."""

    class_names: list[str]
    class_vectors: list[np.ndarray]     # per class: (n_prompts, dim)
    tag_names: list[str]
    tag_vectors: np.ndarray             # (n_tags, dim)

    @classmethod
    def build(cls, encoder: Encoder) -> "Prompts":
        names = list(CONTENT_CLASSES)
        flat = [p for c in names for p in CLASS_PROMPTS[c]]
        vecs = encoder.embed_text(flat) if flat else np.zeros((0, EMBED_DIM), np.float32)
        per_class: list[np.ndarray] = []
        offset = 0
        for c in names:
            n = len(CLASS_PROMPTS[c])
            per_class.append(vecs[offset : offset + n])
            offset += n
        tag_vecs = (
            encoder.embed_text([TAG_PROMPT.format(t) for t in ZERO_SHOT_TAGS])
            if ZERO_SHOT_TAGS else np.zeros((0, EMBED_DIM), np.float32)
        )
        return cls(names, per_class, list(ZERO_SHOT_TAGS), tag_vecs)


@dataclass
class ZeroShot:
    content_class: str | None      # None = flagged (below threshold)
    best_class: str
    confidence: float
    class_scores: dict[str, float]  # softmax probabilities
    tags: list[tuple[str, float]]   # (tag, cosine), best first


def _softmax(x: np.ndarray) -> np.ndarray:
    z = x - x.max()
    e = np.exp(z)
    return e / e.sum()


def classify(
    audio_vec: np.ndarray,
    prompts: Prompts,
    logit_scale: float,
    settings: EmbedSettings,
    harmonic_ratio: float | None = None,
) -> ZeroShot:
    """Nodes X + E on one unit vector."""
    scores = np.array(
        [float((pv @ audio_vec).max()) if pv.size else -1.0 for pv in prompts.class_vectors],
        dtype=np.float64,
    )
    probs = _softmax(scores * float(logit_scale))
    order = np.argsort(-probs)
    best = prompts.class_names[int(order[0])]
    second = prompts.class_names[int(order[1])] if len(order) > 1 else None

    # Descriptor tie-break (spec §7 node E): a close Rhythmic/Melodic call is
    # settled by the HPSS harmonic share, the same quantity that gates pitch.
    if (
        second is not None
        and {best, second} == {"rhythmic", "melodic"}
        and probs[order[0]] - probs[order[1]] < TIE_BREAK_MARGIN
        and harmonic_ratio is not None
    ):
        if harmonic_ratio >= TIE_BREAK_HARMONIC_HIGH:
            best = "melodic"
        elif harmonic_ratio <= TIE_BREAK_HARMONIC_LOW:
            best = "rhythmic"
    confidence = float(probs[prompts.class_names.index(best)])
    assigned = best if confidence >= settings.confidence_threshold else None

    tags: list[tuple[str, float]] = []
    if prompts.tag_vectors.size and settings.top_k_tags:
        sims = prompts.tag_vectors @ audio_vec
        top = np.argsort(-sims)[: settings.top_k_tags]
        tags = [(prompts.tag_names[int(i)], float(sims[int(i)])) for i in top]
    return ZeroShot(
        assigned, best, confidence,
        {c: float(p) for c, p in zip(prompts.class_names, probs)}, tags,
    )


# --- persistence -------------------------------------------------------------------


def _write_tags(conn: sqlite3.Connection, sample_id: int, result: ZeroShot) -> None:
    """Replace the machine tag layer for a sample. User-confirmed chips stay."""
    now = now_iso()
    conn.execute(
        "DELETE FROM text_tags WHERE sample_id = ? AND is_user_confirmed = 0 "
        "AND source_model IN (?, ?)",
        (sample_id, SOURCE_TAGS, SOURCE_CLASS),
    )
    rows = [(sample_id, tag, SOURCE_TAGS, score, now) for tag, score in result.tags]
    # Every class's probability, not only the winner's: the numbers are what
    # the UI shows (2026-09-07, the user's steer); the "class" is their argmax.
    rows.extend(
        (sample_id, name, SOURCE_CLASS, probability, now)
        for name, probability in result.class_scores.items()
    )
    conn.executemany(
        "INSERT OR IGNORE INTO text_tags (sample_id, tag_or_caption, source_model, "
        "score, created_at) VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def _write_facet_a(conn: sqlite3.Connection, sample_id: int, result: ZeroShot) -> bool:
    """Node E's assignment. Returns True if a manual correction protected the row."""
    row = conn.execute(
        "SELECT is_user_confirmed FROM classification WHERE sample_id = ?", (sample_id,)
    ).fetchone()
    if row is not None and row[0]:
        return True
    conn.execute(
        "INSERT INTO classification (sample_id, content_class, confidence) VALUES (?, ?, ?) "
        "ON CONFLICT(sample_id) DO UPDATE "
        "SET content_class = excluded.content_class, confidence = excluded.confidence",
        (sample_id, result.content_class, result.confidence),
    )
    return False


def _write_segment_classification(conn: sqlite3.Connection, sample_id: int) -> None:
    """§6.4: segments inherit the parent's content class; structural type is
    fixed to one-shot. Manually confirmed segment classifications are kept."""
    parent = conn.execute(
        "SELECT content_class, confidence FROM classification WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()
    if parent is None:
        return
    conn.execute(
        "INSERT INTO segment_classification (segment_id, content_class, structural_type, confidence) "
        "SELECT id, ?, 'one-shot', ? FROM segments WHERE sample_id = ? AND detection_method != ? "
        "ON CONFLICT(segment_id) DO UPDATE SET content_class = excluded.content_class, "
        "confidence = excluded.confidence WHERE segment_classification.is_user_confirmed = 0",
        (parent[0], parent[1], sample_id, WINDOW_METHOD),
    )


def _store_windows(
    conn: sqlite3.Connection,
    sample_id: int,
    spans: list[tuple[int, int]],
    sr: int,
    vectors: np.ndarray,
    now: str,
) -> int:
    """§6.4's third kind of segment: one `window` row per 10-s CLAP window of
    a file that needed more than one, each with its vector, so a text search
    or a ranking can land at minute seven of an ambience the way it lands on
    a detected segment. Derived rows — replaced whenever the parent's windows
    are computed again; a file that now fits one window keeps none."""
    conn.execute(
        "DELETE FROM segments WHERE sample_id = ? AND detection_method = ?",
        (sample_id, WINDOW_METHOD),
    )
    if len(spans) < 2:
        return 0
    stored = 0
    for (start, end), vec in zip(spans, vectors):
        cursor = conn.execute(
            "INSERT INTO segments (sample_id, start_ms, end_ms, detection_method, "
            "is_user_confirmed, detected_at) VALUES (?, ?, ?, ?, 0, ?)",
            (sample_id, round(start * 1000 / sr), round(end * 1000 / sr), WINDOW_METHOD, now),
        )
        conn.execute(
            "INSERT INTO segment_embedding (segment_id, model_name, vector) VALUES (?, ?, ?)",
            (cursor.lastrowid, MODEL_NAME, vector_to_blob(vec)),
        )
        stored += 1
    return stored


def _embed_one_sample(
    conn: sqlite3.Connection,
    sample_id: int,
    filepath: str,
    harmonic_ratio: float | None,
    encoder: Encoder,
    prompts: Prompts,
    settings: EmbedSettings,
    summary: EmbedSummary,
    reembed_segments: bool,
    embed_parent: bool = True,
    embed_windows: bool = False,
) -> bool:
    """Nodes D + C2 (embedding) + X + E for one sample. False if it cannot decode.

    `embed_parent=False` is the "only its segments need vectors" case (they
    were detected after the parent was embedded): the parent's vector, tags
    and Facet A are left alone and only the segment windows go to the model.
    `embed_windows` (with `embed_parent=False`) computes the parent's 10-s
    windows only to keep them as `window` segments (§6.4) — the backfill for
    an index embedded before those rows existed; the parent's own vector is
    the mean of the same windows and is left as it is.
    """
    loaded = load_audio_for_clap(filepath)
    if loaded is None:
        return False
    y, sr = loaded

    # Segment windows that need a vector, in one batch with the parent.
    segment_rows: list[tuple[int, int, int]] = []
    windows: list[np.ndarray] = []
    if settings.embed_segments:
        sql = (
            "SELECT g.id, g.start_ms, g.end_ms FROM segments g "
            "WHERE g.sample_id = ? AND g.detection_method != ?"
        )
        if not reembed_segments:
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM segment_embedding e "
                "                 WHERE e.segment_id = g.id AND e.model_name = ?)"
            )
            params: tuple = (sample_id, WINDOW_METHOD, MODEL_NAME)
        else:
            params = (sample_id, WINDOW_METHOD)
        min_samples = int(settings.min_segment_length_ms * sr / 1000)
        for seg_id, start_ms, end_ms in conn.execute(sql + " ORDER BY g.start_ms", params):
            # Judge the window that actually exists in the file: a manual
            # segment past the end (flagged needs_review) would otherwise hand
            # the model an empty clip and fail its whole parent.
            start = min(y.size, int(start_ms * sr / 1000))
            end = min(y.size, int(end_ms * sr / 1000))
            if end - start < max(1, min_samples):
                summary.segments_skipped_short += 1
                continue
            segment_rows.append((seg_id, start_ms, end_ms))
            windows.append(y[start:end])

    parent_spans = window_spans(y.size, sr) if (embed_parent or embed_windows) else []
    parent_windows = [y[start:end] for start, end in parent_spans]
    clips = parent_windows + windows
    if not clips:
        return True
    vectors = encoder.embed_audio(clips, sr)
    now = now_iso()

    parent_vec = None
    if embed_parent:
        mean = vectors[: len(parent_windows)].astype(np.float64).mean(axis=0)
        parent_vec = (mean / (np.linalg.norm(mean) or 1.0)).astype(np.float32)
        conn.execute(
            "INSERT INTO embedding (sample_id, model_name, vector, embedded_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(sample_id, model_name) DO UPDATE "
            "SET vector = excluded.vector, embedded_at = excluded.embedded_at",
            (sample_id, MODEL_NAME, vector_to_blob(parent_vec), now),
        )
    if parent_spans:
        summary.windows_stored += _store_windows(
            conn, sample_id, parent_spans, sr, vectors[: len(parent_spans)], now
        )
    segment_vectors = vectors[len(parent_windows):]
    for (seg_id, _s, _e), vec in zip(segment_rows, segment_vectors):
        conn.execute(
            "INSERT INTO segment_embedding (segment_id, model_name, vector) VALUES (?, ?, ?) "
            "ON CONFLICT(segment_id, model_name) DO UPDATE SET vector = excluded.vector",
            (seg_id, MODEL_NAME, vector_to_blob(vec)),
        )
    summary.segments_embedded += len(segment_rows)

    if embed_parent:
        result = classify(parent_vec, prompts, encoder.logit_scale, settings, harmonic_ratio)
        _write_tags(conn, sample_id, result)
        if _write_facet_a(conn, sample_id, result):
            summary.protected += 1
        elif result.content_class is None:
            summary.flagged += 1
        else:
            summary.classified += 1
    _write_segment_classification(conn, sample_id)
    return True


def embed_pending(
    conn: sqlite3.Connection,
    encoder: Encoder | None = None,
    settings: EmbedSettings | None = None,
    limit: int | None = None,
    reembed: bool = False,
    progress_every: int = 50,
    scope: Sequence[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> EmbedSummary:
    """Nodes D, C2, X, E over samples that need them.

    A sample needs a visit when it has been analysed (Phase 2 — node E's
    tie-break reads `harmonic_ratio`) and either its own vector is missing or
    stale (the scanner's content flag), or — with segment embedding on — it
    has a long-enough segment without a vector, or it is long enough for
    more than one CLAP window and has no `window` rows yet (§6.4; an index
    embedded before those rows existed). The second case is what a later
    `crate-segment` run (or `--resegment`, which makes new rows) leaves
    behind; it embeds only the segments and leaves the parent alone, as the
    third does after computing the windows.

    `scope` (folders, §9.6) limits the visit to files under them; None is
    everything. `should_stop` is polled before each sample.
    """
    settings = settings or EmbedSettings()
    encoder = encoder or ClapEncoder(settings.checkpoint, settings.batch_size)
    summary = EmbedSummary()
    started = time.perf_counter()

    stale_parent = "(e.sample_id IS NULL OR s.content_changed_at > e.embedded_at)"
    orphan_segments = (
        "EXISTS (SELECT 1 FROM segments g WHERE g.sample_id = s.id "
        "        AND g.detection_method != ? AND g.end_ms - g.start_ms >= ? "
        "        AND NOT EXISTS (SELECT 1 FROM segment_embedding se "
        "                        WHERE se.segment_id = g.id AND se.model_name = ?))"
    )
    # A coarse SQL gate (more than a window plus the 1-s tail rule needs);
    # `needs_window_rows` applies the exact rule per file below.
    missing_windows = (
        "(s.duration_s >= ? AND NOT EXISTS (SELECT 1 FROM segments w "
        "                                   WHERE w.sample_id = s.id AND w.detection_method = ?))"
    )
    sql = (
        f"SELECT s.id, s.filepath, a.harmonic_ratio, s.duration_s, "
        f"{stale_parent} AS needs_parent, {missing_windows} AS missing_windows "
        "FROM samples s JOIN analysis a ON a.sample_id = s.id "
        "LEFT JOIN embedding e ON e.sample_id = s.id AND e.model_name = ? "
        "WHERE s.duration_s IS NOT NULL"
    )
    params: list = [CLAP_MAX_SECONDS + 1.0, WINDOW_METHOD, MODEL_NAME]
    if not reembed:
        conditions = [stale_parent, missing_windows]
        params += [CLAP_MAX_SECONDS + 1.0, WINDOW_METHOD]
        if settings.embed_segments:
            conditions.append(orphan_segments)
            params += [WINDOW_METHOD, settings.min_segment_length_ms, MODEL_NAME]
        sql += " AND (" + " OR ".join(conditions) + ")"
    scope_sql, scope_params = scope_clause(scope)
    sql += scope_sql + " ORDER BY s.id"
    params += scope_params
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    worklist = conn.execute(sql, params).fetchall()
    total = len(worklist)
    log.info("embedding: %d samples to visit", total)
    if not worklist:
        summary.elapsed_s = time.perf_counter() - started
        return summary

    prompts = Prompts.build(encoder)
    visited = 0
    for sample_id, filepath, harmonic_ratio, duration_s, needs_parent, missing_windows in worklist:
        if should_stop is not None and should_stop():
            summary.stopped = True
            log.info("embedding stopped by request after %d of %d", visited, total)
            break
        embed_parent = bool(needs_parent) or reembed
        try:
            ok = _embed_one_sample(
                conn, sample_id, filepath, harmonic_ratio, encoder, prompts,
                settings, summary, reembed_segments=reembed,
                embed_parent=embed_parent,
                embed_windows=(
                    not embed_parent and bool(missing_windows) and needs_window_rows(duration_s)
                ),
            )
            if not ok:
                conn.rollback()
                summary.failed += 1
                if len(summary.error_samples) < 5:
                    summary.error_samples.append(f"decode failed: {filepath}")
                continue
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - per-file isolation
            conn.rollback()
            summary.failed += 1
            log.warning("embedding failed: %s (%s: %s)", filepath, type(exc).__name__, exc)
            if len(summary.error_samples) < 5:
                summary.error_samples.append(f"{type(exc).__name__}: {filepath}")
            continue
        visited += 1
        if embed_parent:
            summary.samples_embedded += 1
        if progress_every and visited % progress_every == 0:
            elapsed = time.perf_counter() - started
            log.info(
                "visited %d/%d (%.2f s/sample, %d samples + %d segments embedded, "
                "%d windows kept, %d failed)",
                visited, total, elapsed / visited, summary.samples_embedded,
                summary.segments_embedded, summary.windows_stored, summary.failed,
            )
    summary.elapsed_s = time.perf_counter() - started
    return summary


def reclassify(
    conn: sqlite3.Connection,
    encoder: Encoder | None = None,
    settings: EmbedSettings | None = None,
) -> EmbedSummary:
    """Nodes X + E again from the stored vectors — no audio, no model audio
    pass. This is what makes prompt and threshold tuning cheap."""
    settings = settings or EmbedSettings()
    encoder = encoder or ClapEncoder(settings.checkpoint, settings.batch_size)
    summary = EmbedSummary()
    started = time.perf_counter()
    prompts = Prompts.build(encoder)
    rows = conn.execute(
        "SELECT e.sample_id, e.vector, a.harmonic_ratio FROM embedding e "
        "LEFT JOIN analysis a ON a.sample_id = e.sample_id WHERE e.model_name = ?",
        (MODEL_NAME,),
    ).fetchall()
    for sample_id, blob, harmonic_ratio in rows:
        result = classify(blob_to_vector(blob), prompts, encoder.logit_scale, settings, harmonic_ratio)
        _write_tags(conn, sample_id, result)
        if _write_facet_a(conn, sample_id, result):
            summary.protected += 1
        elif result.content_class is None:
            summary.flagged += 1
        else:
            summary.classified += 1
        _write_segment_classification(conn, sample_id)
    conn.commit()
    summary.elapsed_s = time.perf_counter() - started
    return summary


@dataclass
class ExportSummary:
    path: str = ""
    samples: int = 0
    segments: int = 0
    windows: int = 0       # of the segments, how many are §6.4 CLAP windows of long files
    dimensions: int = 0

    def format(self) -> str:
        return (
            f"exported {self.samples} sample + {self.segments} segment vectors "
            f"({self.windows} of them windows of long files, {self.dimensions} dims) to {self.path}"
        )


def export_vectors(conn: sqlite3.Connection, path, model_name: str = MODEL_NAME) -> ExportSummary:
    """Every stored CLAP vector as one `.npz` for use outside Crate (a
    notebook, a classifier of your own): arrays `ids`, `kinds` ('sample' |
    'segment'), `methods` ('' for a sample; 'auto' | 'manual' | 'window' for
    a segment), `filepaths`, `start_ms`, `end_ms`, `vectors` (n × dim,
    float32, unit length), row-aligned."""
    ids: list[int] = []
    kinds: list[str] = []
    methods: list[str] = []
    paths: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    vectors: list[np.ndarray] = []
    for sid, filepath, blob in conn.execute(
        "SELECT e.sample_id, s.filepath, e.vector FROM embedding e "
        "JOIN samples s ON s.id = e.sample_id WHERE e.model_name = ? ORDER BY e.sample_id",
        (model_name,),
    ):
        ids.append(int(sid)); kinds.append("sample"); methods.append(""); paths.append(str(filepath))
        starts.append(0); ends.append(0); vectors.append(blob_to_vector(blob))
    n_samples = len(ids)
    for gid, filepath, start, end, method, blob in conn.execute(
        "SELECT g.id, s.filepath, g.start_ms, g.end_ms, g.detection_method, se.vector "
        "FROM segment_embedding se "
        "JOIN segments g ON g.id = se.segment_id JOIN samples s ON s.id = g.sample_id "
        "WHERE se.model_name = ? ORDER BY g.id",
        (model_name,),
    ):
        ids.append(int(gid)); kinds.append("segment"); methods.append(str(method)); paths.append(str(filepath))
        starts.append(int(start)); ends.append(int(end)); vectors.append(blob_to_vector(blob))
    matrix = np.stack(vectors).astype(np.float32) if vectors else np.zeros((0, EMBED_DIM), np.float32)
    np.savez_compressed(
        str(path), ids=np.asarray(ids, dtype=np.int64), kinds=np.asarray(kinds),
        methods=np.asarray(methods), filepaths=np.asarray(paths),
        start_ms=np.asarray(starts, dtype=np.int64),
        end_ms=np.asarray(ends, dtype=np.int64), vectors=matrix,
    )
    return ExportSummary(
        str(path), n_samples, len(ids) - n_samples, methods.count(WINDOW_METHOD), int(matrix.shape[1])
    )
