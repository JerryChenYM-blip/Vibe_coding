"""glass_render.py — 液態玻璃面板的 PIL 離屏渲染。

## 為什麼不用 tkinter Canvas 直接畫

參考設計（Claude Design 的 `設計系統.dc.html`）的玻璃是 CSS `backdrop-filter:
blur() saturate()` + 半透明填色 + 內緣高光 + 鏡面漸層 + 柔和陰影。
**tkinter Canvas 沒有 alpha 合成、沒有模糊、沒有 backdrop-filter**（見 CLAUDE.md
坑點 #2）。

做法與 `waveform_render.py` 同一條路：PIL 在記憶體畫好 → `ImageTk.PhotoImage` →
貼上 Canvas。PIL 有真高斯模糊、真 alpha、真漸層。

## 跟波形渲染器的關鍵差異：玻璃是**靜態**的

波形每幀都要重畫（30 FPS），玻璃只在「視窗尺寸變了」或「主題切換」時才需要重畫。
所以這裡的策略是 **算一次、快取起來**，平常每幀成本是零。
實測單塊面板連底圖約 12–14 ms——就算每次 resize 都重算也感覺不到。

## ⚠️ 一個設計上的事實，實作前必須知道

**玻璃疊在「平的、幾乎純白的底」上，看起來就只是一塊白色卡片——這在 CSS 裡也一樣，
不是 PIL 的限制。** 參考設計自己就把某一組標成「這就是你覺得看不出來的那一組」。

玻璃要讀得出來，**底下必須有東西可以被模糊**。所以呼叫端有責任先鋪一層有內容的底
（`make_backdrop()` 的漸層 + 環境光暈），否則這整套視覺投資會看不出效果。

## 座標與單位

所有尺寸都是**像素**（不是 pt）。呼叫端自行處理 Retina 縮放——目前專案其他
Canvas 繪圖（波形、歷史清單）也都是這個慣例，不在這裡特殊處理。
"""

from __future__ import annotations

import math
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter


# ─────────────────────────────────────────────────────────────────────────────
#  小工具
# ─────────────────────────────────────────────────────────────────────────────

def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _mix(a, b, t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _paste_tint(base: Image.Image, color: tuple[int, int, int], mask: Image.Image) -> None:
    """把一個顏色用 mask 當 alpha 疊上去（就地修改 base）。

    為什麼要包成函式：PIL 沒有「畫一條半透明線」這種 API——線、圓角框、高光
    全部都要先畫進一張 L 模式遮罩、再用它當 alpha 貼色。直接用實色畫是
    **本模組開發時踩過的第一個坑**：外緣線 `rgba(15,23,42,.10)` 用實色畫出來
    是一條深藍黑硬邊，整塊玻璃看起來像貼了黑框。
    """
    base.paste(Image.new("RGB", base.size, color), (0, 0), mask)


# ─────────────────────────────────────────────────────────────────────────────
#  底圖
# ─────────────────────────────────────────────────────────────────────────────

def make_backdrop(
    width: int,
    height: int,
    top: str,
    bottom: str,
    *,
    ambient: Optional[list[tuple[float, float, float, str]]] = None,
) -> Image.Image:
    """視窗底：垂直漸層 + 可選的環境光暈。

    `ambient` 是 [(相對x, 相對y, 相對半徑, hex色), ...]，用來鋪「可以被玻璃模糊的
    東西」。不給就是純漸層——那樣玻璃會幾乎看不出來（見 module docstring 的警告）。
    """
    c0, c1 = _rgb(top), _rgb(bottom)
    img = Image.new("RGB", (width, height), c0)
    d = ImageDraw.Draw(img)
    for y in range(height):
        d.line([(0, y), (width, y)], fill=_mix(c0, c1, y / max(1, height - 1)))

    if ambient:
        # 光暈畫在半解析度再放大——高斯模糊成本與像素數成正比，而且放大回來
        # 反而更柔（同 waveform_render 的做法）。
        gw, gh = max(1, width // 2), max(1, height // 2)
        lay = Image.new("RGB", (gw, gh), (0, 0, 0))
        ld = ImageDraw.Draw(lay)
        for rx, ry, rr, col in ambient:
            cx, cy, r = rx * gw, ry * gh, rr * min(gw, gh)
            ld.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_rgb(col))
        lay = lay.filter(ImageFilter.GaussianBlur(max(4, min(gw, gh) // 8)))
        lay = lay.resize((width, height), Image.BILINEAR)
        # screen（濾色）只會變亮。**不能用 ImageChops.add(scale=N)**——那個 scale
        # 是「相加之後的除數」，開發時填 2.2 想當強度用，結果整張底圖被除以 2.2、
        # 直接暗掉一半（存成 PNG 打開才發現）。
        img = ImageChops.screen(img, lay)
    return img


def radial_backdrop(
    width: int,
    height: int,
    base: str,
    blobs: list[tuple[float, float, float, float, str, float]],
) -> Image.Image:
    """純色底 + 多團徑向光暈。blobs = [(相對x, 相對y, 相對w, 相對h, hex, 濃度)]。

    這是**淺色主題能不能用玻璃的關鍵**。實測（存 PNG 目視）：玻璃疊在平的近白底上
    就只是一塊白卡片，看不出任何玻璃感——這不是 PIL 的限制，CSS 也一樣，因為玻璃的
    視覺原理就是「透出並模糊底下的東西」，底下是白的透出來還是白的。

    參考設計（MeetingNotes `淺色玻璃強化.html`）的解法是鋪兩團**很大、很淡、中心在
    畫布外**的徑向光暈：
        radial-gradient(620px 620px at  3%  -8%, rgba(120,150,190,.30))  冷藍
        radial-gradient(440px 420px at 14% 112%, rgba(150,175,155,.24))  灰綠
    濃度只有 .30 / .24，而且中心在畫布外面——淡到不會讓頁面看起來彩色，
    但足夠讓玻璃有東西可以透。本專案沿用同一組參數（見 LIGHT_BLOBS / DARK_BLOBS）。

    PIL 沒有原生的 radial-gradient，用同心橢圓逐層疊出徑向衰減再高斯模糊。
    衰減指數 1.6 是目視調的（1.0 太硬像個圓餅、2.5 太散幾乎看不到）。
    """
    img = Image.new("RGB", (width, height), _rgb(base))
    for rx, ry, rw, rh, col, alpha in blobs:
        cx, cy = rx * width, ry * height
        ax, ay = rw * width / 2, rh * height / 2
        lay = Image.new("L", (width, height), 0)
        ld = ImageDraw.Draw(lay)
        STEPS = 26
        for s in range(STEPS, 0, -1):
            t = s / STEPS
            ld.ellipse([cx - ax * t, cy - ay * t, cx + ax * t, cy + ay * t],
                       fill=int(alpha * 255 * (1 - t) ** 1.6))
        lay = lay.filter(ImageFilter.GaussianBlur(max(4, min(width, height) // 14)))
        img.paste(Image.new("RGB", (width, height), _rgb(col)), (0, 0), lay)
    return img


#  環境光暈參數（抄自參考設計，見 radial_backdrop docstring）
LIGHT_BLOBS = [(0.03, -0.08, 1.30, 1.44, "#7896be", 0.30),
               (0.14,  1.12, 0.92, 0.98, "#96af9b", 0.24)]
DARK_BLOBS  = [(0.03, -0.08, 1.30, 1.44, "#3a588a", 0.46),
               (0.14,  1.12, 0.92, 0.98, "#3a6052", 0.34)]


# ─────────────────────────────────────────────────────────────────────────────
#  玻璃面板
# ─────────────────────────────────────────────────────────────────────────────

class GlassSpec:
    """一級玻璃的材質參數，對應設計稿的 --g1* / --g2* 那組 token。"""

    __slots__ = ("fill", "fill_a", "edge", "edge_a", "hi", "hi_a",
                 "blur", "spec_a", "shadow_a", "shadow_dy", "shadow_blur")

    def __init__(self, fill: str, fill_a: int, edge: str, edge_a: int,
                 hi: str, hi_a: int, blur: float, spec_a: int,
                 shadow_a: int = 52, shadow_dy: int = 8, shadow_blur: float = 13):
        self.fill, self.fill_a = _rgb(fill), fill_a
        self.edge, self.edge_a = _rgb(edge), edge_a
        self.hi, self.hi_a = _rgb(hi), hi_a
        self.blur, self.spec_a = blur, spec_a
        self.shadow_a, self.shadow_dy, self.shadow_blur = shadow_a, shadow_dy, shadow_blur


def draw_glass(
    backdrop: Image.Image,
    box: tuple[int, int, int, int],
    radius: int,
    spec: GlassSpec,
) -> Image.Image:
    """在 backdrop 上就地畫一塊玻璃面板。box = (x, y, w, h)，回傳同一張圖。

    步驟順序是設計過的，換順序會壞：
      ① 先畫投影（在面板之外）
      ② 取面板底下那塊背景做高斯模糊 ← 這是 backdrop-filter 的 PIL 等價
      ③ 疊半透明填色
      ④ 鏡面漸層（上緣亮、往下消失）
      ⑤ 內緣高光（頂端 1px）
      ⑥ 外緣線
      ⑦ 圓角遮罩貼回去
    """
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return backdrop

    # ① 投影。**必須把面板自己那塊從遮罩挖掉**，否則面板底下也被抹黑、
    #    玻璃透出來的是髒色（開發時踩過）。
    sh = Image.new("L", backdrop.size, 0)
    sd = ImageDraw.Draw(sh)
    sd.rounded_rectangle([x, y + spec.shadow_dy, x + w, y + h + spec.shadow_dy],
                         radius=radius, fill=spec.shadow_a)
    sh = sh.filter(ImageFilter.GaussianBlur(spec.shadow_blur))
    ImageDraw.Draw(sh).rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=0)
    backdrop.paste(Image.new("RGB", backdrop.size, (15, 23, 42)), (0, 0), sh)

    # ② 模糊底下那塊
    region = backdrop.crop((x, y, x + w, y + h)).filter(ImageFilter.GaussianBlur(spec.blur))

    # ③ 半透明填色
    region = Image.blend(region, Image.new("RGB", (w, h), spec.fill), spec.fill_a / 255)

    # ④ 鏡面漸層：只在上緣 46%，線性衰減到 0
    top = max(1, int(h * 0.46))
    sp = Image.new("L", (w, h), 0)
    spd = ImageDraw.Draw(sp)
    for i in range(top):
        spd.line([(0, i), (w, i)], fill=int(spec.spec_a * (1 - i / top)))
    _paste_tint(region, (255, 255, 255), sp)

    # ⑤ 內緣高光
    hl = Image.new("L", (w, h), 0)
    ImageDraw.Draw(hl).line([(radius * 0.6, 0), (w - radius * 0.6, 0)],
                            fill=spec.hi_a, width=1)
    _paste_tint(region, spec.hi, hl)

    # ⑥ 外緣線（吃 alpha，見 _paste_tint 的註解）
    el = Image.new("L", (w, h), 0)
    ImageDraw.Draw(el).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius,
                                         outline=spec.edge_a, width=1)
    _paste_tint(region, spec.edge, el)

    # ⑦ 圓角貼回
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    backdrop.paste(region, (x, y), m)
    return backdrop


# ─────────────────────────────────────────────────────────────────────────────
#  設計稿的兩級玻璃（值抄自 Claude Design 的 設計系統.dc.html）
# ─────────────────────────────────────────────────────────────────────────────

#  G1 = 浮起來的主要面板；G2 = 貼在底上的表面。
#  alpha 是把設計稿的 0..1 乘 255 取整（.72 → 184、.46 → 117…）。

G1_LIGHT = GlassSpec("#FFFFFF", 184, "#0F172A", 26, "#FFFFFF", 235, blur=14, spec_a=112)
G2_LIGHT = GlassSpec("#FFFFFF", 117, "#0F172A", 18, "#FFFFFF", 168, blur=10, spec_a=61,
                     shadow_a=26, shadow_dy=4, shadow_blur=8)

G1_DARK = GlassSpec("#2C2F36", 179, "#FFFFFF", 26, "#FFFFFF", 28, blur=14, spec_a=23,
                    shadow_a=86, shadow_dy=10, shadow_blur=16)
G2_DARK = GlassSpec("#FFFFFF", 11, "#FFFFFF", 15, "#FFFFFF", 15, blur=10, spec_a=13,
                    shadow_a=40, shadow_dy=4, shadow_blur=9)


def specs_for(is_light: bool) -> tuple[GlassSpec, GlassSpec]:
    """回傳 (G1, G2)。主題切換時呼叫端要重新取一次並清掉快取。"""
    return (G1_LIGHT, G2_LIGHT) if is_light else (G1_DARK, G2_DARK)
