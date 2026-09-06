"""`crate-scan` — command-line runner for the Phase 1 library scanner.

Examples:
    crate-scan                                # full library, default DB
    crate-scan --root "D:\\_soundPacks\\Some Pack" --db .crate_cache\\subset.db
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .db import DEFAULT_DB_RELPATH, open_db
from .scanner import scan_library

DEFAULT_ROOT = Path(r"D:\_soundPacks")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crate-scan",
        description="Scan a sample library into the Crate SQLite index "
        "(spec §7 node A; incremental via size+mtime diff).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"library root to scan (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.cwd() / DEFAULT_DB_RELPATH,
        help="index database path (default: <cwd>/.crate_cache/crate.db)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show per-file warnings verbosely"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
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