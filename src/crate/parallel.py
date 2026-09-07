"""A bounded process pool for the per-file stages (2026-09-07, the user's
steer: a folder recompute used 5 % of a 32-core machine).

Analysis and segmentation are a loop of independent per-file computations
(decode + descriptors) followed by a few SQLite writes. The computation is
what `BoundedMap` fans out — to Python worker processes, since librosa and
numpy hold the GIL for much of it — and the writes stay in the calling
process, which remains the index's only writer. Still one Python codebase
and no native component (spec §10); the pool is an execution detail behind
one setting, *Worker processes* on the Recompute tab.

Each worker pins its BLAS/OpenMP thread count to one: `workers` processes
each spinning up 32 threads would only fight each other.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from typing import Any

_SINGLE_THREAD_VARS = (
    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMBA_NUM_THREADS",
)


def _worker_init() -> None:
    for name in _SINGLE_THREAD_VARS:
        os.environ[name] = "1"


def make_pool(workers: int) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=max(1, int(workers)), initializer=_worker_init)


class BoundedMap:
    """Iterate `(item, result, error)` in completion order, keeping at most
    `in_flight` tasks submitted at a time, so a 110k-file worklist is never
    materialised as 110k futures. `should_stop` is polled before each
    submission; once it fires nothing more is submitted, what is in flight
    finishes (its results are still yielded and kept), and `stopped` is set.
    """

    def __init__(
        self,
        pool: ProcessPoolExecutor,
        items: Iterable[Any],
        fn: Callable[..., Any],
        args_of: Callable[[Any], tuple],
        should_stop: Callable[[], bool] | None = None,
        in_flight: int | None = None,
    ) -> None:
        self._pool = pool
        self._items = iter(items)
        self._fn = fn
        self._args_of = args_of
        self._should_stop = should_stop
        self._in_flight = in_flight or max(2, 2 * (pool._max_workers or 1))
        self.stopped = False

    def _submit_some(self, pending: dict[Future, Any]) -> None:
        while len(pending) < self._in_flight and not self.stopped:
            if self._should_stop is not None and self._should_stop():
                self.stopped = True
                return
            try:
                item = next(self._items)
            except StopIteration:
                return
            pending[self._pool.submit(self._fn, *self._args_of(item))] = item

    def __iter__(self):
        pending: dict[Future, Any] = {}
        self._submit_some(pending)
        while pending:
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for future in done:
                item = pending.pop(future)
                try:
                    yield item, future.result(), None
                except Exception as exc:  # noqa: BLE001 - the caller decides what a failure means
                    yield item, None, exc
            self._submit_some(pending)
