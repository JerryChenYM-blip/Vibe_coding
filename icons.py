"""
Lucide 風格手繪圖示集（Pillow 實作）。

為什麼不用 SVG 函式庫？
  • cairosvg 需要 libcairo（系統依賴，macOS 安裝容易失敗）
  • svglib / reportlab 在 Python 3.13 上無法編譯
  • 只需要約 12 個圖示——手繪成本遠低於維護一套 toolchain

渲染策略：
  • 每個圖示在 24×24 座標空間（Lucide 標準）中定義
  • 以 4 倍超採樣（96×96）繪製後再用 LANCZOS 縮小，模擬反鋸齒
  • 線寬 2px、round linecap、round linejoin——匹配 Lucide 視覺風格

公開 API：
  get_icon(name, size, color)         → CTkImage（供 CTkButton / CTkLabel 使用）
  get_canvas_icon(name, size, color)  → PhotoImage（供 tk.Canvas.create_image 使用）
  ICON_NAMES                          → 所有可用圖示名稱的列表
"""

from __future__ import annotations

from functools import lru_cache
from typing import Callable

from PIL import Image, ImageDraw, ImageTk
import customtkinter as ctk


# ─── 渲染常數 ────────────────────────────────────────────────────────────────

VIEW      = 24     # Lucide 標準 viewport 大小（單位：邏輯像素）
SS        = 4      # 超採樣倍數：在 4× 大小畫，再縮小以模擬 AA
STROKE_VP = 2.0    # 線寬（viewport 單位）— Lucide 預設值


# ─── 繪圖基元 ────────────────────────────────────────────────────────────────

class _Pen:
    """座標轉換輔助類別：將 24-viewport 座標映射到超採樣像素座標。

    所有圖示繪製函式都接收一個 _Pen 實例，用它的方法畫線、圓、矩形等。
    _Pen 本身負責座標縮放，圖示函式只需在 24×24 的邏輯空間思考。
    """

    def __init__(self, size_px: int, color: str) -> None:
        """初始化畫布與縮放係數。

        Args:
            size_px: 最終輸出尺寸（像素），畫布為此值 × SS 倍。
            color:   筆觸色彩，十六進位格式 "#RRGGBB"。
        """
        # 畫布大小 = 輸出尺寸 × 超採樣倍數；背景透明（RGBA）
        self.img   = Image.new("RGBA", (size_px * SS, size_px * SS), (0, 0, 0, 0))
        self.draw  = ImageDraw.Draw(self.img)
        self.color = color
        self.size  = size_px
        self._k    = size_px * SS / VIEW               # viewport → 像素的縮放係數
        self._sw   = max(1, round(STROKE_VP * self._k))  # 實際像素線寬（最少 1px）

    def _p(self, x: float, y: float) -> tuple[float, float]:
        """將 viewport 座標轉換為超採樣像素座標。"""
        return (x * self._k, y * self._k)

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        """畫一條直線（viewport 座標）。"""
        self.draw.line([self._p(x1, y1), self._p(x2, y2)],
                       fill=self.color, width=self._sw, joint="curve")

    def polyline(self, pts: list[tuple[float, float]]) -> None:
        """畫一條折線（多個頂點，viewport 座標）。"""
        self.draw.line([self._p(*p) for p in pts],
                       fill=self.color, width=self._sw, joint="curve")

    def rect(self, x1: float, y1: float, x2: float, y2: float, r: float = 0) -> None:
        """畫一個（可選圓角）矩形邊框（viewport 座標）。

        Args:
            r: 圓角半徑（viewport 單位），0 代表直角。
        """
        box = [self._p(x1, y1), self._p(x2, y2)]
        if r > 0:
            self.draw.rounded_rectangle(box, radius=r * self._k,
                                         outline=self.color, width=self._sw)
        else:
            self.draw.rectangle(box, outline=self.color, width=self._sw)

    def ellipse(self, cx: float, cy: float, rx: float, ry: float | None = None) -> None:
        """畫一個橢圓邊框（viewport 座標）。ry 省略時為正圓。"""
        ry = ry if ry is not None else rx
        box = [self._p(cx - rx, cy - ry), self._p(cx + rx, cy + ry)]
        self.draw.ellipse(box, outline=self.color, width=self._sw)

    def dot(self, cx: float, cy: float, r: float = 1.0) -> None:
        """畫一個填滿的實心圓（viewport 座標）。用於按鍵點等細節。"""
        box = [self._p(cx - r, cy - r), self._p(cx + r, cy + r)]
        self.draw.ellipse(box, fill=self.color)

    def finish(self) -> Image.Image:
        """完成繪製，縮小到目標尺寸（LANCZOS 高品質縮小）。"""
        return self.img.resize(
            (self.size, self.size),
            Image.Resampling.LANCZOS,
        )


# ─── 圖示定義（所有座標皆在 24-viewport 空間，Lucide 風格）────────────────────

def _i_copy(p: _Pen) -> None:
    """複製圖示：兩個互相疊偏的圓角矩形。"""
    p.rect(9, 9, 20, 20, r=2)   # 後面的矩形（右下偏移）
    p.rect(4, 4, 15, 15, r=2)   # 前面的矩形（左上，遮住一部分後面）


def _i_download(p: _Pen) -> None:
    """下載圖示：向下箭頭 + 底部托盤。"""
    p.polyline([(4, 15), (4, 20), (20, 20), (20, 15)])  # 底部托盤（U 形）
    p.line(12, 4, 12, 16)                                 # 箭頭垂直軸
    p.polyline([(7, 11), (12, 16), (17, 11)])             # 箭頭頭部（人字形）


def _i_keyboard(p: _Pen) -> None:
    """鍵盤圖示：圓角矩形框 + 按鍵點陣 + 空白鍵。"""
    p.rect(3, 6, 21, 18, r=2)              # 鍵盤外框
    for x in (6.5, 10, 13.5, 17):
        p.dot(x, 10, r=0.9)               # 第一排按鍵點
    p.line(7, 14, 17, 14)                 # 空白鍵（較長橫線）


def _i_sparkles(p: _Pen) -> None:
    """閃爍 / AI 圖示：大四芒星 + 小十字。"""
    # 大四芒星：垂直 + 水平兩條線交叉
    p.line(12, 3, 12, 13)
    p.line(7, 8, 17, 8)
    # 小十字（右下角）
    p.line(18, 15, 18, 21)
    p.line(15, 18, 21, 18)
    # 最小裝飾線（左下）
    p.line(5, 17, 9, 17)


def _i_settings(p: _Pen) -> None:
    """設定圖示：三條橫向滑桿（比齒輪在小尺寸時更清晰）。"""
    # 三條橫線（軌道）
    p.line(4, 7, 20, 7)
    p.line(4, 12, 20, 12)
    p.line(4, 17, 20, 17)
    # 滑桿把手（錯開位置，暗示可調整）
    p.dot(9, 7, r=1.6)
    p.dot(15, 12, r=1.6)
    p.dot(7, 17, r=1.6)


def _i_file_text(p: _Pen) -> None:
    """文字檔圖示：折角文件 + 內文橫線。"""
    # 文件外框，右上角折角
    p.polyline([(5, 3), (15, 3), (20, 8), (20, 21), (5, 21), (5, 3)])
    # 折角三角形
    p.polyline([(15, 3), (15, 8), (20, 8)])
    # 文字內容（兩條橫線代表文字）
    p.line(9, 13, 16, 13)
    p.line(9, 17, 16, 17)


def _i_x(p: _Pen) -> None:
    """關閉 / 清除圖示：兩條對角線交叉。"""
    p.line(6, 6, 18, 18)
    p.line(18, 6, 6, 18)


def _i_lock(p: _Pen) -> None:
    """鎖頭圖示：矩形鎖體 + 弧形鎖梁。"""
    p.rect(4, 11, 20, 21, r=2)                              # 鎖體
    p.polyline([(8, 11), (8, 7), (12, 4), (16, 7), (16, 11)])  # 鎖梁（U 形弧）


def _i_folder(p: _Pen) -> None:
    """資料夾圖示：左上凸起的資料夾外形。"""
    p.polyline([(3, 8), (10, 8), (12, 5), (21, 5), (21, 19), (3, 19), (3, 8)])


def _i_check(p: _Pen) -> None:
    """勾選圖示：大勾（✓）。"""
    p.polyline([(5, 12), (10, 18), (20, 6)])


def _i_mic(p: _Pen) -> None:
    """麥克風圖示：膠囊形收音體 + 支架 + 底座。"""
    p.rect(9, 3, 15, 14, r=3)                               # 膠囊形主體
    # 兩側弧形支架（簡化為折線）
    p.polyline([(5, 11), (5, 13)])
    p.polyline([(19, 11), (19, 13)])
    p.polyline([(5, 13), (5, 14), (19, 14), (19, 13)])      # 底部弧形橫檔
    p.line(12, 18, 12, 21)                                   # 垂直支柱
    p.line(8, 21, 16, 21)                                    # 底座橫桿


def _i_square(p: _Pen) -> None:
    """停止圖示（Lucide 'square'）：略帶圓角的正方形。"""
    p.rect(6, 6, 18, 18, r=1.5)


# ─── 圖示註冊表 ──────────────────────────────────────────────────────────────

# 圖示名稱 → 繪製函式的對應表；新增圖示只需在此加一行
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

ICON_NAMES = list(_REGISTRY.keys())   # 供外部查詢可用圖示名稱


# ─── 公開 API ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=128)
def _render_pil(name: str, size: int, color: str) -> Image.Image:
    """渲染圖示為 PIL Image，結果快取（相同參數只渲染一次）。

    Args:
        name:  圖示名稱（須在 _REGISTRY 內）。
        size:  輸出尺寸（像素）。
        color: 筆觸色彩，十六進位格式 "#RRGGBB"。

    Returns:
        RGBA PIL Image。

    Raises:
        ValueError: 若 name 不在 _REGISTRY 中。
    """
    draw_fn = _REGISTRY.get(name)
    if draw_fn is None:
        raise ValueError(f"Unknown icon: {name!r}. Available: {ICON_NAMES}")
    pen = _Pen(size, color)
    draw_fn(pen)
    return pen.finish()


# CTkImage 快取（key: (name, size, color)）
# 與 _render_pil 分開是因為 CTkImage 物件不能被 lru_cache（不可 hash）
_CK_CACHE: dict[tuple[str, int, str], ctk.CTkImage] = {}


def get_icon(name: str, size: int = 16, color: str = "#FFFFFF") -> ctk.CTkImage:
    """取得可用於 CTkButton / CTkLabel 的 CTkImage 圖示。

    同一組 (name, size, color) 參數的結果會被快取，重複呼叫不會重新渲染。

    Args:
        name:  圖示名稱（見 ICON_NAMES）。
        size:  輸出尺寸（像素），預設 16。
        color: 筆觸色彩，預設白色 "#FFFFFF"。

    Returns:
        CTkImage 實例（含 @1x 與 @2x，支援 Retina 螢幕）。
    """
    key = (name, size, color)
    if key in _CK_CACHE:
        return _CK_CACHE[key]

    # 同時渲染 @1x 與 @2x，讓 CustomTkinter 在 Retina 螢幕選擇高解析度版本
    img_2x = _render_pil(name, size * 2, color)
    # 傳入 @2x 圖但告知顯示尺寸為 size，CTk 會自動選用正確版本
    ck = ctk.CTkImage(light_image=img_2x, dark_image=img_2x, size=(size, size))
    _CK_CACHE[key] = ck
    return ck


# PhotoImage 快取（供 tk.Canvas 使用）
# 注意：PhotoImage 必須保持參照，否則 Python GC 會回收，造成圖示消失
_CANVAS_CACHE: dict[tuple[str, int, str], ImageTk.PhotoImage] = {}


def get_canvas_icon(name: str, size: int = 16,
                    color: str = "#FFFFFF") -> ImageTk.PhotoImage:
    """取得可用於 tk.Canvas.create_image 的 PhotoImage 圖示。

    CTkImage 不相容 raw tk.Canvas，必須使用此函式取得 PhotoImage。
    快取由此模組持有，呼叫端不需自行保留參照。

    Args:
        name:  圖示名稱（見 ICON_NAMES）。
        size:  輸出尺寸（像素）。
        color: 筆觸色彩，十六進位格式 "#RRGGBB"。

    Returns:
        ImageTk.PhotoImage 實例（永久快取在此模組）。
    """
    key = (name, size, color)
    cached = _CANVAS_CACHE.get(key)
    if cached is not None:
        return cached
    pil = _render_pil(name, size, color)
    photo = ImageTk.PhotoImage(pil)
    _CANVAS_CACHE[key] = photo   # 模組層級快取，確保 GC 不回收
    return photo
