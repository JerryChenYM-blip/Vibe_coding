"""Unit tests for animation.py — pure functions, no tkinter."""

import math
import pytest

from animation import (
    ease_out_cubic,
    ease_in_out_cubic,
    breathe,
    blend,
    Ripple,
)


# ─── ease_out_cubic ─────────────────────────────────────────────────────────

def test_ease_out_cubic_at_0():
    assert ease_out_cubic(0.0) == 0.0

def test_ease_out_cubic_at_1():
    assert ease_out_cubic(1.0) == 1.0

def test_ease_out_cubic_is_monotonic():
    vals = [ease_out_cubic(t / 10) for t in range(11)]
    assert vals == sorted(vals)

def test_ease_out_cubic_clamps_below_zero():
    assert ease_out_cubic(-0.5) == 0.0

def test_ease_out_cubic_clamps_above_one():
    assert ease_out_cubic(1.5) == 1.0


# ─── ease_in_out_cubic ──────────────────────────────────────────────────────

def test_ease_in_out_cubic_at_0():
    assert ease_in_out_cubic(0.0) == 0.0

def test_ease_in_out_cubic_at_half():
    assert ease_in_out_cubic(0.5) == 0.5

def test_ease_in_out_cubic_at_1():
    assert ease_in_out_cubic(1.0) == 1.0

def test_ease_in_out_cubic_is_monotonic():
    vals = [ease_in_out_cubic(t / 10) for t in range(11)]
    assert vals == sorted(vals)


# ─── breathe ────────────────────────────────────────────────────────────────

def test_breathe_at_0_is_zero():
    assert breathe(0.0) == pytest.approx(0.0, abs=1e-9)

def test_breathe_at_half_is_peak():
    assert breathe(0.5) == pytest.approx(1.0, abs=1e-9)

def test_breathe_at_1_is_zero():
    assert breathe(1.0) == pytest.approx(0.0, abs=1e-9)

def test_breathe_is_symmetric():
    assert breathe(0.25) == pytest.approx(breathe(0.75), abs=1e-9)


# ─── blend ──────────────────────────────────────────────────────────────────

def test_blend_alpha_0_returns_bg():
    assert blend("#FF0000", "#000000", 0.0) == "#000000"

def test_blend_alpha_1_returns_fg():
    assert blend("#FF0000", "#000000", 1.0) == "#FF0000"

def test_blend_halfway():
    assert blend("#000000", "#FFFFFF", 0.5) == "#808080"

def test_blend_output_format_uppercase_hex():
    result = blend("#abcdef", "#123456", 0.5)
    assert result == result.upper()
    assert result.startswith("#") and len(result) == 7

def test_blend_cyan_over_dark_surface():
    # ACCENT #06B6D4 over SURF_1 #0E0E10 at alpha 0.25
    # R: 14 + (6-14)*0.25 = 14 - 2 = 12 = 0x0C
    # G: 14 + (182-14)*0.25 = 14 + 42 = 56 = 0x38
    # B: 16 + (212-16)*0.25 = 16 + 49 = 65 = 0x41
    assert blend("#06B6D4", "#0E0E10", 0.25) == "#0C3841"


# ─── Ripple ─────────────────────────────────────────────────────────────────

def test_ripple_at_start_returns_initial_state():
    r = Ripple(start_time=100.0, duration=1.2, r0=140, r1=180, a0=0.4)
    radius, alpha = r.state(100.0)
    assert radius == pytest.approx(140.0)
    assert alpha == pytest.approx(0.4)

def test_ripple_at_midpoint():
    r = Ripple(start_time=100.0, duration=1.2, r0=140, r1=180, a0=0.4)
    radius, alpha = r.state(100.6)  # t = 0.5
    assert radius == pytest.approx(160.0)
    assert alpha == pytest.approx(0.2)

def test_ripple_expired_returns_none():
    r = Ripple(start_time=100.0, duration=1.2, r0=140, r1=180, a0=0.4)
    assert r.state(101.3) is None

def test_ripple_exactly_at_end_returns_none():
    r = Ripple(start_time=100.0, duration=1.2, r0=140, r1=180, a0=0.4)
    assert r.state(101.2) is None
