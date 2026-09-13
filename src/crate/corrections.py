"""Phase 11 — the correction workflow (spec §4, §8, §11, §12). No Qt.

What the machine decides, the user can overrule, and a recompute never takes
the overruling back:

* **Facet A, the content class** (rhythmic / melodic / vocal / other, §4):
  `set_content_class` writes the user's call and raises the facet's own flag,
  `classification.content_class_confirmed`; node E (`embedding._write_facet_a`)
  leaves a flagged row alone. `reset_content_class` hands the facet back to
  CLAP — the best of the four stored class scores, flagged below the
  confidence threshold, exactly as node E would call it from the same numbers
  (bar its rhythmic-vs-melodic tie-break, which needs the harmonic ratio and
  ties are rare) — and lowers the flag.
* **Facet B, the structural type** (one-shot / multi-hit / loop, §4):
  `set_structural_type` likewise, with `structural_type_confirmed`; re-analysis
  (`analysis._store`) leaves it alone. `reset_structural_type` re-runs the
  Phase 2 rule over the stored descriptor row — no audio — under the one-shot
  cap it is given (§9.6's setting).
* **The curated tags** (`tags` / `sample_tags`, §8): user-owned, never
  written by any stage. `text_tags` stays the machine layer — regenerable,
  disposable; a chip the user accepts is *promoted* here (`add_tag`), and
  `suggested_tags` is the machine's list minus what is already curated.

The flags are per facet (2026-09-13): before this, one `is_user_confirmed`
froze the whole row, so correcting the class would also have blocked every
later structural-type re-analysis. `is_user_confirmed` is kept in step as
"either facet" for the summaries and older code that read it.

A structural-type correction changes what the next Recompute does — node S
reads the index's value, so a sample corrected to one-shot loses its automatic
segments on the next run and one corrected away from it gains them — but
nothing runs on its own (§9.6): the correction is a row in the index until
the user presses the button.

Every writer takes a list of sample ids: a correction applies as naturally to
the rows selected in the list as to one sample, and one commit covers them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .analysis import ONE_SHOT_MAX_DURATION_S, Descriptors, structural_type
from .db import now_iso

CONTENT_CLASSES: tuple[str, ...] = ("rhythmic", "melodic", "vocal", "other")
STRUCTURAL_TYPES: tuple[str, ...] = ("one-shot", "multi-hit", "loop")
CLASS_LABELS: dict[str, str] = {
    "rhythmic": "Rhythmic", "melodic": "Melodic", "vocal": "Vocal", "other": "Other",
}
TYPE_LABELS: dict[str, str] = {
    "one-shot": "One-shot", "multi-hit": "Multi-hit", "loop": "Loop",
}
#: `classification.source_model` for a row the user created outright.
USER_SOURCE = "user"
#: Node E's default flag threshold (`EmbedSettings.confidence_threshold`).
DEFAULT_CONFIDENCE_THRESHOLD = 0.5

_DESCRIPTOR_COLUMNS = list(Descriptors.__dataclass_fields__)


@dataclass(frozen=True)
class Classification:
    """One sample's two facets as the index holds them, with what the machine
    said so a correction can be weighed against it."""

    content_class: str | None           # None = unclassified, or flagged below the threshold
    structural_type: str | None
    confidence: float | None            # node E's softmax confidence in its call
    content_class_confirmed: bool       # the user's call, protected
    structural_type_confirmed: bool
    class_scores: dict[str, float] = field(default_factory=dict)   # CLAP's four probabilities, by class

    @property
    def best_class(self) -> str | None:
        """CLAP's own best class from the stored scores, whether or not it
        was confident enough to assign it."""
        if not self.class_scores:
            return None
        return max(self.class_scores, key=self.class_scores.__getitem__)


# --- reading --------------------------------------------------------------------


def _class_scores(conn: sqlite3.Connection, sample_id: int) -> dict[str, float]:
    return {
        str(name): float(score)
        for name, score in conn.execute(
            "SELECT tag_or_caption, score FROM text_tags "
            "WHERE sample_id = ? AND source_model = 'clap-class' AND score IS NOT NULL",
            (sample_id,),
        )
    }


def load_classification(conn: sqlite3.Connection, sample_id: int) -> Classification:
    """The sample's facets and flags; an unclassified sample reads as empty."""
    row = conn.execute(
        "SELECT content_class, structural_type, confidence, "
        "content_class_confirmed, structural_type_confirmed "
        "FROM classification WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()
    scores = _class_scores(conn, sample_id)
    if row is None:
        return Classification(None, None, None, False, False, scores)
    return Classification(
        row[0], row[1], None if row[2] is None else float(row[2]),
        bool(row[3]), bool(row[4]), scores,
    )


def load_user_tags(conn: sqlite3.Connection, sample_id: int) -> list[str]:
    """The sample's curated tags, alphabetical."""
    return [
        str(r[0]) for r in conn.execute(
            "SELECT t.name FROM sample_tags st JOIN tags t ON t.id = st.tag_id "
            "WHERE st.sample_id = ? ORDER BY t.name COLLATE NOCASE",
            (sample_id,),
        )
    ]


def all_tags(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Every curated tag with how many samples carry it, most used first —
    the tag box's completions."""
    return [
        (str(r[0]), int(r[1])) for r in conn.execute(
            "SELECT t.name, COUNT(st.sample_id) FROM tags t "
            "LEFT JOIN sample_tags st ON st.tag_id = t.id "
            "GROUP BY t.id ORDER BY COUNT(st.sample_id) DESC, t.name COLLATE NOCASE"
        )
    ]


def suggested_tags(conn: sqlite3.Connection, sample_id: int, limit: int = 6) -> list[tuple[str, float]]:
    """The machine's best chips the user has not curated yet, best first —
    what a click can promote (§8's flow: text_tags → tags/sample_tags)."""
    curated = {name.lower() for name in load_user_tags(conn, sample_id)}
    result: list[tuple[str, float]] = []
    for name, score in conn.execute(
        "SELECT tag_or_caption, score FROM text_tags "
        "WHERE sample_id = ? AND source_model = 'clap-zeroshot' ORDER BY score DESC",
        (sample_id,),
    ):
        if str(name).lower() not in curated:
            result.append((str(name), float(score)))
            if len(result) >= limit:
                break
    return result


# --- Facet A -----------------------------------------------------------------------


def _inherit_class(conn: sqlite3.Connection, sample_id: int, content_class: str | None) -> None:
    """§6.4: a sample's segments carry its class — bar one the user classified itself."""
    conn.execute(
        "UPDATE segment_classification SET content_class = ? "
        "WHERE is_user_confirmed = 0 AND segment_id IN (SELECT id FROM segments WHERE sample_id = ?)",
        (content_class, sample_id),
    )


def set_content_class(conn: sqlite3.Connection, sample_ids: Iterable[int], content_class: str) -> int:
    """The user's Facet A for these samples: written, flagged, and protected
    from node E from now on. Returns how many samples were written."""
    if content_class not in CONTENT_CLASSES:
        raise ValueError(f"content class must be one of {CONTENT_CLASSES}, not {content_class!r}")
    written = 0
    for sample_id in sample_ids:
        conn.execute(
            "INSERT INTO classification (sample_id, content_class, provenance, source_model, "
            "content_class_confirmed, is_user_confirmed) VALUES (?, ?, 'manual', ?, 1, 1) "
            "ON CONFLICT(sample_id) DO UPDATE SET content_class = excluded.content_class, "
            "provenance = 'manual', content_class_confirmed = 1, is_user_confirmed = 1",
            (sample_id, content_class, USER_SOURCE),
        )
        _inherit_class(conn, sample_id, content_class)
        written += 1
    conn.commit()
    return written


def reset_content_class(
    conn: sqlite3.Connection,
    sample_ids: Iterable[int],
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> int:
    """Facet A back to CLAP's call from the stored class scores — the best
    of the four, flagged (NULL) below the threshold — and unprotected. A
    sample with no scores yet reads as unclassified until the next embed."""
    written = 0
    for sample_id in sample_ids:
        scores = _class_scores(conn, sample_id)
        if scores:
            best = max(scores, key=scores.__getitem__)
            confidence: float | None = scores[best]
            content_class = best if scores[best] >= confidence_threshold else None
        else:
            content_class, confidence = None, None
        cursor = conn.execute(
            "UPDATE classification SET content_class = ?, confidence = ?, "
            "content_class_confirmed = 0, is_user_confirmed = structural_type_confirmed, "
            "provenance = CASE WHEN structural_type_confirmed THEN 'manual' ELSE 'automatic' END "
            "WHERE sample_id = ?",
            (content_class, confidence, sample_id),
        )
        if cursor.rowcount:
            _inherit_class(conn, sample_id, content_class)
            written += 1
    conn.commit()
    return written


# --- Facet B -----------------------------------------------------------------------


def set_structural_type(conn: sqlite3.Connection, sample_ids: Iterable[int], structural_type_: str) -> int:
    """The user's Facet B for these samples: written, flagged, and protected
    from re-analysis from now on. The next Recompute's segmentation follows
    it (node S reads the index). Returns how many samples were written."""
    if structural_type_ not in STRUCTURAL_TYPES:
        raise ValueError(f"structural type must be one of {STRUCTURAL_TYPES}, not {structural_type_!r}")
    written = 0
    for sample_id in sample_ids:
        conn.execute(
            "INSERT INTO classification (sample_id, structural_type, provenance, source_model, "
            "structural_type_confirmed, is_user_confirmed) VALUES (?, ?, 'manual', ?, 1, 1) "
            "ON CONFLICT(sample_id) DO UPDATE SET structural_type = excluded.structural_type, "
            "provenance = 'manual', structural_type_confirmed = 1, is_user_confirmed = 1",
            (sample_id, structural_type_, USER_SOURCE),
        )
        written += 1
    conn.commit()
    return written


def automatic_structural_type(
    conn: sqlite3.Connection,
    sample_id: int,
    one_shot_max_duration_s: float | None = ONE_SHOT_MAX_DURATION_S,
) -> str | None:
    """What the Phase 2 rule says for the sample from its stored descriptor
    row — no audio; None when it has not been analysed."""
    columns = ", ".join(f"a.{c}" for c in _DESCRIPTOR_COLUMNS)
    row = conn.execute(
        f"SELECT s.duration_s, {columns} FROM analysis a JOIN samples s ON s.id = a.sample_id "
        "WHERE a.sample_id = ?",
        (sample_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    descriptors = Descriptors(**{c: row[i + 1] for i, c in enumerate(_DESCRIPTOR_COLUMNS)})
    return structural_type(descriptors, float(row[0]), one_shot_max_duration_s)


def reset_structural_type(
    conn: sqlite3.Connection,
    sample_ids: Iterable[int],
    one_shot_max_duration_s: float | None = ONE_SHOT_MAX_DURATION_S,
) -> int:
    """Facet B back to the rule's answer from the stored descriptors, and
    unprotected. An unanalysed sample keeps NULL until the next analysis."""
    written = 0
    for sample_id in sample_ids:
        automatic = automatic_structural_type(conn, sample_id, one_shot_max_duration_s)
        cursor = conn.execute(
            "UPDATE classification SET structural_type = ?, structural_type_confirmed = 0, "
            "is_user_confirmed = content_class_confirmed, "
            "provenance = CASE WHEN content_class_confirmed THEN 'manual' ELSE 'automatic' END "
            "WHERE sample_id = ?",
            (automatic, sample_id),
        )
        written += cursor.rowcount
    conn.commit()
    return written


# --- the curated tags -------------------------------------------------------------------


def normalise_tag(name: str) -> str:
    """One space between words, none around; a tag needs at least one word."""
    name = " ".join(str(name).split())
    if not name:
        raise ValueError("a tag needs a name")
    return name


def add_tag(conn: sqlite3.Connection, sample_ids: Sequence[int], name: str) -> int:
    """Curate `name` on these samples (§8: promoting a chip is this with the
    chip's text). Case-insensitive: "Kick" and "kick" are one tag, spelled
    the way it was first written. Returns how many samples newly carry it."""
    name = normalise_tag(name)
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
    now = now_iso()
    added = 0
    for sample_id in sample_ids:
        added += conn.execute(
            "INSERT OR IGNORE INTO sample_tags (sample_id, tag_id, added_at) VALUES (?, ?, ?)",
            (sample_id, tag_id, now),
        ).rowcount
    conn.commit()
    return added


def remove_tag(conn: sqlite3.Connection, sample_ids: Sequence[int], name: str) -> int:
    """Take `name` off these samples; a tag no sample carries any more is
    forgotten. Returns how many samples lost it."""
    name = normalise_tag(name)
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row is None:
        return 0
    tag_id = row[0]
    removed = 0
    for sample_id in sample_ids:
        removed += conn.execute(
            "DELETE FROM sample_tags WHERE sample_id = ? AND tag_id = ?", (sample_id, tag_id)
        ).rowcount
    conn.execute(
        "DELETE FROM tags WHERE id = ? AND NOT EXISTS (SELECT 1 FROM sample_tags WHERE tag_id = ?)",
        (tag_id, tag_id),
    )
    conn.commit()
    return removed
