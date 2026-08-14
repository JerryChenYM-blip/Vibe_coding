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
      - 背景 #F4F4F5（Aperture 主視窗重寫調整，v2.6.0 原值 #FAFAF7 微暖白）
      - 卡片 #FFFFFF 純白（Apple cleanness）
      - 主 ACCENT #D97757 Claude 珊瑚（CTA / active state，Aperture 第二輪後語意收窄，CTA 改用 BTN_BG/BTN_FG）
      - LINK #007AFF Apple 藍（連結 / info icon）

參考文件：docs/superpowers/plans/2026-05-23-light-theme-and-appearance-toggle.md
"""

from __future__ import annotations

from animation import blend as _blend   # PROC_DIM 計算用（不寫死 hex，見下方 Aperture 主視窗重寫區塊）


# ═══════════════════════════════════════════════════════════════════════════════
#  Palette 字典 — Theme 切換的單一真相來源
#  兩套 palette 必須 key 完全一致（避免 import 漏 key → KeyError）。
#  既有 dark 維持 v2.5.0 zinc + cyan；light 為 v2.6.0 新增（Variant C Hybrid）。
# ═══════════════════════════════════════════════════════════════════════════════

_PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        # ── 表面層次 ─────────────────────────────────────
        # BG：Aperture 主視窗重寫調整（轉錄流底／視窗底），從 v2.5.0 純黑 #000000 改 #0F1012
        "BG":          "#0F1012",   # dp=0  轉錄流底、視窗底
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
        # ACCENT：Aperture 第二輪（語意收窄）用途收窄為互動色（選取列/焦點/連結）
        "ACCENT":      "#06B6D4",   # Cyan 500
        "ACCENT_HV":   "#22D3EE",
        "ACCENT_BG":   "#164E63",
        # SUCCESS：Aperture 第二輪（語意收窄）只給「已貼上」toast 與權限已授權，從閒置態撤出
        "SUCCESS":     "#22C55E",
        "SUCCESS_HV":  "#16A34A",
        "SUCCESS_DIM": "#14532D",
        "DANGER":      "#EF4444",
        "DANGER_HV":   "#DC2626",
        "DANGER_DIM":  "#7F1D1D",
        # WARN：Aperture 第二輪（語意收窄）只給削波（clipping），從處理態撤出
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
        # ── Aperture 第二輪新增（13 key，兩套 palette 的 key 必須完全一致）──
        # 能量色溫 5 停留點（錄音態，音量 0→1，供 ENERGY_RAMP_DARK 組裝）
        # Aperture 主視窗重寫：斜坡末端從白熾改琥珀（削波預警內建在色溫裡），數值全面更新
        "WAVE_E0":     "#1E7A8C",
        "WAVE_E1":     "#22D3EE",
        "WAVE_E2":     "#67E8F9",
        "WAVE_E3":     "#FDE68A",
        "WAVE_E4":     "#F59E0B",
        # 閒置 2 停留點（呼吸律動）—— Aperture 主視窗重寫：dark 端點不變，沿用原值
        "WAVE_I0":     "#2A2E33",
        "WAVE_I1":     "#4A525C",
        # 處理 3 停留點（旋轉弧 / 頻譜掃描）
        "WAVE_P0":     "#312E81",
        "WAVE_P1":     "#6366F1",
        "WAVE_P2":     "#A5B4FC",
        # 處理態「還沒讀到」的 bar（掃描未到達的部分）
        "WAVE_DIM":    "#3C424A",
        # 搜尋高亮底色（歷史紀錄 FTS5 命中）
        "MARK_BG":     "#164E63",

        # ── Aperture 主視窗重寫新增（32 key，兩套 palette 的 key 必須完全一致）──
        "CHROME":      "#141518",   # 頂列／狀態槽／底列
        "CARD":        "#15161A",   # 舊段落卡片
        "CARD_HI":     "#191A1E",   # 最新段卡片
        "LINE":        "#232427",   # 所有 1pt 分隔線、卡片邊框
        "LINE_HI":     "#2A2B31",   # 最新段邊框、hover
        "ROW_HI":      "#1B1C21",   # 列／圖示鈕 hover 底
        "META":        "#8A8A90",   # 舊段時間
        "META_HI":     "#9A9BA2",   # 最新段時間、常駐圖示鈕
        "CYAN_TEXT":   "#67C7DC",   # 校正 chip 文字、展開連結
        "MARK":        "#A5E9F5",   # 字典校正詞
        "BTN_DIS":     "#1E1F23",   # 主鈕 disabled 底（實色，非 alpha）
        "BTN_DIS_FG":  "#55565D",   # 主鈕 disabled 文字（實色，非 alpha）
        "PILL_OFF":    "#34353A",   # 模式關（只有邊框）
        "PILL_OFF_FG": "#7E7F86",
        "SEG_BG":      "#101114",   # segmented 外殼
        "SEG_LINE":    "#2A2B31",
        "SEG_ON":      "#A1A1AA",   # segmented 選中格
        "SEG_ON_FG":   "#0F1012",
        "CHIP_BG":     "#1B2430",   # 校正 N 徽章底
        "KEY_BG":      "#1E1F23",   # R⌘ 鍵帽
        "KEY_LINE":    "#2E2F35",
        "ICON":        "#8A8A90",   # 頂列導覽圖示
        "SCROLL":      "#2E2F35",   # 捲動條 thumb
        "SKEL":        "#26283A",   # 轉錄中骨架條
        "SKEL_2":      "#212330",
        "RED_TEXT":    "#FCA5A5",
        "RED_BG":      "#2A1518",
        "RED_LINE":    "#3F1D22",
        "TEXT_BODY":   "#E8E8EA",
        "TEXT_BODY_2": "#D4D4D8",
        # 主鈕（record CTA）背景／前景：ACCENT 第二輪語意收窄後不再兼任 CTA 色，
        # 由 BTN_BG/BTN_FG 專職——dark BTN_BG 恰等於既有 ACCENT_HV（#22D3EE），非巧合。
        "BTN_BG":      "#22D3EE",
        "BTN_FG":      "#06212A",
    },
    "light": {
        # ── 表面層次（Variant C Hybrid）──────────────
        # BG：Aperture 主視窗重寫調整（轉錄流底／視窗底），從 v2.6.0 微暖白 #FAFAF7 改 #F4F4F5
        "BG":          "#F4F4F5",   # 轉錄流底、視窗底
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
        # ACCENT：Aperture 第二輪（語意收窄）淺色珊瑚 #D97757 退場，
        # 改用互動色 #0E7490（選取列/焦點/連結），不再兼任主 CTA 色。
        # v2.28.0 總管 code review 修：ACCENT 在第二輪已收窄成藍綠 #0E7490，
        #   但 ACCENT_HV / ACCENT_BG 還留著舊珊瑚色 #C66445 / #F6E8DE。
        #   ACCENT_HV 在 gui.py 被用了 18 處——淺色主題下滑鼠移過去會從藍綠
        #   「跳成珊瑚橘」，是實際看得到的色相斷裂（深色主題無此問題：
        #   ACCENT #06B6D4 → HV #22D3EE 同屬青色系）。
        #   規格第二輪明講「淺色的珊瑚 #D97757 退場」，這兩個是漏網的。
        #   淺色 hover 往「更深」走（白底上加深才是強調），底色沿用同色系淡底。
        "ACCENT":      "#0E7490",
        "ACCENT_HV":   "#0B5D73",   # 藍綠加深（白底上 hover 要更深、不是更亮）
        "ACCENT_BG":   "#DFF1F6",   # 藍綠淡底（與 CHIP_BG 同一支色系）
        # SUCCESS：Aperture 第二輪（語意收窄）只給「已貼上」toast 與權限已授權，從閒置態撤出
        "SUCCESS":     "#2E8B57",   # balanced green
        "SUCCESS_HV":  "#246E47",
        "SUCCESS_DIM": "#E0EFE5",
        # DANGER：Aperture 主視窗重寫調整（既有分支調整 RED），從 #D14B41 改 #DC2626
        "DANGER":      "#DC2626",
        "DANGER_HV":   "#B53A30",
        "DANGER_DIM":  "#F8E2DF",
        # WARN：Aperture 第二輪（語意收窄）只給削波（clipping），從處理態撤出
        # Aperture 主視窗重寫調整（既有分支調整 AMBER），從 #C7842B 改 #B45309
        "WARN":        "#B45309",
        "WARN_HV":     "#A56E22",
        "WARN_DIM":    "#F5E9D4",
        # INDIGO：Aperture 主視窗重寫調整（既有分支調整 PROC，PROCESS 別名跟著變），
        # 從 #6366F1 改 #4F46E5
        "INDIGO":      "#4F46E5",
        "INDIGO_HV":   "#4F46E5",
        "INDIGO_DIM":  "#E0E1FA",
        # ── 衍生 ─────────────────────────────────────────
        "LINK":        "#007AFF",   # Apple system blue（連結 / info icon）
        "WAVE_IDLE":   "#D4D2C8",   # = SURF_4
        "WAVE_LIVE":   "#1A1612",   # = TEXT_1
        # ── Aperture 第二輪新增（13 key，兩套 palette 的 key 必須完全一致）──
        # Aperture 主視窗重寫：斜坡末端從墨水濃度改琥珀（削波預警內建在色溫裡），數值全面更新
        "WAVE_E0":     "#7FBFCE",
        "WAVE_E1":     "#0891B2",
        "WAVE_E2":     "#0E7490",
        "WAVE_E3":     "#C2740A",
        "WAVE_E4":     "#B45309",
        # 閒置 2 停留點（呼吸律動）—— Aperture 主視窗重寫：光底無法靠「更亮」分層，改用這組更冷的灰階
        "WAVE_I0":     "#D9DBDE",
        "WAVE_I1":     "#98A0A8",
        "WAVE_P0":     "#D6D7FA",
        "WAVE_P1":     "#6366F1",
        "WAVE_P2":     "#3730A3",
        "WAVE_DIM":    "#C4C2BA",
        "MARK_BG":     "#FDE68A",   # 兩套色相不同是刻意的（深色沿用 cyan-900、淺色用琥珀提亮）

        # ── Aperture 主視窗重寫新增（32 key，兩套 palette 的 key 必須完全一致）──
        # 淺色沒有「更亮」可用來分層（白底之上不會更亮），改靠邊框深淺（LINE → LINE_HI）
        # 分層；CARD 與 CARD_HI 在淺色因此是同一個白，不像深色靠底色階差區分。
        "CHROME":      "#FFFFFF",
        "CARD":        "#FFFFFF",
        "CARD_HI":     "#FFFFFF",
        "LINE":        "#E4E4E7",
        "LINE_HI":     "#A9B0B8",
        "ROW_HI":      "#F4F4F5",
        "META":        "#71717A",
        "META_HI":     "#3F3F46",
        # CYAN_TEXT：淺色不用 #22D3EE（對白底僅 1.6:1 對比，不能當文字／1pt 邊框），改 #0E7490
        "CYAN_TEXT":   "#0E7490",
        "MARK":        "#0E7490",
        "BTN_DIS":     "#E4E4E7",   # 實色，非 alpha
        "BTN_DIS_FG":  "#A1A1AA",   # 實色，非 alpha
        "PILL_OFF":    "#D4D4D8",
        "PILL_OFF_FG": "#71717A",
        "SEG_BG":      "#F4F4F5",
        "SEG_LINE":    "#D4D4D8",
        "SEG_ON":      "#52525B",
        "SEG_ON_FG":   "#FFFFFF",
        "CHIP_BG":     "#DFF1F6",
        "KEY_BG":      "#FFFFFF",
        "KEY_LINE":    "#C9CDD2",
        "ICON":        "#71717A",
        "SCROLL":      "#C9CDD2",
        "SKEL":        "#DEDFF4",
        "SKEL_2":      "#E9EAF8",
        "RED_TEXT":    "#991B1B",
        "RED_BG":      "#FEF2F2",
        "RED_LINE":    "#FECACA",
        "TEXT_BODY":   "#27272A",
        "TEXT_BODY_2": "#3F3F46",
        "BTN_BG":      "#0E7490",   # 恰等於既有 ACCENT（cyan 語意在淺色下必須是 #0E7490），非巧合
        "BTN_FG":      "#FFFFFF",
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

# PROCESS：語意收窄新增——INDIGO 的別名（非另存 hex），轉錄／潤飾／自動貼上
# 這三個「背景處理中」動作共用同一色相；活 alias 保證永遠跟 INDIGO 同步，不會日後改色時各走各的。
PROCESS     = INDIGO


# ═══════════════════════════════════════════════════════════════════════════════
#  Aperture 主視窗重寫新增 token（32 key，兩套 palette 對稱）
# ═══════════════════════════════════════════════════════════════════════════════

# ── 表面／邊框 ───────────────────────────────────────────────────────────────
CHROME      = _P["CHROME"]      # 頂列／狀態槽／底列
CARD        = _P["CARD"]        # 舊段落卡片
CARD_HI     = _P["CARD_HI"]     # 最新段卡片（淺色下與 CARD 同值，靠邊框分層）
LINE        = _P["LINE"]        # 所有 1pt 分隔線、卡片邊框
LINE_HI     = _P["LINE_HI"]     # 最新段邊框、hover
ROW_HI      = _P["ROW_HI"]      # 列／圖示鈕 hover 底

# HAIR：時間分隔線——判斷後直接沿用 LINE，不另開新色階（同一種「細線」語意，
# 沒有理由分岔成兩個 token；活 alias，LINE 改色會自動跟著變）。
HAIR        = LINE

# ── 文字（次階） ─────────────────────────────────────────────────────────────
META        = _P["META"]        # 舊段時間
META_HI     = _P["META_HI"]     # 最新段時間、常駐圖示鈕
TEXT_BODY   = _P["TEXT_BODY"]
TEXT_BODY_2 = _P["TEXT_BODY_2"]

# ── 校正 chip／字典標記 ──────────────────────────────────────────────────────
CYAN_TEXT   = _P["CYAN_TEXT"]   # 校正 chip 文字、展開連結（淺色不能用 ACCENT_HV，對比不夠）
MARK        = _P["MARK"]        # 字典校正詞
CHIP_BG     = _P["CHIP_BG"]     # 校正 N 徽章底

# ── 主鈕（record CTA）── ACCENT 第二輪語意收窄後不再兼任 CTA 色，改由這組專職 ──
BTN_BG      = _P["BTN_BG"]
BTN_FG      = _P["BTN_FG"]
BTN_DIS     = _P["BTN_DIS"]     # disabled 底，實色、非 alpha
BTN_DIS_FG  = _P["BTN_DIS_FG"]  # disabled 文字，實色、非 alpha

# ── 模式 pill／segmented 控制項 ──────────────────────────────────────────────
PILL_OFF     = _P["PILL_OFF"]      # 模式關（只有邊框）
PILL_OFF_FG  = _P["PILL_OFF_FG"]

# PILL_ON／PILL_ON_FG（主視窗骨架重寫新增）：模式 pill 開狀態——沿用
# BTN_BG/BTN_FG 同一組「實色強調」語意，活 alias（同 PROCESS = INDIGO 的
# 既有慣例），不另開一份 hex。
PILL_ON      = BTN_BG
PILL_ON_FG   = BTN_FG
SEG_BG       = _P["SEG_BG"]        # segmented 外殼
SEG_LINE     = _P["SEG_LINE"]
SEG_ON       = _P["SEG_ON"]        # segmented 選中格
SEG_ON_FG    = _P["SEG_ON_FG"]

# ── 其餘小元件 ───────────────────────────────────────────────────────────────
KEY_BG   = _P["KEY_BG"]     # R⌘ 鍵帽
KEY_LINE = _P["KEY_LINE"]
ICON     = _P["ICON"]       # 頂列導覽圖示
SCROLL   = _P["SCROLL"]     # 捲動條 thumb
SKEL     = _P["SKEL"]       # 轉錄中骨架條
SKEL_2   = _P["SKEL_2"]

# ── 錯誤狀態（紅） ───────────────────────────────────────────────────────────
RED_TEXT = _P["RED_TEXT"]
RED_BG   = _P["RED_BG"]
RED_LINE = _P["RED_LINE"]

# ── 處理態掠掃窗口外淡化色（computed，不寫死）─────────────────────────────────
# PROC_DIM = blend(PROC, CHROME, 0.30)。PROC 即 PROCESS（=INDIGO 別名），
# 沿用既有 blend() 混色公式（animation.py，import 見檔案開頭），兩套 palette
# 各自算一份供獨立驗證，再依 active theme 選出 module-level PROC_DIM。
PROC_DIM_DARK  = _blend(_PALETTES["dark"]["INDIGO"], _PALETTES["dark"]["CHROME"], 0.30)
PROC_DIM_LIGHT = _blend(_PALETTES["light"]["INDIGO"], _PALETTES["light"]["CHROME"], 0.30)
PROC_DIM = PROC_DIM_LIGHT if _THEME == "light" else PROC_DIM_DARK


# ═══════════════════════════════════════════════════════════════════════════════
#  波形顏色（Waveform）
# ═══════════════════════════════════════════════════════════════════════════════

WAVE_IDLE_COL = _P["WAVE_IDLE"]    # 閒置時波形：搭配四級表面、不搶眼
WAVE_LIVE_COL = _P["WAVE_LIVE"]    # 錄音中波形：強對比、強調「活躍」感

# ── Aperture 第二輪新增：13 key 的其餘 12 個（PROCESS 已在上面定義為 INDIGO 別名）──

WAVE_E0  = _P["WAVE_E0"]
WAVE_E1  = _P["WAVE_E1"]
WAVE_E2  = _P["WAVE_E2"]
WAVE_E3  = _P["WAVE_E3"]
WAVE_E4  = _P["WAVE_E4"]

WAVE_I0  = _P["WAVE_I0"]
WAVE_I1  = _P["WAVE_I1"]

WAVE_P0  = _P["WAVE_P0"]
WAVE_P1  = _P["WAVE_P1"]
WAVE_P2  = _P["WAVE_P2"]

WAVE_DIM = _P["WAVE_DIM"]    # 處理態「還沒讀到」的 bar，搭配 WAVE_P0..P2 掃描色使用
MARK_BG  = _P["MARK_BG"]     # 搜尋高亮底色（兩套 palette 色相不同是刻意的）


# ═══════════════════════════════════════════════════════════════════════════════
#  能量色溫斜坡（Aperture D2）— 音量 0→1 映射成色溫
#  深色主題：石板藍→青→冰藍→琥珀；淺色主題：淺青→深青→琥珀
#  （Aperture 主視窗重寫：末端從「白熾」改「琥珀」——削波預警內建在色溫裡，
#  越接近削波顏色越暖，不用另外跳警示色）
#  斜坡不再另存一份 hex，改從 _PALETTES 的 WAVE_E0..E4 token 讀，
#  單一真相來源在 palette 字典。停留點位置固定為 [0, 0.30, 0.55, 0.80, 1.0]。
# ═══════════════════════════════════════════════════════════════════════════════

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    """`#RRGGBB` → (r,g,b)。"""
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _stops_from_tokens(
    palette: dict[str, str], keys: tuple[str, ...], positions: tuple[float, ...]
) -> list[tuple[float, tuple[int, int, int]]]:
    """依 positions 把 palette 裡一串 hex token 組成 ramp stops list（給三條 ramp 共用）。"""
    return [(pos, _hex_to_rgb(palette[key])) for pos, key in zip(positions, keys)]


_ENERGY_STOP_POSITIONS = (0.00, 0.30, 0.55, 0.80, 1.00)
_ENERGY_KEYS = ("WAVE_E0", "WAVE_E1", "WAVE_E2", "WAVE_E3", "WAVE_E4")

ENERGY_RAMP_DARK: list[tuple[float, tuple[int, int, int]]] = _stops_from_tokens(
    _PALETTES["dark"], _ENERGY_KEYS, _ENERGY_STOP_POSITIONS
)
ENERGY_RAMP_LIGHT: list[tuple[float, tuple[int, int, int]]] = _stops_from_tokens(
    _PALETTES["light"], _ENERGY_KEYS, _ENERGY_STOP_POSITIONS
)


def _energy_ramp_at(
    stops: list[tuple[float, tuple[int, int, int]]], v: float
) -> tuple[int, int, int]:
    """在 stops 上做分段線性內插，回傳 (r,g,b)。

    Port 自 aperture_engine.js 的 rampAt()，數學不變。v 超出 [0,1] 會 clamp。
    名稱沿用「energy」但實作與 ramp 語意無關，閒置 / 處理兩條 ramp 也共用此函式。
    """
    v = 0.0 if v < 0 else 1.0 if v > 1 else v
    for i in range(1, len(stops)):
        p1, c1 = stops[i]
        if v <= p1 or i == len(stops) - 1:
            p0, c0 = stops[i - 1]
            t = 0.0 if p1 == p0 else (v - p0) / (p1 - p0)
            return (
                round(c0[0] + (c1[0] - c0[0]) * t),
                round(c0[1] + (c1[1] - c0[1]) * t),
                round(c0[2] + (c1[2] - c0[2]) * t),
            )
    return stops[-1][1]


_ENERGY_LUT_SIZE = 64  # 預先算 64 階快取：46 bar × 20fps 每幀呼叫，不能每次都做浮點內插搜尋


def _build_energy_lut(
    stops: list[tuple[float, tuple[int, int, int]]],
) -> list[tuple[int, int, int]]:
    """把 ramp 離散成 64 階（含頭尾兩端點），供 *_color() O(1) 查表 + 相鄰兩階內插。

    名稱沿用「energy」，但實作通用，閒置 / 處理兩條 ramp 的 LUT 也呼叫這支建。
    """
    return [
        _energy_ramp_at(stops, i / (_ENERGY_LUT_SIZE - 1))
        for i in range(_ENERGY_LUT_SIZE)
    ]


# LUT 跟著既有的 theme 鎖定機制走（_THEME / _P 同一個來源），不額外讀設定檔
_ENERGY_LUT = _build_energy_lut(ENERGY_RAMP_LIGHT if _THEME == "light" else ENERGY_RAMP_DARK)


def _lut_color(lut: list[tuple[int, int, int]], v: float) -> str:
    """v(0→1) 在 64 階 LUT 上查表 + 相鄰兩階內插 → hex 色碼。v 超出 [0,1] 會 clamp。

    energy_color / idle_color / process_color 三個對外函式共用同一段查表邏輯。
    """
    v = 0.0 if v < 0 else 1.0 if v > 1 else v
    pos = v * (_ENERGY_LUT_SIZE - 1)
    i0 = int(pos)
    i1 = i0 + 1 if i0 < _ENERGY_LUT_SIZE - 1 else i0
    frac = pos - i0
    c0, c1 = lut[i0], lut[i1]
    r = round(c0[0] + (c1[0] - c0[0]) * frac)
    g = round(c0[1] + (c1[1] - c0[1]) * frac)
    b = round(c0[2] + (c1[2] - c0[2]) * frac)
    return f"#{r:02X}{g:02X}{b:02X}"


def energy_color(v: float) -> str:
    """音量 0→1 → 色溫 hex 色碼（依 import 時鎖定的 theme 走 dark/light 斜坡）。

    64 階 LUT 查表 + 相鄰兩階內插，避免每幀對 5 個 stop 做浮點搜尋。
    v 超出 [0,1] 會 clamp。
    """
    return _lut_color(_ENERGY_LUT, v)


# ═══════════════════════════════════════════════════════════════════════════════
#  閒置態 2 停留點 ramp（呼吸律動）— 同一套 64 階 LUT 機制，跟著 _THEME 鎖定
# ═══════════════════════════════════════════════════════════════════════════════

_IDLE_STOP_POSITIONS = (0.00, 1.00)
_IDLE_KEYS = ("WAVE_I0", "WAVE_I1")

IDLE_RAMP_DARK: list[tuple[float, tuple[int, int, int]]] = _stops_from_tokens(
    _PALETTES["dark"], _IDLE_KEYS, _IDLE_STOP_POSITIONS
)
IDLE_RAMP_LIGHT: list[tuple[float, tuple[int, int, int]]] = _stops_from_tokens(
    _PALETTES["light"], _IDLE_KEYS, _IDLE_STOP_POSITIONS
)

_IDLE_LUT = _build_energy_lut(IDLE_RAMP_LIGHT if _THEME == "light" else IDLE_RAMP_DARK)


def idle_color(v: float) -> str:
    """閒置態呼吸律動 0→1 → hex 色碼。與 energy_color() 同一套 64 階 LUT 機制。"""
    return _lut_color(_IDLE_LUT, v)


# ═══════════════════════════════════════════════════════════════════════════════
#  處理態 3 停留點 ramp（旋轉弧 / 頻譜掃描）— 同一套 64 階 LUT 機制，跟著 _THEME 鎖定
# ═══════════════════════════════════════════════════════════════════════════════

_PROCESS_STOP_POSITIONS = (0.00, 0.50, 1.00)
_PROCESS_KEYS = ("WAVE_P0", "WAVE_P1", "WAVE_P2")

PROCESS_RAMP_DARK: list[tuple[float, tuple[int, int, int]]] = _stops_from_tokens(
    _PALETTES["dark"], _PROCESS_KEYS, _PROCESS_STOP_POSITIONS
)
PROCESS_RAMP_LIGHT: list[tuple[float, tuple[int, int, int]]] = _stops_from_tokens(
    _PALETTES["light"], _PROCESS_KEYS, _PROCESS_STOP_POSITIONS
)

_PROCESS_LUT = _build_energy_lut(PROCESS_RAMP_LIGHT if _THEME == "light" else PROCESS_RAMP_DARK)


def process_color(v: float) -> str:
    """處理態掃描進度 0→1 → hex 色碼。與 energy_color() 同一套 64 階 LUT 機制。"""
    return _lut_color(_PROCESS_LUT, v)


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
