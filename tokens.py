"""
Whisper Pro 設計系統 Token（v2.6.0 起 theme-aware）。

UI 所有色彩、字型、間距、圓角、動畫時長的單一真相來源。
所有 UI 模組必須從此檔匯入；任何地方都不應直接寫死十六進位色碼。

v2.6.0（2026-05-23）：theme-aware 重構
  • `_PALETTES` 字典持有 dark / light 兩套色彩；module-level 常數從 active palette 抽出
  • Active theme 在 import 時讀 `Config.load().theme`，不支援 live switch（換 theme 要 App restart）
  • 既有 `from tokens import BG, SURF_1, ACCENT` 一行都不用改、自動跟著 active theme
  • 未知 theme 值靜默 fallback 到 "dark" + log warning（Eng Review Issue 2）

調色盤設計理念：
  • 深色（v2.5.0 起預設）：Zinc 暖灰 + Cyan #06B6D4，OLED 最佳化、技術感
  • 淺色（v2.6.0 新增）：Hybrid — Apple 結構 + Claude 溫度
      - 背景 #FAFAF7 微暖白（Apple-bright with Claude tint）
      - 卡片 #FFFFFF 純白（Apple cleanness）
      - 主 ACCENT #D97757 Claude 珊瑚（CTA / active state）
      - LINK #007AFF Apple 藍（連結 / info icon）

參考文件：docs/superpowers/plans/2026-05-23-light-theme-and-appearance-toggle.md
"""

from __future__ import annotations


# ═══════════════════════════════════════════════════════════════════════════════
#  Palette 字典 — Theme 切換的單一真相來源
#  兩套 palette 必須 key 完全一致（避免 import 漏 key → KeyError）。
#  既有 dark 維持 v2.5.0 zinc + cyan；light 為 v2.6.0 新增（Variant C Hybrid）。
# ═══════════════════════════════════════════════════════════════════════════════

_PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        # ── 表面層次 ─────────────────────────────────────
        "BG":          "#000000",   # dp=0  虛空黑
        "SURF_1":      "#0E0E10",   # dp=1  卡片、頂部
        "SURF_2":      "#18181B",   # dp=2  狀態列、hover
        "SURF_3":      "#27272A",   # dp=3  pressed
        "SURF_4":      "#3F3F46",   # dp=4  邊框、分隔線
        # ── 文字層次 ─────────────────────────────────────
        "TEXT_1":      "#FAFAFA",   # 100% — 標題
        "TEXT_2":      "#E4E4E7",   # ~75% — 內文
        "TEXT_3":      "#A1A1AA",   # ~55% — 說明
        "TEXT_4":      "#71717A",   # ~35% — 停用
        # ── 語意色 ───────────────────────────────────────
        "ACCENT":      "#06B6D4",   # Cyan 500
        "ACCENT_HV":   "#22D3EE",
        "ACCENT_BG":   "#164E63",
        "SUCCESS":     "#22C55E",
        "SUCCESS_HV":  "#16A34A",
        "SUCCESS_DIM": "#14532D",
        "DANGER":      "#EF4444",
        "DANGER_HV":   "#DC2626",
        "DANGER_DIM":  "#7F1D1D",
        "WARN":        "#F59E0B",
        "WARN_HV":     "#D97706",
        "WARN_DIM":    "#78350F",
        "INDIGO":      "#818CF8",
        "INDIGO_HV":   "#6366F1",
        "INDIGO_DIM":  "#312E81",
        # ── 衍生 ─────────────────────────────────────────
        # LINK：深色主題下與 ACCENT 同色（cyan），淺色才會分開（Apple 藍）
        "LINK":        "#06B6D4",
        "WAVE_IDLE":   "#3F3F46",   # = SURF_4
        "WAVE_LIVE":   "#FAFAFA",   # = TEXT_1
    },
    "light": {
        # ── 表面層次（Variant C Hybrid）──────────────
        "BG":          "#FAFAF7",   # 微暖白（Apple-bright with Claude tint）
        "SURF_1":      "#FFFFFF",   # 卡片：純白 Apple-clean
        "SURF_2":      "#F1F0EC",   # raised / 副表面
        "SURF_3":      "#E5E3DC",   # pressed / hover
        "SURF_4":      "#D4D2C8",   # 邊框 / 分隔線
        # ── 文字層次 ─────────────────────────────────────
        "TEXT_1":      "#1A1612",   # warm 近黑（標題）
        "TEXT_2":      "#3D362B",   # warm dark（內文）
        "TEXT_3":      "#6E6E73",   # Apple-cool 灰（刻意冷以平衡暖度）
        "TEXT_4":      "#A8A8AD",   # 停用
        # ── 語意色 ───────────────────────────────────────
        "ACCENT":      "#D97757",   # Claude coral（主 CTA / active）
        "ACCENT_HV":   "#C66445",
        "ACCENT_BG":   "#F6E8DE",   # 珊瑚 chip 淡底
        "SUCCESS":     "#2E8B57",   # balanced green
        "SUCCESS_HV":  "#246E47",
        "SUCCESS_DIM": "#E0EFE5",
        "DANGER":      "#D14B41",   # warm Apple red
        "DANGER_HV":   "#B53A30",
        "DANGER_DIM":  "#F8E2DF",
        "WARN":        "#C7842B",   # muted amber
        "WARN_HV":     "#A56E22",
        "WARN_DIM":    "#F5E9D4",
        "INDIGO":      "#6366F1",
        "INDIGO_HV":   "#4F46E5",
        "INDIGO_DIM":  "#E0E1FA",
        # ── 衍生 ─────────────────────────────────────────
        "LINK":        "#007AFF",   # Apple system blue（連結 / info icon）
        "WAVE_IDLE":   "#D4D2C8",   # = SURF_4
        "WAVE_LIVE":   "#1A1612",   # = TEXT_1
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Active theme 解析（import 時鎖定、不支援 live switch）
# ═══════════════════════════════════════════════════════════════════════════════

def _active_theme() -> str:
    """讀 `~/.whisper_app/config.json` 的 theme 欄位、決定 active palette。

    失敗時靜默 fallback 到 "dark"（保留現狀行為）。
    """
    try:
        from config import Config
        return Config.load().theme
    except Exception:
        return "dark"


_THEME = _active_theme()

# Eng Review Issue 2 / 2026-05-23：使用者實驗性手改 config.theme 為 "purple"
# 或舊版 typo（"Light" / "LIGHT"）→ _PALETTES[_THEME] KeyError 啟動 crash。
# 一行 fallback 保護：未知值靜默降級為 "dark" + log 警告（讓使用者查 log
# 看到「為什麼是深色？」能查到原因）。
if _THEME not in _PALETTES:
    import logging as _logging
    _logging.getLogger("whisper_pro.tokens").warning(
        f"tokens: unknown theme {_THEME!r} in config, fallback to 'dark'"
    )
    _THEME = "dark"

_P = _PALETTES[_THEME]


# ═══════════════════════════════════════════════════════════════════════════════
#  Module-level 常數（既有 import 全部相容、不需要修改）
#  v2.6.0：從 active palette 抽出，由 _THEME 在 import 時鎖定。
# ═══════════════════════════════════════════════════════════════════════════════

# ── 表面層次（Surface Elevation, dp 0-4）──────────────────────────────────────

BG        = _P["BG"]
SURF_1    = _P["SURF_1"]
SURF_2    = _P["SURF_2"]
SURF_3    = _P["SURF_3"]
SURF_4    = _P["SURF_4"]

# ── 文字層次（Text Hierarchy）─────────────────────────────────────────────────

TEXT_1    = _P["TEXT_1"]
TEXT_2    = _P["TEXT_2"]
TEXT_3    = _P["TEXT_3"]
TEXT_4    = _P["TEXT_4"]

# ── 語意強調色（Semantic Accents）─────────────────────────────────────────────

ACCENT      = _P["ACCENT"]
ACCENT_HV   = _P["ACCENT_HV"]
ACCENT_BG   = _P["ACCENT_BG"]

SUCCESS     = _P["SUCCESS"]
SUCCESS_HV  = _P["SUCCESS_HV"]
SUCCESS_DIM = _P["SUCCESS_DIM"]

DANGER      = _P["DANGER"]
DANGER_HV   = _P["DANGER_HV"]
DANGER_DIM  = _P["DANGER_DIM"]

WARN        = _P["WARN"]
WARN_HV     = _P["WARN_HV"]
WARN_DIM    = _P["WARN_DIM"]

INDIGO      = _P["INDIGO"]
INDIGO_HV   = _P["INDIGO_HV"]
INDIGO_DIM  = _P["INDIGO_DIM"]

# ── v2.6.0 新增 token ─────────────────────────────────────────────────────────

# LINK：連結 / info icon 專用色。深色主題下與 ACCENT 同色（cyan）；
# 淺色主題下分開（Apple 藍 #007AFF），給「珊瑚 = CTA、藍 = 連結」明確語意。
LINK        = _P["LINK"]


# ═══════════════════════════════════════════════════════════════════════════════
#  波形顏色（Waveform）
# ═══════════════════════════════════════════════════════════════════════════════

WAVE_IDLE_COL = _P["WAVE_IDLE"]    # 閒置時波形：搭配四級表面、不搶眼
WAVE_LIVE_COL = _P["WAVE_LIVE"]    # 錄音中波形：強對比、強調「活躍」感


# ═══════════════════════════════════════════════════════════════════════════════
#  以下 token 與 theme 無關（字型 / 間距 / 圓角 / 動畫），不需要 palette variant
# ═══════════════════════════════════════════════════════════════════════════════

# ── 字型（Typography）────────────────────────────────────────────────────────

FONT_FAMILY_UI   = "SF Pro Display"   # 標題、大字
FONT_FAMILY_TEXT = "SF Pro Text"      # 內文、按鈕、說明
FONT_FAMILY_MONO = "SF Mono"          # 計時器、數值、程式碼

TYPE = {
    "display":  (28, "bold"),
    "title":    (17, "bold"),
    "headline": (15, "bold"),
    "body":     (14, "normal"),
    "caption":  (12, "normal"),
    "micro":    (11, "normal"),
    "mono":     (13, "normal"),
}

# ── 間距（Spacing，4pt 基線）─────────────────────────────────────────────────

SPACE_XS  = 4
SPACE_SM  = 8
SPACE_MD  = 12
SPACE_LG  = 16
SPACE_XL  = 24
SPACE_2XL = 32
SPACE_3XL = 48

# ── 圓角（Border Radius）─────────────────────────────────────────────────────

RADIUS_SM   = 6
RADIUS_MD   = 10
RADIUS_LG   = 14
RADIUS_XL   = 20
RADIUS_PILL = 999

# ── 動畫時長（Animation Duration, ms）────────────────────────────────────────

DUR_FAST   = 120
DUR_NORMAL = 240
DUR_SLOW   = 400

BREATHE_IDLE_MS       = 6000
BREATHE_RECORDING_MS  = 2500
BREATHE_PROCESSING_MS = 1800

ROTATE_PROCESSING_MS  = 1500
RENDER_TICK_MS        = 50

# ── 載入指示字元 ─────────────────────────────────────────────────────────────

SPINNER = ["⠋", "⠙", "⠸", "⠴", "⠦", "⠇"]
