r"""Command-line runners: `crate-scan` (Phase 1), `crate-analyze` (Phase 2),
`crate-segment` (Phase 3), `crate-embed` (Phase 4).

Examples:
    crate-scan                                # full library, default DB
    crate-scan --root "D:\_soundPacks\Some Pack" --db subset.db
    crate-analyze --db subset.db --limit 60   # time a subset first (spec §3)
    crate-segment --db subset.db --max-segments 8
    crate-embed --db subset.db --limit 20      # time CLAP on a subset first (spec §3)

Every command takes `--db` and `-v`, logs progress at INFO — these are the
multi-hour stages of spec §3, so a silent run is not acceptable — and per-file
detail at DEBUG under `-v`, and prints one summary at the end. The shared
parts live in `_parser` / `_run` so the three cannot drift (2026-09-06
review: they had — three log levels and three `-v` help texts).
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .analysis import ONE_SHOT_MAX_DURATION_S, analyze_pending
from .config import DEFAULT_LIBRARY_PATH
from .db import default_db_path, open_db
from .embedding import DEFAULT_CHECKPOINT, EmbedSettings, embed_pending, export_vectors, reclassify
from .qwen_audio import caption_pending
from .scanner import scan_library
from .segmentation import (
    BOUNDARY_MODES,
    DEFAULT_MAX_LENGTH_S,
    DEFAULT_MAX_SEGMENTS,
    DEFAULT_MIN_LENGTH_S,
    DEFAULT_SENSITIVITY,
    PROFILES,
    UNITS,
    SegmentationSettings,
    segment_pending,
)

_LOG_FORMAT = "%(levelname)s %(message)s"


def _parser(prog: str, description: str) -> argparse.ArgumentParser:
    """The arguments every command shares."""
    default_db = default_db_path()
    parser = argparse.ArgumentParser(prog=prog, description=description)
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
        help="stream per-file detail (unreadable headers, walk errors, decode "
        "failures) as it happens; the summary always shows counts regardless",
    )
    return parser


def _run(args: argparse.Namespace, job: Callable[[Any], Any]) -> int:
    """Configure logging, open the index, run `job(conn)`, print its summary."""
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format=_LOG_FORMAT
    )
    # The model stack logs every HTTP request at INFO; that is noise on a
    # multi-hour run, not progress. Our own loggers keep the chosen level.
    for noisy in ("httpx", "huggingface_hub", "urllib3", "filelock", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    conn = open_db(args.db)
    try:
        summary = job(conn)
    finally:
        conn.close()
    print(summary.format())
    return 0


def scan_main(argv: list[str] | None = None) -> int:
    """`crate-scan` — index a library root (spec §7 node A, Phase 1)."""
    parser = _parser(
        "crate-scan",
        "Scan a sample library into the Crate SQLite index "
        "(spec §7 node A; incremental via size+mtime diff, moves kept in place).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(DEFAULT_LIBRARY_PATH),
        help=f"library root to scan (default: {DEFAULT_LIBRARY_PATH})",
    )
    args = parser.parse_args(argv)
    return _run(args, lambda conn: scan_library(conn, args.root))


def analyze_main(argv: list[str] | None = None) -> int:
    """`crate-analyze` — run nodes B/C over indexed samples (spec §7, Phase 2)."""
    parser = _parser(
        "crate-analyze",
        "Derive the §5.1 descriptor set and Facet B structural type for samples "
        "that are new or whose content changed since they were last analyzed "
        "(spec §7 nodes B/C).",
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
        "--workers", type=int, default=1, metavar="N",
        help="worker processes for the per-file work (default: 1)",
    )
    args = parser.parse_args(argv)
    one_shot_cap = None if args.one_shot_any_duration else args.one_shot_max_duration
    return _run(
        args,
        lambda conn: analyze_pending(
            conn,
            limit=args.limit,
            reanalyze=args.reanalyze,
            one_shot_max_duration_s=one_shot_cap,
            workers=args.workers,
        ),
    )


def segment_main(argv: list[str] | None = None) -> int:
    """`crate-segment` — transient segmentation, nodes S/T (spec §6, Phase 3)."""
    parser = _parser(
        "crate-segment",
        "Find one-shot hits buried inside longer samples and index them as "
        "segments (spec §6). Samples Facet B typed as one-shots are skipped; "
        "manual segments are never touched.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="segment at most N samples — time a subset first (spec §3)",
    )
    parser.add_argument(
        "--resegment", action="store_true",
        help="re-detect every candidate sample, not just new/stale ones "
        "(manual segments are still kept, spec §6.3)",
    )
    parser.add_argument(
        "--profile", choices=PROFILES, default="auto",
        help="onset-detection profile: tight (percussive/drums), loose "
        "(gestural takes), or auto by structural type (default: %(default)s)",
    )
    parser.add_argument(
        "--sensitivity", type=float, default=DEFAULT_SENSITIVITY, metavar="FRACTION",
        help="how strong a transient must be to count, as a fraction of the "
        "strongest in the file (default: %(default)s)",
    )
    parser.add_argument(
        "--boundary-mode", choices=BOUNDARY_MODES, default=BOUNDARY_MODES[0],
        help="how a segment ends (default: %(default)s)",
    )
    parser.add_argument(
        "--min-length", type=float, default=DEFAULT_MIN_LENGTH_S, metavar="LEN",
        help="shorter segments are dropped as noise (default: %(default)s)",
    )
    parser.add_argument(
        "--min-length-unit", choices=UNITS, default="s",
        help="unit for --min-length: seconds or %% of parent duration",
    )
    parser.add_argument(
        "--max-length", type=float, default=DEFAULT_MAX_LENGTH_S, metavar="LEN",
        help="longer segments are truncated, not dropped (default: %(default)s)",
    )
    parser.add_argument(
        "--max-length-unit", choices=UNITS, default="s",
        help="unit for --max-length: seconds or %% of parent duration",
    )
    parser.add_argument(
        "--max-segments", type=int, default=DEFAULT_MAX_SEGMENTS, metavar="N",
        help="cap per sample, strongest transients win (default: %(default)s)",
    )
    parser.add_argument(
        "--workers", type=int, default=1, metavar="N",
        help="worker processes for the decode + detection (default: 1)",
    )
    parser.add_argument(
        "--no-segment-analysis", action="store_true",
        help="index segment boundaries only, skipping the per-segment "
        "descriptor pass",
    )
    args = parser.parse_args(argv)
    try:
        settings = SegmentationSettings(
            profile=args.profile,
            sensitivity=args.sensitivity,
            boundary_mode=args.boundary_mode,
            min_length=args.min_length,
            min_length_unit=args.min_length_unit,
            max_length=args.max_length,
            max_length_unit=args.max_length_unit,
            max_segments=args.max_segments,
        )
    except ValueError as exc:
        parser.error(str(exc))
    return _run(
        args,
        lambda conn: segment_pending(
            conn,
            settings=settings,
            limit=args.limit,
            resegment=args.resegment,
            analyze_segments=not args.no_segment_analysis,
            workers=args.workers,
        ),
    )


def embed_main(argv: list[str] | None = None) -> int:
    """`crate-embed` — CLAP embeddings, zero-shot tags, Facet A (spec §7 nodes
    D/C2/X/E, Phase 4)."""
    parser = _parser(
        "crate-embed",
        "Embed samples (and their segments) with CLAP, write zero-shot tag "
        "chips, and assign the Facet A content class (spec §7 nodes D, C2, X, E). "
        "First use downloads the checkpoint (~600 MB). Time a subset first: this "
        "is the library's dominant cost (spec §3).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="embed at most N samples — time a subset before the full library",
    )
    parser.add_argument(
        "--reembed", action="store_true",
        help="re-embed every analysed sample and segment, not just new/stale ones",
    )
    parser.add_argument(
        "--reclassify", action="store_true",
        help="re-run tags + Facet A from the stored vectors only (no audio, no "
        "model audio pass) — cheap prompt/threshold tuning",
    )
    parser.add_argument(
        "--export", type=Path, default=None, metavar="FILE.npz",
        help="write every stored vector (samples + segments, with ids and paths) to a "
        "NumPy .npz for use outside Crate, and do nothing else",
    )
    parser.add_argument(
        "--no-segments", action="store_true",
        help="skip per-segment embeddings (the §3 cost multiplier); segment "
        "detection is unaffected",
    )
    parser.add_argument(
        "--min-segment-length", type=int, default=200, metavar="MS",
        help="segments shorter than this are indexed but not embedded (default: %(default)s ms)",
    )
    parser.add_argument(
        "--confidence-threshold", type=float, default=0.5, metavar="P",
        help="Facet A below this is flagged, not assigned (default: %(default)s)",
    )
    parser.add_argument(
        "--checkpoint", default=DEFAULT_CHECKPOINT,
        help="CLAP checkpoint on the Hugging Face hub (default: %(default)s)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=8, metavar="N",
        help="clips per model forward pass (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        settings = EmbedSettings(
            embed_segments=not args.no_segments,
            min_segment_length_ms=args.min_segment_length,
            confidence_threshold=args.confidence_threshold,
            checkpoint=args.checkpoint,
            batch_size=args.batch_size,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.export is not None:
        return _run(args, lambda conn: export_vectors(conn, args.export))
    if args.reclassify:
        return _run(args, lambda conn: reclassify(conn, settings=settings))
    return _run(
        args,
        lambda conn: embed_pending(
            conn, settings=settings, limit=args.limit, reembed=args.reembed
        ),
    )


if __name__ == "__main__":
    raise SystemExit(scan_main())


def caption_main(argv: list[str] | None = None) -> int:
    """`crate-caption` — one Qwen2-Audio sentence per sample (spec §5.2 node
    X1, Phase 5). Opt-in and slow by nature: about 10 s per file."""
    parser = _parser(
        "crate-caption",
        "Write one Qwen2-Audio sentence per sample that has none, or whose content "
        "changed since (spec §5.2, node X1). Measured at ~10 s per file on a 16-core "
        "CPU: for a folder, not the library. First use needs the 16 GB checkpoint.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="caption at most N samples — time a handful before a folder",
    )
    parser.add_argument(
        "--recaption", action="store_true",
        help="rewrite every sample's caption, not just the missing or stale ones",
    )
    args = parser.parse_args(argv)
    return _run(
        args, lambda conn: caption_pending(conn, limit=args.limit, recaption=args.recaption)
    )
