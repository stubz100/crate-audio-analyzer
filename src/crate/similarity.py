"""Similarity and search over the index — spec §5.1's blend and the list
behaviour of §9.4/§9.5 — with no Qt (Phase 7).

A `FeatureTable` holds every analysed item — samples *and* their segments,
because a segment can be the actual best match (§6.4, §9.4) — as rows of
per-axis standardised features plus its CLAP vector. Anchoring computes each
item's distance from the anchor on every axis **once** (§9.5: "computed once
at anchor-time, then instant to adjust"); *Recompute ranking* blends those
per-axis distances with the weight bars into one distance (§9.6); free-text
search scores the CLAP vectors against a query (§5.2). Both fold item-level
scores into per-sample scores with **sub-hits**: a segment that beats its own
parent surfaces as the parent's "hit within" row (§9.4) — never as a row of
its own.

Axes and their descriptors follow §5.1. Every scalar is standardised
robustly (median / MAD, clipped) over the whole population so no single
descriptor dominates an axis; each axis distance is then scaled by its 95th
percentile so the five axes are comparable before the weights blend them,
and so a range of "within 30 %" on the Attributes tab means the same thing
on every axis.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import numpy as np

from .db import WINDOW_METHOD
from .embedding import MODEL_NAME, blob_to_vector

log = logging.getLogger(__name__)

AXES: tuple[str, ...] = ("amplitude", "pitch", "timbre", "spectrum", "conceptual")
AXIS_LABELS: dict[str, str] = {
    "amplitude": "Amplitude",
    "pitch": "Pitch",
    "timbre": "Timbre",
    "spectrum": "Spectrum",
    "conceptual": "Conceptual",
}
KIND_SAMPLE = "sample"
KIND_SEGMENT = "segment"

_SCALE_PERCENTILE = 95.0
_Z_CLIP = 4.0

_DESCRIPTOR_COLUMNS = (
    "peak_db", "rms_db", "crest_factor", "attack_ms", "decay_ms", "f0_hz",
    "mfcc_mean", "mfcc_var", "spectral_contrast",
    "spectral_centroid", "spectral_bandwidth", "spectral_rolloff", "spectral_flatness",
)


@dataclass(frozen=True)
class Hit:
    """A segment that beats its own parent for the current scoring (§9.4) —
    a detected or manual segment, or (`window`) one of the 10-s CLAP windows
    of a long file (§6.4)."""

    segment_id: int
    start_ms: int
    end_ms: int
    score: float
    window: bool = False


@dataclass
class Scores:
    """One scoring's result, folded to what the list shows: a score per
    sample (its own, or its best segment's when that is better), the raw
    per-segment scores, and the sub-hits. Higher is better, 0..1 for
    similarity, cosine for text search."""

    sample: dict[int, float] = field(default_factory=dict)
    segment: dict[int, float] = field(default_factory=dict)
    hits: dict[int, Hit] = field(default_factory=dict)


def _log_or_nan(value: float | None) -> float:
    return float(np.log(value)) if value is not None and value > 0 else np.nan


def _log1p_or_nan(value: float | None) -> float:
    return float(np.log1p(value)) if value is not None and value >= 0 else np.nan


def _or_nan(value: float | None) -> float:
    return np.nan if value is None else float(value)


def _vector_or_nan(text: str | None, size: int) -> list[float]:
    if not text:
        return [np.nan] * size
    values = json.loads(text)
    if len(values) != size:
        return [np.nan] * size
    return [float(v) for v in values]


def _axis_features(row: Mapping[str, object]) -> dict[str, list[float]]:
    """Raw (not yet standardised) per-axis feature lists for one item."""
    return {
        "amplitude": [
            _or_nan(row["peak_db"]),
            _or_nan(row["rms_db"]),
            _log1p_or_nan(row["crest_factor"]),
            _log1p_or_nan(row["attack_ms"]),
            _log1p_or_nan(row["decay_ms"]),
        ],
        "pitch": [np.log2(row["f0_hz"]) if row["f0_hz"] else np.nan],
        "timbre": (
            _vector_or_nan(row["mfcc_mean"], 13)
            + [float(np.log1p(v)) if v == v and v >= 0 else np.nan for v in _vector_or_nan(row["mfcc_var"], 13)]
            + _vector_or_nan(row["spectral_contrast"], 7)
        ),
        "spectrum": [
            _log_or_nan(row["spectral_centroid"]),
            _log_or_nan(row["spectral_bandwidth"]),
            _log_or_nan(row["spectral_rolloff"]),
            _or_nan(row["spectral_flatness"]),
        ],
    }


def _standardise(matrix: np.ndarray) -> np.ndarray:
    """Robust z-scores per column (median / 1.4826·MAD), clipped, NaN kept.
    A column with no spread keeps its raw offset from the median."""
    if matrix.size == 0:
        return matrix
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # an all-NaN column (nothing pitched) is fine
        median = np.nanmedian(matrix, axis=0)
        mad = np.nanmedian(np.abs(matrix - median), axis=0) * 1.4826
        scale = np.where(np.isfinite(mad) & (mad > 1e-9), mad, 1.0)
        z = (matrix - median) / scale
    return np.clip(z, -_Z_CLIP, _Z_CLIP)


class FeatureTable:
    """Every analysed sample and segment as standardised per-axis features
    plus its CLAP vector (zeros and `has_vector=False` when not embedded).
    Loaded once per index; rebuilt after a recompute."""

    def __init__(
        self,
        ids: np.ndarray,
        is_segment: np.ndarray,
        parents: np.ndarray,
        starts: np.ndarray,
        ends: np.ndarray,
        features: dict[str, np.ndarray],
        vectors: np.ndarray,
        has_vector: np.ndarray,
        is_window: np.ndarray | None = None,
    ) -> None:
        self.ids = ids
        self.is_segment = is_segment
        self.is_window = np.zeros(len(ids), dtype=bool) if is_window is None else is_window
        self.parents = parents
        self.starts = starts
        self.ends = ends
        self._features = features
        self._vectors = vectors
        self._has_vector = has_vector
        self._row_of: dict[tuple[str, int], int] = {
            (KIND_SEGMENT if seg else KIND_SAMPLE, int(item_id)): i
            for i, (item_id, seg) in enumerate(zip(ids, is_segment))
        }

    # --- construction ---

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "FeatureTable":
        sample_rows = conn.execute(
            f"SELECT a.sample_id AS item_id, a.sample_id AS parent_id, 0 AS start_ms, 0 AS end_ms, "
            f"{', '.join('a.' + c for c in _DESCRIPTOR_COLUMNS)}, e.vector AS vector "
            "FROM analysis a LEFT JOIN embedding e "
            "  ON e.sample_id = a.sample_id AND e.model_name = ? ORDER BY a.sample_id",
            (MODEL_NAME,),
        ).fetchall()
        # A detected or manual segment has descriptors and (once embedded) a
        # vector; a CLAP window of a long file (§6.4) has its vector only, so
        # it is a conceptual-axis item — the blend and the search skip what an
        # item lacks.
        segment_rows = conn.execute(
            f"SELECT g.id AS item_id, g.sample_id AS parent_id, g.start_ms, g.end_ms, "
            f"g.detection_method AS method, "
            f"{', '.join('sa.' + c for c in _DESCRIPTOR_COLUMNS)}, se.vector AS vector "
            "FROM segments g "
            "LEFT JOIN segment_analysis sa ON sa.segment_id = g.id "
            "LEFT JOIN segment_embedding se ON se.segment_id = g.id AND se.model_name = ? "
            "WHERE sa.segment_id IS NOT NULL OR se.segment_id IS NOT NULL "
            "ORDER BY g.id",
            (MODEL_NAME,),
        ).fetchall()
        rows = [(r, False) for r in sample_rows] + [(r, True) for r in segment_rows]
        n = len(rows)
        ids = np.zeros(n, dtype=np.int64)
        is_segment = np.zeros(n, dtype=bool)
        is_window = np.zeros(n, dtype=bool)
        parents = np.zeros(n, dtype=np.int64)
        starts = np.zeros(n, dtype=np.int64)
        ends = np.zeros(n, dtype=np.int64)
        per_axis: dict[str, list[list[float]]] = {axis: [] for axis in AXES[:4]}
        vectors: list[np.ndarray | None] = []
        for i, (row, seg) in enumerate(rows):
            ids[i] = row["item_id"]
            is_segment[i] = seg
            is_window[i] = seg and row["method"] == WINDOW_METHOD
            parents[i] = row["parent_id"]
            starts[i] = row["start_ms"]
            ends[i] = row["end_ms"]
            feats = _axis_features(row)
            for axis in AXES[:4]:
                per_axis[axis].append(feats[axis])
            blob = row["vector"]
            vectors.append(blob_to_vector(blob) if blob is not None else None)
        features = {
            axis: _standardise(np.asarray(values, dtype=np.float64).reshape(n, -1))
            for axis, values in per_axis.items()
        }
        dim = next((v.shape[0] for v in vectors if v is not None), 0)
        matrix = np.zeros((n, dim), dtype=np.float32)
        has_vector = np.zeros(n, dtype=bool)
        for i, v in enumerate(vectors):
            if v is not None and v.shape[0] == dim:
                matrix[i] = v
                has_vector[i] = True
        log.info(
            "feature table: %d samples + %d segments (%d of them CLAP windows), %d with vectors",
            int((~is_segment).sum()), int(is_segment.sum()), int(is_window.sum()), int(has_vector.sum()),
        )
        return cls(ids, is_segment, parents, starts, ends, features, matrix, has_vector, is_window)

    # --- lookups ---

    def __len__(self) -> int:
        return len(self.ids)

    def row_of(self, kind: str, item_id: int) -> int | None:
        return self._row_of.get((kind, int(item_id)))

    def sample_rows(self) -> np.ndarray:
        return np.flatnonzero(~self.is_segment)

    # --- anchoring (§9.5) ---

    def distances(self, anchor_row: int) -> np.ndarray:
        """Per-axis distance of every item from the anchor: shape (n, 5),
        each axis scaled to 0..1 by its 95th percentile (values beyond it
        clip to 1), NaN where the item or the anchor lacks that axis."""
        n = len(self.ids)
        out = np.full((n, len(AXES)), np.nan)
        for j, axis in enumerate(AXES[:4]):
            matrix = self._features[axis]
            if matrix.size == 0:
                continue
            diff = (matrix - matrix[anchor_row]) ** 2
            valid = ~np.isnan(diff)
            count = valid.sum(axis=1)
            with np.errstate(invalid="ignore"):
                d = np.sqrt(np.nansum(diff, axis=1) / np.maximum(count, 1))
            d[count == 0] = np.nan
            out[:, j] = d
        j = AXES.index("conceptual")
        if self._has_vector[anchor_row] and self._vectors.size:
            d = 1.0 - self._vectors @ self._vectors[anchor_row]
            d[~self._has_vector] = np.nan
            out[:, j] = d
        for j in range(len(AXES)):
            column = out[:, j]
            finite = column[np.isfinite(column)]
            if finite.size:
                scale = float(np.percentile(finite, _SCALE_PERCENTILE))
                if scale > 0:
                    out[:, j] = np.clip(column / scale, 0.0, 1.0)
        return out

    # --- scoring ---

    @staticmethod
    def blend(distances: np.ndarray, weights: Mapping[str, float]) -> np.ndarray:
        """One distance per item from the per-axis ones: the weighted mean
        over the axes the item has; NaN when it has none (or every weight
        is zero)."""
        w = np.array([max(0.0, float(weights.get(axis, 0.0))) for axis in AXES])
        valid = ~np.isnan(distances)
        total = (valid * w).sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            blended = np.nansum(distances * w, axis=1) / np.where(total > 0, total, np.nan)
        return blended

    def rank(
        self,
        distances: np.ndarray,
        weights: Mapping[str, float],
        sample_ids: Iterable[int] | None = None,
    ) -> Scores:
        """§9.6 *Recompute ranking*: similarity = 1 − blended distance,
        folded per sample with sub-hits. `sample_ids` restricts the scope
        to those samples (and their segments) — the "visible set only"
        option."""
        similarity = 1.0 - self.blend(distances, weights)
        return self.fold(similarity, sample_ids)

    def search(self, query_vector: np.ndarray, sample_ids: Iterable[int] | None = None) -> Scores:
        """§5.2 free-text search: cosine of the query against every CLAP
        vector (both are L2-normalised), folded per sample with sub-hits."""
        if self._vectors.size == 0:
            return Scores()
        q = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(q))
        if norm > 0:
            q = q / norm
        cosine = self._vectors @ q
        cosine = np.where(self._has_vector, cosine, np.nan)
        return self.fold(cosine, sample_ids)

    def fold(self, values: np.ndarray, sample_ids: Iterable[int] | None = None) -> Scores:
        """Item scores → per-sample scores + sub-hits (§9.4): a sample scores
        its own value or its best segment's, whichever is higher, and the
        segment becomes the sample's `Hit` when it wins. NaN = no score."""
        in_scope = np.ones(len(self.ids), dtype=bool)
        if sample_ids is not None:
            in_scope = np.isin(self.parents, np.fromiter(sample_ids, dtype=np.int64))
        scored = in_scope & ~np.isnan(values)
        scores = Scores()
        for i in np.flatnonzero(scored & ~self.is_segment):
            scores.sample[int(self.ids[i])] = float(values[i])
        best: dict[int, tuple[float, int]] = {}
        for i in np.flatnonzero(scored & self.is_segment):
            value = float(values[i])
            scores.segment[int(self.ids[i])] = value
            parent = int(self.parents[i])
            if parent not in best or value > best[parent][0]:
                best[parent] = (value, int(i))
        for parent, (value, i) in best.items():
            own = scores.sample.get(parent)
            if own is None or value > own:
                scores.sample[parent] = value
                scores.hits[parent] = Hit(
                    int(self.ids[i]), int(self.starts[i]), int(self.ends[i]), value,
                    window=bool(self.is_window[i]),
                )
        return scores

    def weighted_matrix(self, rows: np.ndarray, weights: Mapping[str, float]) -> np.ndarray:
        """The §5.1 blend as one feature space, for a projection (node G):
        each axis's standardised columns scaled so the axis contributes in
        proportion to its weight (÷ √dim keeps a 33-column axis from
        outweighing a 1-column one), missing values at the median (0 after
        standardisation), the CLAP vector as the conceptual axis."""
        blocks: list[np.ndarray] = []
        for axis in AXES[:4]:
            weight = max(0.0, float(weights.get(axis, 0.0)))
            matrix = self._features[axis]
            if weight == 0.0 or matrix.size == 0:
                continue
            block = np.nan_to_num(matrix[rows], nan=0.0)
            blocks.append(block * np.sqrt(weight / block.shape[1]))
        weight = max(0.0, float(weights.get("conceptual", 0.0)))
        if weight > 0.0 and self._vectors.size:
            blocks.append(self._vectors[rows].astype(np.float64) * np.sqrt(weight))
        if not blocks:
            raise ValueError("every weight is zero: nothing to project")
        return np.hstack(blocks).astype(np.float32)

    def axis_distances_by_sample(self, distances: np.ndarray) -> dict[int, dict[str, float]]:
        """The per-axis distances of every *sample* (not segment) keyed by
        id — what the Attributes tab's range filters read (§9.5)."""
        out: dict[int, dict[str, float]] = {}
        for i in self.sample_rows():
            out[int(self.ids[i])] = {axis: float(distances[i, j]) for j, axis in enumerate(AXES)}
        return out
