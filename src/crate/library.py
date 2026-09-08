"""The folders the index knows — spec §9.6's library scope, kept in the
index itself since 2026-09-08 (the user's steer: one Library panel, the
folders managing the database entries).

A *library* is a folder the scanner has walked (or is about to). Each has
two flags: **in scope** — shown in the list and on the map, walked by
Rescan, covered by Recompute; off means dormant: its rows stay in the index
untouched — and **root**, at most one: the library's home folder, where the
Add-folder dialog starts and what "outside the library" is measured
against. Adding a folder scans it in; removing one deletes its samples and
everything hanging off them (analysis, segments, embeddings, map positions
— the cascades of `db.SCHEMA`), which is why the panel asks first.

No Qt here; the panel and the tests call exactly these.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .db import now_iso, scope_clause


@dataclass(frozen=True)
class Library:
    id: int
    path: str
    is_root: bool
    in_scope: bool
    added_at: str
    last_scanned_at: str | None
    sample_count: int


@dataclass
class RemovalSummary:
    path: str = ""
    samples_removed: int = 0
    elapsed_s: float = 0.0

    def format(self) -> str:
        return (
            f"removed {self.path} from the index: {self.samples_removed} samples with their "
            f"analysis, segments, embeddings and map positions (files on disk untouched)"
        )


def normalize(path: Path | str) -> str:
    """The one spelling of a folder: resolved, native separators, no
    trailing separator — the scanner's `Path(root).resolve()`."""
    return str(Path(path).resolve())


def list_libraries(conn: sqlite3.Connection) -> list[Library]:
    """Every known folder, by path, with how many samples the index holds under it."""
    out: list[Library] = []
    for row in conn.execute(
        "SELECT id, path, is_root, in_scope, added_at, last_scanned_at FROM libraries ORDER BY path"
    ):
        out.append(
            Library(
                int(row[0]), str(row[1]), bool(row[2]), bool(row[3]), str(row[4]),
                None if row[5] is None else str(row[5]), count_under(conn, str(row[1])),
            )
        )
    return out


def count_under(conn: sqlite3.Connection, path: str) -> int:
    clause, params = scope_clause([path], "filepath")
    return int(conn.execute(f"SELECT COUNT(*) FROM samples WHERE 1=1{clause}", params).fetchone()[0])


def add_library(conn: sqlite3.Connection, path: Path | str, in_scope: bool = True) -> str:
    """Register a folder (a no-op for one already known); returns its normalised path."""
    text = normalize(path)
    conn.execute(
        "INSERT OR IGNORE INTO libraries (path, in_scope, added_at) VALUES (?, ?, ?)",
        (text, int(in_scope), now_iso()),
    )
    conn.commit()
    return text


def register_scan(conn: sqlite3.Connection, root: Path | str) -> None:
    """The scanner's note that it walked `root`: a known folder keeps its
    flags and gets a scan stamp; a new one (a CLI `crate-scan --root`, say)
    is added in scope so it shows up."""
    text = normalize(root)
    now = now_iso()
    conn.execute(
        "INSERT INTO libraries (path, in_scope, added_at, last_scanned_at) VALUES (?, 1, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET last_scanned_at = excluded.last_scanned_at",
        (text, now, now),
    )
    conn.commit()


def set_root(conn: sqlite3.Connection, path: Path | str | None) -> None:
    """Mark `path` as the library's root — at most one — or none."""
    conn.execute("UPDATE libraries SET is_root = 0")
    if path is not None:
        conn.execute("UPDATE libraries SET is_root = 1 WHERE path = ?", (normalize(path),))
    conn.commit()


def set_in_scope(conn: sqlite3.Connection, path: Path | str, in_scope: bool) -> None:
    conn.execute("UPDATE libraries SET in_scope = ? WHERE path = ?", (int(in_scope), normalize(path)))
    conn.commit()


def remove_library(conn: sqlite3.Connection, path: Path | str) -> RemovalSummary:
    """Delete a folder's samples from the index (the cascades take every
    dependent row) and forget the folder. Files on disk are never touched."""
    import time

    started = time.perf_counter()
    text = normalize(path)
    clause, params = scope_clause([text], "filepath")
    removed = conn.execute(f"DELETE FROM samples WHERE 1=1{clause}", params).rowcount
    conn.execute("DELETE FROM libraries WHERE path = ?", (text,))
    conn.commit()
    return RemovalSummary(text, int(removed), time.perf_counter() - started)


def scope_paths(conn: sqlite3.Connection) -> tuple[str, ...] | None:
    """The folders in scope — what the list, the map, Rescan and Recompute
    cover. None when the index knows no folders at all (nothing to scope
    by: everything shows); an empty tuple when it knows some and none is in
    scope (nothing shows)."""
    if conn.execute("SELECT COUNT(*) FROM libraries").fetchone()[0] == 0:
        return None
    return tuple(
        str(row[0]) for row in conn.execute("SELECT path FROM libraries WHERE in_scope = 1 ORDER BY path")
    )


def root_path(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT path FROM libraries WHERE is_root = 1 LIMIT 1").fetchone()
    return None if row is None else str(row[0])


def outermost(paths: Iterable[str]) -> list[str]:
    """Drop every folder that lies inside another of the given folders, so a
    walk over the result visits each file once (Rescan)."""
    folders = sorted({normalize(p) for p in paths})
    kept: list[str] = []
    for folder in folders:
        prefix_of_kept = any(folder.startswith(os.path.join(k, "")) for k in kept)
        if not prefix_of_kept:
            kept.append(folder)
    return kept


def is_inside(path: str, root: str | None) -> bool:
    if root is None:
        return True
    return normalize(path) == normalize(root) or normalize(path).startswith(os.path.join(normalize(root), ""))


def derive_scan_roots(conn: sqlite3.Connection) -> list[str]:
    """The roots an index was scanned under, recovered from its rows: every
    sample's path is `root / folder / filename` (the scanner's `folder` is
    relative to the root it walked), so the root is what is left after the
    tail is removed. For seeding the folder list from an index built before
    the `libraries` table existed."""
    roots: set[str] = set()
    for filepath, folder, filename in conn.execute("SELECT filepath, folder, filename FROM samples"):
        tail = (str(folder).replace("/", os.sep) + os.sep if folder else "") + str(filename)
        text = str(filepath)
        if text.endswith(tail):
            root = text[: -len(tail)].rstrip("\\/")
            if root:
                roots.add(root)
    return sorted(roots)


def seed_libraries(
    conn: sqlite3.Connection,
    root_hint: str | None = None,
    scope_hint: Iterable[str] = (),
) -> int:
    """First launch of a build with the `libraries` table: fill it from what
    the index already holds (its scan roots) and from what the old settings
    said (the root, the scope list). A folder in the old scope list is in
    scope; when there was no scope list, everything the index holds is. The
    old root is the root. Returns how many folders were added; 0 when the
    table was not empty (seeding happens once)."""
    if conn.execute("SELECT COUNT(*) FROM libraries").fetchone()[0]:
        return 0
    scope = {normalize(p) for p in scope_hint if p}
    root = normalize(root_hint) if root_hint else None
    known = set(derive_scan_roots(conn)) | scope
    if root is not None:
        known.add(root)
    now = now_iso()
    for path in sorted(known):
        in_scope = (path in scope) if scope else True
        conn.execute(
            "INSERT OR IGNORE INTO libraries (path, is_root, in_scope, added_at) VALUES (?, ?, ?, ?)",
            (path, int(path == root), int(in_scope), now),
        )
    conn.commit()
    return len(known)
