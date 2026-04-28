"""
Whisper Pro App Icon 生成器（Phase 4.1）。

輸出：
  assets/icon.png            主圖 1024×1024
  assets/icon.iconset/*.png  Apple iconset（10 個尺寸）
  assets/WhisperPro.icns     macOS 圖示檔（透過 iconutil 合成）

設計（規格見 docs/superpowers/specs/2026-04-22-app-icon-splash-design.md）：
  • 背景：Zinc 950 #09090B
  • 主體：Lucide 風格麥克風（Cyan #06B6D4）+ 右側 3 條音波弧線
  • 音波透明度：100% / 60% / 30%（內 → 外）
  • 光暈：麥克風後方低透明度 Cyan 圓形
  • 線寬：2px（4× 超採樣再縮）
  • 風格：與 icons.py 完全對齊（純 PIL 手繪、無外部依賴）

獨立執行：
  $ venv/bin/python3 app_icon.py
  → 自動產生 assets/ 目錄底下所有檔案；macOS 上會額外執行 iconutil 產 .icns
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


# ── 常數 ──────────────────────────────────────────────────────────────────────

# 路徑
HERE        = Path(__file__).parent
ASSETS_DIR  = HERE / "assets"
ICON_PNG    = ASSETS_DIR / "icon.png"
ICONSET_DIR = ASSETS_DIR / "icon.iconset"
ICNS_OUT    = ASSETS_DIR / "WhisperPro.icns"

# 顏色（Zinc + Cyan，與 tokens.py 對齊）
BG          = (9, 9, 11, 255)         # #09090B
ACCENT      = (6, 182, 212)            # #06B6D4
ACCENT_GLOW = (6, 182, 212, 38)        # 15% alpha
TEXT_LIGHT  = (250, 250, 250)          # 麥克風的反光線

# Lucide-style 線寬（在 1024 主圖中 = 24px；對應 24-viewport 的 2px × 縮放比）
LINE_W = 24

# Apple iconset 規定的尺寸
APPLE_SIZES: list[tuple[int, str]] = [
    (16,   "icon_16x16.png"),
    (32,   "icon_16x16@2x.png"),
    (32,   "icon_32x32.png"),
    (64,   "icon_32x32@2x.png"),
    (128,  "icon_128x128.png"),
    (256,  "icon_128x128@2x.png"),
    (256,  "icon_256x256.png"),
    (512,  "icon_256x256@2x.png"),
    (512,  "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]


# ── 繪圖 ──────────────────────────────────────────────────────────────────────

def draw_icon(size: int = 1024) -> Image.Image:
    """繪製指定尺寸的 App Icon（macOS 圓角由系統處理，這裡輸出方形）。

    技法：4× 超採樣 + LANCZOS 縮放（icons.py 同樣手法）抗鋸齒。
    """
    SS = 4
    canvas_size = size * SS
    img = Image.new("RGBA", (canvas_size, canvas_size), BG)
    d = ImageDraw.Draw(img)

    # 中心
    cx = cy = canvas_size // 2

    # 線寬：基準 24（在 1024 上），按超採樣 + 縮放比例調整
    base_lw = LINE_W * SS * (size / 1024)

    # ── 1. 後方光暈（柔和的 Cyan 圓形） ──────────────────────────────────────
    # 用大半徑、低 alpha、然後 GaussianBlur 模糊
    glow_layer = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    glow_d = ImageDraw.Draw(glow_layer)
    glow_r = int(canvas_size * 0.32)
    glow_d.ellipse(
        (cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r),
        fill=ACCENT_GLOW,
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=canvas_size * 0.05))
    img = Image.alpha_composite(img, glow_layer)
    d = ImageDraw.Draw(img)

    # ── 2. 麥克風（Lucide 風格、置左偏中） ─────────────────────────────────
    # 麥克風佔位：在 24-viewport 裡是 (9, 3, 15, 17) 的膠囊 + 支架。
    # 我們把整個圖案稍微偏左，留右邊空間給音波。

    # 圖示中心點（X 偏左 ~12% 留音波空間）
    mx = cx - int(canvas_size * 0.06)
    my = cy

    # 麥克風主體尺寸（膠囊形）
    mic_w = int(canvas_size * 0.18)   # 寬
    mic_h = int(canvas_size * 0.32)   # 高
    mic_rx = mx - mic_w // 2
    mic_ry = my - mic_h // 2 - int(canvas_size * 0.04)

    # 主體輪廓（膠囊：圓角矩形，半徑 = 寬度一半 → 完整圓弧）
    # Pillow 的 rounded_rectangle 在大尺寸 + outline 模式下角落 stroke 很乾淨，
    # 比手動 4 線 + 4 弧拼接（會有縫）好得多。
    d.rounded_rectangle(
        (mic_rx, mic_ry, mic_rx + mic_w, mic_ry + mic_h),
        radius=mic_w // 2,
        outline=ACCENT,
        width=int(base_lw),
    )

    # 麥克風支架弧線（U 型在底部）：從 mic 底部延伸出兩條短直線 → 大 U
    # 支架寬度 = 主體寬度 × 1.6
    arc_w = int(mic_w * 1.6)
    arc_x0 = mx - arc_w // 2
    arc_x1 = mx + arc_w // 2
    arc_y_top = mic_ry + mic_h - int(canvas_size * 0.02)
    arc_y_bot = mic_ry + mic_h + int(canvas_size * 0.08)

    # 兩側豎線（從 mic 底部往下延伸到 U 型起點）
    d.line(
        [(arc_x0, arc_y_top), (arc_x0, arc_y_bot)],
        fill=ACCENT, width=int(base_lw),
    )
    d.line(
        [(arc_x1, arc_y_top), (arc_x1, arc_y_bot)],
        fill=ACCENT, width=int(base_lw),
    )
    # U 型底（半圓弧）
    d.arc(
        (arc_x0, arc_y_bot - arc_w // 2,
         arc_x1, arc_y_bot + arc_w // 2),
        start=0, end=180, fill=ACCENT, width=int(base_lw),
    )

    # 垂直支柱（從 U 型底中央往下）
    pole_y0 = arc_y_bot + arc_w // 2
    pole_y1 = pole_y0 + int(canvas_size * 0.06)
    d.line([(mx, pole_y0), (mx, pole_y1)], fill=ACCENT, width=int(base_lw))

    # 底座短橫線
    base_w = int(canvas_size * 0.12)
    d.line(
        [(mx - base_w // 2, pole_y1), (mx + base_w // 2, pole_y1)],
        fill=ACCENT, width=int(base_lw),
    )

    # ── 3. 三條音波弧線（在麥克風右側，由內而外、淡出） ───────────────────
    # 三個弧的半徑與 alpha
    wave_specs = [
        (canvas_size * 0.16, 1.00),
        (canvas_size * 0.23, 0.60),
        (canvas_size * 0.30, 0.30),
    ]
    for radius, alpha in wave_specs:
        wave_layer = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        wave_d = ImageDraw.Draw(wave_layer)
        # 弧線中心放在麥克風中心（讓三條弧形成同心半圓向右開口）
        bbox = (mx - int(radius), my - int(radius),
                mx + int(radius), my + int(radius))
        # arc 的 0 度在右側、順時針；右側半圓開口 = -50° → +50°
        wave_d.arc(
            bbox, start=-50, end=50,
            fill=(*ACCENT, int(255 * alpha)),
            width=int(base_lw * 0.85),
        )
        img = Image.alpha_composite(img, wave_layer)

    # ── 4. 縮回目標尺寸 ───────────────────────────────────────────────────
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    return img


def _rounded_rect_outline(
    d: ImageDraw.ImageDraw,
    x0: int, y0: int, x1: int, y1: int,
    radius: int, color, width: int,
) -> None:
    """繪製圓角矩形的「線框」。

    Pillow 的 `rounded_rectangle` 雖然有 `outline` 參數，但在大尺寸下
    線粗渲染品質不一致。這裡手動拼：4 段直線 + 4 個圓弧。
    """
    # 上邊
    d.line([(x0 + radius, y0), (x1 - radius, y0)], fill=color, width=width)
    # 下邊
    d.line([(x0 + radius, y1), (x1 - radius, y1)], fill=color, width=width)
    # 左邊
    d.line([(x0, y0 + radius), (x0, y1 - radius)], fill=color, width=width)
    # 右邊
    d.line([(x1, y0 + radius), (x1, y1 - radius)], fill=color, width=width)
    # 四角圓弧
    d.arc((x0, y0, x0 + radius * 2, y0 + radius * 2),
          start=180, end=270, fill=color, width=width)
    d.arc((x1 - radius * 2, y0, x1, y0 + radius * 2),
          start=270, end=360, fill=color, width=width)
    d.arc((x0, y1 - radius * 2, x0 + radius * 2, y1),
          start=90, end=180, fill=color, width=width)
    d.arc((x1 - radius * 2, y1 - radius * 2, x1, y1),
          start=0, end=90, fill=color, width=width)


# ── Iconset / .icns 產生 ──────────────────────────────────────────────────────

def generate_iconset() -> bool:
    """產生 assets/icon.iconset/ 底下所有尺寸 PNG，並（macOS 上）跑 iconutil。

    Returns:
        True 表示 .icns 也產出；False 表示只產 PNG（非 macOS 或 iconutil 缺）。
    """
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    # 1) 主圖
    print(f"App-Icon: drawing {ICON_PNG.name} (1024×1024)...")
    main_img = draw_icon(1024)
    main_img.save(ICON_PNG, "PNG")

    # 2) iconset 各尺寸
    if ICONSET_DIR.exists():
        shutil.rmtree(ICONSET_DIR)
    ICONSET_DIR.mkdir(parents=True)
    for size, fname in APPLE_SIZES:
        # 直接從 1024 縮（避免每尺寸重畫的計算成本，視覺上 LANCZOS 縮放足夠好）
        img = main_img.resize((size, size), Image.Resampling.LANCZOS)
        img.save(ICONSET_DIR / fname, "PNG")
    print(f"App-Icon: wrote {len(APPLE_SIZES)} iconset PNGs")

    # 3) iconutil 合成 .icns（僅 macOS）
    if shutil.which("iconutil") is None:
        print("App-Icon: iconutil not found — skipping .icns (non-macOS env?)")
        return False
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", str(ICONSET_DIR), "-o", str(ICNS_OUT)],
            check=True, capture_output=True, text=True,
        )
        print(f"App-Icon: wrote {ICNS_OUT}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"App-Icon: iconutil failed: {e.stderr or e}", file=sys.stderr)
        return False


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"App-Icon: output dir = {ASSETS_DIR}")
    ok = generate_iconset()
    print(f"App-Icon: {'done with .icns' if ok else 'PNG only (no .icns)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
