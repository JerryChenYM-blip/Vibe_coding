"""waveform_render.py — 錄音態大波形的「華麗版」渲染器（PIL 離屏繪製）。

## 為什麼不用 tkinter Canvas 直接畫

tkinter Canvas 只有實心圖元（矩形／多邊形／線／文字），**沒有** alpha 合成、
沒有漸層、沒有模糊。既有的 `_draw_aperture_bars()` 已經把 Canvas 能做的做到
頂了：透明度靠 `blend(fg, bg, alpha)` 做 RGB 線性插值「假裝」，輝光靠疊好幾層
顏色逐漸接近背景色來「暗示」。要真的做出漸層 / 高斯輝光 / 倒影，只能換一條
繪製管線。

這裡的做法是：用 PIL 在記憶體裡把整幀畫好（PIL 有真 alpha、真高斯模糊、
真漸層），再轉成一張 PhotoImage 貼到 Canvas 上。Canvas 只負責顯示一張圖，
不再負責畫幾百個圖元。

實測（640×200、46 根 bar、含高斯輝光）：**5.0 ms/幀 ≈ 199 FPS**。30 FPS 的
預算是 33 ms，用掉不到六分之一。PIL 本來就是既有相依（`icons.py` 的圖示是
純 PIL 手繪的），不增加安裝負擔。

## 效能上的刻意取捨

1. **輝光在半解析度算完再放大**：高斯模糊成本與像素數成正比，半解析度是
   1/4 的像素。放大回來反而讓輝光更柔，是雙贏，不是妥協。
2. **buffer 重用**：Image 物件掛在 renderer 實例上重複使用，避免每幀配置
   ~1.5 MB 再交給 GC。冷啟動第一幀會慢一點，之後穩定。
3. **色彩查表沿用 `tokens.energy_color()`**：不自己另做一套色溫，否則大波形
   與上方細條、迷你 HUD 會是兩種顏色語言。

## 刻意不做的事

- **不畫峰值帽**：小尺寸的細條需要峰值帽來補足資訊密度（bar 太窄看不出動態），
  大尺寸下 bar 本身的高度變化已經夠清楚，再加帽子只會變吵。
- **不做每根 bar 的獨立色相**：色溫吃「整體音量」而非「該根 bar 的高度」，
  否則安靜時中央那幾根會無故變成高音量的顏色，誤導使用者以為破音。
"""

from __future__ import annotations

import math
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter

import tokens


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """線性混色，t=0 全 a、t=1 全 b。"""
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


class GlowWaveRenderer:
    """把一組 bar 高度（0..1）畫成一張帶漸層／輝光／倒影的 PIL Image。

    用法：
        r = GlowWaveRenderer(640, 240, n_bars=46)
        img = r.render(bars, level=rms, clipping=False)
        photo = ImageTk.PhotoImage(img)     # 呼叫端自行轉換並保留參考
    """

    # 輝光降採樣倍率（2 = 半解析度）。越大越快、輝光越柔、細節越糊。
    _GLOW_SCALE = 2
    # 高斯模糊半徑（在降採樣後的座標系上算，所以視覺半徑是這個值的 _GLOW_SCALE 倍）
    _GLOW_BLUR = 6
    # 輝光疊回主圖的強度
    _GLOW_MIX = 0.55
    # 倒影相對本體的亮度起點（往下線性衰減到 0）
    _REFLECT_TOP_ALPHA = 0.30
    # bar 之間的間距（像素）
    _GAP = 5.0

    def __init__(
        self, width: int, height: int, n_bars: int = 46, band_h: Optional[int] = None,
    ) -> None:
        self.width = int(width)
        self.height = int(height)
        self.n_bars = int(n_bars)
        # band_h：波形本身佔多高（畫面垂直置中），其餘留白。
        # 為什麼要跟 height 分開：呼叫端需要一張「蓋住整個轉錄流區」的不透明畫布
        # 來遮住底下的卡片，但波形本身不該跟著長到 700pt 高——那會變成一面牆。
        # 早期版本是靠 pack_forget() 把轉錄流整塊拿掉來讓位，實測連續踩到三個
        # Tk pack 的坑（順序跑到最後、CTkScrollableFrame 的 winfo_manager 不轉發、
        # 底列往上遞補），改成「畫布蓋住、波形只佔中間一條」完全不用動版面。
        self.band_h = int(band_h) if band_h else self.height

        # 背景／基準線顏色跟著 tokens 走，主題切換時呼叫端要重建 renderer
        self._bg = _hex_rgb(tokens.BG)
        self._surf = _hex_rgb(tokens.SURF_1)
        self._is_light = getattr(tokens, "_THEME", "dark") == "light"

        # 重用 buffer（見 module docstring「效能上的刻意取捨」第 2 點）
        self._base = Image.new("RGB", (self.width, self.height), self._bg)
        gw = max(1, self.width // self._GLOW_SCALE)
        gh = max(1, self.height // self._GLOW_SCALE)
        self._glow = Image.new("RGB", (gw, gh), (0, 0, 0))
        self._backdrop: Optional[Image.Image] = None

    # ── 背景（只算一次，之後每幀 paste）─────────────────────────────────────

    def _build_backdrop(self) -> Image.Image:
        """垂直漸層底 + 中央光暈 + 上下暈影。

        為什麼要預算：這層完全不隨音量改變，每幀重算是純浪費。實測預算後
        每幀省下約 1.8 ms（佔原本 5.0 ms 的三分之一強）。
        """
        img = Image.new("RGB", (self.width, self.height), self._bg)
        d = ImageDraw.Draw(img)
        cy = self.height / 2.0
        # 中央亮、上下暗——模擬光從中軸線散出來
        near = _mix(self._bg, self._surf, 0.55 if not self._is_light else 0.75)
        for y in range(self.height):
            t = min(1.0, abs(y - cy) / (self.band_h / 2.0))   # 0 中央 → 1 波形帶邊緣
            fade = (1.0 - t) ** 2                     # 平方讓中央的亮帶收窄、更像光帶
            d.line([(0, y), (self.width, y)], fill=_mix(self._bg, near, fade))
        return img

    # ── 主繪製 ────────────────────────────────────────────────────────────

    def render(self, bars, level: float = 0.0, clipping: bool = False) -> Image.Image:
        """畫一幀。bars 是長度 n_bars 的 0..1 序列。

        繪製順序是「背景 → 輝光 → bar 本體」，不是「bar → 再疊輝光」。輝光本來
        就該在物體**後面**：疊在上面會把 bar 自己的漸層洗掉一層灰。
        """
        if self._backdrop is None:
            self._backdrop = self._build_backdrop()

        img = self._base
        img.paste(self._backdrop, (0, 0))

        glow = self._glow
        glow.paste((0, 0, 0), (0, 0, glow.width, glow.height))
        gd = ImageDraw.Draw(glow)

        n = min(self.n_bars, len(bars)) or 1
        cy = self.height / 2.0
        bw = max(2.0, (self.width - (n - 1) * self._GAP) / n)
        max_h = self.band_h / 2.0 - 14               # 上下各留 14px 讓輝光有地方擴散
        gs = self._GLOW_SCALE

        # 色溫吃整體音量而非單根 bar 高度（見 module docstring「刻意不做的事」）
        tip = _hex_rgb(tokens.energy_color(min(1.0, max(0.0, level))))
        # 中軸線附近更亮：往白（深色主題）或往深墨（淺色主題）推
        core = _mix(tip, (255, 255, 255) if not self._is_light else (10, 30, 38), 0.45)

        # ── 第一遍：只畫輝光圖層（半解析度）─────────────────────────────────
        heights = []
        for i in range(n):
            v = bars[i]
            v = 0.0 if v < 0 else 1.0 if v > 1 else v
            h = max(1.5, v * max_h)
            heights.append(h)
            x0 = i * (bw + self._GAP)
            gd.rectangle([x0 / gs, (cy - h) / gs, (x0 + bw) / gs, (cy + h) / gs], fill=tip)

        # ── 輝光合成（在 bar 之前疊，讓光在物體後面）───────────────────────
        # 深色主題用 screen（濾色，只變亮不變暗）——這是繪圖軟體做輝光的標準
        # 運算子。**不能用 blend()**：輝光圖層是「黑底 + 彩色 bar」，跟主圖
        # blend 等於把整張圖往黑色拉，實測會把白底洗成死灰。
        #
        # 淺色主題不能用 screen（白底加亮還是白），改成「用模糊後的亮度當
        # alpha、把 bar 的顏色鋪上去」＝同色柔影。**不能用 invert(blurred)**：
        # invert 連色相一起反轉，實測青色 bar 周圍會冒出粉橘色光暈、琥珀 bar
        # 周圍冒出藍色光暈，完全走味。
        blurred = glow.filter(ImageFilter.GaussianBlur(self._GLOW_BLUR))
        blurred = blurred.resize((self.width, self.height), Image.BILINEAR)
        if self._is_light:
            mask = blurred.convert("L").point(lambda p: int(p * self._GLOW_MIX))
            img.paste(Image.new("RGB", (self.width, self.height), tip), (0, 0), mask)
        else:
            img.paste(Image.blend(img, ImageChops.screen(img, blurred), self._GLOW_MIX), (0, 0))

        d = ImageDraw.Draw(img)

        # ── 第二遍：畫 bar 本體（蓋在輝光上）───────────────────────────────
        for i in range(n):
            h = heights[i]
            x0 = i * (bw + self._GAP)
            x1 = x0 + bw
            r = bw / 2.0

            # 本體：由中軸線往外做垂直漸層（核心亮 → 尖端回到色溫本色）。
            # 一根 bar 切成 STEPS 段畫，段數固定不隨高度變——高度變時段數也變的話
            # 漸層的「條紋密度」會隨音量抖動，看起來會閃。
            STEPS = 7
            for s in range(STEPS):
                f0 = s / STEPS
                f1 = (s + 1) / STEPS
                col = _mix(core, tip, f0)
                y_top0, y_top1 = cy - h * f1, cy - h * f0
                y_bot0, y_bot1 = cy + h * f0, cy + h * f1
                d.rectangle([x0, y_top0, x1, y_top1], fill=col)
                d.rectangle([x0, y_bot0, x1, y_bot1], fill=col)

            # 圓端帽（只有夠高的 bar 才畫，矮 bar 畫圓帽會變成一顆球）
            if h > r:
                d.ellipse([x0, cy - h - r, x1, cy - h + r], fill=tip)
                d.ellipse([x0, cy + h - r, x1, cy + h + r], fill=tip)

        # 中軸線：一道細亮帶，強化「能量從中間散出」
        axis = _mix(core, self._bg, 0.25 if not self._is_light else 0.55)
        d.line([(0, cy), (self.width, cy)], fill=axis)

        # 削波警示：整幀往琥珀推一點點（不是換色，是「燙」的感覺）
        if clipping:
            warm = Image.new("RGB", (self.width, self.height), _hex_rgb(tokens.WARN))
            return Image.blend(img, warm, 0.12)

        # 回傳複本而不是內部緩衝區。共用緩衝區省下的配置成本（實測 0.2ms／幀，
        # 佔 33ms 預算的 0.6%）遠小於它的風險：呼叫端只要多留住一幀（存起來
        # 比對、做動畫、除錯時放進 list），拿到的就會是「之後某一幀」的內容，
        # 而且完全不會報錯。開發這支渲染器時就中招過一次——連續算 5 種音量存進
        # list，5 張全是同一張。這種 bug 不會炸、只會讓人懷疑演算法壞了。
        return img.copy()


def build_idle_bars(n_bars: int, t_ms: float) -> list:
    """閒置態的安靜行波（不需要跑完整 WaveformEngine 的場合用）。

    刻意獨立成純函式：閒置畫面不該把 engine 的包絡狀態往前推，否則一開始
    錄音時 attack 會從「閒置累積下來的值」起跳、第一下反應偏鈍。
    """
    centre = 0.0 if n_bars == 1 else (n_bars - 1) / 2.0
    out = []
    for i in range(n_bars):
        f = 0.0 if n_bars == 1 else abs(i - centre) / centre
        out.append(0.016 + 0.030 * (0.5 + 0.5 * math.sin(t_ms * 0.0011 + f * 9.66)))
    return out
