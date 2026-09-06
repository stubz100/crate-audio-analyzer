"""`crate-scan` — command-line runner for the Phase 1 library scanner.

Examples:
    crate-scan                                # full library, default DB
    crate-scan --root "D:\\_soundPacks\\Some Pack" --db subset.db
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import DEFAULT_LIBRARY_PATH
from .db import default_db_path, open_db
from .scanner import scan_library


def main(argv: list[str] | None = None) -> int:
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
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    conn = open_db(args.db)
    try:
        summary = scan_library(conn, args.root)
    finally:
        conn.close()

    print(summary.format())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())