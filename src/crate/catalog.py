"""Read-side queries for the list view — plain rows, no Qt (Phase 4.5).

Everything the "listen and grab" list shows comes from here, so the GUI
model is a thin adapter and this layer is testable on its own. Segments are
never rows in the sample list (§6.4); they are fetched per sample for the
drill-down.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


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


_SAMPLES_SQL = """
SELECT s.id, s.filepath, s.filename, s.folder, s.duration_s,
       k.structural_type, k.content_class, k.confidence,
       a.tempo_bpm, a.key,
       COALESCE((SELECT group_concat(tag_or_caption, ', ') FROM (
                    SELECT tag_or_caption FROM text_tags t
                    WHERE t.sample_id = s.id AND t.source_model = 'clap-zeroshot'
                    ORDER BY t.score DESC LIMIT ?)), '') AS tags,
       (SELECT COUNT(*) FROM segments g WHERE g.sample_id = s.id) AS segment_count,
       (SELECT COUNT(*) FROM segments g WHERE g.sample_id = s.id AND g.needs_review = 1) AS flagged
FROM samples s
LEFT JOIN classification k ON k.sample_id = s.id
LEFT JOIN analysis a ON a.sample_id = s.id
ORDER BY s.folder, s.filename
"""


def load_samples(conn: sqlite3.Connection, top_tags: int = 3) -> list[SampleRow]:
    """Every sample in the index, one row each, with what the list displays."""
    return [SampleRow(*row) for row in conn.execute(_SAMPLES_SQL, (top_tags,))]


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


def index_summary(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts for the status bar."""
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "samples": q("SELECT COUNT(*) FROM samples"),
        "analysed": q("SELECT COUNT(*) FROM analysis"),
        "segments": q("SELECT COUNT(*) FROM segments"),
        "embedded": q("SELECT COUNT(*) FROM embedding"),
    }
