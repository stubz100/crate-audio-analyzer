"""Library scanner — pipeline node `A` (spec §7), the Phase 1 deliverable.

Recursively walks a library root, keeps one skeleton `samples` row per
supported audio file, and diffs against the previous run using the cheap
change key (`file_size` + `file_mtime`, spec §8). The full content hash is
computed ONLY for files whose size/mtime changed (spec §8); if the new hash
matches the stored one, the file is demoted to `unchanged` so later phases
never re-analyze identical content (backup restore, NAS re-sync, re-extract).

Formats (spec §3): `.wav` and `.flac` are supported (~77% of the library).
`.rx2` is skip-and-log (spec §3 decision, §13 risk #5); everything else is
skip-and-log by extension. Unreadable audio headers are logged, not fatal
(spec §7 node B guarantee) — such files keep a row with NULL metadata.

Error safety (2026-09-06 review): entries the walk cannot list or stat are
counted as walk errors and PROTECTED from removal — a locked folder or a
momentarily unreadable drive must never be mistaken for a deletion.

One database may hold several roots: removal on re-scan only touches rows
under the scanned root, so subset scans never delete other roots' rows.

Changed content is FLAGGED, never recomputed here (2026-09-06 decision): a
genuine change stamps `samples.content_changed_at`, which makes any older
`analysis` row stale; the scan summary reports the stale count and the
expensive recompute stays an explicit `crate-analyze` run (spec §9.6).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import soundfile as sf

from .db import now_iso

log = logging.getLogger(__name__)

SUPPORTED_EXTS = {".wav", ".flac"}
RX2_EXT = ".rx2"
_HASH_CHUNK_BYTES = 1024 * 1024
_COMMIT_BATCH = 5000  # a crash mid-scan loses at most one batch, not the run
_ERROR_SAMPLES = 5    # per-issue examples surfaced in the summary


@dataclass
class ScanSummary:
    """Counts from one scanner run — the node-A "worklist" for later phases."""

    root: str = ""
    added: int = 0
    changed: int = 0
    unchanged: int = 0  # includes hash-demoted files (content identical)
    moved: int = 0      # renamed/moved: row updated in place, dependents kept
    removed: int = 0
    unreadable: int = 0  # supported ext, header unreadable (row kept, NULL metadata)
    walk_errors: int = 0  # listing/stat failures; affected rows kept, never removed
    stale_analysis: int = 0  # analysis rows older than their file's content (whole DB)
    skipped_rx2: int = 0
    skipped_other: dict[str, int] = field(default_factory=dict)
    error_samples: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def format(self) -> str:
        lines = [
            f"root: {self.root}",
            f"added {self.added} | changed {self.changed} | "
            f"unchanged {self.unchanged} | moved {self.moved} | removed {self.removed}",
        ]
        if self.unreadable:
            lines.append(
                f"unreadable headers: {self.unreadable} (rows kept with NULL metadata)"
            )
        if self.walk_errors:
            lines.append(
                f"walk errors: {self.walk_errors} (affected rows kept; "
                f"re-scan once readable)"
            )
        if self.stale_analysis:
            lines.append(
                f"stale analysis: {self.stale_analysis} (content changed since "
                f"analysis; run crate-analyze to refresh)"
            )
        lines.extend(f"  ! {sample}" for sample in self.error_samples)
        lines.append(f"skipped .rx2: {self.skipped_rx2} (skip-and-log per spec §3)")
        if self.skipped_other:
            total = sum(self.skipped_other.values())
            top = ", ".join(
                f"{ext}×{n}"
                for ext, n in sorted(self.skipped_other.items(), key=lambda kv: -kv[1])[:8]
            )
            lines.append(f"skipped other: {total} ({top})")
        lines.append(f"elapsed: {self.elapsed_s:.1f}s")
        return "\n".join(lines)


def _sample_error(summary: ScanSummary, message: str) -> None:
    if len(summary.error_samples) < _ERROR_SAMPLES:
        summary.error_samples.append(message)


def _hash_file(path: Path) -> str:
    h = hashlib.blake2b()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_BYTES), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_metadata(path: Path) -> tuple[float, int, int] | None:
    """Header-only read via sf.info: (duration_s, sample_rate, channels).

    Returns None if the header is unreadable — decode failures are logged,
    not fatal (spec §7 node B). The exception itself is logged at DEBUG
    (stream with `-v`) so a broken install is distinguishable from corrupt
    files (2026-09-06 review).
    """
    try:
        info = sf.info(str(path))
        return info.duration, info.samplerate, info.channels
    except Exception as exc:
        log.debug(
            "unreadable audio header: %s (%s: %s)", path, type(exc).__name__, exc
        )
        return None


def _metadata_or_null(path: Path, summary: ScanSummary) -> tuple:
    """`_read_metadata` + unreadable bookkeeping — shared by both branches,
    so first-scan and re-scan can never silently diverge (2026-09-06)."""
    meta = _read_metadata(path)
    if meta is None:
        summary.unreadable += 1
        _sample_error(summary, f"unreadable header: {path}")
        return (None, None, None)
    return meta


def _reconcile_moves(
    conn: sqlite3.Connection,
    vanished: list[str],
    inserts: list[tuple],
    now: str,
) -> tuple[set[int], set[str], list[tuple]]:
    """Pair vanished rows with added files that are the same content, so a
    move or rename updates the row in place instead of delete + insert.

    Why (2026-09-06 review): deleting a row cascades everything hanging off it
    — confirmed classifications, manual segments, analysis, and from Phase 4
    every embedding. Reorganising a folder would silently throw all of that
    away and re-run hours of compute.

    Two tiers, in order of certainty:
      1. The old row has a stored hash and the new file hashes the same.
      2. No stored hash (the common case — spec §8 defers hashing until a
         change): same filename, size, duration, rate and channels, with
         exactly one candidate on each side. That is a folder move; a pure
         rename of a never-hashed file is not guessed at.
    Files hashed here keep their hash, so their next move is a tier-1 match.

    Returns (insert indexes consumed, vanished paths consumed, UPDATE rows).
    """
    if not vanished or not inserts:
        return set(), set(), []
    old_rows: list[sqlite3.Row] = []
    chunk = 500
    for start in range(0, len(vanished), chunk):
        batch = vanished[start : start + chunk]
        marks = ",".join("?" for _ in batch)
        old_rows.extend(
            conn.execute(
                "SELECT id, filepath, filename, file_size, file_hash, duration_s, "
                f"sample_rate, channels FROM samples WHERE filepath IN ({marks})",
                batch,
            )
        )
    by_size: dict[int, list[sqlite3.Row]] = {}
    for row in old_rows:
        by_size.setdefault(row["file_size"], []).append(row)
    if not by_size:
        return set(), set(), []
    insert_keys: dict[tuple, int] = {}  # (size, filename) -> how many added files
    for ins in inserts:
        key = (ins[9], ins[1])
        insert_keys[key] = insert_keys.get(key, 0) + 1

    consumed_inserts: set[int] = set()
    consumed_vanished: set[str] = set()
    moves: list[tuple] = []
    for index, ins in enumerate(inserts):
        path_str, name, rel_folder, dur, sr, ch, _a, _l, _c, size, mtime = ins
        candidates = [
            r for r in by_size.get(size, []) if r["filepath"] not in consumed_vanished
        ]
        if not candidates:
            continue
        match = None
        new_hash = None
        hashed = [r for r in candidates if r["file_hash"] is not None]
        if hashed:
            try:
                new_hash = _hash_file(Path(path_str))
            except OSError as exc:
                log.debug("move check could not hash %s (%s)", path_str, exc)
                continue
            match = next((r for r in hashed if r["file_hash"] == new_hash), None)
        if match is None:
            same = [
                r for r in candidates
                if r["file_hash"] is None and r["filename"] == name
                and r["duration_s"] == dur and r["sample_rate"] == sr
                and r["channels"] == ch
            ]
            if len(same) == 1 and insert_keys[(size, name)] == 1:
                match = same[0]
        if match is None:
            continue
        consumed_inserts.add(index)
        consumed_vanished.add(match["filepath"])
        moves.append(
            (path_str, name, rel_folder, now, mtime,
             new_hash or match["file_hash"], match["id"])
        )
    return consumed_inserts, consumed_vanished, moves


def _under_root_prefix(root: Path) -> str:
    """Prefix matching every path strictly under `root` — drive-root safe.

    `str(Path("D:\\"))` already ends in a separator, so naive
    `str(root) + os.sep` doubles it and matches nothing (2026-09-06 review);
    `os.path.join` normalizes both cases.
    """
    return os.path.join(str(root), "")


def _walk_files(
    root: Path, on_error: Callable[[str, OSError, bool], None]
) -> Iterator[tuple[os.DirEntry, str]]:
    """Yield `(entry, rel_folder)` for every file under `root`, depth-first.

    Built on `os.scandir` rather than `os.walk` (2026-09-06 review):
    `DirEntry.stat()` reuses the listing's metadata on Windows (no second
    syscall), `rel_folder` is computed once per directory, and a listing
    failure surfaces via `on_error(path, exc, subtree=True)` instead of
    being silently swallowed. An entry whose type cannot even be determined
    is reported with `subtree=True` as well — it might be a directory, and
    the cost of being wrong is one stale row versus deleted rows.
    """
    stack: list[tuple[Path, str]] = [(root, "")]
    visited: set[str] = set()  # real paths: junctions are followed (a library
    while stack:               # relocated via junction should index), but a
        dirpath, rel_folder = stack.pop()  # junction back to an ancestor must
        real = os.path.realpath(dirpath)   # not walk forever (2026-09-06 review)
        if real in visited:
            log.debug("directory already walked (junction cycle?): %s", dirpath)
            continue
        visited.add(real)
        try:
            entries = list(os.scandir(dirpath))
        except OSError as exc:
            on_error(str(dirpath), exc, True)
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    sub = f"{rel_folder}/{entry.name}" if rel_folder else entry.name
                    stack.append((Path(entry.path), sub))
                elif entry.is_file(follow_symlinks=False):
                    yield entry, rel_folder
            except OSError as exc:
                on_error(entry.path, exc, True)


def scan_library(conn: sqlite3.Connection, root: Path | str) -> ScanSummary:
    """Run node `A` over `root` against the DB on `conn`. Returns a summary.

    Inserts skeleton rows for new files, refreshes `last_scanned_at`/`folder`
    for unchanged ones, demotes hash-identical ones (content unchanged even
    though mtime moved), re-reads metadata and re-hashes genuinely changed
    ones (spec §8), and removes rows whose file vanished — never rows under
    a directory the walk could not read (those are walk errors).
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"library root does not exist: {root}")

    summary = ScanSummary(root=str(root))
    started = time.perf_counter()
    now = now_iso()
    root_prefix = _under_root_prefix(root)

    known: dict[str, sqlite3.Row] = {
        row["filepath"]: row
        for row in conn.execute(
            "SELECT filepath, file_size, file_mtime, file_hash FROM samples"
        )
    }

    seen: set[str] = set()
    failed_dirs: list[str] = []     # listing failed → subtree rows protected
    failed_files: set[str] = set()  # stat/hash failed → that row protected
    inserts: list[tuple] = []
    touches: list[tuple] = []    # unchanged: last_scanned_at + folder refresh
    retouches: list[tuple] = []  # mtime moved, hash equal: change-key refresh
    updates: list[tuple] = []    # genuinely changed: metadata + key + hash

    def _on_walk_error(path_str: str, exc: OSError, subtree: bool) -> None:
        summary.walk_errors += 1
        _sample_error(
            summary, f"walk error: {path_str} ({type(exc).__name__}: {exc})"
        )
        log.debug("walk error: %s (%s: %s)", path_str, type(exc).__name__, exc)
        failed_files.add(path_str)  # the path itself is always protected
        if subtree:
            failed_dirs.append(os.path.join(path_str, ""))

    for entry, rel_folder in _walk_files(root, _on_walk_error):
        name = entry.name
        ext = os.path.splitext(name)[1].lower()
        if ext not in SUPPORTED_EXTS:
            if ext == RX2_EXT:
                summary.skipped_rx2 += 1
            else:
                key = ext or "<no-ext>"
                summary.skipped_other[key] = summary.skipped_other.get(key, 0) + 1
            continue

        path_str = entry.path
        try:
            st = entry.stat()
        except OSError as exc:
            _on_walk_error(path_str, exc, subtree=False)
            continue

        seen.add(path_str)
        old = known.get(path_str)
        if old is None:
            meta = _metadata_or_null(Path(path_str), summary)
            inserts.append(
                (
                    path_str, name, rel_folder,
                    meta[0], meta[1], meta[2],
                    now, now, now, st.st_size, st.st_mtime,
                )
            )
            summary.added += 1
        elif old["file_size"] == st.st_size and old["file_mtime"] == st.st_mtime:
            touches.append((now, rel_folder, path_str))
            summary.unchanged += 1
        else:
            try:
                new_hash = _hash_file(Path(path_str))
            except OSError as exc:
                _on_walk_error(path_str, exc, subtree=False)
                continue  # already in `seen`: row kept as-is
            size_same = old["file_size"] == st.st_size
            if (
                size_same
                and old["file_hash"] is not None
                and old["file_hash"] == new_hash
            ):
                # Content identical despite mtime change (backup restore, NAS
                # re-sync, touch): refresh the change key but don't mark
                # changed — later phases must not re-analyze identical bytes.
                retouches.append(
                    (now, rel_folder, st.st_size, st.st_mtime, path_str)
                )
                summary.unchanged += 1
            else:
                meta = _metadata_or_null(Path(path_str), summary)
                updates.append(
                    (
                        name, rel_folder,
                        meta[0], meta[1], meta[2],
                        now, now, st.st_size, st.st_mtime, new_hash, path_str,
                    )
                )
                summary.changed += 1

    def _executemany(sql: str, rows: list[tuple]) -> None:
        """Chunked executemany: a crash mid-scan loses at most one batch,
        not the whole (potentially multi-hour, full-library) scan."""
        for start in range(0, len(rows), _COMMIT_BATCH):
            conn.executemany(sql, rows[start : start + _COMMIT_BATCH])
            conn.commit()

    # Removal candidates: only rows under this root whose file truly vanished.
    # One DB may hold several roots, so other roots' rows are never touched.
    # Rows under a failed listing, or whose stat/hash failed, are PROTECTED —
    # an unreadable entry is unknown state, not a deletion (2026-09-06 review).
    vanished = [
        filepath
        for filepath in known
        if filepath not in seen
        and filepath.startswith(root_prefix)
        and filepath not in failed_files
        and not any(filepath.startswith(d) for d in failed_dirs)
    ]
    # ... but a vanished row plus an added file with the same content is a
    # move, and the row must survive with its id (and everything FK'd to it).
    used_inserts, used_vanished, moves = _reconcile_moves(conn, vanished, inserts, now)
    if used_inserts:
        inserts = [ins for i, ins in enumerate(inserts) if i not in used_inserts]
        vanished = [fp for fp in vanished if fp not in used_vanished]
        summary.added -= len(used_inserts)
        summary.moved = len(used_inserts)

    _executemany(
        """
        INSERT INTO samples
            (filepath, filename, folder, duration_s, sample_rate, channels,
             added_at, last_scanned_at, content_changed_at, file_size, file_mtime)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        inserts,
    )
    _executemany(
        "UPDATE samples SET last_scanned_at = ?, folder = ? WHERE filepath = ?",
        touches,
    )
    _executemany(
        """
        UPDATE samples
        SET last_scanned_at = ?, folder = ?, file_size = ?, file_mtime = ?
        WHERE filepath = ?
        """,
        retouches,
    )
    _executemany(
        """
        UPDATE samples
        SET filename = ?, folder = ?,
            duration_s = ?, sample_rate = ?, channels = ?,
            last_scanned_at = ?, content_changed_at = ?,
            file_size = ?, file_mtime = ?, file_hash = ?
        WHERE filepath = ?
        """,
        updates,
    )
    _executemany(
        """
        UPDATE samples
        SET filepath = ?, filename = ?, folder = ?, last_scanned_at = ?,
            file_mtime = ?, file_hash = ?
        WHERE id = ?
        """,
        moves,
    )

    if vanished:
        _executemany(
            "DELETE FROM samples WHERE filepath = ?", [(fp,) for fp in vanished]
        )
        summary.removed = len(vanished)

    # Staleness is a flag, not a trigger: report it, leave the recompute to
    # an explicit crate-analyze run (2026-09-06 decision, spec §9.6).
    summary.stale_analysis = conn.execute(
        "SELECT COUNT(*) FROM analysis a JOIN samples s ON s.id = a.sample_id "
        "WHERE s.content_changed_at > a.analyzed_at"
    ).fetchone()[0]

    summary.elapsed_s = time.perf_counter() - started
    return summary