"""VAD 對齊切窗 unit tests（v2.22.0）。

治「fixed_chunk streaming 每滿 10s 硬切、切點與語意無關」的根：切點改選在
使用者真正停頓處（簡單 RMS 法找靜音）。

涵蓋：
  (a) 合成音「speech + 靜音 + speech」→ 切點應落在靜音段，而非 buffer 邊界
  (b) 連續講話沒有停頓 → hard cap 12s 強制切
  (c) 切點後的殘餘 samples 保留給下一段 buffer（一個 sample 都不丟）
  (d) chunk_cut_mode="fixed" 走舊路（滿 10s 就無腦切、不找靜音）

不啟動真正的 Tk root / AppWindow（過於重量）；改用 AppWindow.__new__ 繞過
__init__ 注入最小依賴，與 tests/test_stability.py 的既有模式一致。
"""

import types

import numpy as np
import pytest

from gui import (
    AppWindow,
    find_silence_cut_point,
    STREAM_CUT_SEARCH_WINDOW_S,
)


SR = 16_000


def _make_stub_appwindow(chunk_cut_mode: str = "vad_aligned"):
    """繞過 __init__ 建出一個只夠跑 _decide_stream_cut_length 的 AppWindow 殼。"""
    win = AppWindow.__new__(AppWindow)
    win.cfg = types.SimpleNamespace(chunk_cut_mode=chunk_cut_mode)
    win._stream_samples = 0
    return win


def _speech(rng, seconds: float, amp: float = 0.15) -> np.ndarray:
    return rng.normal(0, amp, int(seconds * SR)).astype(np.float32)


def _silence(rng, seconds: float, amp: float = 0.001) -> np.ndarray:
    return rng.normal(0, amp, int(seconds * SR)).astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
#  (a) 切點應落在靜音段，而非 10.0s 硬邊界
# ─────────────────────────────────────────────────────────────────────────────

def test_silence_cut_point_lands_in_pause_not_at_buffer_edge():
    """合成音：7.5s 語音 + 0.5s 靜音 + 2.0s 語音（共 10s，靜音落在最後 2.5s
    搜尋窗內）。切點應落在靜音段（7.5s~8.0s 之間），不該落在 10.0s 邊界。"""
    rng = np.random.default_rng(42)
    audio = np.concatenate([
        _speech(rng, 7.5),
        _silence(rng, 0.5),
        _speech(rng, 2.0),
    ])
    assert len(audio) == 10 * SR

    cut = find_silence_cut_point(audio, sample_rate=SR)
    assert cut is not None, "應該要在靜音段找到切點"
    cut_s = cut / SR
    assert 7.5 <= cut_s <= 8.0, f"切點應落在靜音段 7.5s~8.0s，實際 {cut_s}s"
    assert abs(cut_s - 10.0) > 0.5, "切點不該落在 buffer 尾端（10.0s 硬邊界）"


def test_silence_cut_point_none_when_pause_outside_search_window():
    """靜音落在最後 2.5s 搜尋窗之外（例：4.0s~4.5s）時，不該被誤判成切點——
    搜尋窗設計就是只看「最近」的停頓，避免切太早丟失剛累積的語音。"""
    rng = np.random.default_rng(1)
    audio = np.concatenate([
        _speech(rng, 4.0),
        _silence(rng, 0.5),
        _speech(rng, 5.5),
    ])
    assert len(audio) == 10 * SR
    cut = find_silence_cut_point(audio, sample_rate=SR)
    assert cut is None


# ─────────────────────────────────────────────────────────────────────────────
#  (b) 連續講話無停頓 → hard cap 12s 強制切
# ─────────────────────────────────────────────────────────────────────────────

def test_continuous_speech_no_silence_returns_none():
    """12 秒連續講話、完全沒有停頓 → find_silence_cut_point 回 None
    （找不到夠安靜的地方）。"""
    rng = np.random.default_rng(99)
    audio = _speech(rng, 12.0)
    cut = find_silence_cut_point(audio, sample_rate=SR)
    assert cut is None


def test_decide_stream_cut_defers_before_hard_cap_when_no_silence():
    """未達 hard cap（12s）、連續講話找不到靜音 → _decide_stream_cut_length
    回 None（先不切，讓 _stream_tick 繼續累積等下一秒），不會卡死變成
    '找不到就永遠不切'。"""
    win = _make_stub_appwindow("vad_aligned")
    rng = np.random.default_rng(7)
    # available = 10s（剛好達到 dispatch 門檻），但還沒到 12s hard cap
    snap = _speech(rng, 10.0)
    result = win._decide_stream_cut_length(snap, available=len(snap))
    assert result is None


def test_decide_stream_cut_forces_hard_cap_when_no_silence_at_12s():
    """已達 hard cap（12s）、連續講話仍找不到靜音 → 強制在 hard cap 處切，
    行為與舊 fixed 路徑一致（保底）。"""
    win = _make_stub_appwindow("vad_aligned")
    rng = np.random.default_rng(8)
    snap = _speech(rng, 12.0)   # 12s 連續講話、無停頓
    result = win._decide_stream_cut_length(snap, available=len(snap))
    assert result == win.STREAM_HARD_CAP_SAMPLES
    assert result == 12 * SR


def test_decide_stream_cut_prefers_silence_even_at_hard_cap():
    """已達 hard cap 但這次剛好搜尋窗內有靜音 → 仍優先切在靜音，而非
    無腦切在 12s 邊界。"""
    win = _make_stub_appwindow("vad_aligned")
    rng = np.random.default_rng(11)
    # 9.5s 語音 + 0.5s 靜音 + 2.0s 語音 = 12s，靜音落在最後 2.5s 窗內
    snap = np.concatenate([
        _speech(rng, 9.5),
        _silence(rng, 0.5),
        _speech(rng, 2.0),
    ])
    assert len(snap) == 12 * SR
    result = win._decide_stream_cut_length(snap, available=len(snap))
    assert result is not None
    assert result != win.STREAM_HARD_CAP_SAMPLES, "應該切在靜音，不是硬邊界"
    cut_s = result / SR
    assert 9.5 <= cut_s <= 10.0


# ─────────────────────────────────────────────────────────────────────────────
#  (c) 切點後殘餘 samples 保留給下一段 buffer（一個 sample 都不丟）
# ─────────────────────────────────────────────────────────────────────────────

def test_residual_samples_after_cut_carry_to_next_chunk():
    """模擬 _stream_tick 的累積邏輯：切點之後的殘餘音訊必須完整保留、
    成為下一段 buffer 的開頭，一個 sample 都不能丟。"""
    win = _make_stub_appwindow("vad_aligned")
    rng = np.random.default_rng(21)
    audio = np.concatenate([
        _speech(rng, 7.5),
        _silence(rng, 0.5),
        _speech(rng, 2.0),
    ])
    assert len(audio) == 10 * SR

    win._stream_samples = 0
    cut_len = win._decide_stream_cut_length(audio, available=len(audio))
    assert cut_len is not None

    chunk_end = win._stream_samples + cut_len
    dispatched_chunk = audio[win._stream_samples:chunk_end]
    residual = audio[chunk_end:]

    # 殘餘 + 已 dispatch 的 chunk 首尾相接，等於完整原始音訊
    reconstructed = np.concatenate([dispatched_chunk, residual])
    assert np.array_equal(reconstructed, audio), "切割後重新接回必須與原始音訊完全一致"
    assert len(residual) == len(audio) - cut_len
    assert len(residual) > 0, "有殘餘才有意義（切點不在尾端）"

    # 下一輪：_stream_samples 前進到切點，殘餘留在 snap 裡等下次 tick 累積用
    win._stream_samples = chunk_end
    new_available = len(audio) - win._stream_samples
    assert new_available == len(residual)


# ─────────────────────────────────────────────────────────────────────────────
#  (d) chunk_cut_mode="fixed" 走舊路
# ─────────────────────────────────────────────────────────────────────────────

def test_fixed_mode_cuts_exactly_at_10s_ignoring_silence():
    """chunk_cut_mode="fixed"（逃生門）：即使搜尋窗內有明顯靜音，也應該
    無腦切滿 10s，完全比照 v2.21.x 以前的舊行為。"""
    win = _make_stub_appwindow("fixed")
    rng = np.random.default_rng(5)
    # 刻意放一段搜尋窗內的靜音，驗證 fixed 模式完全不理會它
    audio = np.concatenate([
        _speech(rng, 7.5),
        _silence(rng, 0.5),
        _speech(rng, 2.0),
    ])
    result = win._decide_stream_cut_length(audio, available=len(audio))
    assert result == win.STREAM_CHUNK_SAMPLES
    assert result == 10 * SR


def test_fixed_mode_never_defers():
    """fixed 模式任何時候都立刻回傳切點（不會回 None 等待）。"""
    win = _make_stub_appwindow("fixed")
    rng = np.random.default_rng(6)
    audio = _speech(rng, 10.0)   # 連續講話，vad_aligned 模式下會 defer
    result = win._decide_stream_cut_length(audio, available=len(audio))
    assert result == win.STREAM_CHUNK_SAMPLES


# ─────────────────────────────────────────────────────────────────────────────
#  find_silence_cut_point 邊界情況
# ─────────────────────────────────────────────────────────────────────────────

def test_find_silence_cut_point_too_short_returns_none():
    """音訊太短（不夠兩個 RMS 視窗）→ 回 None，不該 crash。"""
    audio = np.zeros(10, dtype=np.float32)
    assert find_silence_cut_point(audio, sample_rate=SR) is None


def test_find_silence_cut_point_empty_array_returns_none():
    """空陣列 → 回 None，不該 crash。"""
    audio = np.zeros(0, dtype=np.float32)
    assert find_silence_cut_point(audio, sample_rate=SR) is None
