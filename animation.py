"""
動畫輔助函式集——純函式，無 tkinter 依賴。

所有函式皆為確定性（deterministic）且無副作用，方便單元測試。
gui.py 的錄音 Ambient Chamber 使用這些函式驅動呼吸光圈、顏色混合與漣漪效果。

匯出：
  ease_out_cubic()       緩出三次方 easing
  ease_in_out_cubic()    緩入緩出三次方 easing
  breathe()              對稱餘弦呼吸曲線
  blend()                十六進位色彩線性插值（模擬 alpha 合成）
  Ripple                 擴張漣漪環（RMS 峰值時發射）
"""

from __future__ import annotations

import math
from typing import Optional


# ─── Easing 函式 ─────────────────────────────────────────────────────────────

def ease_out_cubic(t: float) -> float:
    """緩出三次方 easing：開頭快、結尾慢。

    Args:
        t: 正規化時間，自動夾到 [0, 1]。

    Returns:
        easing 後的值，範圍 [0, 1]。
    """
    t = max(0.0, min(1.0, t))    # 防止超出範圍
    return 1.0 - (1.0 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    """緩入緩出三次方 easing：對稱 S 形曲線，開頭與結尾都慢。

    Args:
        t: 正規化時間，自動夾到 [0, 1]。

    Returns:
        easing 後的值，範圍 [0, 1]。
    """
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        # 前半段：加速
        return 4.0 * t ** 3
    # 後半段：減速（與前半段對稱）
    return 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


# ─── 呼吸曲線 ────────────────────────────────────────────────────────────────

def breathe(phase: float) -> float:
    """對稱餘弦呼吸曲線，模擬自然的吸氣／呼氣節奏。

    phase ∈ [0, 1] 代表一個完整呼吸週期。
    回傳 [0, 1]：phase=0 時為 0（吐氣底部），phase=0.5 時為 1（吸氣頂部），
    phase=1 時回到 0。

    Args:
        phase: 週期進度，範圍 [0, 1]。

    Returns:
        呼吸強度，範圍 [0, 1]。
    """
    # 用餘弦讓曲線自然對稱，省去手工調參
    return (1.0 - math.cos(phase * 2.0 * math.pi)) / 2.0


# ─── 色彩混合 ────────────────────────────────────────────────────────────────

def blend(fg: str, bg: str, alpha: float) -> str:
    """對兩個十六進位色彩做線性插值，模擬 alpha 合成效果。

    tkinter Canvas 不支援真正的 alpha 透明，因此預先計算混合色再填入。

    Args:
        fg:    前景色，格式 "#RRGGBB"。
        bg:    背景色，格式 "#RRGGBB"。
        alpha: 不透明度，0.0 = 完全透明（回傳 bg），1.0 = 完全不透明（回傳 fg）。

    Returns:
        混合後的色彩字串，格式 "#RRGGBB"（大寫）。
    """
    # 解析前景色三通道
    fg_r = int(fg[1:3], 16)
    fg_g = int(fg[3:5], 16)
    fg_b = int(fg[5:7], 16)
    # 解析背景色三通道
    bg_r = int(bg[1:3], 16)
    bg_g = int(bg[3:5], 16)
    bg_b = int(bg[5:7], 16)

    # 夾住 alpha，避免超出範圍產生溢位
    alpha = max(0.0, min(1.0, alpha))

    # 各通道線性插值：result = bg + (fg - bg) * alpha
    r = round(bg_r + (fg_r - bg_r) * alpha)
    g = round(bg_g + (fg_g - bg_g) * alpha)
    b = round(bg_b + (fg_b - bg_b) * alpha)

    return f"#{r:02X}{g:02X}{b:02X}"


# ─── 漣漪效果 ────────────────────────────────────────────────────────────────

class Ripple:
    """單一擴張漣漪環，在 RMS 音量峰值時由 gui.py 發射。

    漣漪半徑從 r0 線性擴張到 r1，透明度同步從 a0 線性衰減到 0，
    整個過程歷時 `duration` 秒。到期後 state() 回傳 None，由呼叫端移除。

    設計為 fire-and-forget：建立後不需要外部管理，只需定期呼叫 state()。
    """

    # 使用 __slots__ 減少記憶體用量（每幀可能有多個 Ripple 同時存在）
    __slots__ = ("t0", "dur", "r0", "r1", "a0")

    def __init__(
        self,
        start_time: float,   # 發射時刻（time.perf_counter()）
        duration: float = 1.2,
        r0: float = 140.0,   # 起始半徑（像素）
        r1: float = 180.0,   # 終止半徑（像素）
        a0: float = 0.4,     # 起始透明度（0~1）
    ) -> None:
        self.t0  = start_time
        self.dur = duration
        self.r0  = r0
        self.r1  = r1
        self.a0  = a0

    def state(self, now: float) -> Optional[tuple[float, float]]:
        """取得漣漪在 `now` 時刻的（半徑, 透明度），過期則回傳 None。

        Args:
            now: 目前時刻（time.perf_counter()）。

        Returns:
            (radius, alpha) tuple，或 None（漣漪已到期）。
        """
        # 計算正規化時間（0~1）
        t = (now - self.t0) / self.dur
        if t >= 1.0 or t < 0.0:
            return None   # 到期或尚未發射，通知呼叫端移除
        return (
            self.r0 + (self.r1 - self.r0) * t,   # 半徑：線性擴張
            self.a0 * (1.0 - t),                  # 透明度：線性衰減到 0
        )
