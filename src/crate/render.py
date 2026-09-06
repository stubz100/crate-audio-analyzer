"""Lazy segment rendering — node `J`'s file half (spec §6.5, §7), Phase 4.5.

A segment is an index into its parent (§6.4); it only becomes a file the
first time someone previews or drags it. The render is a small WAV in a
disposable cache — parent audio sliced at the markers, with a short fade so a
cut mid-waveform does not click — and the path is remembered on the segment
row so the next preview or drag is free. `update_segment` and a parent content
change clear that path (the bounds or audio it was rendered from no longer
hold); nothing evicts the files themselves yet — that is Phase 12.

The cache is trivially regenerable from parent + offsets, which is why it
lives under the per-user app data folder rather than in the library.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import numpy as np
import soundfile as sf

from .db import now_iso

log = logging.getLogger(__name__)

FADE_IN_MS = 2.0     # just enough to kill a click at the cut
FADE_OUT_MS = 20.0   # §6.5 "short fade-out"
_WAV_SUBTYPES = {"PCM_16", "PCM_24", "PCM_32", "FLOAT", "DOUBLE"}


def default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Crate" / "cache" / "segments"


def _apply_fades(audio: np.ndarray, sr: int) -> np.ndarray:
    """Linear fade in/out on a (frames, channels) float buffer, in place.
    Fades shrink to half the clip on very short windows."""
    frames = audio.shape[0]
    fade_in = min(int(FADE_IN_MS * sr / 1000), frames // 2)
    fade_out = min(int(FADE_OUT_MS * sr / 1000), frames // 2)
    if fade_in > 0:
        audio[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)[:, None]
    if fade_out > 0:
        audio[frames - fade_out :] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)[:, None]
    return audio


def render_segment(
    conn: sqlite3.Connection,
    segment_id: int,
    cache_dir: Path | str | None = None,
    force: bool = False,
) -> Path:
    """The cached WAV for a segment, rendering it on first use.

    Reads the parent at its native rate and channel count (no resampling —
    this file goes into a DAW), slices at the markers, fades, writes with the
    parent's PCM subtype where WAV supports it (24-bit otherwise). Raises
    LookupError for an unknown id and ValueError for a segment that lies
    entirely past the end of its file (a `needs_review` case).
    """
    row = conn.execute(
        "SELECT g.start_ms, g.end_ms, g.cache_path, s.filepath "
        "FROM segments g JOIN samples s ON s.id = g.sample_id WHERE g.id = ?",
        (segment_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no segment with id {segment_id}")
    start_ms, end_ms, cache_path, filepath = row[0], row[1], row[2], row[3]
    if not force and cache_path and Path(cache_path).exists():
        return Path(cache_path)

    info = sf.info(filepath)
    sr = info.samplerate
    start = int(start_ms * sr / 1000)
    end = min(info.frames, int(end_ms * sr / 1000))
    if end <= start:
        raise ValueError(
            f"segment {segment_id} lies past the end of {filepath} — needs review"
        )
    audio, _ = sf.read(filepath, start=start, stop=end, dtype="float32", always_2d=True)
    audio = _apply_fades(np.ascontiguousarray(audio), sr)
    np.clip(audio, -1.0, 1.0, out=audio)

    out_dir = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"seg_{segment_id}.wav"
    subtype = info.subtype if info.subtype in _WAV_SUBTYPES else "PCM_24"
    sf.write(out, audio, sr, subtype=subtype)
    conn.execute(
        "UPDATE segments SET cache_path = ?, cache_rendered_at = ? WHERE id = ?",
        (str(out), now_iso(), segment_id),
    )
    conn.commit()
    log.debug("rendered segment %d -> %s (%d frames, %s)", segment_id, out, end - start, subtype)
    return out


def preview_source(
    conn: sqlite3.Connection,
    kind: str,
    item_id: int,
    cache_dir: Path | str | None = None,
) -> Path:
    """The file to play or drag for a sample (its own path) or a segment (its
    render, produced on first use — spec §9.2)."""
    if kind == "sample":
        row = conn.execute("SELECT filepath FROM samples WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise LookupError(f"no sample with id {item_id}")
        return Path(row[0])
    if kind == "segment":
        return render_segment(conn, item_id, cache_dir)
    raise ValueError(f"kind must be 'sample' or 'segment', not {kind!r}")
