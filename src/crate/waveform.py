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
class below holds the view and the buttons.

The envelope is read in blocks (`soundfile`), so a 16-minute ambience costs
one pass over the file and never more memory than one block.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .catalog import SegmentRow
from .theme import ACCENT, AMBER, BG, BORDER, GREEN, PINK, TEXT, TEXT_DIM, WHITE, ElidedLabel

log = logging.getLogger(__name__)

DEFAULT_COLUMNS = 1200
_TOP = 18.0        # title strip
_BOTTOM = 16.0     # time axis
_SIDE = 6.0
_TICK_STEPS = (0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)
_DECAY_LEVEL = 0.1  # −20 dB, the level analysis.decay_ms is measured to
INLINE_MAX_SECONDS = 30.0  # longer files read on a thread: a 16-minute ambience took 4.7 s
MARKER_GRAB_PX = 6.0       # a press this close to a marker grabs it
DRAG_THRESHOLD_PX = 4.0    # less movement than this is a click
MIN_SEGMENT_MS = 1         # a marker never crosses its partner


@dataclass
class Envelope:
    mins: np.ndarray
    maxs: np.ndarray
    duration_s: float
    peak_column: int

    @property
    def columns(self) -> int:
        return int(self.mins.size)


def envelope_columns(path: Path | str, columns: int = DEFAULT_COLUMNS) -> Envelope:
    """Per-column min/max of the mono mix, at most `columns` columns."""
    with sf.SoundFile(str(path)) as handle:
        frames, sr = handle.frames, handle.samplerate
        if frames <= 0:
            return Envelope(np.zeros(0), np.zeros(0), 0.0, 0)
        per = max(1, -(-frames // columns))
        n = -(-frames // per)
        mins = np.full(n, np.nan)
        maxs = np.full(n, np.nan)
        col = 0
        for block in handle.blocks(blocksize=per * 64, dtype="float32", always_2d=True):
            mono = block.mean(axis=1)
            k = -(-len(mono) // per)
            padded = np.full(k * per, np.nan, dtype=np.float32)
            padded[: len(mono)] = mono
            chunk = padded.reshape(k, per)
            mins[col : col + k] = np.nanmin(chunk, axis=1)
            maxs[col : col + k] = np.nanmax(chunk, axis=1)
            col += k
    mins = np.nan_to_num(mins[:col])
    maxs = np.nan_to_num(maxs[:col])
    amplitude = np.maximum(np.abs(mins), np.abs(maxs))
    peak = int(np.argmax(amplitude)) if amplitude.size else 0
    return Envelope(mins, maxs, frames / sr, peak)


def _alpha(colour: QColor, alpha: int) -> QColor:
    out = QColor(colour)
    out.setAlpha(alpha)
    return out


def _tick_step(duration_s: float, width_px: float, min_px: float = 70.0) -> float:
    for step in _TICK_STEPS:
        if duration_s <= 0 or width_px * step / duration_s >= min_px:
            return step
    return _TICK_STEPS[-1]


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

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.setMouseTracking(True)                     # the cursor says when a marker is under it
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # Esc / Del after a click on the plot
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
        try:
            seconds = sf.info(str(path)).duration
        except Exception as exc:  # noqa: BLE001 - an unreadable file is shown, not raised
            self._env = None
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("waveform unavailable for %s: %s", path, exc)
            self.update()
            return
        if seconds <= INLINE_MAX_SECONDS:
            try:
                self._env = envelope_columns(path)
                self._error = None
            except Exception as exc:  # noqa: BLE001
                self._env = None
                self._error = f"{type(exc).__name__}: {exc}"
                log.warning("waveform unavailable for %s: %s", path, exc)
            self.update()
            return
        # A long file: read it on a thread and show it when it lands; a newer
        # load in the meantime wins (generation counter).
        self._env = None
        self._error = f"reading {seconds / 60:.1f} min of audio…"
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

    def wait_for_load(self, timeout_ms: int = 60_000) -> None:
        """Block until a threaded read lands (tests; window close)."""
        if self._thread is not None:
            self._thread.wait(timeout_ms)

    def clear(self) -> None:
        self._generation += 1
        self._env = None
        self._title = ""
        self._error = None
        self._segments = []
        self._windows = []
        self._selected_segment = None
        self._position_ms = None
        self._reset_staging()
        self.selection_changed.emit(None)
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

    # --- geometry ---

    def _plot_rect(self) -> QRectF:
        return QRectF(_SIDE, _TOP, max(self.width() - 2 * _SIDE, 1.0), max(self.height() - _TOP - _BOTTOM, 1.0))

    def _x_of(self, seconds: float, rect: QRectF) -> float:
        if self._env is None or self._env.duration_s <= 0:
            return rect.left()
        return rect.left() + rect.width() * seconds / self._env.duration_s

    def time_at_x(self, x: float) -> float:
        """Seconds at a widget x — the inverse of `_x_of`, for clicks."""
        rect = self._plot_rect()
        if self._env is None or rect.width() <= 0:
            return 0.0
        return float(np.clip((x - rect.left()) / rect.width(), 0.0, 1.0) * self._env.duration_s)

    def segment_at(self, seconds: float) -> SegmentRow | None:
        """The shortest segment spanning `seconds` (nested manual ones win)."""
        ms = seconds * 1000
        inside = [s for s in self._segments if s.start_ms <= ms <= s.end_ms]
        return min(inside, key=lambda s: s.length_ms) if inside else None

    # --- painting ---

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BG)
        rect = self._plot_rect()
        painter.setPen(TEXT)
        painter.drawText(QRectF(_SIDE, 2, self.width() - 2 * _SIDE, _TOP - 2), Qt.AlignmentFlag.AlignLeft, self._header_text())
        if self._env is None:
            painter.setPen(TEXT_DIM)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._error or "select a sample to see its waveform")
            painter.end()
            return
        env = self._env
        mid = rect.center().y()
        half = rect.height() / 2 - 2
        painter.setPen(QPen(BORDER, 1))
        painter.drawLine(QPointF(rect.left(), mid), QPointF(rect.right(), mid))

        self._paint_windows(painter, rect)
        self._paint_segments(painter, rect)

        if env.columns:
            path = QPainterPath()
            xs = rect.left() + (np.arange(env.columns) + 0.5) / env.columns * rect.width()
            path.moveTo(xs[0], mid - float(env.maxs[0]) * half)
            for x, v in zip(xs[1:], env.maxs[1:]):
                path.lineTo(float(x), mid - float(v) * half)
            for x, v in zip(xs[::-1], env.mins[::-1]):
                path.lineTo(float(x), mid - float(v) * half)
            path.closeSubpath()
            painter.setPen(QPen(ACCENT, 1))
            painter.setBrush(_alpha(ACCENT, 150))
            painter.drawPath(path)

        self._paint_envelope(painter, rect, mid, half)
        self._paint_axis(painter, rect)
        if self._position_ms is not None:
            x = self._x_of(self._position_ms / 1000, rect)
            painter.setPen(QPen(WHITE, 1.5))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
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
        return "  ·  ".join(parts)

    def _paint_windows(self, painter: QPainter, rect: QRectF) -> None:
        """The CLAP windows as a thin strip along the bottom of the plot —
        where the model looked, not slices anyone chose — and the selected
        one (a hit) as a band across the full height."""
        strip = 5.0
        for win in self._windows:
            x0 = self._x_of(win.start_ms / 1000, rect)
            x1 = self._x_of(win.end_ms / 1000, rect)
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
        if label and x1 - x0 > painter.fontMetrics().horizontalAdvance(label) + 6:   # only when it fits
            painter.setPen(colour)
            painter.drawText(QRectF(x0 + 2, rect.top() + 1, x1 - x0 - 4, 14), Qt.AlignmentFlag.AlignLeft, label)

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
        step = _tick_step(self._env.duration_s, rect.width())
        painter.setPen(TEXT_DIM)
        t = 0.0
        while t <= self._env.duration_s + 1e-9:
            x = self._x_of(t, rect)
            painter.drawLine(QPointF(x, rect.bottom()), QPointF(x, rect.bottom() + 4))
            label = f"{t:.2f}" if step < 1 else (f"{t:.0f}" if step < 60 else f"{int(t // 60)}:{int(t % 60):02d}")
            painter.drawText(QRectF(x - 30, rect.bottom() + 3, 60, 13), Qt.AlignmentFlag.AlignHCenter, label)
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
        if event.button() != Qt.MouseButton.LeftButton or self._env is None:
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

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.has_staged:
            self.discard()
        elif event.key() == Qt.Key.Key_Delete and self.can_delete:
            self.delete_requested.emit(self._selected_segment)
        else:
            super().keyPressEvent(event)


class WaveformPanel(QWidget):
    """The bottom panel: the view and its segment buttons (§9.2). *Save
    segment* writes the staged markers, *Discard* drops them, *Delete
    segment* removes the selected one; the window does the writing."""

    save_requested = Signal()
    delete_requested = Signal(int)     # the selected segment's id

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.view = WaveformView()
        self._save = QPushButton("Save segment")
        self._save.setToolTip(
            "Write the moved or drawn markers to the index as manual segments (§6.3): exempt "
            "from the automatic length and cap rules, never overwritten by a recompute."
        )
        self._discard = QPushButton("Discard")
        self._discard.setToolTip("Drop the unsaved markers (Esc).")
        self._delete = QPushButton("Delete segment")
        self._delete.setToolTip("Remove the selected segment, automatic or manual, from the index (Del).")
        hint = ElidedLabel(
            "drag a marker to move it  ·  drag on the waveform to draw a segment  ·  "
            "nothing is written until Save"
        )
        hint.setObjectName("caption")
        row = QHBoxLayout()
        row.setContentsMargins(4, 0, 4, 2)
        row.setSpacing(6)
        row.addWidget(self._save)
        row.addWidget(self._discard)
        row.addWidget(self._delete)
        row.addWidget(hint, stretch=1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.view, stretch=1)
        layout.addLayout(row)
        self._save.clicked.connect(self.save_requested)
        self._discard.clicked.connect(self.view.discard)
        self._delete.clicked.connect(self._emit_delete)
        self.view.staged_changed.connect(self._refresh)
        self.view.selection_changed.connect(self._refresh)
        self.view.delete_requested.connect(self.delete_requested)
        self._refresh()

    def _emit_delete(self) -> None:
        if self.view.can_delete:
            self.delete_requested.emit(self.view.selected_segment)

    def _refresh(self, *_args) -> None:
        n = len(self.view.staged())
        self._save.setEnabled(n > 0)
        self._save.setText("Save segment" if n <= 1 else f"Save {n} segments")
        self._discard.setEnabled(n > 0)
        self._delete.setEnabled(self.view.can_delete)
