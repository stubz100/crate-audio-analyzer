"""Shared configuration constants.

Single source of truth for values both the GUI (`main.py`) and the CLI
(`cli.py`) need — introduced by the 2026-09-06 review (finding 9: the
library root was hardcoded in two places that would drift).
"""

from __future__ import annotations

#: Default sample library root (spec §1). The GUI may override it via
#: QSettings; the CLI takes `--root`.
DEFAULT_LIBRARY_PATH = r"D:\_soundPacks"