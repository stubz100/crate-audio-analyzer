"""Progress lines for the long stages (Phase 12, 2026-09-13): one place for
"how fast, and how much longer".

The user's first full run of the library gave no feedback for ten minutes
of scanning and no estimate through hours of analysis and embedding. Every
stage now logs its running rate *and* an ETA from it, so a run that will
take the night says so within its first minute — and can be stopped and
narrowed instead of waited out.
"""

from __future__ import annotations


def eta_text(elapsed_s: float, done: int, total: int) -> str:
    """'ETA 45 s' / 'ETA 12 min' / 'ETA 1 h 20 min' from the running rate;
    empty before anything is done or once everything is."""
    if done <= 0 or total <= done or elapsed_s <= 0:
        return ""
    remaining = elapsed_s / done * (total - done)
    if remaining < 90:
        return f"ETA {remaining:.0f} s"
    if remaining < 5400:
        return f"ETA {remaining / 60:.0f} min"
    hours, minutes = divmod(int(remaining // 60), 60)
    return f"ETA {hours} h {minutes:02d} min"


def duration_text(seconds: float) -> str:
    """'45 s' / '12 min' / '1 h 20 min' — a projected total, for an opening line."""
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    hours, minutes = divmod(int(seconds // 60), 60)
    return f"{hours} h {minutes:02d} min"
