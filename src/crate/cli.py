r"""Command-line runners: `crate-scan` (Phase 1) and `crate-analyze` (Phase 2).

Examples:
    crate-scan                                # full library, default DB
    crate-scan --root "D:\_soundPacks\Some Pack" --db subset.db
    crate-analyze --db subset.db --limit 60   # time a subset first (spec §3)
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .analysis import ONE_SHOT_MAX_DURATION_S, analyze_pending
from .config import DEFAULT_LIBRARY_PATH
from .db import default_db_path, open_db
from .scanner import scan_library

_LOG_FORMAT = "%(levelname)s %(message)s"


def scan_main(argv: list[str] | None = None) -> int:
    default_db = default_db_path()
    parser = argparse.ArgumentParser(
        prog="crate-scan",
        description="Scan a sample library into the Crate SQLite index "
        "(spec §7 node A; incremental via size+mtime diff).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(DEFAULT_LIBRARY_PATH),
        help=f"library root to scan (default: {DEFAULT_LIBRARY_PATH})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=default_db,
        help=f"index database path (default: {default_db})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="stream per-file detail (unreadable headers, walk errors) "
        "as it happens; the summary always shows counts regardless",
    )
    args = parser.parse_args(argv)

    # Default: quiet run, issues summarized at the end. -v: stream each
    # unreadable header / walk error as DEBUG (2026-09-06 review — the old
    # flag was a no-op with misleading help text).
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING, format=_LOG_FORMAT
    )

    conn = open_db(args.db)
    try:
        summary = scan_library(conn, args.root)
    finally:
        conn.close()

    print(summary.format())
    return 0


def analyze_main(argv: list[str] | None = None) -> int:
    """`crate-analyze` — run nodes B/C over indexed samples (spec §7, Phase 2)."""
    default_db = default_db_path()
    parser = argparse.ArgumentParser(
        prog="crate-analyze",
        description="Derive the §5.1 descriptor set and Facet B structural type "
        "for samples that are new or whose content changed since they were last "
        "analyzed (spec §7 nodes B/C).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=default_db,
        help=f"index database path (default: {default_db})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="analyze at most N samples — use this to time a subset before "
        "committing to the full library (spec §3)",
    )
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="re-analyze every sample, not just new/stale ones "
        "(manually-confirmed classifications are still protected, spec §11)",
    )
    parser.add_argument(
        "--one-shot-max-duration",
        type=float,
        default=ONE_SHOT_MAX_DURATION_S,
        metavar="SECONDS",
        help="a sample with at most one dominant onset is a one-shot only up to "
        "this length (spec §4 'short'; default %(default)s s)",
    )
    parser.add_argument(
        "--one-shot-any-duration",
        action="store_true",
        help="turn the length cap off: any sample with at most one dominant "
        "onset is a one-shot, however long (ringing hits, cinematic impacts)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="stream per-file detail (decode failures) as it happens",
    )
    args = parser.parse_args(argv)
    one_shot_cap = None if args.one_shot_any_duration else args.one_shot_max_duration

    # Progress is INFO and visible by default — this is the multi-hour stage
    # (§3), so a silent run is not acceptable. -v adds per-file DEBUG detail.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format=_LOG_FORMAT
    )

    conn = open_db(args.db)
    try:
        summary = analyze_pending(
            conn,
            limit=args.limit,
            reanalyze=args.reanalyze,
            one_shot_max_duration_s=one_shot_cap,
        )
    finally:
        conn.close()

    print(summary.format())
    return 0


if __name__ == "__main__":
    raise SystemExit(scan_main())
