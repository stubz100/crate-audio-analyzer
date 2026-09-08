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

from .db import WINDOW_METHOD, scope_clause


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
    caption: str = ""         # the Qwen2-Audio sentence, when there is one (a list column since 2026-09-08)
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


# The list's segment count leaves the CLAP windows of long files out (§6.4):
# they are not slices anyone chose, only where the model looked.
_SAMPLES_SQL = f"""
SELECT s.id, s.filepath, s.filename, s.folder, s.duration_s,
       k.structural_type, k.content_class, k.confidence,
       a.tempo_bpm, a.key,
       COALESCE((SELECT group_concat(tag_or_caption, ', ') FROM (
                    SELECT tag_or_caption FROM text_tags t
                    WHERE t.sample_id = s.id AND t.source_model = 'clap-zeroshot'
                    ORDER BY t.score DESC LIMIT ?)), '') AS tags,
       (SELECT COUNT(*) FROM segments g WHERE g.sample_id = s.id
        AND g.detection_method != '{WINDOW_METHOD}') AS segment_count,
       (SELECT COUNT(*) FROM segments g WHERE g.sample_id = s.id AND g.needs_review = 1) AS flagged,
       COALESCE((SELECT tag_or_caption FROM text_tags t
                 WHERE t.sample_id = s.id AND t.source_model = 'qwen2audio-caption' LIMIT 1), '') AS caption,
       COALESCE((SELECT group_concat(tag_or_caption || '=' || score, ';') FROM text_tags t
                 WHERE t.sample_id = s.id AND t.source_model = 'clap-class'), '') AS clap
FROM samples s
LEFT JOIN classification k ON k.sample_id = s.id
LEFT JOIN analysis a ON a.sample_id = s.id
WHERE 1=1{{scope}}
ORDER BY s.folder, s.filename
"""


def _scope(scope) -> tuple[str, list[str]]:
    """The §9.6 scope as SQL: None = everything (no folders known); an empty
    scope = nothing (folders known, none in scope); else the folders."""
    if scope is None:
        return "", []
    if not scope:
        return " AND 0", []
    return scope_clause(scope, "s.filepath")


def _parse_scores(text: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for part in text.split(";") if text else ():
        name, _, value = part.partition("=")
        try:
            scores[name] = float(value)
        except ValueError:
            continue
    return scores


def load_samples(conn: sqlite3.Connection, top_tags: int = 3, scope=None) -> list[SampleRow]:
    """The samples in scope (§9.6: the folders ticked on the Recompute tab;
    None = every sample), one row each, with what the list displays."""
    clause, params = _scope(scope)
    rows: list[SampleRow] = []
    for row in conn.execute(_SAMPLES_SQL.replace("{scope}", clause), [top_tags, *params]):
        *fields, clap = row
        rows.append(SampleRow(*fields, clap_scores=_parse_scores(clap)))
    return rows


_SEGMENT_COLUMNS = (
    "SELECT id, sample_id, start_ms, end_ms, strength, detection_method, needs_review, cache_path "
    "FROM segments WHERE sample_id = ? AND detection_method "
)


@dataclass(frozen=True)
class Section:
    """A sample's section as the list shows it under the sample (2026-09-08,
    the user's steer: every section sits under its sample) — a detected or
    manual segment, or a CLAP window of a long file (§6.4), which only shows
    while it carries a score."""

    segment_id: int
    start_ms: int
    end_ms: int
    window: bool = False
    manual: bool = False


def load_sections(conn: sqlite3.Connection, scope=None) -> dict[int, list[Section]]:
    """Every sample's sections in scope, in time order, by sample id — the
    list's child rows."""
    clause, params = _scope(scope)
    sections: dict[int, list[Section]] = {}
    for seg_id, sample_id, start_ms, end_ms, method in conn.execute(
        "SELECT g.id, g.sample_id, g.start_ms, g.end_ms, g.detection_method FROM segments g "
        f"JOIN samples s ON s.id = g.sample_id WHERE 1=1{clause} ORDER BY g.sample_id, g.start_ms",
        params,
    ):
        sections.setdefault(int(sample_id), []).append(Section(
            int(seg_id), int(start_ms), int(end_ms), window=method == WINDOW_METHOD, manual=method == "manual",
        ))
    return sections


def load_segments(conn: sqlite3.Connection, sample_id: int) -> list[SegmentRow]:
    """One sample's detected and manual segments in time order — the §6.4
    drill-down. The CLAP windows of a long file are `load_windows`."""
    return [
        SegmentRow(*row)
        for row in conn.execute(_SEGMENT_COLUMNS + "!= ? ORDER BY start_ms", (sample_id, WINDOW_METHOD))
    ]


def load_windows(conn: sqlite3.Connection, sample_id: int) -> list[SegmentRow]:
    """The 10-s CLAP windows a long file was embedded through (§6.4), in
    time order — drawn on the waveform, never listed as segments."""
    return [
        SegmentRow(*row)
        for row in conn.execute(_SEGMENT_COLUMNS + "= ? ORDER BY start_ms", (sample_id, WINDOW_METHOD))
    ]


def clock(ms: int) -> str:
    """A position in a file: seconds to three places under a minute, m:ss
    beyond it — a hit at minute seven of an ambience reads as 7:10.250."""
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.3f} s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:06.3f}"


def hit_label(start_ms: int, end_ms: int, window: bool = False) -> str:
    """How a hit inside a longer sample is named everywhere (§9.4): the
    sub-hit row, the transport caption, the anchor's label."""
    length_ms = end_ms - start_ms
    if window:
        return f"window @ {clock(start_ms)} ({length_ms / 1000:.0f} s)"
    return f"hit @ {clock(start_ms)} ({length_ms} ms)"


def load_tags(conn: sqlite3.Connection, sample_id: int, limit: int | None = None) -> list[tuple[str, float]]:
    """One sample's zero-shot tags with their cosines, best first — the
    Attributes tab's chips and its score bars (§5.2 "auto-suggested");
    editing them is Phase 11. All 32 are stored since 2026-09-08."""
    sql = (
        "SELECT tag_or_caption, score FROM text_tags "
        "WHERE sample_id = ? AND source_model = 'clap-zeroshot' ORDER BY score DESC"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [(row[0], float(row[1])) for row in conn.execute(sql, (sample_id,))]


def load_caption(conn: sqlite3.Connection, sample_id: int) -> str | None:
    """The sample's Qwen2-Audio sentence (§5.2), if it has one."""
    row = conn.execute(
        "SELECT tag_or_caption FROM text_tags WHERE sample_id = ? AND source_model = 'qwen2audio-caption' "
        "ORDER BY id DESC LIMIT 1",
        (sample_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def load_vector(conn: sqlite3.Connection, kind: str, item_id: int, model_name: str = "clap"):
    """The stored CLAP vector of a sample or a segment (float32, unit
    length), or None if it has not been embedded. The embedding strip and
    anything else that wants the raw numbers reads them from here."""
    from .embedding import blob_to_vector  # local: catalog stays importable without the model stack

    if kind == "sample":
        row = conn.execute(
            "SELECT vector FROM embedding WHERE sample_id = ? AND model_name = ?", (item_id, model_name)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT vector FROM segment_embedding WHERE segment_id = ? AND model_name = ?",
            (item_id, model_name),
        ).fetchone()
    return None if row is None else blob_to_vector(row[0])


def describe_item(conn: sqlite3.Connection, kind: str, item_id: int) -> str | None:
    """A short label for a sample or a segment (the anchor's caption); None
    if the id is unknown."""
    if kind == "sample":
        row = conn.execute("SELECT filename FROM samples WHERE id = ?", (item_id,)).fetchone()
        return None if row is None else str(row[0])
    row = conn.execute(
        "SELECT s.filename, g.start_ms, g.end_ms, g.detection_method FROM segments g "
        "JOIN samples s ON s.id = g.sample_id WHERE g.id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    return f"{hit_label(row[1], row[2], row[3] == WINDOW_METHOD)} in {row[0]}"


def index_summary(conn: sqlite3.Connection, scope=None) -> dict[str, int]:
    """Counts for the status bar: everything but `indexed` is over the scope."""
    clause, params = _scope(scope)
    q = lambda sql: conn.execute(sql, params).fetchone()[0]  # noqa: E731
    return {
        "indexed": conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0],
        "samples": q(f"SELECT COUNT(*) FROM samples s WHERE 1=1{clause}"),
        "analysed": q(f"SELECT COUNT(*) FROM analysis a JOIN samples s ON s.id = a.sample_id WHERE 1=1{clause}"),
        "segments": q(
            f"SELECT COUNT(*) FROM segments g JOIN samples s ON s.id = g.sample_id "
            f"WHERE g.detection_method != '{WINDOW_METHOD}'{clause}"
        ),
        "windows": q(
            f"SELECT COUNT(*) FROM segments g JOIN samples s ON s.id = g.sample_id "
            f"WHERE g.detection_method = '{WINDOW_METHOD}'{clause}"
        ),
        "embedded": q(f"SELECT COUNT(*) FROM embedding e JOIN samples s ON s.id = e.sample_id WHERE 1=1{clause}"),
    }
