"""Retrieval metrics for comparing similarity representations on a labelled
evaluation set (spec §5.3's spike; reusable for any later axis). No Qt, no
models: unit vectors in, numbers out.

Every item is a query against all the others (leave-one-out): the top-k
neighbours by cosine, the fraction of them sharing the query's label
(precision@k), the overlap of two representations' top-k sets (Jaccard),
and how far apart a representation keeps the groups (mean within-group
cosine minus mean between-group cosine).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def cosine_matrix(vectors: np.ndarray) -> np.ndarray:
    """Pairwise cosines of unit vectors (n × dim), the diagonal set to −∞ so
    an item is never its own neighbour."""
    v = np.asarray(vectors, dtype=np.float64)
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    v = v / np.where(norms > 0, norms, 1.0)
    sims = v @ v.T
    np.fill_diagonal(sims, -np.inf)
    return sims


def top_k(sims: np.ndarray, k: int) -> np.ndarray:
    """Indices of each row's k best neighbours, best first (n × k)."""
    k = max(1, min(k, sims.shape[1] - 1))
    order = np.argsort(-sims, axis=1)[:, :k]
    return order


def precision_at_k(sims: np.ndarray, labels: Sequence, k: int = 5, queries: np.ndarray | None = None) -> np.ndarray:
    """Per query, the fraction of its top-k neighbours with the same label.
    `queries` restricts which rows are queries (all by default); neighbours
    are always drawn from every item."""
    labels = np.asarray(labels)
    neighbours = top_k(sims, k)
    same = labels[neighbours] == labels[:, None]
    per_query = same.mean(axis=1)
    return per_query if queries is None else per_query[np.asarray(queries)]


def jaccard_at_k(sims_a: np.ndarray, sims_b: np.ndarray, k: int = 5) -> np.ndarray:
    """Per item, |top-k(a) ∩ top-k(b)| / |top-k(a) ∪ top-k(b)|."""
    a, b = top_k(sims_a, k), top_k(sims_b, k)
    out = np.zeros(a.shape[0])
    for i in range(a.shape[0]):
        sa, sb = set(a[i].tolist()), set(b[i].tolist())
        out[i] = len(sa & sb) / len(sa | sb)
    return out


def separation(sims: np.ndarray, labels: Sequence) -> float:
    """Mean within-group cosine minus mean between-group cosine (the
    diagonal excluded). Higher = the groups sit further apart."""
    labels = np.asarray(labels)
    same = labels[:, None] == labels[None, :]
    finite = np.isfinite(sims)
    within = sims[same & finite]
    between = sims[~same & finite]
    if within.size == 0 or between.size == 0:
        return float("nan")
    return float(within.mean() - between.mean())


def hits_in_top_k(sims: np.ndarray, targets: np.ndarray, k: int = 5, queries: np.ndarray | None = None) -> np.ndarray:
    """Per query, whether any of `targets` (a boolean mask over items) is
    among its top-k neighbours — e.g. "does the same voice, different
    content, come up at all?"."""
    neighbours = top_k(sims, k)
    targets = np.asarray(targets, dtype=bool)
    per_query = targets[neighbours].any(axis=1)
    return per_query if queries is None else per_query[np.asarray(queries)]
