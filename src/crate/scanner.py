"""Library scanner — pipeline node `A` (spec §7), the Phase 1 deliverable.

Recursively walks a library root, keeps one skeleton `samples` row per
supported audio file, and diffs against the previous run using the cheap
change key (`file_size` + `file_mtime`, spec §8). The full content hash is
computed ONLY for files whose size/mtime changed — hashing all 340GB on
every re-scan would be a disk-bound non-starter (spec §8).

Formats (spec §3): `.wav` and `.flac` are supported (~77% of the library).
`.rx2` is skip-and-log (spec §3 decision, §13 risk #5); everything else is
skip-and-log by extension. Unreadable audio headers are logged, not fatal
(spec §7 node B guarantee) — such files keep a row with NULL metadata.

One database represents one library root: removal on re-scan only touches
rows under the scanned root, so scanning a different root into the same DB
never deletes the first root's rows.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import soundfile as sf

log = logging.getLogger(__name__)

SUPPORTED_EXTS = {".wav", ".flac"}
RX2_EXT = ".rx2"
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass
class ScanSummary:
    """Counts from one scanner run — the node-A "worklist" for later phases."""

    root: str = ""
    added: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    unreadable: int = 0  # supported ext, header unreadable (row kept, NULL metadata)
    skipped_rx2: int = 0
    skipped_other: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0

    def format(self) -> str:
        lines = [
            f"root: {self.root}",
            f"added {self.added} | changed {self.changed} | "
            f"unchanged {self.unchanged} | removed {self.removed}",
        ]
        if self.unreadable:
            lines.append(
                f"unreadable headers: {self.unreadable} (rows kept with NULL metadata)"
            )
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_file(path: Path) -> str:
    h = hashlib.blake2b()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_BYTES), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_metadata(path: Path) -> tuple[float, int, int] | None:
    """Header-only read via sf.info: (duration_s, sample_rate, channels).

    Returns None (and logs) if the header is unreadable — decode failures
    are logged, not fatal (spec §7 node B).
    """
    try:
        info = sf.info(str(path))
        return info.duration, info.samplerate, info.channels
    except Exception:
        log.warning("unreadable audio header: %s", path)
        return None


def scan_library(conn: sqlite3.Connection, root: Path | str) -> ScanSummary:
    """Run node `A` over `root` against the DB on `conn`. Returns a summary.

    Inserts skeleton rows for new files, updates `last_scanned_at` for
    unchanged ones, re-reads metadata + hashes changed ones (spec §8: hash
    only when size/mtime changed), and removes rows whose file vanished.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"library root does not exist: {root}")

    summary = ScanSummary(root=str(root))
    started = time.perf_counter()
    now = _now_iso()
    root_prefix = str(root) + os.sep

    known: dict[str, sqlite3.Row] = {
        row["filepath"]: row for row in conn.execute("SELECT * FROM samples")
    }

    seen: set[str] = set()
    inserts: list[tuple] = []
    touches: list[tuple] = []  # unchanged rows: last_scanned_at only
    updates: list[tuple] = []  # changed rows: metadata + change key + hash

    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            ext = path.suffix.lower()
            if ext not in SUPPORTED_EXTS:
                if ext == RX2_EXT:
                    summary.skipped_rx2 += 1
                else:
                    key = ext or "<no-ext>"
                    summary.skipped_other[key] = summary.skipped_other.get(key, 0) + 1
                continue

            try:
                st = path.stat()
            except OSError:
                log.warning("cannot stat, skipping: %s", path)
                continue

            filepath = str(path)
            seen.add(filepath)
            rel_folder = path.parent.relative_to(root).as_posix()
            if rel_folder == ".":
                rel_folder = ""

            old = known.get(filepath)
            if old is None:
                meta = _read_metadata(path)
                if meta is None:
                    summary.unreadable += 1
                    meta = (None, None, None)
                inserts.append(
                    (
                        filepath, name, rel_folder,
                        meta[0], meta[1], meta[2],
                        now, now, st.st_size, st.st_mtime,
                    )
                )
                summary.added += 1
            elif old["file_size"] == st.st_size and old["file_mtime"] == st.st_mtime:
                touches.append((now, filepath))
                summary.unchanged += 1
            else:
                meta = _read_metadata(path)
                if meta is None:
                    summary.unreadable += 1
                    meta = (None, None, None)
                updates.append(
                    (
                        name, rel_folder,
                        meta[0], meta[1], meta[2],
                        now, st.st_size, st.st_mtime, _hash_file(path),
                        filepath,
                    )
                )
                summary.changed += 1

    conn.executemany(
        """
        INSERT INTO samples
            (filepath, filename, folder, duration_s, sample_rate, channels,
             added_at, last_scanned_at, file_size, file_mtime)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        inserts,
    )
    conn.executemany(
        "UPDATE samples SET last_scanned_at = ? WHERE filepath = ?", touches
    )
    conn.executemany(
        """
        UPDATE samples
        SET filename = ?, folder = ?,
            duration_s = ?, sample_rate = ?, channels = ?,
            last_scanned_at = ?, file_size = ?, file_mtime = ?, file_hash = ?
        WHERE filepath = ?
        """,
        updates,
    )

    # Removal: only rows under this root whose file vanished (one DB may
    # hold several roots; a scan never deletes another root's rows).
    vanished = [
        filepath
        for filepath in known
        if filepath not in seen and filepath.startswith(root_prefix)
    ]
    if vanished:
        conn.executemany(
            "DELETE FROM samples WHERE filepath = ?", [(fp,) for fp in vanished]
        )
        summary.removed = len(vanished)

    conn.commit()
    summary.elapsed_s = time.perf_counter() - started
    return summary