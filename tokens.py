"""
Design tokens — Whisper Pro v2.0

Single source of truth for colors, typography, spacing, radii, motion.
All UI modules should import from here; no raw hex literals in components.

Palette:      Zinc warm-gray (Linear-inspired) + Cyan accent (AI-tool feel)
Elevation:    4-level surface hierarchy (dp 0 → 4)
Reference:    UX_OPTIMIZATION_STRATEGY.md §3
"""

from __future__ import annotations

# ═══════════════════════════════════════════════════════════════════════════
#  SURFACE ELEVATION (dp 0-4)
#  Dark-first, OLED-optimised. Use these to express visual depth without
#  relying on shadows (tkinter has no real shadow support).
# ═══════════════════════════════════════════════════════════════════════════

BG        = "#000000"   # dp=0  void black (window body)
SURF_1    = "#0E0E10"   # dp=1  primary card / bar
SURF_2    = "#18181B"   # dp=2  secondary (status bar, hover base)
SURF_3    = "#27272A"   # dp=3  tertiary (pressed, hover on surf-2)
SURF_4    = "#3F3F46"   # dp=4  quaternary (borders, dividers, subtle strokes)


# ═══════════════════════════════════════════════════════════════════════════
#  TEXT HIERARCHY (warm white)
# ═══════════════════════════════════════════════════════════════════════════

TEXT_1    = "#FAFAFA"   # 100% — headlines, primary CTA labels
TEXT_2    = "#E4E4E7"   # ~75% — body
TEXT_3    = "#A1A1AA"   # ~55% — captions, hints, timestamps
TEXT_4    = "#71717A"   # ~35% — disabled, tertiary meta


# ═══════════════════════════════════════════════════════════════════════════
#  SEMANTIC ACCENTS
#  Desaturated for dark backgrounds — avoid pure system colors which can
#  feel "retail" on a developer tool.
# ═══════════════════════════════════════════════════════════════════════════

# Primary brand / CTA — Cyan 500 (AI / technical feel)
ACCENT      = "#06B6D4"
ACCENT_HV   = "#22D3EE"
ACCENT_BG   = "#164E63"   # tinted chip background

# Success / idle — Green 500
SUCCESS     = "#22C55E"
SUCCESS_HV  = "#16A34A"
SUCCESS_DIM = "#14532D"

# Danger / recording — Red 500
DANGER      = "#EF4444"
DANGER_HV   = "#DC2626"
DANGER_DIM  = "#7F1D1D"

# Warn / processing — Amber 500
WARN        = "#F59E0B"
WARN_HV     = "#D97706"
WARN_DIM    = "#78350F"

# Auto-paste — Indigo 400 (softer than system indigo on dark)
INDIGO      = "#818CF8"
INDIGO_HV   = "#6366F1"
INDIGO_DIM  = "#312E81"


# ═══════════════════════════════════════════════════════════════════════════
#  WAVEFORM
# ═══════════════════════════════════════════════════════════════════════════

WAVE_IDLE_COL = SURF_4    # matches quaternary surface
WAVE_LIVE_COL = TEXT_1    # bright white on live


# ═══════════════════════════════════════════════════════════════════════════
#  TYPOGRAPHY
# ═══════════════════════════════════════════════════════════════════════════

FONT_FAMILY_UI   = "SF Pro Display"
FONT_FAMILY_TEXT = "SF Pro Text"
FONT_FAMILY_MONO = "SF Mono"

# Type scale — (size_pt, weight)
TYPE = {
    "display":  (28, "bold"),
    "title":    (17, "bold"),
    "headline": (15, "bold"),
    "body":     (14, "normal"),
    "caption":  (12, "normal"),
    "micro":    (11, "normal"),
    "mono":     (13, "normal"),
}


# ═══════════════════════════════════════════════════════════════════════════
#  SPACING (4pt baseline)
# ═══════════════════════════════════════════════════════════════════════════

SPACE_XS  = 4
SPACE_SM  = 8
SPACE_MD  = 12
SPACE_LG  = 16
SPACE_XL  = 24
SPACE_2XL = 32
SPACE_3XL = 48


# ═══════════════════════════════════════════════════════════════════════════
#  BORDER RADIUS
# ═══════════════════════════════════════════════════════════════════════════

RADIUS_SM   = 6
RADIUS_MD   = 10
RADIUS_LG   = 14
RADIUS_XL   = 20
RADIUS_PILL = 999


# ═══════════════════════════════════════════════════════════════════════════
#  ANIMATION
# ═══════════════════════════════════════════════════════════════════════════

DUR_FAST   = 120   # hover, tap feedback
DUR_NORMAL = 240   # state transitions
DUR_SLOW   = 400   # card enter, modal

# Breathing periods for the record chamber's three states.
BREATHE_IDLE_MS       = 6000   # calm
BREATHE_RECORDING_MS  = 2500   # alive
BREATHE_PROCESSING_MS = 1800   # working

# Rotating particle belt used in the processing state.
ROTATE_PROCESSING_MS  = 1500

# Canvas render loop cadence — 20 FPS, matches prior _update_wave budget.
RENDER_TICK_MS        = 50


# ═══════════════════════════════════════════════════════════════════════════
#  SPINNER (Unicode Braille animation)
# ═══════════════════════════════════════════════════════════════════════════

SPINNER = ["⠋", "⠙", "⠸", "⠴", "⠦", "⠇"]


# ═══════════════════════════════════════════════════════════════════════════
#  LEGACY ALIASES
#  Kept for backward-compatibility during gradual migration.
#  Prefer canonical names above in new code.
# ═══════════════════════════════════════════════════════════════════════════

SURF1 = SURF_1
SURF2 = SURF_2
SURF3 = SURF_3
SURF4 = SURF_4

TEXT1 = TEXT_1
TEXT2 = TEXT_2
TEXT3 = TEXT_3
TEXT4 = TEXT_4

BLUE      = ACCENT
BLUE_HV   = ACCENT_HV
BLUE_DIM  = ACCENT_BG

GREEN     = SUCCESS
GREEN_DIM = SUCCESS_DIM

RED       = DANGER
RED_DIM   = DANGER_DIM

ORANGE    = WARN
