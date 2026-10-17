"""Human-readable durations, shared by the progress message and the banner."""

from __future__ import annotations


def format_elapsed(seconds: float) -> str:
    """Render a running duration.

    Past a minute a bare second count stops being readable — "347s" has to be
    divided in your head to mean anything — so it becomes "5min 47s". Below a
    minute the seconds alone are clearer than "0min 12s".
    """
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    return f"{minutes}min {secs}s"
