"""Aperture 三態統一（v2.27.0）unit tests。

範圍：純函式（不啟動真正的 Tk root / Canvas，跟 tests/test_stability.py、
tests/test_chunking.py 既有慣例一致）。涵蓋：
  (a) _processing_sweep_progress —— 真實進度直傳、等速 fallback 週期循環
  (b) _processing_bar_color —— 光帶前緣亮／已掃過依振幅／未掃到 WAVE_DIM 三段
  (c) WAVE_CHAMBER_W / WAVE_CHAMBER_H 尺寸常數（712×160，46 bar 比例）
  (d) LocalAgreementBuffer.committed_audio_end_s —— 掠掃光帶餵真實進度的資料來源
"""

from gui import (
    WAVE_CHAMBER_W,
    WAVE_CHAMBER_H,
    WAVE_N_BARS_MAIN,
    PROCESS_SWEEP_CYCLE_MS,
    _processing_sweep_progress,
    _processing_bar_color,
)
from tokens import WAVE_DIM, process_color
from streaming_local_agreement import LocalAgreementBuffer


# ═════════════════════════════════════════════════════════════════════════════
#  (c) canvas 尺寸常數
# ═════════════════════════════════════════════════════════════════════════════

def test_wave_chamber_dimensions():
    """主視窗 chamber canvas 712×160（設計規格：寬條、46 bar 比例才對）。"""
    assert WAVE_CHAMBER_W == 712
    assert WAVE_CHAMBER_H == 160


# ═════════════════════════════════════════════════════════════════════════════
#  (a) _processing_sweep_progress
# ═════════════════════════════════════════════════════════════════════════════

def test_sweep_progress_real_passes_known_frac_through():
    """use_real=True：直接回 known_frac，不受 elapsed_ms 影響（真實進度不是動畫）。"""
    assert _processing_sweep_progress(0.0, True, 0.4) == 0.4
    assert _processing_sweep_progress(9999.0, True, 0.4) == 0.4


def test_sweep_progress_real_clamps_to_unit_range():
    """known_frac 超出 [0,1]（理論上不該發生，但 clamp 保底）要被夾住。"""
    assert _processing_sweep_progress(0.0, True, -0.5) == 0.0
    assert _processing_sweep_progress(0.0, True, 1.5) == 1.0


def test_sweep_progress_fallback_is_constant_speed_cycle():
    """use_real=False：等速掠掃，週期 PROCESS_SWEEP_CYCLE_MS，無限循環。"""
    assert _processing_sweep_progress(0.0, False, 0.0) == 0.0
    half = PROCESS_SWEEP_CYCLE_MS / 2.0
    assert abs(_processing_sweep_progress(half, False, 0.0) - 0.5) < 1e-9
    # 剛好一個週期 → 回到 0（不是停在 1.0——沒有下一輪就不知道還要多久，
    # 循環動畫比停在假數字誠實）
    assert abs(_processing_sweep_progress(PROCESS_SWEEP_CYCLE_MS, False, 0.0)) < 1e-9
    # 一個半週期 → 跟半週期一樣是 0.5（循環）
    assert abs(_processing_sweep_progress(PROCESS_SWEEP_CYCLE_MS * 1.5, False, 0.0) - 0.5) < 1e-9


def test_sweep_progress_fallback_ignores_known_frac():
    """use_real=False 時 known_frac 不該影響結果（已經決定不用真實進度了）。"""
    a = _processing_sweep_progress(100.0, False, 0.0)
    b = _processing_sweep_progress(100.0, False, 0.9)
    assert a == b


# ═════════════════════════════════════════════════════════════════════════════
#  (b) _processing_bar_color —— 光帶三段（未掃到 WAVE_DIM／前緣最亮／已掃過依振幅）
# ═════════════════════════════════════════════════════════════════════════════

def test_processing_bar_color_far_ahead_of_sweep_is_dim():
    """光帶還沒掃到的 bar（遠在 sweep 位置之後）→ WAVE_DIM。"""
    n = WAVE_N_BARS_MAIN
    color = _processing_bar_color(40, 0.7, n, progress=0.0)
    assert color == WAVE_DIM


def test_processing_bar_color_leading_edge_is_brightest():
    """光帶前緣（sweep 位置附近）→ process_color(1.0)，不受該 bar 自己振幅影響。"""
    n = WAVE_N_BARS_MAIN
    # progress=0.0 時 sweep_idx=0，bar 0（中心 0.5）落在前緣亮帶內
    quiet = _processing_bar_color(0, 0.05, n, progress=0.0)
    loud = _processing_bar_color(0, 0.95, n, progress=0.0)
    assert quiet == process_color(1.0)
    assert loud == process_color(1.0)
    assert quiet == loud   # 前緣亮度跟這根 bar 自己多大聲無關


def test_processing_bar_color_already_swept_reflects_amplitude():
    """已經被光帶掃過（遠在 sweep 位置之前）→ process_color(v)，依凍結振幅取色。"""
    n = WAVE_N_BARS_MAIN
    progress = 0.5   # sweep_idx = 23，遠離前緣亮帶的 bar 5 已經算「掃過」
    quiet = _processing_bar_color(5, 0.1, n, progress=progress)
    loud = _processing_bar_color(5, 0.9, n, progress=progress)
    assert quiet == process_color(0.1)
    assert loud == process_color(0.9)
    assert quiet != loud   # 已掃過的 bar 顏色要能反映實際振幅（不是統一顏色）


def test_processing_bar_color_progress_zero_and_one_are_symmetric_edges():
    """progress=0 時第一根 bar 在前緣、progress=1 時最後一根 bar 在前緣。"""
    n = WAVE_N_BARS_MAIN
    assert _processing_bar_color(0, 0.5, n, progress=0.0) == process_color(1.0)
    assert _processing_bar_color(n - 1, 0.5, n, progress=1.0) == process_color(1.0)


# ═════════════════════════════════════════════════════════════════════════════
#  (d) LocalAgreementBuffer.committed_audio_end_s —— 掠掃光帶的真實進度來源
# ═════════════════════════════════════════════════════════════════════════════

def _make_buffer() -> LocalAgreementBuffer:
    return LocalAgreementBuffer(transcriber=None, language=None, model_size="stub")


def test_committed_audio_end_s_starts_at_zero():
    buf = _make_buffer()
    assert buf.committed_audio_end_s == 0.0


def test_committed_audio_end_s_mirrors_internal_state():
    """property 只是把 lock 保護的內部狀態安全地讀出來，不改變語意。"""
    buf = _make_buffer()
    buf._committed_audio_end_s = 4.25
    assert buf.committed_audio_end_s == 4.25


def test_committed_audio_end_s_resets_with_reset():
    buf = _make_buffer()
    buf._committed_audio_end_s = 7.0
    buf.reset()
    assert buf.committed_audio_end_s == 0.0
