"""Phase 12's profiling pass (spec §12, §10): the window's hot paths timed
against the real index, read-only. Run by hand; never imported by the app.

    uv run python scripts/phase12_profile.py            # the index paths
    uv run python scripts/phase12_profile.py --walk     # + the scanner's directory walk
    uv run python scripts/phase12_profile.py --db PATH --repeat 3

What is measured is what a session actually does at start and on a click:
open the index; load the list (samples, sections, the status counts) and
build the tree model; filter and sort it; load the feature table and rank
against an anchor; the model taking the ranking. Memory is the process's
working set, sampled after each step, so growth is attributable.

Nothing is written. The numbers decide what Phase 12 hardens (§10: a Rust
extension is on the table only for a hot path this pass names).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                        # noqa: E402
from PySide6.QtWidgets import QApplication           # noqa: E402

from crate.catalog import index_summary, load_samples, load_sections   # noqa: E402
from crate.db import default_db_path, open_db        # noqa: E402
from crate.library import scope_paths                # noqa: E402
from crate.listmodel import ColumnFilter, ListProxy, SampleTreeModel   # noqa: E402
from crate.similarity import AXES, FeatureTable      # noqa: E402


def working_set_mb() -> float:
    """The process's working set on Windows, in MB (0 elsewhere)."""
    try:
        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p          # a HANDLE: 64 bits, not an int
        kernel32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
        if not kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return 0.0
        return counters.WorkingSetSize / 1e6
    except Exception:  # noqa: BLE001 - not Windows, or no psapi
        return 0.0


class Profile:
    def __init__(self) -> None:
        self.rows: list[tuple[str, float, float, str]] = []
        self._last_mb = working_set_mb()

    def step(self, name: str, fn, note=""):
        started = time.perf_counter()
        result = fn()
        elapsed = time.perf_counter() - started
        now = working_set_mb()
        text = note(result) if callable(note) else note
        self.rows.append((name, elapsed, now - self._last_mb, text))
        self._last_mb = now
        print(f"  {name:44s} {elapsed:8.2f} s  {now:7.0f} MB  {text}", flush=True)
        return result

    def table(self) -> str:
        lines = ["| step | seconds | working set change, MB | notes |", "|---|---:|---:|---|"]
        for name, seconds, delta, note in self.rows:
            lines.append(f"| {name} | {seconds:.2f} | {delta:+.0f} | {note} |")
        return "\n".join(lines)


def profile_index(db_path: Path, repeat: int) -> Profile:
    p = Profile()
    print(f"index: {db_path} ({db_path.stat().st_size / 1e6:.0f} MB); working set {working_set_mb():.0f} MB")
    conn = p.step("open_db", lambda: open_db(db_path))
    scope = scope_paths(conn)
    print(f"  scope: {scope}")

    rows = p.step("load_samples (the list's rows)", lambda: load_samples(conn, scope=scope),
                  lambda r: f"{len(r)} samples")
    sections = p.step("load_sections (every child row)", lambda: load_sections(conn, scope),
                      lambda s: f"{sum(len(v) for v in s.values())} sections under {len(s)} samples")
    p.step("index_summary (status bar counts)", lambda: index_summary(conn, scope), lambda c: str(c))

    p.app = QApplication.instance() or QApplication([])   # kept on the profile: the model needs it alive
    model = SampleTreeModel()
    p.step("SampleTreeModel.set_rows", lambda: model.set_rows(rows, sections), "tree built, sorted by the default")
    proxy = ListProxy()
    p.step("ListProxy.setSourceModel", lambda: proxy.setSourceModel(model), lambda _: f"{proxy.rowCount()} top rows")
    p.step("column filter File contains 'kick'",
           lambda: proxy.set_column_filter(SampleTreeModel.COL_FILE, ColumnFilter(text="kick")),
           lambda _: f"{proxy.rowCount()} rows kept")
    p.step("column filter cleared", lambda: proxy.set_column_filter(SampleTreeModel.COL_FILE, None),
           lambda _: f"{proxy.rowCount()} rows")
    p.step("sort by Length (model sort)", lambda: proxy.sort(SampleTreeModel.COL_LENGTH, Qt.SortOrder.DescendingOrder), "")
    p.step("sort by File (model sort)", lambda: proxy.sort(SampleTreeModel.COL_FILE, Qt.SortOrder.AscendingOrder), "")

    table = p.step("FeatureTable.load", lambda: FeatureTable.load(conn),
                   lambda t: f"{len(t)} items, {int(t._has_vector.sum())} with vectors")
    feature_bytes = sum(a.nbytes for a in table._features.values()) + table._vectors.nbytes
    print(f"    arrays: {feature_bytes / 1e6:.0f} MB "
          f"(features {sum(a.nbytes for a in table._features.values()) / 1e6:.0f} MB, "
          f"vectors {table._vectors.nbytes / 1e6:.0f} MB, {table._vectors.shape}); "
          f"row index {len(table._row_of)} entries")
    anchor_row = int(table.sample_rows()[len(table.sample_rows()) // 2])
    for i in range(repeat):
        axis = p.step(f"distances from an anchor #{i + 1}", lambda: table.distances(anchor_row), "(n, 5)")
    weights = {axis_name: 1.0 for axis_name in AXES}
    sample_ids = set(r.id for r in rows)
    for i in range(repeat):
        scores = p.step(f"rank (blend + fold) #{i + 1}", lambda: table.rank(axis, weights, sample_ids),
                        lambda s: f"{len(s.sample)} samples scored, {len(s.hits)} hits")
    p.step("model.set_similarity (ranking into the tree)", lambda: model.set_similarity(scores), "")
    p.step("proxy.sort by Similarity", lambda: proxy.sort(SampleTreeModel.COL_SIMILARITY, Qt.SortOrder.DescendingOrder), "")
    p.step("model.set_similarity(None)", lambda: model.set_similarity(None), "")
    conn.close()
    return p


def profile_walk(root: Path) -> Profile:
    from crate.scanner import _walk_files

    p = Profile()
    errors: list[str] = []

    def walk():
        count = 0
        size = 0
        for entry, _folder in _walk_files(root, lambda path, exc, subtree: errors.append(path)):
            count += 1
            size += entry.stat().st_size
        return count, size

    p.step(f"scanner walk of {root}", walk, lambda r: f"{r[0]} files, {r[1] / 1e9:.0f} GB, {len(errors)} errors")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=default_db_path())
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--walk", action="store_true", help="also time the scanner's directory walk")
    parser.add_argument("--walk-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.walk_only:
        profile = profile_index(args.db, args.repeat)
        print("\n" + profile.table())
    if args.walk or args.walk_only:
        conn = open_db(args.db)
        roots = [Path(p) for p in scope_paths(conn) or ()]
        conn.close()
        for root in roots:
            print("\n" + profile_walk(root).table())
    return 0


if __name__ == "__main__":
    sys.exit(main())
