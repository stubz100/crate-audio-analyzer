"""Tests for the smpl/ACID chunk reader (spec §10, Phase 2)."""

from __future__ import annotations

import struct

import numpy as np
import soundfile as sf

from crate.wavmeta import read_embedded_metadata

ACID_ONE_SHOT = 0x01
ACID_ROOT_SET = 0x02
ACID_STRETCH = 0x04
ACID_ACIDIZED = 0x10


def _acid_chunk(
    flags: int = ACID_ACIDIZED,
    root_note: int = 60,
    beats: int = 8,
    meter_num: int = 4,
    meter_den: int = 4,
    tempo: float = 120.0,
) -> bytes:
    """Documented 24-byte ACID chunk: the layout ACIDizer-style tools write."""
    body = struct.pack(
        "<IHHfIHHf", flags, root_note, 0x8000, 0.0, beats, meter_den, meter_num, tempo
    )
    return b"acid" + struct.pack("<I", len(body)) + body


def _smpl_chunk(loop_count: int = 1, unity_note: int = 60) -> bytes:
    head = struct.pack(
        "<9I", 0, 0, 22675, unity_note, 0, 0, 0, loop_count, 0
    )
    loops = b"".join(
        struct.pack("<6I", i, 0, 100, 200, 0, 0) for i in range(loop_count)
    )
    body = head + loops
    return b"smpl" + struct.pack("<I", len(body)) + body


def _write_wav_with_chunks(path, *chunks: bytes, seconds: float = 0.05) -> None:
    """Write a real WAV, then splice extra RIFF chunks in before EOF."""
    sr = 22050
    sf.write(path, np.zeros(int(sr * seconds), dtype="float32"), sr, subtype="PCM_16")
    original = path.read_bytes()
    extra = b"".join(chunks)
    # RIFF size field covers everything after the first 8 bytes.
    new_size = struct.unpack_from("<I", original, 4)[0] + len(extra)
    patched = original[:4] + struct.pack("<I", new_size) + original[8:] + extra
    path.write_bytes(patched)


def test_acid_chunk_round_trips_tempo_beats_and_meter(tmp_path):
    """The whole point of reading ACID: an authoritative tempo (spec §7 node C).

    Regression test for the 2026-09-06 review — the original struct format
    omitted the uint32 beat count, so beats/meter/tempo were all garbage
    (tempo came back as 3.7e-40 for a 120 BPM file).
    """
    wav = tmp_path / "loop.wav"
    _write_wav_with_chunks(wav, _acid_chunk(beats=8, meter_num=3, meter_den=4, tempo=128.5))

    meta = read_embedded_metadata(wav)

    assert meta is not None
    acid = meta["acid"]
    assert acid["tempo_bpm"] == 128.5
    assert acid["beats"] == 8
    assert acid["meter"] == "3/4"
    assert acid["root_note"] == 60
    assert acid["acidized"] is True
    assert acid["one_shot"] is False


def test_acid_one_shot_flag_is_read(tmp_path):
    wav = tmp_path / "hit.wav"
    _write_wav_with_chunks(wav, _acid_chunk(flags=ACID_ONE_SHOT | ACID_ROOT_SET))

    acid = read_embedded_metadata(wav)["acid"]

    assert acid["one_shot"] is True
    assert acid["root_set"] is True
    assert acid["stretch"] is False


def test_smpl_loops_are_parsed(tmp_path):
    wav = tmp_path / "sampler.wav"
    _write_wav_with_chunks(wav, _smpl_chunk(loop_count=2, unity_note=48))

    smpl = read_embedded_metadata(wav)["smpl"]

    assert smpl["midi_unity_note"] == 48
    assert smpl["loop_count"] == 2
    assert smpl["loops"][0]["start"] == 100
    assert smpl["loops"][0]["end"] == 200


def test_both_chunks_can_coexist(tmp_path):
    wav = tmp_path / "both.wav"
    _write_wav_with_chunks(wav, _smpl_chunk(), _acid_chunk(tempo=90.0))

    meta = read_embedded_metadata(wav)

    assert set(meta) == {"smpl", "acid"}
    assert meta["acid"]["tempo_bpm"] == 90.0


def test_plain_wav_without_chunks_returns_none(tmp_path):
    wav = tmp_path / "plain.wav"
    _write_wav_with_chunks(wav)

    assert read_embedded_metadata(wav) is None


def test_non_riff_file_returns_none(tmp_path):
    flac = tmp_path / "not_a_wav.flac"
    sf.write(flac, np.zeros(1000, dtype="float32"), 22050)

    assert read_embedded_metadata(flac) is None


def test_missing_file_returns_none_rather_than_raising(tmp_path):
    assert read_embedded_metadata(tmp_path / "nope.wav") is None


def test_truncated_chunk_does_not_raise(tmp_path):
    """A chunk header promising more bytes than the file holds must not crash."""
    wav = tmp_path / "truncated.wav"
    _write_wav_with_chunks(wav, _acid_chunk())
    data = wav.read_bytes()
    wav.write_bytes(data[:-8])  # cut into the ACID body

    assert read_embedded_metadata(wav) is None


def test_garbage_tempo_is_rejected(tmp_path):
    """Sanity guard: an implausible tempo means the chunk is not really ACID."""
    wav = tmp_path / "garbage.wav"
    _write_wav_with_chunks(wav, _acid_chunk(tempo=99999.0))

    assert read_embedded_metadata(wav) is None


def test_zero_tempo_placeholder_keeps_beats_and_root(tmp_path):
    """The tempo field is a placeholder in practice; a 0.0 there must not
    throw away a valid beat count and root note (2026-09-06 review)."""
    wav = tmp_path / "zero_tempo.wav"
    _write_wav_with_chunks(wav, _acid_chunk(flags=ACID_ROOT_SET, root_note=55, beats=16, tempo=0.0))

    acid = read_embedded_metadata(wav)["acid"]

    assert acid["beats"] == 16
    assert acid["root_note"] == 55 and acid["root_set"] is True
    assert acid["tempo_bpm"] == 0.0
