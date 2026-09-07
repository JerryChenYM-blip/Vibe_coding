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


#  淺色主題的波形墨色（抄自參考設計 `淺色玻璃強化.html` 的 .bar）
_INK        = (0x4a, 0x4a, 0x52)   # 主體
_INK_TRACK  = (0xd8, 0xd8, 0xde)   # 未達到的軌道底——讓人看得出「還有多少空間」
_INK_WARN   = (0xb4, 0x53, 0x09)   # 接近削波
_INK_CLIP   = (0xb9, 0x1c, 0x1c)   # 削波
#  開始上色的振幅門檻：低於此一律深墨。0.78 是目視調的——再低會讓正常說話
#  就開始泛色、又回到「一直喊狼來了」的老problem。
_INK_TINT_FROM = 0.78


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

        # ── 顏色策略：深色主題「色溫斜坡」、淺色主題「墨色為主、只有削波才上色」──
        #
        # 為什麼兩套不一樣：色溫斜坡（青→琥珀→橘）在深色底上是漂亮的能量指示，
        # 但在淺色底上①對比不夠、②整條一直在變色反而讓「破音」這件事看不出來
        # ——講話時色相一直動，使用者其實分不出「有點大聲」和「真的破音」。
        #
        # 淺色改成參考設計的做法（MeetingNotes `淺色玻璃強化.html`）：
        # 主體 #4a4a52 深墨、軌道 #d8d8de 淺灰、同色柔影（淺色底上用陰影不用發光，
        # 白底加亮還是白——這在本檔的輝光合成那段已經踩過同一個坑）。
        # **顏色只保留給削波**，符合專案 v2.26.0 定的「WARN 只給削波」原則。
        if self._is_light:
            tip = _INK          # 平常就是深墨，不隨音量變色
            core = _mix(_INK, (10, 30, 38), 0.35)
        else:
            tip = _hex_rgb(tokens.energy_color(min(1.0, max(0.0, level))))
            core = _mix(tip, (255, 255, 255), 0.45)

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

        # 音量超過門檻多少（0 = 還很安全、1 = 已達削波邊緣）。算一次給下面用。
        lvl_t = 0.0
        if self._is_light and level > _INK_TINT_FROM:
            lvl_t = min(1.0, (level - _INK_TINT_FROM) / (1.0 - _INK_TINT_FROM))

        # ── 淺色主題專屬：先畫「軌道底」（滿高的淺灰 bar）──────────────────
        # 為什麼只有淺色有：深色主題的 bar 本身就是發光體，背後再放一條灰軌道
        # 會把輝光壓掉；淺色主題的 bar 是墨色實體，有軌道才看得出「這根還有
        # 多少空間可以長」——那是音量餘裕的視覺線索，抄自參考設計的 .bar 底層。
        if self._is_light:
            full_h = max(1.5, max_h)
            for i in range(n):
                x0 = i * (bw + self._GAP)
                d.rounded_rectangle(
                    [x0, cy - full_h, x0 + bw, cy + full_h],
                    radius=bw / 2.0, fill=_INK_TRACK,
                )

        # ── 第二遍：畫 bar 本體（蓋在輝光上）───────────────────────────────
        for i in range(n):
            h = heights[i]
            x0 = i * (bw + self._GAP)
            x1 = x0 + bw
            r = bw / 2.0

            # 淺色主題：只有「接近或超過削波」的那幾根才上色，其餘維持深墨。
            # 這是刻意的資訊設計——連續色溫斜坡會讓整條一直變色、反而讓真正的
            # 削波看不出來；改成「平常全灰、爆掉的那幾根變紅」，一眼就看得到
            # 是哪幾根爆掉。符合專案「WARN 只給削波」的既有原則。
            if self._is_light:
                # **上色與否由「整體音量 level」決定，不是由「這根 bar 多高」。**
                #   第一版用 bar 高度比例判斷，結果普通音量（level 0.45）中央就
                #   整片變橘——因為頻譜形狀讓中央的 bar 本來就接近滿格，跟使用者
                #   講多大聲無關。存 PNG 一看就知道錯了。
                #   正確語意：level 才是「你多大聲」，bar 高度是「頻譜長什麼形狀」。
                if clipping:
                    tip = _INK_CLIP
                    core = _mix(_INK_CLIP, (60, 0, 0), 0.25)
                elif lvl_t > 0.0:
                    # 只有真的偏大聲時才染，且越高的 bar 染得越明顯
                    v_ratio = h / max(1e-6, max_h)
                    t = lvl_t * min(1.0, v_ratio / 0.85)
                    tip = _mix(_INK, _INK_WARN, t)
                    core = _mix(tip, (10, 30, 38), 0.35)
                else:
                    tip = _INK
                    core = _mix(_INK, (10, 30, 38), 0.35)

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
