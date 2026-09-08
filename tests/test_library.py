"""Tests for the folder list behind the Library panel (`library.py`,
2026-09-08): the `libraries` table, its two flags, adding and removing
folders, the scanner registering what it walked, and the seed from an
index that predates the table."""

from __future__ import annotations

import numpy as np
import soundfile as sf

from crate.db import open_db
from crate.library import (
    add_library,
    count_under,
    derive_scan_roots,
    is_inside,
    list_libraries,
    normalize,
    outermost,
    register_scan,
    remove_library,
    root_path,
    scope_paths,
    seed_libraries,
    set_in_scope,
    set_root,
)
from crate.scanner import scan_library


def _write(path, seconds=0.05, sr=8000):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(int(seconds * sr), dtype=np.float32), sr)


def test_flags_scope_and_root(tmp_path):
    conn = open_db(tmp_path / "index.db")
    a, b = tmp_path / "packA", tmp_path / "packB"
    a.mkdir(), b.mkdir()
    assert scope_paths(conn) is None                        # no folders known: nothing to scope by

    add_library(conn, a)
    add_library(conn, b, in_scope=False)
    add_library(conn, a)                                    # already known: a no-op
    libs = list_libraries(conn)
    assert [(lib.path, lib.in_scope, lib.is_root) for lib in libs] == [
        (normalize(a), True, False), (normalize(b), False, False),
    ]
    assert scope_paths(conn) == (normalize(a),)

    set_root(conn, b)
    assert root_path(conn) == normalize(b)
    set_root(conn, a)                                       # at most one root
    assert [lib.is_root for lib in list_libraries(conn)] == [True, False]
    set_root(conn, None)
    assert root_path(conn) is None

    set_in_scope(conn, a, False)
    assert scope_paths(conn) == ()                          # folders known, none in scope: nothing
    set_in_scope(conn, b, True)
    assert scope_paths(conn) == (normalize(b),)
    conn.close()


def test_scanning_registers_the_root_and_removing_deletes_its_rows(tmp_path):
    conn = open_db(tmp_path / "index.db")
    a, b = tmp_path / "packA", tmp_path / "packB"
    _write(a / "one.wav")
    _write(a / "deep" / "two.wav")
    _write(b / "three.wav")
    scan_library(conn, a)
    scan_library(conn, b)
    libs = {lib.path: lib for lib in list_libraries(conn)}
    assert set(libs) == {normalize(a), normalize(b)}
    assert all(lib.in_scope and lib.last_scanned_at for lib in libs.values())
    assert libs[normalize(a)].sample_count == 2 and libs[normalize(b)].sample_count == 1

    set_in_scope(conn, a, False)
    scan_library(conn, a)                                   # a re-scan keeps the flags
    assert not [lib for lib in list_libraries(conn) if lib.path == normalize(a)][0].in_scope

    stopped = scan_library(conn, tmp_path / "packB", should_stop=lambda: True)
    assert stopped.stopped                                  # a stopped walk registers nothing new
    conn.execute("INSERT INTO analysis (sample_id, analyzed_at) SELECT id, 't' FROM samples")
    conn.commit()
    summary = remove_library(conn, a)
    assert summary.samples_removed == 2 and "2 samples" in summary.format()
    assert count_under(conn, str(a)) == 0 and count_under(conn, str(b)) == 1
    assert conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 1      # the cascade
    assert [lib.path for lib in list_libraries(conn)] == [normalize(b)]
    assert (tmp_path / "packA" / "one.wav").exists()                            # disk untouched
    conn.close()


def test_outermost_and_inside(tmp_path):
    root, sub, other = tmp_path / "lib", tmp_path / "lib" / "Drums", tmp_path / "elsewhere"
    assert outermost([str(sub), str(root), str(other)]) == sorted([normalize(root), normalize(other)])
    assert outermost([]) == []
    assert is_inside(str(sub), str(root)) and is_inside(str(root), str(root))
    assert not is_inside(str(other), str(root)) and is_inside(str(other), None)
    assert not is_inside(str(tmp_path / "library"), str(root))                  # a prefix, not a parent


def test_seed_from_an_index_that_predates_the_table(tmp_path):
    conn = open_db(tmp_path / "index.db")
    a, b = tmp_path / "packA", tmp_path / "packB"
    _write(a / "one.wav")
    _write(a / "deep" / "two.wav")
    _write(b / "three.wav")
    scan_library(conn, a)
    scan_library(conn, b)
    conn.execute("DELETE FROM libraries")                   # as an index built before v9 looks
    conn.commit()
    assert derive_scan_roots(conn) == sorted([normalize(a), normalize(b)])

    # The old settings said: root = packA, scope = [packA]; packB is dormant.
    assert seed_libraries(conn, root_hint=str(a), scope_hint=[str(a)]) == 2
    libs = {lib.path: lib for lib in list_libraries(conn)}
    assert libs[normalize(a)].is_root and libs[normalize(a)].in_scope
    assert not libs[normalize(b)].is_root and not libs[normalize(b)].in_scope
    assert seed_libraries(conn, root_hint=str(b), scope_hint=[]) == 0            # once only

    conn.execute("DELETE FROM libraries")
    conn.commit()
    assert seed_libraries(conn) == 2                        # no settings at all: everything in scope
    assert all(lib.in_scope and not lib.is_root for lib in list_libraries(conn))

    conn.execute("DELETE FROM libraries")
    conn.commit()
    c = tmp_path / "packC"
    c.mkdir()
    seed_libraries(conn, root_hint=str(c), scope_hint=[str(b)])   # a root never scanned is listed too
    libs = {lib.path: lib for lib in list_libraries(conn)}
    assert libs[normalize(c)].is_root and not libs[normalize(c)].in_scope and libs[normalize(b)].in_scope
    register_scan(conn, c)
    assert libs[normalize(c)].sample_count == 0 and root_path(conn) == normalize(c)
    conn.close()
