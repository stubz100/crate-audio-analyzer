"""Embedded WAV metadata: `smpl` and ACID chunk reader (spec §10).

Neither soundfile nor librosa parses RIFF chunk extensions, so this is the
small purpose-built reader §10 calls for, reassigned from Phase 1 to Phase 2
(spec §12). `smpl` is the standard MIDI sampler chunk; ACID follows the
community-documented layout written by ACIDizer-style tools. Parsing is
defensive: malformed input yields whatever parsed cleanly (or None) and
never raises.

Chunks are located by seeking, never by reading the file into memory — a
340GB library would otherwise pay a full extra read per file to find a
24-byte chunk (2026-09-06).
"""

from __future__ import annotations

import logging
import os
import struct
from pathlib import Path

log = logging.getLogger(__name__)

# ACID flag bits (community-documented):
ACID_ONE_SHOT = 0x01
ACID_ROOT_SET = 0x02
ACID_STRETCH = 0x04      # beat-based loop
ACID_DISK_BASED = 0x08
ACID_ACIDIZED = 0x10

# Documented ACID chunk layout, 24 bytes:
#   uint32 flags | uint16 rootNote | uint16 unknown1 | float  unknown2
#   uint32 beats | uint16 meterDen | uint16 meterNum | float  tempo
_ACID_STRUCT = struct.Struct("<IHHfIHHf")
_SMPL_HEAD = struct.Struct("<9I")   # 36 bytes, through numSampleLoops
_SMPL_LOOP = struct.Struct("<6I")   # 24 bytes per loop
_MAX_CHUNK_BYTES = 1 << 20          # sanity cap for chunks we actually read


def _parse_smpl(body: bytes) -> dict | None:
    if len(body) < _SMPL_HEAD.size:
        return None
    (
        _manufacturer, _product, sample_period, unity_note, _pitch_fraction,
        _smpte_format, _smpte_offset, loop_count, _sampler_data,
    ) = _SMPL_HEAD.unpack_from(body, 0)
    loops: list[dict] = []
    offset = _SMPL_HEAD.size
    for _ in range(loop_count):
        if offset + _SMPL_LOOP.size > len(body):
            break
        ident, loop_type, start, end, fraction, play_count = _SMPL_LOOP.unpack_from(
            body, offset
        )
        loops.append(
            {
                "id": ident, "type": loop_type, "start": start, "end": end,
                "fraction": fraction, "play_count": play_count,
            }
        )
        offset += _SMPL_LOOP.size
    return {
        "midi_unity_note": unity_note,
        "sample_period": sample_period,
        "loop_count": len(loops),
        "loops": loops,
    }


def _parse_acid(body: bytes) -> dict | None:
    if len(body) < _ACID_STRUCT.size:
        return None
    flags, root_note, _unknown1, _unknown2, beats, meter_den, meter_num, tempo = (
        _ACID_STRUCT.unpack_from(body, 0)
    )
    # Guard against garbage: an ACIDized file has a sane beat count and a tempo
    # field that is at least not nonsense. Tempo 0.0 is allowed — the field is
    # a placeholder in practice (see below) and the beat count is what matters;
    # NaN fails both comparisons and is rejected.
    if not (0 <= beats <= 100_000) or not (0.0 <= tempo <= 1000.0):
        return None
    # NOTE: `tempo` is frequently a nominal 120.0 placeholder rather than the
    # real tempo — measured across 52 ACIDized files in D:\_soundPacks, every
    # single one stored 120.0 while `beats` was correct. Callers that know the
    # file duration should derive tempo from `beats` instead (see
    # analysis._acid_tempo_bpm); this dict reports the chunk verbatim.
    return {
        "one_shot": bool(flags & ACID_ONE_SHOT),
        "root_set": bool(flags & ACID_ROOT_SET),
        "stretch": bool(flags & ACID_STRETCH),
        "acidized": bool(flags & ACID_ACIDIZED),
        "root_note": root_note,
        "beats": int(beats),
        "meter": f"{meter_num}/{meter_den}",
        "tempo_bpm": float(tempo),
    }


def read_embedded_metadata(path: Path | str) -> dict | None:
    """Walk the RIFF chunks of a WAV file, seeking past the ones we don't need.

    Returns `{"smpl": {...}}`, `{"acid": {...}}`, both, or None when the file
    is not RIFF/WAVE (e.g. FLAC) or carries neither chunk.
    """
    found: dict = {}
    try:
        with open(path, "rb") as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None
            while True:
                chunk_header = f.read(8)
                if len(chunk_header) < 8:
                    break
                chunk_id = chunk_header[:4]
                (chunk_size,) = struct.unpack_from("<I", chunk_header, 4)
                wanted = chunk_id in (b"smpl", b"acid") and chunk_id.decode() not in found
                if wanted and chunk_size <= _MAX_CHUNK_BYTES:
                    parser = _parse_smpl if chunk_id == b"smpl" else _parse_acid
                    parsed = parser(f.read(chunk_size))  # short read → parser's length guard
                    if parsed:
                        found[chunk_id.decode()] = parsed
                else:
                    f.seek(chunk_size, os.SEEK_CUR)
                f.seek(chunk_size & 1, os.SEEK_CUR)  # RIFF chunks are word-aligned
    except OSError as exc:
        log.debug("embedded metadata: unreadable file %s (%s)", path, exc)
        return None
    # No struct.error path: every unpack is preceded by a length guard.
    return found or None
