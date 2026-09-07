"""waveform.py — Aperture「Spectral Bands」波形引擎的純 Python 實作。

零 Tk 相依、零 numpy 相依（每幀只有 n_bars 個元素，純 list 比 numpy 快
也少一個相依），完全可單元測試。

數學規格**照抄**自 /tmp/aperture_engine.js 的 bucket()（頻譜塑形）與
drawBands()（非對稱包絡 / 峰值帽重力 / 削波）——那是唯一權威規格，這裡
不自己調參數。JS 的 back 層（第二條慢包絡，B 級模糊背層用）不在 tkinter
情境使用，故未實作。

時間軸完全由呼叫端透過 update(rms, dt_ms) 的 dt_ms 累加推進，engine 本身
不呼叫 time.time()，確保行為可決定性（deterministic）、可單元測試。
"""

from __future__ import annotations

import math
import random


class WaveformEngine:
    """Spectral Bands 波形引擎：把 RMS 音量轉成 n_bars 根 bar 的高度序列。

    每根 bar 對應頻譜上的一個頻段。數學上完全比照 aperture_engine.js 的
    bucket()（頻譜塑形：低頻重、隨機共振峰形狀）+ drawBands() 的非對稱
    包絡（attack 快、release 慢）、峰值帽重力、削波壓平。
    """

    # 削波判定門檻：lvl 超過此值視為削波（對應 JS `lvl > 0.86`）
    _CLIP_THRESHOLD = 0.86
    # 削波時的高度壓平上限（對應 JS `ceil = clip ? 0.965 : 1`）
    _CLIP_CEIL = 0.965
    # dt 的時間單位：一幀 60fps ≈ 16.667ms（對應 JS `dt = (now-last) / 16.667`）
    _FRAME_MS = 16.667

    def __init__(self, n_bars: int = 46, seed: float | None = None) -> None:
        self.n_bars = n_bars
        # phase：對應 JS `rig.ph = Math.random() * 100`，讓 jitter／地板行波
        # 有不重複的相位。seed 給定時直接當 phase 用，確保「同 seed 同輸入
        # 序列 → 同輸出」的決定性（engine 本身不吃 time.time()，唯一的隨機
        # 來源就是這裡）。
        self._phase: float = seed if seed is not None else random.random() * 100
        self._t: float = 0.0  # 內部累積時間（毫秒），只由 update() 的 dt_ms 推進
        self._cur: list[float] = [0.0] * n_bars
        self._peak: list[float] = [0.0] * n_bars
        self._clipping: bool = False

    def update(self, rms: float, dt_ms: float) -> None:
        """推進一幀包絡狀態。

        rms：音量 0..1（超出範圍會被夾住——麥克風訊號是合理可預期的外部
             邊界，不是不可能發生的情境）。
        dt_ms：距離上一幀的毫秒數。
        """
        lvl = 0.0 if rms < 0 else 1.0 if rms > 1 else rms
        self._t += dt_ms
        dt = dt_ms / self._FRAME_MS
        t = self._t
        phase = self._phase
        n = self.n_bars

        clipping = lvl > self._CLIP_THRESHOLD
        self._clipping = clipping
        ceil = self._CLIP_CEIL if clipping else 1.0

        # 中心對稱：f 是「距離中央的正規化距離」（中央 0、兩端 1），不是原本的
        # 「由左到右 0→1」。
        #
        # 為什麼要改：原本 f = i/(n-1) 配上 base = exp(-f*1.9) 與三個落在
        # f=0.06/0.20/0.40 的共振峰，等於把所有能量都堆在最左邊三分之一，右半
        # 邊天生就是平的。使用者回報「你的波都是從左邊開始，我原本的認知應該
        # 是在中間」——那不是錯覺，是這條公式的直接結果。改成距離中央後，
        # 同一組係數會讓最高點落在正中央、往兩側對稱衰減。
        #
        # 刻意不做的事：jitter（下面的 jit）仍然用 i 而不是 f，所以左右不會是
        # 像素級完美鏡像。完美鏡像看起來會像貼圖、很假；保留這點不對稱才有
        # 「活的」感覺。這是刻意的，不要「順手修正」成對稱。
        centre = 0.0 if n == 1 else (n - 1) / 2.0

        for i in range(n):
            f = 0.0 if n == 1 else abs(i - centre) / centre

            # 閒置地板：安靜時的呼吸行波（相位改吃 f，讓行波也是由中央往外擴散，
            # 跟錄音態的能量方向一致；用 i 的話閒置時會看到波由左往右跑）
            floor = 0.016 + 0.014 * (0.5 + 0.5 * math.sin(t * 0.0011 + f * 9.66))

            # 頻譜塑形（bucket）：低頻重的 base + 三個共振峰 form + 呼吸 jitter
            base = math.exp(-f * 1.9)
            form = (
                1
                + 0.95 * math.exp(-(((f - 0.06) / 0.055) ** 2))
                + 0.70 * math.exp(-(((f - 0.20) / 0.075) ** 2))
                + 0.42 * math.exp(-(((f - 0.40) / 0.11) ** 2))
            )
            jit = 0.68 + 0.52 * (0.5 + 0.5 * math.sin(t * 0.021 + i * 1.73 + phase * 3))
            bucket = min(1.0, lvl * base * form * jit * 1.85)

            tgt = max(bucket, floor)
            tgt = min(tgt, ceil)

            cur = self._cur[i]
            if tgt > cur:
                cur += (tgt - cur) * (1 - 0.5 ** (dt * 0.5))
            else:
                cur += (tgt - cur) * (1 - 0.5 ** (dt * 0.085))
            self._cur[i] = cur

            peak = self._peak[i]
            if cur > peak:
                peak = cur
            else:
                peak = max(cur, peak - 0.0016 * dt * 3)
            self._peak[i] = peak

    @property
    def bars(self) -> list[float]:
        """每根 bar 的高度比例 0..1（複本，呼叫端改動不影響內部狀態）。"""
        return list(self._cur)

    @property
    def peaks(self) -> list[float]:
        """每根 bar 的峰值帽位置 0..1（複本）。"""
        return list(self._peak)

    @property
    def clipping(self) -> bool:
        """最近一次 update() 的音量是否超過削波門檻。"""
        return self._clipping

    def reset(self) -> None:
        """歸零包絡狀態（例如重新開始一次錄音時呼叫）。phase／n_bars 不變。"""
        self._t = 0.0
        self._cur = [0.0] * self.n_bars
        self._peak = [0.0] * self.n_bars
        self._clipping = False
