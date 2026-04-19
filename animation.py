"""
Animation helpers — pure functions, no tkinter dependency.

All functions are deterministic and side-effect-free for easy testing.
Used by the record chamber in gui.py to drive the ambient light field.
"""

from __future__ import annotations

import math
from typing import Optional


# ─── Easing ─────────────────────────────────────────────────────────────────

def ease_out_cubic(t: float) -> float:
    """Ease-out cubic. Fast start, slow end. Input clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    """Ease-in-out cubic. Symmetric S-curve. Input clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 4.0 * t ** 3
    return 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


# ─── Breathing ──────────────────────────────────────────────────────────────

def breathe(phase: float) -> float:
    """Symmetric cosine breathing curve.

    phase ∈ [0, 1] represents one full breath cycle.
    Returns [0, 1]: starts at 0, peaks at phase=0.5, returns to 0 at phase=1.
    """
    return (1.0 - math.cos(phase * 2.0 * math.pi)) / 2.0


# ─── Color blending ─────────────────────────────────────────────────────────

def blend(fg: str, bg: str, alpha: float) -> str:
    """Linear-interpolate two hex colors to simulate alpha over bg.

    alpha=0 → bg, alpha=1 → fg. Returns uppercase #RRGGBB.
    Used to pre-compute ring colors since tkinter Canvas has no true alpha.
    """
    fg_r = int(fg[1:3], 16)
    fg_g = int(fg[3:5], 16)
    fg_b = int(fg[5:7], 16)
    bg_r = int(bg[1:3], 16)
    bg_g = int(bg[3:5], 16)
    bg_b = int(bg[5:7], 16)

    alpha = max(0.0, min(1.0, alpha))

    r = round(bg_r + (fg_r - bg_r) * alpha)
    g = round(bg_g + (fg_g - bg_g) * alpha)
    b = round(bg_b + (fg_b - bg_b) * alpha)

    return f"#{r:02X}{g:02X}{b:02X}"


# ─── Ripple (expanding ring emitted on RMS peaks) ───────────────────────────

class Ripple:
    """Single expanding ring emitted when RMS peaks during recording.

    Radius linearly expands r0 → r1 over `duration` seconds.
    Alpha linearly fades a0 → 0 over the same duration.
    Fire-and-forget — returns None once expired.
    """

    __slots__ = ("t0", "dur", "r0", "r1", "a0")

    def __init__(
        self,
        start_time: float,
        duration: float = 1.2,
        r0: float = 140.0,
        r1: float = 180.0,
        a0: float = 0.4,
    ) -> None:
        self.t0 = start_time
        self.dur = duration
        self.r0 = r0
        self.r1 = r1
        self.a0 = a0

    def state(self, now: float) -> Optional[tuple[float, float]]:
        """Return (radius, alpha) at `now`, or None if expired."""
        t = (now - self.t0) / self.dur
        if t >= 1.0 or t < 0.0:
            return None
        return (
            self.r0 + (self.r1 - self.r0) * t,
            self.a0 * (1.0 - t),
        )
