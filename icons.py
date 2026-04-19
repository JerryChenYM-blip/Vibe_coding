"""
Lucide-inspired icon set, hand-drawn via Pillow.

Why not SVG libraries?
  - cairosvg needs libcairo (system dep, fragile on macOS)
  - svglib/reportlab fails to build on Python 3.13
  - We only need ~8 icons — hand-drawing is cheaper than a toolchain.

Rendering strategy:
  - Each icon is defined in a 24×24 coordinate space (Lucide standard).
  - Drawn at 4× supersampled resolution, then downsampled with LANCZOS
    to simulate antialiasing (tkinter's Canvas/PhotoImage has no native AA).
  - Stroke 2px, round linecap, round linejoin — matches Lucide's look.

Public API:
    get_icon(name, size=16, color="#FFFFFF") → CTkImage (cached)
    ICON_NAMES — list of available icon names
"""

from __future__ import annotations

from functools import lru_cache
from typing import Callable

from PIL import Image, ImageDraw
import customtkinter as ctk

# ─── Constants ───────────────────────────────────────────────────────────────
VIEW = 24          # Lucide icon viewport
SS   = 4           # Supersampling factor
STROKE_VP = 2.0    # Stroke width in viewport units (Lucide default)


# ─── Primitives (all coords are in 24-viewport space) ───────────────────────

class _Pen:
    """Drawing helper — translates 24-vp coords to supersampled pixel coords."""

    def __init__(self, size_px: int, color: str) -> None:
        self.img   = Image.new("RGBA", (size_px * SS, size_px * SS), (0, 0, 0, 0))
        self.draw  = ImageDraw.Draw(self.img)
        self.color = color
        self.size  = size_px
        self._k    = size_px * SS / VIEW                  # vp → px scale
        self._sw   = max(1, round(STROKE_VP * self._k))   # stroke px width

    # vp(x) = a single coord in viewport space
    def _p(self, x: float, y: float) -> tuple[float, float]:
        return (x * self._k, y * self._k)

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.draw.line([self._p(x1, y1), self._p(x2, y2)],
                       fill=self.color, width=self._sw, joint="curve")

    def polyline(self, pts: list[tuple[float, float]]) -> None:
        self.draw.line([self._p(*p) for p in pts],
                       fill=self.color, width=self._sw, joint="curve")

    def rect(self, x1: float, y1: float, x2: float, y2: float, r: float = 0) -> None:
        box = [self._p(x1, y1), self._p(x2, y2)]
        if r > 0:
            self.draw.rounded_rectangle(box, radius=r * self._k,
                                         outline=self.color, width=self._sw)
        else:
            self.draw.rectangle(box, outline=self.color, width=self._sw)

    def ellipse(self, cx: float, cy: float, rx: float, ry: float | None = None) -> None:
        ry = ry if ry is not None else rx
        box = [self._p(cx - rx, cy - ry), self._p(cx + rx, cy + ry)]
        self.draw.ellipse(box, outline=self.color, width=self._sw)

    def dot(self, cx: float, cy: float, r: float = 1.0) -> None:
        """Filled circle (no stroke)."""
        box = [self._p(cx - r, cy - r), self._p(cx + r, cy + r)]
        self.draw.ellipse(box, fill=self.color)

    def finish(self) -> Image.Image:
        """Return downsampled final image."""
        return self.img.resize(
            (self.size, self.size),
            Image.Resampling.LANCZOS,
        )


# ─── Icons (all in 24-viewport space, Lucide-inspired) ───────────────────────

def _i_copy(p: _Pen) -> None:
    """Two overlapping rounded rectangles."""
    p.rect(9, 9, 20, 20, r=2)    # back
    p.rect(4, 4, 15, 15, r=2)    # front


def _i_download(p: _Pen) -> None:
    """Arrow pointing into a tray — Lucide 'download'."""
    # Horizontal base tray
    p.polyline([(4, 15), (4, 20), (20, 20), (20, 15)])
    # Vertical shaft
    p.line(12, 4, 12, 16)
    # Arrow head (chevron)
    p.polyline([(7, 11), (12, 16), (17, 11)])


def _i_keyboard(p: _Pen) -> None:
    """Rounded rectangle with key dots."""
    p.rect(3, 6, 21, 18, r=2)
    # Top row of keys (dots)
    for x in (6.5, 10, 13.5, 17):
        p.dot(x, 10, r=0.9)
    # Space bar (middle row short line)
    p.line(7, 14, 17, 14)


def _i_sparkles(p: _Pen) -> None:
    """Four-point star + small star. Simplified from Lucide."""
    # Big star: cross
    p.line(12, 3, 12, 13)
    p.line(7, 8, 17, 8)
    # Small star
    p.line(18, 15, 18, 21)
    p.line(15, 18, 21, 18)
    # Smallest accent
    p.line(5, 17, 9, 17)


def _i_settings(p: _Pen) -> None:
    """Sliders-style settings (simpler than gear, cleaner at 16px)."""
    # Three horizontal sliders
    p.line(4, 7, 20, 7)
    p.line(4, 12, 20, 12)
    p.line(4, 17, 20, 17)
    # Slider handles
    p.dot(9, 7, r=1.6)
    p.dot(15, 12, r=1.6)
    p.dot(7, 17, r=1.6)


def _i_file_text(p: _Pen) -> None:
    """File with lines — result title icon."""
    # File outline with folded corner
    p.polyline([(5, 3), (15, 3), (20, 8), (20, 21), (5, 21), (5, 3)])
    # Folded corner
    p.polyline([(15, 3), (15, 8), (20, 8)])
    # Text lines
    p.line(9, 13, 16, 13)
    p.line(9, 17, 16, 17)


def _i_x(p: _Pen) -> None:
    """Close / clear."""
    p.line(6, 6, 18, 18)
    p.line(18, 6, 6, 18)


def _i_lock(p: _Pen) -> None:
    """Padlock."""
    p.rect(4, 11, 20, 21, r=2)
    # Shackle
    p.polyline([(8, 11), (8, 7), (12, 4), (16, 7), (16, 11)])


def _i_folder(p: _Pen) -> None:
    """Folder."""
    p.polyline([(3, 8), (10, 8), (12, 5), (21, 5), (21, 19), (3, 19), (3, 8)])


def _i_check(p: _Pen) -> None:
    """Checkmark."""
    p.polyline([(5, 12), (10, 18), (20, 6)])


def _i_mic(p: _Pen) -> None:
    """Microphone."""
    p.rect(9, 3, 15, 14, r=3)       # capsule
    p.polyline([(5, 11), (5, 13)])
    p.polyline([(19, 11), (19, 13)])
    # Arch
    p.polyline([(5, 13), (5, 14), (19, 14), (19, 13)])  # simplified arch
    p.line(12, 18, 12, 21)          # stem
    p.line(8, 21, 16, 21)            # base


def _i_square(p: _Pen) -> None:
    """Lucide 'square' — stop icon, slightly rounded corners."""
    p.rect(6, 6, 18, 18, r=1.5)


# ─── Registry ────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, Callable[[_Pen], None]] = {
    "copy":       _i_copy,
    "download":   _i_download,
    "keyboard":   _i_keyboard,
    "sparkles":   _i_sparkles,
    "settings":   _i_settings,
    "file-text":  _i_file_text,
    "x":          _i_x,
    "lock":       _i_lock,
    "folder":     _i_folder,
    "check":      _i_check,
    "mic":        _i_mic,
    "square":     _i_square,
}

ICON_NAMES = list(_REGISTRY.keys())


# ─── Public API ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=128)
def _render_pil(name: str, size: int, color: str) -> Image.Image:
    draw_fn = _REGISTRY.get(name)
    if draw_fn is None:
        raise ValueError(f"Unknown icon: {name!r}. Available: {ICON_NAMES}")
    pen = _Pen(size, color)
    draw_fn(pen)
    return pen.finish()


_CK_CACHE: dict[tuple[str, int, str], ctk.CTkImage] = {}


def get_icon(name: str, size: int = 16, color: str = "#FFFFFF") -> ctk.CTkImage:
    """
    Return a CTkImage for `name` at `size` pixels, stroked in `color`.

    Cached — calling repeatedly with the same args is free.
    """
    key = (name, size, color)
    if key in _CK_CACHE:
        return _CK_CACHE[key]

    # Render @1x and @2x for HiDPI support
    img_1x = _render_pil(name, size, color)
    img_2x = _render_pil(name, size * 2, color)
    ck = ctk.CTkImage(light_image=img_1x, dark_image=img_1x,
                      size=(size, size))
    # Use larger image so CustomTkinter picks the right one on Retina
    ck = ctk.CTkImage(light_image=img_2x, dark_image=img_2x,
                      size=(size, size))
    _CK_CACHE[key] = ck
    return ck
