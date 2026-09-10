"""The waveform panel — spec §9.2's preview strip, placed in the bottom
panel on the user's steer (2026-09-07) instead of the header.

Shows the selected sample's waveform, every segment as a begin/end marker
pair with its strength, the measured attack and decay (the envelope the
Amplitude axis reads, §5.1 — the closest thing to an ADSR a recording has),
and the preview's playhead. Clicking inside a segment selects it (which
previews it); clicking elsewhere seeks.

Phase 9 (2026-09-08): the markers are editable. Drag a segment's begin or
end marker to move it; drag on the waveform to draw a new segment. Every
edit is *staged* — dashed, "unsaved" — and written only by *Save segment*
(§6.3: staged on drag, committed only via Save), which the window does;
*Discard* or Esc drops it, as does loading another sample. *Delete
segment* (or Del) asks the window to remove the selected one. The panel
class below holds the view, its caption line and the buttons.

Zoom and rendering (2026-09-08, the user's steer — "zoom for precision", "a
less crude look", "separate the playback from the graphics"):

- The audio is read on a worker thread for every file, never on the GUI
  thread: selecting a sample starts the preview at once and the waveform
  lands when it is ready (~10 ms for a 30-s WAV, seconds for a 16-minute
  ambience; a newer load wins). Files up to KEEP_SAMPLES_SECONDS keep their
  mono samples (5 MB for 30 s) so any zoom is drawn from the samples
  themselves (a 3-minute file: 32 MB); longer files keep OVERVIEW_COLUMNS
  min/max/RMS columns (a 16-minute ambience: 29 ms per column).
- The wheel zooms about the cursor, Shift+wheel (or a horizontal wheel)
  pans, right-click or Home fits the file; the panel's scrollbar pans too.
- The waveform body — peaks as a light fill, RMS as a brighter core, one
  column per device pixel — is rasterised once per view into a cached
  pixmap; a playhead tick only blits it and draws the overlays. All CPU:
- A **spectral view** (2026-09-09, the user's steer) swaps the body for a
  log-frequency spectrogram — one Hann-windowed FFT per pixel column over
  the view, from the samples kept in memory, so its cost does not grow
  with the file — drawn through the same cache, zoom and overlays; the
  panel's *Spectrum* button toggles it and the window remembers the choice.
  Qt's antialiased raster engine is more than enough for a 2-D strip, and a
  GPU surface would add a driver dependency for no gain (CLAUDE.md).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
from PySide6.QtCore import QCoreApplication, QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QScrollBar, QVBoxLayout, QWidget

from .catalog import SegmentRow
from .theme import ACCENT, ACCENT_DIM, AMBER, BG, BORDER, GREEN, PINK, TEXT, TEXT_DIM, WHITE, ElidedLabel
from .widgets import Toolbar, make_button

log = logging.getLogger(__name__)

OVERVIEW_COLUMNS = 32768       # min/max/RMS columns kept for every file: the overview, and a long file's zoom
DEFAULT_COLUMNS = OVERVIEW_COLUMNS
KEEP_SAMPLES_SECONDS = 180.0   # up to this, the mono samples are kept so a zoom draws from them (32 MB at 44.1k)
_TOP = 18.0        # title strip
_BOTTOM = 16.0     # time axis
_SIDE = 6.0
_TICK_STEPS = (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)
_DECAY_LEVEL = 0.1  # −20 dB, the level analysis.decay_ms is measured to
INLINE_MAX_SECONDS = 30.0  # above this the panel says it is reading (a 16-minute ambience took 4.7 s)
MARKER_GRAB_PX = 6.0       # a press this close to a marker grabs it
DRAG_THRESHOLD_PX = 4.0    # less movement than this is a click
MIN_SEGMENT_MS = 1         # a marker never crosses its partner
MIN_VIEW_SECONDS = 0.002   # the closest zoom: 2 ms across the plot
ZOOM_STEP = 1.25           # per wheel notch
PAN_FRACTION = 0.1         # of the view per wheel notch
SPECTRUM_FLOOR_DB = -90.0  # the spectral view's black, below the loudest bin in view
SPECTRUM_F_MIN = 30.0      # Hz, the bottom of the log-frequency axis
MODE_WAVEFORM = "waveform"
MODE_SPECTRUM = "spectrum"


@dataclass
class Envelope:
    mins: np.ndarray
    maxs: np.ndarray
    rms: np.ndarray
    duration_s: float
    peak_column: int
    samples: np.ndarray | None = None   # mono float32 — files up to KEEP_SAMPLES_SECONDS
    sample_rate: int = 0

    @property
    def columns(self) -> int:
        return int(self.mins.size)


def envelope_columns(
    path: Path | str, columns: int = OVERVIEW_COLUMNS, keep_samples_s: float = KEEP_SAMPLES_SECONDS
) -> Envelope:
    """Per-column min/max/RMS of the mono mix, at most `columns` columns —
    and, for a file up to `keep_samples_s`, the mono samples themselves."""
    with sf.SoundFile(str(path)) as handle:
        frames, sr = handle.frames, handle.samplerate
        if frames <= 0:
            return Envelope(np.zeros(0), np.zeros(0), np.zeros(0), 0.0, 0, None, sr)
        per = max(1, -(-frames // columns))
        n = -(-frames // per)
        mins = np.full(n, np.nan)
        maxs = np.full(n, np.nan)
        squares = np.zeros(n)
        counts = np.zeros(n)
        keep = frames / sr <= keep_samples_s
        kept: list[np.ndarray] = []
        col = 0
        for block in handle.blocks(blocksize=per * 64, dtype="float32", always_2d=True):
            mono = block.mean(axis=1)
            if keep:
                kept.append(mono)
            k = -(-len(mono) // per)
            padded = np.full(k * per, np.nan, dtype=np.float32)
            padded[: len(mono)] = mono
            chunk = padded.reshape(k, per)
            mins[col : col + k] = np.nanmin(chunk, axis=1)
            maxs[col : col + k] = np.nanmax(chunk, axis=1)
            squares[col : col + k] = np.nansum(chunk * chunk, axis=1)
            counts[col : col + k] = np.sum(~np.isnan(chunk), axis=1)
            col += k
    mins = np.nan_to_num(mins[:col])
    maxs = np.nan_to_num(maxs[:col])
    rms = np.sqrt(squares[:col] / np.maximum(counts[:col], 1))
    amplitude = np.maximum(np.abs(mins), np.abs(maxs))
    peak = int(np.argmax(amplitude)) if amplitude.size else 0
    samples = np.concatenate(kept).astype(np.float32) if keep and kept else None
    return Envelope(mins, maxs, rms, frames / sr, peak, samples, sr)


def peaks_for_view(env: Envelope, start_s: float, end_s: float, width_px: int):
    """Per-pixel (min, max, rms) across [start_s, end_s] of the plot — from
    the samples when they are kept, else from the overview columns. None
    when there is nothing to draw."""
    if width_px <= 0 or end_s <= start_s:
        return None
    if env.samples is not None and env.sample_rate and env.samples.size:
        y = env.samples
        edges = np.linspace(start_s * env.sample_rate, end_s * env.sample_rate, width_px + 1)
        idx = np.clip(np.floor(edges).astype(np.int64), 0, y.size)
        starts = np.minimum(idx[:-1], y.size - 1)
        counts = np.maximum(np.diff(idx), 1)
        mins = np.minimum.reduceat(y, starts)
        maxs = np.maximum.reduceat(y, starts)
        rms = np.sqrt(np.add.reduceat(y.astype(np.float64) ** 2, starts) / counts)
        return mins, maxs, rms
    if env.columns == 0 or env.duration_s <= 0:
        return None
    per_col = env.duration_s / env.columns
    edges = np.linspace(start_s / per_col, end_s / per_col, width_px + 1)
    idx = np.clip(np.floor(edges).astype(np.int64), 0, env.columns)
    starts = np.minimum(idx[:-1], env.columns - 1)
    counts = np.maximum(np.diff(idx), 1)
    mins = np.minimum.reduceat(env.mins, starts)
    maxs = np.maximum.reduceat(env.maxs, starts)
    rms = np.sqrt(np.add.reduceat(env.rms ** 2, starts) / counts)
    return mins, maxs, rms


def spectrogram_image(env: Envelope, start_s: float, end_s: float, width: int, height: int) -> np.ndarray | None:
    """An H × W × 3 uint8 image of the spectrum across [start_s, end_s]: one
    Hann-windowed FFT per pixel column (the FFT length follows the zoom, 256
    to 2048), magnitudes in dB below the loudest bin in view down to
    SPECTRUM_FLOOR_DB, rows on a log-frequency axis from SPECTRUM_F_MIN to
    the Nyquist frequency (top). None without samples in memory (a file
    beyond KEEP_SAMPLES_SECONDS)."""
    if env.samples is None or not env.sample_rate or not env.samples.size:
        return None
    if width <= 0 or height <= 0 or end_s <= start_s:
        return None
    y, sr = env.samples, env.sample_rate
    hop = (end_s - start_s) * sr / width
    n_fft = int(min(2048, max(256, 2 ** round(math.log2(max(hop * 8, 1.0))))))
    centres = (start_s * sr + (np.arange(width) + 0.5) * hop).astype(np.int64)
    idx = (centres - n_fft // 2)[:, None] + np.arange(n_fft)[None, :]
    frames = np.where((idx >= 0) & (idx < y.size), y[np.clip(idx, 0, y.size - 1)], 0.0).astype(np.float32)
    frames *= np.hanning(n_fft).astype(np.float32)
    mags = np.abs(np.fft.rfft(frames, axis=1))                       # W × (n_fft // 2 + 1)
    db = 20.0 * np.log10(mags + 1e-9)
    db -= float(db.max())
    level = np.clip((db - SPECTRUM_FLOOR_DB) / -SPECTRUM_FLOOR_DB, 0.0, 1.0)
    f_max = sr / 2.0
    f_min = min(SPECTRUM_F_MIN, f_max / 4.0)
    rows = f_min * (f_max / f_min) ** (1.0 - (np.arange(height) + 0.5) / height)   # top row = f_max
    bins = np.clip(np.round(rows / (f_max / (n_fft // 2))).astype(np.int64), 0, n_fft // 2)
    return _SPECTRUM_LUT[(level[:, bins].T * 255.0).astype(np.uint8)]  # H × W × 3


def spectrum_row(height: float, frequency_hz: float, sample_rate: int) -> float:
    """Where a frequency sits on the spectral view's axis: 0 = top (Nyquist)."""
    f_max = sample_rate / 2.0
    f_min = min(SPECTRUM_F_MIN, f_max / 4.0)
    frequency_hz = min(max(frequency_hz, f_min), f_max)
    return height * (1.0 - math.log(frequency_hz / f_min) / math.log(f_max / f_min))


def _build_spectrum_lut() -> np.ndarray:
    """256 colours from the panel ground through the accent to amber and white."""
    stops = [(0.0, BG), (0.35, ACCENT_DIM), (0.6, ACCENT), (0.85, AMBER), (1.0, WHITE)]
    xs = np.array([p for p, _ in stops])
    lut = np.zeros((256, 3), dtype=np.uint8)
    t = np.linspace(0.0, 1.0, 256)
    for channel, pick in enumerate((QColor.red, QColor.green, QColor.blue)):
        lut[:, channel] = np.interp(t, xs, [pick(c) for _, c in stops]).astype(np.uint8)
    return lut


_SPECTRUM_LUT = _build_spectrum_lut()


def _alpha(colour: QColor, alpha: int) -> QColor:
    out = QColor(colour)
    out.setAlpha(alpha)
    return out


def _tick_step(duration_s: float, width_px: float, min_px: float = 70.0) -> float:
    for step in _TICK_STEPS:
        if duration_s <= 0 or width_px * step / duration_s >= min_px:
            return step
    return _TICK_STEPS[-1]


def _tick_label(t: float, step: float) -> str:
    if step < 0.01:
        return f"{t:.3f}"
    if step < 1:
        return f"{t:.2f}"
    if step < 60:
        return f"{t:.0f}"
    return f"{int(t // 60)}:{int(t % 60):02d}"


class _EnvelopeThread(QThread):
    done = Signal(int, object)          # generation, Envelope
    failed = Signal(int, str)

    def __init__(self, generation: int, path: Path | str, parent=None) -> None:
        super().__init__(parent)
        self._generation = generation
        self._path = path

    def run(self) -> None:  # worker thread
        try:
            self.done.emit(self._generation, envelope_columns(self._path))
        except Exception as exc:  # noqa: BLE001 - shown in the panel
            self.failed.emit(self._generation, f"{type(exc).__name__}: {exc}")


class WaveformView(QWidget):
    segment_clicked = Signal(int)      # a click inside a segment's span
    position_clicked = Signal(int)     # a click elsewhere: milliseconds
    staged_changed = Signal()          # unsaved markers came, went or moved
    selection_changed = Signal(object) # the selected segment id, or None
    delete_requested = Signal(int)     # Del on the selected segment
    view_changed = Signal()            # zoomed, panned, fitted or loaded
    mode_changed = Signal(str)         # MODE_WAVEFORM | MODE_SPECTRUM

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.setMouseTracking(True)                     # the cursor says when a marker is under it
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # Esc / Del / Home after a click on the plot
        self._env: Envelope | None = None
        self._title = ""
        self._error: str | None = None
        self._segments: list[SegmentRow] = []
        self._windows: list[SegmentRow] = []      # the CLAP windows of a long file (§6.4)
        self._selected_segment: int | None = None
        self._attack_ms: float | None = None
        self._decay_ms: float | None = None
        self._position_ms: int | None = None
        self._generation = 0
        self._thread: _EnvelopeThread | None = None
        self._edits: dict[int, tuple[int, int]] = {}    # segment id → staged (start_ms, end_ms), unsaved
        self._drafts: list[tuple[int, int]] = []         # segments drawn by hand, unsaved
        self._drag: tuple | None = None                  # ("move", key, edge) | ("press", ms, x)
        self._view: tuple[float, float] = (0.0, 0.0)     # the seconds across the plot
        self._layer: QPixmap | None = None               # the rasterised waveform body (or the spectrum)
        self._layer_key: tuple | None = None
        self._mode = MODE_WAVEFORM

    # --- data in ---

    def load(
        self,
        path: Path | str,
        title: str,
        segments: list[SegmentRow],
        attack_ms: float | None,
        decay_ms: float | None,
        windows: list[SegmentRow] = (),
    ) -> None:
        """Show `path`: the audio is read on a thread — the preview never
        waits for the graphics — and lands through `_on_envelope_ready`."""
        self._title = title
        self._segments = list(segments)
        self._windows = list(windows)
        self._attack_ms = attack_ms
        self._decay_ms = decay_ms
        self._selected_segment = None
        self._position_ms = None
        self._reset_staging()                       # another sample: an unsaved drag is dropped
        self.selection_changed.emit(None)
        self._generation += 1
        self._env = None
        self._layer = None
        self._view = (0.0, 0.0)
        try:
            seconds = sf.info(str(path)).duration
        except Exception as exc:  # noqa: BLE001 - an unreadable file is shown, not raised
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("waveform unavailable for %s: %s", path, exc)
            self.update()
            return
        self._error = f"reading {seconds / 60:.1f} min of audio…" if seconds > INLINE_MAX_SECONDS else None
        self.update()
        thread = _EnvelopeThread(self._generation, path, self)
        thread.done.connect(self._on_envelope_ready)
        thread.failed.connect(self._on_envelope_failed)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        thread.start()

    def _on_envelope_ready(self, generation: int, env: Envelope) -> None:
        if generation != self._generation:
            return
        self._env = env
        self._error = None
        self._thread = None
        self._view = (0.0, env.duration_s)
        self._layer = None
        self.view_changed.emit()
        self.update()

    def _on_envelope_failed(self, generation: int, error: str) -> None:
        if generation != self._generation:
            return
        self._error = error
        self._thread = None
        self.update()

    @property
    def loading(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def wait_for_load(self, timeout_ms: int = 60_000, deliver: bool = True) -> None:
        """Block until the threaded read lands; with `deliver`, hand the
        result over too (tests). The window's close passes False."""
        if self._thread is not None:
            self._thread.wait(timeout_ms)
            if deliver:
                QCoreApplication.processEvents()

    def clear(self) -> None:
        self._generation += 1
        self._env = None
        self._layer = None
        self._title = ""
        self._error = None
        self._segments = []
        self._windows = []
        self._selected_segment = None
        self._position_ms = None
        self._view = (0.0, 0.0)
        self._reset_staging()
        self.selection_changed.emit(None)
        self.view_changed.emit()
        self.update()

    def set_segments(self, segments: list[SegmentRow]) -> None:
        self._segments = list(segments)
        self._reset_staging()
        self.selection_changed.emit(self._selected_segment)
        self.update()

    def set_selected_segment(self, segment_id: int | None) -> None:
        if segment_id != self._selected_segment:
            self._selected_segment = segment_id
            self.selection_changed.emit(segment_id)
            self.update()

    def set_position_ms(self, position_ms: int | None) -> None:
        if position_ms != self._position_ms:
            self._position_ms = position_ms
            self.update()

    @property
    def loaded(self) -> bool:
        return self._env is not None

    @property
    def duration_s(self) -> float:
        return self._env.duration_s if self._env is not None else 0.0

    # --- waveform or spectrum (2026-09-09) ---

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """MODE_WAVEFORM or MODE_SPECTRUM; the overlays, zoom and markers are the same."""
        mode = MODE_SPECTRUM if mode == MODE_SPECTRUM else MODE_WAVEFORM
        if mode != self._mode:
            self._mode = mode
            self._layer = None
            self.mode_changed.emit(mode)
            self.update()

    @property
    def spectrum_available(self) -> bool:
        """The spectral view needs the samples in memory (files up to KEEP_SAMPLES_SECONDS)."""
        return self._env is not None and self._env.samples is not None and bool(self._env.samples.size)

    # --- the view: zoom and pan (2026-09-08) ---

    @property
    def view(self) -> tuple[float, float]:
        """The seconds across the plot: (start, end)."""
        return self._view

    @property
    def zoomed(self) -> bool:
        return self._env is not None and (self._view[1] - self._view[0]) < self._env.duration_s - 1e-9

    def set_view(self, start_s: float, end_s: float) -> None:
        """Show [start_s, end_s]: clipped to the file, never narrower than
        MIN_VIEW_SECONDS."""
        duration = self.duration_s
        if duration <= 0:
            return
        span = min(max(end_s - start_s, MIN_VIEW_SECONDS), duration)
        start = min(max(start_s, 0.0), duration - span)
        view = (start, start + span)
        if view != self._view:
            self._view = view
            self.view_changed.emit()
            self.update()

    def zoom(self, factor: float, about_s: float | None = None) -> None:
        """Zoom by `factor` (> 1 in) keeping `about_s` where it is."""
        start, end = self._view
        if end <= start:
            return
        about = (start + end) / 2 if about_s is None else about_s
        fraction = (about - start) / (end - start)
        span = (end - start) / factor
        self.set_view(about - fraction * span, about - fraction * span + span)

    def pan(self, delta_s: float) -> None:
        start, end = self._view
        self.set_view(start + delta_s, end + delta_s)

    def fit(self) -> None:
        self.set_view(0.0, self.duration_s)

    # --- geometry ---

    def _plot_rect(self) -> QRectF:
        return QRectF(_SIDE, _TOP, max(self.width() - 2 * _SIDE, 1.0), max(self.height() - _TOP - _BOTTOM, 1.0))

    def _x_of(self, seconds: float, rect: QRectF) -> float:
        start, end = self._view
        if self._env is None or end <= start:
            return rect.left()
        return rect.left() + rect.width() * (seconds - start) / (end - start)

    def time_at_x(self, x: float) -> float:
        """Seconds at a widget x — the inverse of `_x_of`, for clicks;
        clamped to the view."""
        rect = self._plot_rect()
        start, end = self._view
        if self._env is None or rect.width() <= 0 or end <= start:
            return 0.0
        return float(start + np.clip((x - rect.left()) / rect.width(), 0.0, 1.0) * (end - start))

    def segment_at(self, seconds: float) -> SegmentRow | None:
        """The shortest segment spanning `seconds` (nested manual ones win)."""
        ms = seconds * 1000
        inside = [s for s in self._segments if s.start_ms <= ms <= s.end_ms]
        return min(inside, key=lambda s: s.length_ms) if inside else None

    # --- painting ---

    def _layer_for(self, rect: QRectF) -> QPixmap | None:
        """The waveform body for the current view, rasterised once per
        (file, view, size, scale) and reused by every repaint after."""
        if self._env is None:
            return None
        dpr = float(self.devicePixelRatioF())
        width = max(1, int(round(rect.width() * dpr)))
        height = max(1, int(round(rect.height() * dpr)))
        key = (self._generation, self._view, width, height, dpr, self._mode)
        if self._layer is not None and self._layer_key == key:
            return self._layer
        if self._mode == MODE_SPECTRUM:
            pixels = spectrogram_image(self._env, self._view[0], self._view[1], width, height)
            if pixels is None:
                self._layer, self._layer_key = None, key
                return None
            data = np.ascontiguousarray(pixels).tobytes()
            image = QImage(data, width, height, width * 3, QImage.Format.Format_RGB888).copy()
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(dpr)
            self._layer, self._layer_key = pixmap, key
            return pixmap
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        peaks = peaks_for_view(self._env, self._view[0], self._view[1], width)
        if peaks is not None:
            mins, maxs, rms = peaks
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            mid = height / 2
            half = max(height / 2 - 2 * dpr, 1.0)
            xs = np.arange(width) + 0.5
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_alpha(ACCENT, 110))
            painter.drawPath(_band_path(xs, mid - maxs * half, mid - mins * half))
            painter.setBrush(_alpha(ACCENT, 210))
            painter.drawPath(_band_path(xs, mid - rms * half, mid + rms * half))
            painter.end()
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(dpr)
        self._layer, self._layer_key = pixmap, key
        return pixmap

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BG)
        rect = self._plot_rect()
        painter.setPen(TEXT)
        painter.drawText(QRectF(_SIDE, 2, self.width() - 2 * _SIDE, _TOP - 2), Qt.AlignmentFlag.AlignLeft, self._header_text())
        if self._env is None:
            painter.setPen(TEXT_DIM)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._error or ("" if self._title else "select a sample to see its waveform"))
            painter.end()
            return
        mid = rect.center().y()
        half = rect.height() / 2 - 2
        painter.setPen(QPen(BORDER, 1))
        painter.drawLine(QPointF(rect.left(), mid), QPointF(rect.right(), mid))

        painter.save()
        painter.setClipRect(rect)
        if self._mode == MODE_SPECTRUM:
            # the spectrum is opaque: it goes under the segment fills; no envelope
            layer = self._layer_for(rect)
            if layer is not None:
                painter.drawPixmap(rect.topLeft(), layer)
                self._paint_frequency_ticks(painter, rect)
            else:
                painter.setPen(TEXT_DIM)
                painter.drawText(
                    rect, Qt.AlignmentFlag.AlignCenter,
                    f"the spectrum needs the audio in memory: files up to {KEEP_SAMPLES_SECONDS / 60:.0f} minutes",
                )
            self._paint_windows(painter, rect)
            self._paint_segments(painter, rect)
        else:
            self._paint_windows(painter, rect)
            self._paint_segments(painter, rect)
            layer = self._layer_for(rect)
            if layer is not None:
                painter.drawPixmap(rect.topLeft(), layer)
            self._paint_envelope(painter, rect, mid, half)
        if self._position_ms is not None:
            x = self._x_of(self._position_ms / 1000, rect)
            painter.setPen(QPen(WHITE, 1.5))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        painter.restore()
        self._paint_axis(painter, rect)
        painter.end()

    def _header_text(self) -> str:
        if self._env is None:
            return self._title
        parts = [f"{self._title}  ·  {self._env.duration_s:.3f} s"]
        if self._attack_ms is not None:
            parts.append(f"attack {self._attack_ms:.0f} ms")
        if self._decay_ms is not None:
            parts.append(f"decay {self._decay_ms:.0f} ms (to −20 dB)")
        if self._segments:
            manual = sum(1 for s in self._segments if s.detection_method == "manual")
            parts.append(f"{len(self._segments)} segments" + (f" ({manual} manual)" if manual else ""))
        if self.has_staged:
            parts.append(f"{len(self._edits) + len(self._drafts)} unsaved")
        if self._windows:
            parts.append(f"{len(self._windows)} CLAP windows")
        if self.zoomed:
            start, end = self._view
            parts.append(f"zoom ×{self._env.duration_s / (end - start):.0f} ({start:.3f}–{end:.3f} s)")
        if self._mode == MODE_SPECTRUM:
            parts.append("spectrum")
        return "  ·  ".join(parts)

    def _paint_windows(self, painter: QPainter, rect: QRectF) -> None:
        """The CLAP windows as a thin strip along the bottom of the plot —
        where the model looked, not slices anyone chose — and the selected
        one (a hit) as a band across the full height."""
        strip = 5.0
        for win in self._windows:
            x0 = self._x_of(win.start_ms / 1000, rect)
            x1 = self._x_of(win.end_ms / 1000, rect)
            if x1 < rect.left() or x0 > rect.right():
                continue
            selected = win.id == self._selected_segment
            painter.setPen(Qt.PenStyle.NoPen)
            if selected:
                painter.setBrush(_alpha(ACCENT, 45))
                painter.drawRect(QRectF(x0, rect.top(), max(x1 - x0, 1.0), rect.height()))
                painter.setPen(QPen(ACCENT, 2.0))
                painter.drawLine(QPointF(x0, rect.top()), QPointF(x0, rect.bottom()))
                painter.drawLine(QPointF(x1, rect.top()), QPointF(x1, rect.bottom()))
                if x1 - x0 > 50:
                    painter.setPen(ACCENT)
                    painter.drawText(
                        QRectF(x0 + 2, rect.top() + 1, x1 - x0 - 4, 14), Qt.AlignmentFlag.AlignRight, "window"
                    )
                painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ACCENT if selected else _alpha(ACCENT, 70))
            painter.drawRect(QRectF(x0 + 0.5, rect.bottom() - strip, max(x1 - x0 - 1.0, 1.0), strip))

    def _paint_segments(self, painter: QPainter, rect: QRectF) -> None:
        """Every segment at its shown bounds — staged ones dashed and marked
        "unsaved", drafts (drawn, not yet saved) green and marked "new"."""
        for seg in self._segments:
            start_ms, end_ms = self.bounds_of(seg.id)
            staged = seg.id in self._edits
            manual = seg.detection_method == "manual" or staged   # a moved marker makes it manual on save (§6.3)
            label = f"{seg.strength:.2f}" if seg.strength is not None else ("manual" if manual else "")
            if seg.needs_review:
                label = "review!"
            if staged:
                label = "unsaved"
            self._paint_span(
                painter, rect, start_ms, end_ms, GREEN if manual else AMBER,
                selected=seg.id == self._selected_segment, dashed=staged, label=label,
            )
        for start_ms, end_ms in self._drafts:
            self._paint_span(painter, rect, start_ms, end_ms, GREEN, selected=False, dashed=True, label="new")

    def _paint_span(
        self, painter: QPainter, rect: QRectF, start_ms: int, end_ms: int, colour: QColor,
        selected: bool, dashed: bool, label: str,
    ) -> None:
        x0 = self._x_of(start_ms / 1000, rect)
        x1 = self._x_of(end_ms / 1000, rect)
        if x1 < rect.left() or x0 > rect.right():
            return
        strong, weak = (80, 40) if colour is GREEN else (90, 45)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_alpha(colour, strong if selected else weak))
        painter.drawRect(QRectF(x0, rect.top(), max(x1 - x0, 1.0), rect.height()))
        pen = QPen(colour, 2.0 if (selected or dashed) else 1.0)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(x0, rect.top()), QPointF(x0, rect.bottom()))
        painter.drawLine(QPointF(x1, rect.top()), QPointF(x1, rect.bottom()))
        left = max(x0, rect.left())
        if label and min(x1, rect.right()) - left > painter.fontMetrics().horizontalAdvance(label) + 6:
            painter.setPen(colour)
            painter.drawText(QRectF(left + 2, rect.top() + 1, x1 - left - 4, 14), Qt.AlignmentFlag.AlignLeft, label)

    def _paint_frequency_ticks(self, painter: QPainter, rect: QRectF) -> None:
        """100 Hz, 1 kHz and 10 kHz on the spectral view's log axis, at the left edge."""
        if self._env is None or not self._env.sample_rate:
            return
        painter.setPen(TEXT_DIM)
        for hz, label in ((100.0, "100"), (1000.0, "1k"), (10000.0, "10k")):
            if hz >= self._env.sample_rate / 2.0:
                continue
            y = rect.top() + spectrum_row(rect.height(), hz, self._env.sample_rate)
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.left() + 6, y))
            painter.drawText(QRectF(rect.left() + 8, y - 7, 40, 14), Qt.AlignmentFlag.AlignLeft, label)

    def _paint_envelope(self, painter: QPainter, rect: QRectF, mid: float, half: float) -> None:
        if self._env is None or self._attack_ms is None or self._env.columns == 0:
            return
        peak_t = (self._env.peak_column + 0.5) / self._env.columns * self._env.duration_s
        start_t = max(0.0, peak_t - self._attack_ms / 1000)
        points = [QPointF(self._x_of(start_t, rect), mid), QPointF(self._x_of(peak_t, rect), mid - half)]
        if self._decay_ms is not None:
            end_t = min(self._env.duration_s, peak_t + self._decay_ms / 1000)
            points.append(QPointF(self._x_of(end_t, rect), mid - half * _DECAY_LEVEL))
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        painter.setPen(QPen(PINK, 1.5, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    def _paint_axis(self, painter: QPainter, rect: QRectF) -> None:
        if self._env is None or self._env.duration_s <= 0:
            return
        start, end = self._view
        step = _tick_step(end - start, rect.width())
        painter.setPen(TEXT_DIM)
        t = math.ceil(start / step - 1e-9) * step
        while t <= end + 1e-9:
            x = self._x_of(t, rect)
            painter.drawLine(QPointF(x, rect.bottom()), QPointF(x, rect.bottom() + 4))
            painter.drawText(QRectF(x - 30, rect.bottom() + 3, 60, 13), Qt.AlignmentFlag.AlignHCenter, _tick_label(t, step))
            t += step

    # --- staged edits (§9.2: staged on drag, committed only via Save) ---

    def bounds_of(self, key: int) -> tuple[int, int]:
        """A segment's bounds as shown: staged if moved, else stored. Keys ≥ 1
        are segment ids; negative keys are drafts (-1 = the first drawn)."""
        if key < 0:
            return self._drafts[-key - 1]
        if key in self._edits:
            return self._edits[key]
        seg = next((s for s in self._segments if s.id == key), None)
        if seg is None:
            raise LookupError(f"no segment {key} on the waveform")
        return seg.start_ms, seg.end_ms

    def stage_edit(self, segment_id: int, start_ms: int, end_ms: int) -> None:
        """Stage new bounds for a segment (nothing is written). Bounds back
        where the stored ones are leave nothing to save."""
        seg = next((s for s in self._segments if s.id == segment_id), None)
        if seg is None:
            raise LookupError(f"no segment {segment_id} on the waveform")
        bounds = self._ordered(start_ms, end_ms)
        if bounds == (seg.start_ms, seg.end_ms):
            self._edits.pop(segment_id, None)
        else:
            self._edits[segment_id] = bounds
        self.staged_changed.emit()
        self.update()

    def add_draft(self, start_ms: int, end_ms: int) -> int:
        """Stage a new segment drawn by hand; returns its key (negative)."""
        self._drafts.append(self._ordered(start_ms, end_ms))
        self.staged_changed.emit()
        self.update()
        return -len(self._drafts)

    def staged(self) -> list[tuple[int | None, int, int]]:
        """What Save would write: (segment id or None for a new one, start_ms, end_ms)."""
        return [(sid, s, e) for sid, (s, e) in self._edits.items()] + [(None, s, e) for s, e in self._drafts]

    @property
    def has_staged(self) -> bool:
        return bool(self._edits or self._drafts)

    def discard(self) -> None:
        """Drop every unsaved marker (the Discard button, Esc)."""
        if self._reset_staging():
            self.update()

    def _reset_staging(self) -> bool:
        had = self.has_staged
        self._edits.clear()
        self._drafts.clear()
        self._drag = None
        if had:
            self.staged_changed.emit()
        return had

    def _ordered(self, a: int, b: int) -> tuple[int, int]:
        """Bounds in order, inside the file, at least MIN_SEGMENT_MS long."""
        lo, hi = (int(a), int(b)) if a <= b else (int(b), int(a))
        lo = max(0, lo)
        hi = max(lo + MIN_SEGMENT_MS, hi)
        if self._env is not None and self._env.duration_s > 0:
            limit = int(round(self._env.duration_s * 1000))
            hi = min(hi, max(limit, MIN_SEGMENT_MS))
            lo = min(lo, hi - MIN_SEGMENT_MS)
        return lo, hi

    @property
    def selected_segment(self) -> int | None:
        return self._selected_segment

    @property
    def can_delete(self) -> bool:
        """A segment is selected — a CLAP window (§6.4) is not deletable."""
        return any(s.id == self._selected_segment for s in self._segments)

    def marker_at(self, x: float) -> tuple[int, str] | None:
        """The marker within MARKER_GRAB_PX of `x`, the nearest: (key,
        "start" | "end"); None if none. Windows have no markers to move."""
        if self._env is None:
            return None
        rect = self._plot_rect()
        best: tuple[int, str] | None = None
        best_d = MARKER_GRAB_PX + 1e-9
        keys = [s.id for s in self._segments] + [-(i + 1) for i in range(len(self._drafts))]
        for key in keys:
            start_ms, end_ms = self.bounds_of(key)
            for edge, ms in (("start", start_ms), ("end", end_ms)):
                d = abs(x - self._x_of(ms / 1000, rect))
                if d < best_d:
                    best, best_d = (key, edge), d
        return best

    def _ms_at_x(self, x: float) -> int:
        return int(round(self.time_at_x(x) * 1000))

    def _move_marker(self, key: int, edge: str, ms: int) -> None:
        start_ms, end_ms = self.bounds_of(key)
        if edge == "start":
            start_ms = min(ms, end_ms - MIN_SEGMENT_MS)
        else:
            end_ms = max(ms, start_ms + MIN_SEGMENT_MS)
        if key < 0:
            self._drafts[-key - 1] = self._ordered(start_ms, end_ms)
            self.staged_changed.emit()
            self.update()
        else:
            self.stage_edit(key, start_ms, end_ms)

    # --- interaction ---

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._env is None:
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.fit()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        marker = self.marker_at(x)
        if marker is not None:
            self._drag = ("move", *marker)
        else:
            self._drag = ("press", self._ms_at_x(x), x)      # a click, unless it moves

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        x = event.position().x()
        if self._drag is None:
            self.setCursor(
                Qt.CursorShape.SizeHorCursor if self.marker_at(x) is not None else Qt.CursorShape.ArrowCursor
            )
            return
        if self._drag[0] == "press":
            _, anchor_ms, press_x = self._drag
            if abs(x - press_x) < DRAG_THRESHOLD_PX:
                return
            # Drawing a new segment from the press point, in either direction:
            # the far edge follows the mouse from here on.
            ms = self._ms_at_x(x)
            key = self.add_draft(min(anchor_ms, ms), max(anchor_ms, ms))
            self._drag = ("move", key, "start" if x < press_x else "end")
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            return
        _, key, edge = self._drag
        self._move_marker(key, edge, self._ms_at_x(x))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._drag is None:
            return
        drag, self._drag = self._drag, None
        if drag[0] == "press":                              # it never moved: the click it was
            ms = drag[1]
            segment = self.segment_at(ms / 1000)
            if segment is not None:
                self.segment_clicked.emit(segment.id)
            else:
                self.position_clicked.emit(ms)
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        """The wheel zooms about the cursor; Shift+wheel or a horizontal
        wheel pans by a tenth of the view per notch."""
        if self._env is None:
            return
        delta = event.angleDelta()
        horizontal = bool(delta.x()) and not delta.y()
        if horizontal or event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            notches = (delta.x() if horizontal else delta.y()) / 120.0
            start, end = self._view
            self.pan(-notches * (end - start) * PAN_FRACTION)
        elif delta.y():
            self.zoom(ZOOM_STEP ** (delta.y() / 120.0), self.time_at_x(event.position().x()))
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.has_staged:
            self.discard()
        elif event.key() == Qt.Key.Key_Delete and self.can_delete:
            self.delete_requested.emit(self._selected_segment)
        elif event.key() == Qt.Key.Key_Home:
            self.fit()
        else:
            super().keyPressEvent(event)


def _band_path(xs: np.ndarray, top: np.ndarray, bottom: np.ndarray) -> QPainterPath:
    """A closed path along `top` left to right and `bottom` back."""
    path = QPainterPath()
    path.moveTo(float(xs[0]), float(top[0]))
    for x, y in zip(xs[1:].tolist(), top[1:].tolist()):
        path.lineTo(x, y)
    for x, y in zip(xs[::-1].tolist(), bottom[::-1].tolist()):
        path.lineTo(x, y)
    path.closeSubpath()
    return path


class WaveformPanel(QWidget):
    """The bottom panel: the caption line on top (the selected sample's
    Qwen2-Audio sentence, "—" without one, and its Caption / Recaption
    button), the view, its pan scrollbar, and the button row: play, stop,
    the segment buttons, auto-play (§9.2).
    *Save segment* writes the staged markers, *Discard* drops them, *Delete
    segment* removes the selected one; the window does the writing."""

    save_requested = Signal()
    delete_requested = Signal(int)     # the selected segment's id
    caption_requested = Signal()
    play_requested = Signal()
    stop_requested = Signal()
    mode_changed = Signal(str)         # the view's mode, for the window to remember

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.view = WaveformView()

        # the caption line (2026-09-08, the user's steer: at the top of the waveform)
        self._caption_label = ElidedLabel("—")
        self._caption_label.setToolTip(
            "One sentence from Qwen2-Audio (§5.2) — written by the Recompute tab's Captions "
            "step (a batch per Run) or by the button next to it (this sample only), about "
            "10 s per file. “—” = none yet."
        )
        self._caption_button = QPushButton("Caption")
        self._caption_button.setToolTip(
            "Write (or rewrite) this sample's Qwen2-Audio sentence now — about 10 s, plus a few "
            "seconds the first time while the model loads. Runs as a job on the Recompute tab."
        )
        self._caption_button.setEnabled(False)
        self._caption_button.clicked.connect(self.caption_requested)
        caption_row = QHBoxLayout()
        caption_row.setContentsMargins(6, 2, 4, 0)
        caption_row.setSpacing(6)
        caption_row.addWidget(self._caption_label, stretch=1)
        caption_row.addWidget(self._caption_button)

        self._scroll = QScrollBar(Qt.Orientation.Horizontal)
        self._scroll.setToolTip("Pan the zoomed waveform (Shift+wheel does too; right-click or Home fits the file).")
        self._scroll.setEnabled(False)
        self._syncing = False
        self._scroll.valueChanged.connect(self._on_scroll)
        self.view.view_changed.connect(self._sync_scroll)

        # play / stop / auto-play, here since 2026-09-08 (the user's steer: the transport row went)
        self.play_button = make_button(
            icon_name="play", intent="primary", tooltip="Play the selected sample or hit (Space)"
        )
        self.play_button.clicked.connect(self.play_requested)
        self.stop_button = make_button(icon_name="stop", tooltip="Stop")
        self.stop_button.clicked.connect(self.stop_requested)
        self.autoplay = QCheckBox("Auto-play on select")
        self.mode_button = make_button(               # the spectral view (2026-09-09)
            "Spectrum", icon_name="spectrum", checkable=True
        )
        self.mode_button.setToolTip(
            "Show the spectrum instead of the waveform: one FFT per pixel column over the view, "
            "log frequency from 30 Hz to half the sample rate, the loudest bin in view white. "
            "Files up to three minutes; zoom, markers and the playhead work the same."
        )
        self.mode_button.toggled.connect(
            lambda on: self.view.set_mode(MODE_SPECTRUM if on else MODE_WAVEFORM)
        )
        self.view.mode_changed.connect(self._on_mode_changed)
        self._save = make_button(
            "Save segment",
            icon_name="save",
            tooltip=(
                "Write the moved or drawn markers to the index as manual segments (§6.3): exempt "
                "from the automatic length and cap rules, never overwritten by a recompute."
            ),
        )
        self._discard = make_button(
            "Discard", icon_name="close", intent="quiet", tooltip="Drop the unsaved markers (Esc)."
        )
        self._delete = make_button(
            "Delete segment",
            icon_name="trash",
            intent="danger",
            tooltip="Remove the selected segment, automatic or manual, from the index (Del).",
        )
        self.view.setToolTip(
            "Drag a marker to move it; drag on the waveform to draw a segment — nothing is written "
            "until Save. Wheel zooms, Shift+wheel pans, right-click fits."
        )
        # The row as three groups rather than six self-sized buttons in one run
        # (2026-09-10, layer 2, item 5 of the diagnosis): transport, then the
        # view toggle, then the segment edits — where Delete is `danger` and so
        # no longer looks exactly like Spectrum. `Toolbar` squeezes with the
        # pane the way `SqueezableWidget` did, so the splitter is unaffected.
        buttons = Toolbar()
        buttons.add_group(self.play_button, self.stop_button, equal_width=False)
        buttons.add_group(self.mode_button)
        buttons.add_group(self._save, self._discard, self._delete)
        buttons.add_stretch()
        buttons.add_widget(self.autoplay)
        self._buttons = buttons
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(caption_row)
        layout.addWidget(self.view, stretch=1)
        layout.addWidget(self._scroll)
        layout.addWidget(buttons)
        self._save.clicked.connect(self.save_requested)
        self._discard.clicked.connect(self.view.discard)
        self._delete.clicked.connect(self._emit_delete)
        self.view.staged_changed.connect(self._refresh)
        self.view.selection_changed.connect(self._refresh)
        self.view.delete_requested.connect(self.delete_requested)
        self._refresh()

    # --- the caption line ---

    def set_caption(self, text: str | None, can_caption: bool = True) -> None:
        """The selected sample's sentence — "—" without one — and the button
        that writes or rewrites it."""
        self._caption_label.setText(text if text else "—")
        self._caption_button.setText("Recaption" if text else "Caption")
        self._caption_button.setEnabled(can_caption)

    @property
    def caption_text(self) -> str:
        return self._caption_label.text()

    # --- waveform or spectrum ---

    def _on_mode_changed(self, mode: str) -> None:
        self.mode_button.setChecked(mode == MODE_SPECTRUM)
        self.mode_changed.emit(mode)

    # --- the scrollbar ---

    def _sync_scroll(self) -> None:
        """The scrollbar follows the view (in milliseconds)."""
        start, end = self.view.view
        duration = self.view.duration_s
        self._syncing = True
        try:
            if duration <= 0 or end - start >= duration - 1e-9:
                self._scroll.setRange(0, 0)
                self._scroll.setEnabled(False)
            else:
                span_ms = int(round((end - start) * 1000))
                self._scroll.setRange(0, max(0, int(round(duration * 1000)) - span_ms))
                self._scroll.setPageStep(span_ms)
                self._scroll.setSingleStep(max(1, span_ms // 10))
                self._scroll.setValue(int(round(start * 1000)))
                self._scroll.setEnabled(True)
        finally:
            self._syncing = False

    def _on_scroll(self, value_ms: int) -> None:
        if self._syncing:
            return
        start, end = self.view.view
        self.view.set_view(value_ms / 1000, value_ms / 1000 + (end - start))

    # --- the buttons ---

    def _emit_delete(self) -> None:
        if self.view.can_delete:
            self.delete_requested.emit(self.view.selected_segment)

    def _refresh(self, *_args) -> None:
        n = len(self.view.staged())
        self._save.setEnabled(n > 0)
        self._save.setText("Save segment" if n <= 1 else f"Save {n} segments")
        self._discard.setEnabled(n > 0)
        self._delete.setEnabled(self.view.can_delete)
        self._buttons.equalise()      # Save carries a count now; keep the group level
