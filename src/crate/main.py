"""Phase 0 scaffold: an application window that can point at a sample library.

No scanning, analysis, or indexing yet — just a persisted library-root path,
per .docs/samples_final.md §12 Phase 0's deliverable. Later phases build the
scanner, pipeline, and map/list views on top of this window.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

ORG_NAME = "Crate"
APP_NAME = "Crate"
SETTINGS_KEY_LIBRARY_PATH = "library/root_path"
DEFAULT_LIBRARY_PATH = r"D:\_soundPacks"


class MainWindow(QMainWindow):
    """The application's main window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Crate")
        self.resize(640, 200)

        self._settings = QSettings(ORG_NAME, APP_NAME)
        stored_path = self._settings.value(SETTINGS_KEY_LIBRARY_PATH, DEFAULT_LIBRARY_PATH)
        self._library_path = Path(stored_path)

        self._path_label = QLabel(str(self._library_path))

        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._on_browse_clicked)

        path_row = QHBoxLayout()
        path_row.addWidget(self._path_label, stretch=1)
        path_row.addWidget(browse_button)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Sample library:"))
        layout.addLayout(path_row)
        layout.addStretch(1)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

    def _set_library_path(self, path: Path) -> None:
        self._library_path = path
        self._path_label.setText(str(path))
        self._settings.setValue(SETTINGS_KEY_LIBRARY_PATH, str(path))

    def _on_browse_clicked(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose sample library folder", str(self._library_path)
        )
        if chosen:
            self._set_library_path(Path(chosen))


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
