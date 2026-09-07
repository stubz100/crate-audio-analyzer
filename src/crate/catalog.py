"""Read-side queries and filter rules for the list view — plain rows, no
Qt (Phase 4.5, extended in Phase 7).

Everything the list shows comes from here, so the GUI model is a thin
adapter and this layer is testable on its own. Segments are never rows in
the sample list (§6.4); they are fetched per sample for the drill-down, and
surface in the list only as a parent's "hit within" row (§9.4), which the
similarity module decides.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SampleRow:
    id: int
    filepath: str
    filename: str
    folder: str
    duration_s: float | None
    structural_type: str | None
    content_class: str | None
    confidence: float | None
    tempo_bpm: float | None
    key: str | None
    tags: str                 # top zero-shot chips, best first, comma-joined
    segment_count: int
    flagged_segments: int     # manual segments needing review (§6.3)
    clap_scores: dict[str, float] = field(default_factory=dict)  # CLAP class probabilities, by name


@dataclass(frozen=True)
class SegmentRow:
    id: int
    sample_id: int
    start_ms: int
    end_ms: int
    strength: float | None
    detection_method: str
    needs_review: int
    cache_path: str | None

    @property
    def length_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class Criteria:
    """The Attributes tab's filter criteria (§9.5), applied to sample rows.

    `types`: the values to keep, `""` standing for untyped; None keeps
    everything. `clap_min`: (prompt set, minimum probability) pairs — a
    sample without CLAP numbers fails any of them. Absolute ranges are
    open-ended with None. `axis_ranges` are per-axis distance-from-anchor windows in 0..1,
    listed only for axes the user narrowed: a sample whose distance on such
    an axis is unknown (no anchor, or the axis is missing for it) is
    excluded — narrowing an axis is asking for samples that *have* it.
    """

    types: frozenset[str] | None = None
    clap_min: tuple[tuple[str, float], ...] = ()
    duration_s: tuple[float | None, float | None] = (None, None)
    tempo_bpm: tuple[float | None, float | None] = (None, None)
    axis_ranges: tuple[tuple[str, float, float], ...] = ()

    def accepts(self, row: SampleRow, axis_distance: Mapping[str, float] | None) -> bool:
        if self.types is not None and (row.structural_type or "") not in self.types:
            return False
        for name, minimum in self.clap_min:
            value = row.clap_scores.get(name)
            if value is None or value < minimum:
                return False
        low, high = self.duration_s
        if low is not None and (row.duration_s is None or row.duration_s < low):
            return False
        if high is not None and (row.duration_s is None or row.duration_s > high):
            return False
        low, high = self.tempo_bpm
        if low is not None or high is not None:
            if row.tempo_bpm is None:
                return False
            if low is not None and row.tempo_bpm < low:
                return False
            if high is not None and row.tempo_bpm > high:
                return False
        for axis, low, high in self.axis_ranges:
            distance = None if axis_distance is None else axis_distance.get(axis)
            if distance is None or math.isnan(distance):
                return False
            if distance < low or distance > high:
                return False
        return True


_SAMPLES_SQL = """
SELECT s.id, s.filepath, s.filename, s.folder, s.duration_s,
       k.structural_type, k.content_class, k.confidence,
       a.tempo_bpm, a.key,
       COALESCE((SELECT group_concat(tag_or_caption, ', ') FROM (
                    SELECT tag_or_caption FROM text_tags t
                    WHERE t.sample_id = s.id AND t.source_model = 'clap-zeroshot'
                    ORDER BY t.score DESC LIMIT ?)), '') AS tags,
       (SELECT COUNT(*) FROM segments g WHERE g.sample_id = s.id) AS segment_count,
       (SELECT COUNT(*) FROM segments g WHERE g.sample_id = s.id AND g.needs_review = 1) AS flagged,
       COALESCE((SELECT group_concat(tag_or_caption || '=' || score, ';') FROM text_tags t
                 WHERE t.sample_id = s.id AND t.source_model = 'clap-class'), '') AS clap
FROM samples s
LEFT JOIN classification k ON k.sample_id = s.id
LEFT JOIN analysis a ON a.sample_id = s.id
ORDER BY s.folder, s.filename
"""


def _parse_scores(text: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for part in text.split(";") if text else ():
        name, _, value = part.partition("=")
        try:
            scores[name] = float(value)
        except ValueError:
            continue
    return scores


def load_samples(conn: sqlite3.Connection, top_tags: int = 3) -> list[SampleRow]:
    """Every sample in the index, one row each, with what the list displays."""
    rows: list[SampleRow] = []
    for row in conn.execute(_SAMPLES_SQL, (top_tags,)):
        *fields, clap = row
        rows.append(SampleRow(*fields, clap_scores=_parse_scores(clap)))
    return rows


def load_segments(conn: sqlite3.Connection, sample_id: int) -> list[SegmentRow]:
    """One sample's segments in time order — the §6.4 drill-down."""
    return [
        SegmentRow(*row)
        for row in conn.execute(
            "SELECT id, sample_id, start_ms, end_ms, strength, detection_method, "
            "needs_review, cache_path FROM segments WHERE sample_id = ? ORDER BY start_ms",
            (sample_id,),
        )
    ]


def load_tags(conn: sqlite3.Connection, sample_id: int) -> list[tuple[str, float]]:
    """One sample's zero-shot chips, best first — the Attributes tab's
    auto-tag chips (§5.2 "auto-suggested"); editing them is Phase 11."""
    return [
        (row[0], float(row[1]))
        for row in conn.execute(
            "SELECT tag_or_caption, score FROM text_tags "
            "WHERE sample_id = ? AND source_model = 'clap-zeroshot' ORDER BY score DESC",
            (sample_id,),
        )
    ]


def describe_item(conn: sqlite3.Connection, kind: str, item_id: int) -> str | None:
    """A short label for a sample or a segment (the anchor's caption); None
    if the id is unknown."""
    if kind == "sample":
        row = conn.execute("SELECT filename FROM samples WHERE id = ?", (item_id,)).fetchone()
        return None if row is None else str(row[0])
    row = conn.execute(
        "SELECT s.filename, g.start_ms, g.end_ms FROM segments g "
        "JOIN samples s ON s.id = g.sample_id WHERE g.id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    return f"hit @ {row[1] / 1000:.3f} s ({row[2] - row[1]} ms) in {row[0]}"


def index_summary(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts for the status bar."""
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "samples": q("SELECT COUNT(*) FROM samples"),
        "analysed": q("SELECT COUNT(*) FROM analysis"),
        "segments": q("SELECT COUNT(*) FROM segments"),
        "embedded": q("SELECT COUNT(*) FROM embedding"),
    }
