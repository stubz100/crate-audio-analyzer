"""Node G — the 2D projection behind the map view (spec §7, §8, §9.3, §9.6),
no Qt (Phase 6).

A layout is one explicit computation (§9.6 *Recompute map layout*): the
§5.1 feature space under the blend weights in force at fit time, projected
to 2D with UMAP over the samples in scope, stored as a `map_layout` row plus
one `map_position` per sample, with the fitted reducer pickled next to the
cache so the **anchored-only** path can `transform` a single sample into the
existing layout later. Coordinates are a function of weights + scope + fit,
never of one embedding model, which is why they live here and not on the
embedding rows (§8). Segments never get a point (§6.4).

umap-learn is the `map` extra (§10, §12). Without it a two-component PCA
stands in, and the summary says so — the map is then a projection, not a
manifold, but the window still works.
"""

from __future__ import annotations

import json
import logging
import pickle
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .db import now_iso, scope_clause
from .similarity import KIND_SAMPLE, FeatureTable

log = logging.getLogger(__name__)

DEFAULT_N_NEIGHBORS = 15
DEFAULT_MIN_DIST = 0.1
DEFAULT_RANDOM_SEED = 42
MIN_SAMPLES = 3


@dataclass
class LayoutSettings:
    """What one *Recompute map layout* run is asked to do."""

    weights: dict[str, float]
    scope: tuple[str, ...] = ()              # folder-scope list; () = every analysed sample
    scope_description: str = "whole index"
    n_neighbors: int = DEFAULT_N_NEIGHBORS
    min_dist: float = DEFAULT_MIN_DIST
    random_seed: int = DEFAULT_RANDOM_SEED


@dataclass
class LayoutSummary:
    layout_id: int = 0
    placed: int = 0
    reducer: str = ""
    scope_description: str = ""
    stopped: bool = False
    notes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def format(self) -> str:
        if self.stopped:
            return f"map layout stopped before the fit: nothing written\nelapsed: {self.elapsed_s:.1f}s"
        lines = [
            f"map layout #{self.layout_id}: {self.placed} samples placed "
            f"({self.scope_description}, {self.reducer})"
        ]
        lines.extend(f"note: {note}" for note in self.notes)
        lines.append(f"elapsed: {self.elapsed_s:.1f}s")
        return "\n".join(lines)


@dataclass
class PlacementSummary:
    layout_id: int
    sample_id: int
    x: float
    y: float
    elapsed_s: float = 0.0

    def format(self) -> str:
        return (
            f"anchor placed in layout #{self.layout_id}: sample {self.sample_id} "
            f"at ({self.x:.2f}, {self.y:.2f})\nelapsed: {self.elapsed_s:.1f}s"
        )


@dataclass(frozen=True)
class LayoutInfo:
    """The current layout's provenance — what the map's caption shows."""

    id: int
    computed_at: str
    scope_description: str
    weights: dict[str, float]
    reducer: str
    placed: int


class PcaReducer:
    """Two principal components: the stand-in when umap-learn is missing,
    and the test double (deterministic, picklable, `transform`-able)."""

    def __init__(self) -> None:
        self._mean: np.ndarray | None = None
        self._components: np.ndarray | None = None

    def fit_transform(self, matrix: np.ndarray) -> np.ndarray:
        x = np.asarray(matrix, dtype=np.float64)
        self._mean = x.mean(axis=0)
        centred = x - self._mean
        _, _, vt = np.linalg.svd(centred, full_matrices=False)
        components = vt[:2]
        if components.shape[0] < 2:                       # a single feature column
            components = np.vstack([components, np.zeros_like(components)])
        self._components = components
        return centred @ components.T

    def transform(self, matrix: np.ndarray) -> np.ndarray:
        if self._mean is None or self._components is None:
            raise ValueError("reducer not fitted")
        return (np.asarray(matrix, dtype=np.float64) - self._mean) @ self._components.T


def make_reducer(settings: LayoutSettings, n_samples: int) -> tuple[object, str]:
    """UMAP when the `map` extra is installed, else PCA (with its name, so
    the summary and the layout row say which)."""
    n_neighbors = max(2, min(settings.n_neighbors, n_samples - 1))
    try:
        import umap  # noqa: WPS433 - optional extra, imported on demand
    except ImportError:
        return PcaReducer(), "pca"
    return (
        umap.UMAP(
            n_components=2,
            n_neighbors=n_neighbors,
            min_dist=settings.min_dist,
            random_state=settings.random_seed,
        ),
        "umap",
    )


def _scoped_sample_rows(
    conn: sqlite3.Connection, features: FeatureTable, scope: tuple[str, ...]
) -> np.ndarray:
    rows = features.sample_rows()
    if not scope:
        return rows
    clause, params = scope_clause(scope)
    ids = {r[0] for r in conn.execute(f"SELECT s.id FROM samples s WHERE 1 = 1{clause}", params)}
    return rows[np.isin(features.ids[rows], np.fromiter(ids, dtype=np.int64))]


def fit_layout(
    conn: sqlite3.Connection,
    settings: LayoutSettings,
    model_dir: Path | str,
    features: FeatureTable | None = None,
    reducer: object | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> LayoutSummary:
    """§9.6 *Recompute map layout*, library scope: a full re-fit over the
    samples in scope, written as the new current layout.

    The fit itself cannot be interrupted, so `should_stop` is honoured only
    before it starts (a stopped run writes nothing). `reducer` is injectable
    for tests; otherwise UMAP, or PCA without the `map` extra.
    """
    summary = LayoutSummary(scope_description=settings.scope_description)
    started = time.perf_counter()
    if not any(v > 0 for v in settings.weights.values()):
        raise ValueError("every weight is zero: nothing to project")
    features = features or FeatureTable.load(conn)
    rows = _scoped_sample_rows(conn, features, settings.scope)
    if rows.size < MIN_SAMPLES:
        raise ValueError(
            f"a layout needs at least {MIN_SAMPLES} analysed samples in scope, found {rows.size}"
        )
    matrix = features.weighted_matrix(rows, settings.weights)
    if should_stop is not None and should_stop():
        summary.stopped = True
        summary.elapsed_s = time.perf_counter() - started
        return summary
    fell_back = False
    if reducer is None:
        reducer, reducer_name = make_reducer(settings, rows.size)
        fell_back = reducer_name == "pca"
    else:
        reducer_name = type(reducer).__name__.lower().removesuffix("reducer") or "custom"
    log.info(
        "map layout: fitting %s over %d samples × %d features (%s)",
        reducer_name, rows.size, matrix.shape[1], settings.scope_description,
    )
    xy = np.asarray(reducer.fit_transform(matrix), dtype=np.float64)
    if xy.shape != (rows.size, 2):
        raise ValueError(f"reducer returned shape {xy.shape}, expected ({rows.size}, 2)")

    params = {
        "n_neighbors": settings.n_neighbors, "min_dist": settings.min_dist,
        "n_features": int(matrix.shape[1]),
    }
    conn.execute("UPDATE map_layout SET is_current = 0 WHERE is_current = 1")
    cursor = conn.execute(
        "INSERT INTO map_layout (computed_at, scope_description, blend_weights_json, "
        "umap_params_json, random_seed, is_current, reducer) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (
            now_iso(), settings.scope_description, json.dumps(settings.weights),
            json.dumps(params), settings.random_seed, reducer_name,
        ),
    )
    layout_id = int(cursor.lastrowid)
    conn.executemany(
        "INSERT INTO map_position (layout_id, sample_id, map_x, map_y) VALUES (?, ?, ?, ?)",
        [
            (layout_id, int(features.ids[row]), float(xy[i, 0]), float(xy[i, 1]))
            for i, row in enumerate(rows)
        ],
    )
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / f"layout_{layout_id}.pkl"
    with model_path.open("wb") as handle:
        pickle.dump({"reducer": reducer, "weights": dict(settings.weights), "name": reducer_name}, handle)
    conn.execute("UPDATE map_layout SET model_path = ? WHERE id = ?", (str(model_path), layout_id))
    conn.commit()

    summary.layout_id = layout_id
    summary.placed = int(rows.size)
    summary.reducer = reducer_name
    if fell_back:
        summary.notes.append(
            "umap-learn is not installed (the `map` extra): this layout is a PCA projection"
        )
    summary.elapsed_s = time.perf_counter() - started
    return summary


def _current_layout_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, computed_at, scope_description, blend_weights_json, reducer, model_path "
        "FROM map_layout WHERE is_current = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()


def place_anchor(
    conn: sqlite3.Connection,
    kind: str,
    item_id: int,
    features: FeatureTable | None = None,
) -> PlacementSummary:
    """§9.6 *Recompute map layout*, anchored only: transform the anchor's
    sample into the current layout with the pickled reducer — cheap, moves
    only that point. A segment anchor places its parent sample (§6.4)."""
    started = time.perf_counter()
    layout = _current_layout_row(conn)
    if layout is None:
        raise ValueError("no map layout yet: run a library-scope layout first")
    model_path = layout["model_path"]
    if not model_path or not Path(model_path).exists():
        raise ValueError("the current layout's model file is missing: re-fit the layout")
    if kind == KIND_SAMPLE:
        sample_id = int(item_id)
    else:
        row = conn.execute("SELECT sample_id FROM segments WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown segment {item_id}")
        sample_id = int(row[0])
    features = features or FeatureTable.load(conn)
    feature_row = features.row_of(KIND_SAMPLE, sample_id)
    if feature_row is None:
        raise ValueError("the anchor has no analysis yet: run Recompute attributes first")
    with Path(model_path).open("rb") as handle:
        bundle = pickle.load(handle)  # noqa: S301 - our own file, written by fit_layout
    matrix = features.weighted_matrix(np.array([feature_row]), bundle["weights"])
    xy = np.asarray(bundle["reducer"].transform(matrix), dtype=np.float64)[0]
    conn.execute(
        "INSERT INTO map_position (layout_id, sample_id, map_x, map_y) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(layout_id, sample_id) DO UPDATE SET map_x = excluded.map_x, map_y = excluded.map_y",
        (int(layout["id"]), sample_id, float(xy[0]), float(xy[1])),
    )
    conn.commit()
    return PlacementSummary(
        int(layout["id"]), sample_id, float(xy[0]), float(xy[1]),
        elapsed_s=time.perf_counter() - started,
    )


def load_current_layout(
    conn: sqlite3.Connection,
) -> tuple[LayoutInfo | None, dict[int, tuple[float, float]]]:
    """The current layout's provenance and every sample's position in it —
    what the map view draws (§9.3: 'the last explicitly-computed layout')."""
    layout = _current_layout_row(conn)
    if layout is None:
        return None, {}
    positions = {
        int(r[0]): (float(r[1]), float(r[2]))
        for r in conn.execute(
            "SELECT sample_id, map_x, map_y FROM map_position "
            "WHERE layout_id = ? AND sample_id IS NOT NULL",
            (layout["id"],),
        )
    }
    weights = json.loads(layout["blend_weights_json"] or "{}")
    info = LayoutInfo(
        int(layout["id"]), str(layout["computed_at"]), str(layout["scope_description"] or ""),
        {k: float(v) for k, v in weights.items()}, str(layout["reducer"] or ""), len(positions),
    )
    return info, positions
