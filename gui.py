# Mac/Windows 雙棲 — 移植自 macOS v2.21.6
"""
主應用程式視窗——Apple MacBook Pro 美學設計。

設計語言：  apple.com/tw/macbook-pro/（深色、簡潔、金屬感）
色彩系統：  強制深色，虛空黑底色 + 四層表面深度
字型：      SF Pro Display / Text（macOS 原生字型）
自動貼上：  錄音開始前記錄前景 App，轉錄完成後模擬 ⌘V 注入文字

視窗佈局（776 × 880 px，由上到下）：
  TopBar      — 品牌 logo、模型選單、語言選單
  RecordCard  — Ambient Chamber 畫布（呼吸光圈 + 錄音按鈕）、計時器
  ResultCard  — 轉錄結果文字區（含原文／潤飾切換）
  ActionBar   — 複製、存檔、自動貼上、AI 潤飾、設定按鈕
  StatusBar   — 狀態點、狀態文字、快捷鍵顯示

匯出：
  AppWindow           主視窗元件
  SettingsWindow      設定對話框
  HotkeyBindDialog    快捷鍵重新綁定對話框
  AccessibilityDialog 輔助使用權限引導對話框
  WIN_W, WIN_H        視窗預設尺寸常數
"""

from __future__ import annotations

import math
import os
import subprocess
import threading
import time
import tkinter as tk
import tkinter.filedialog as fd
import tkinter.font as tkfont
from typing import Optional

import customtkinter as ctk
from PIL import ImageTk

from config import MODEL_INFO, LANGUAGE_OPTIONS, Config
from logger import get_logger, log_action, log_error, log_settings, log_state
from hotkey_manager import (
    HotkeyManager, format_hotkey,
    is_pynput_available, check_accessibility,
)
from ollama_client import OllamaClient, OllamaConfig
import presets as _presets
import dictionary as _dictionary
from history import HistoryStore
from prompt_reloader import PromptReloader
from recorder import AudioRecorder
from transcriber import Transcriber, TranscriptionResult
from icons import get_icon, get_canvas_icon
from animation import blend, breathe, ease_in_out_cubic, Ripple
from waveform import WaveformEngine
from waveform_render import GlowWaveRenderer
import auto_paste as _ap
from platform_util import IS_MAC, IS_WINDOWS

log = get_logger("gui")


# ── v2.19.0 pipeline 觀測性 helper ───────────────────────────────────────────
# pipeline_id / session_summary 由 Agent N 同步在建。可能還沒 ready 或 import
# 失敗。所有呼叫一律包 try/except，觀測性 bug 絕不能影響主流程。

def _pipe_new_id():
    """新增一個 pipeline_id 並設為 thread-local current。失敗回 None。"""
    try:
        from pipeline_id import new_pipeline_id, set_current  # type: ignore
        pid = new_pipeline_id()
        set_current(pid)
        return pid
    except Exception:
        return None


def _pipe_clear():
    """清掉 thread-local current pipeline_id。失敗 silent。"""
    try:
        from pipeline_id import set_current  # type: ignore
        set_current(None)
    except Exception:
        pass


def _pipe_event(name: str, **fields):
    """印一個 pipeline event；失敗 silent。"""
    try:
        from pipeline_id import event as pipeline_event  # type: ignore
        pipeline_event(name, **fields)
    except Exception:
        pass


def _pipe_record_paste_latency(latency_s: float):
    """記錄一筆 paste latency 到 session summary；失敗 silent。"""
    try:
        import session_summary  # type: ignore
        session_summary.record_paste_latency(latency_s=latency_s)
    except Exception:
        pass


def _open_path_in_os_default(path) -> None:
    """用作業系統預設方式開啟檔案／資料夾（Finder / 檔案總管 / 預設編輯器）。

    mac：`open` 指令（既有行為不變）。
    Windows：`os.startfile`（Windows 內建 API，行為等同雙擊該路徑）。
    """
    if IS_MAC:
        subprocess.run(["open", str(path)])
    elif IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        # Linux 等其他平台：桌面環境慣例是 xdg-open
        subprocess.run(["xdg-open", str(path)])

# ── 強制套用 Apple 深色美學 ───────────────────────────────────────────────────
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

WIN_W, WIN_H = 776, 880   # 視窗預設寬度 × 高度（像素）—— 逃生門（record_visual
# == "chamber"）專用，舊版面尺寸不動。
# WIN_H 推導：實測 AppWindow.winfo_reqheight() ≈ 858（TopBar 60 + RecordCard 398 +
# ResultCard 281 + ActionBar 58 + StatusBar 32 + 分隔線×3 + 內邊距），舊值 800
# 會把 ActionBar（5 顆動作按鈕）與 StatusBar 擠出視窗下緣。預留 22px 緩衝。
# v2.27.0 三態統一：RecordCard 改放 712×160 寬條 canvas（見 WAVE_CHAMBER_W/H），
# WIN_W 760→776，讓 712 寬 canvas 在 card 內（padx=SPACE_LG=16）左右各留 16px
# margin、跟 card 自己的外距同一個節奏，不擠壓其他元件。RecordCard 高度反而
# 因 canvas 280→160 變矮（少 120px），WIN_H 維持 880 綽綽有餘（多出的高度會
# 讓下面 fill="both", expand=True 的 ResultCard 拿到更多轉錄文字顯示空間）。

# ─────────────────────────────────────────────────────────────────────────────
#  主視窗骨架重寫（v2.28.0）── 「這個視窗是閱讀器、不是錄音器」
#  錄音已有主場（R⌘ + 迷你條），主視窗注意力預算重分配：波形 33%→4.6%、
#  轉錄流 50.7%→83%。record_visual != "chamber"（預設值）時全部走這組常數；
#  record_visual == "chamber" 逃生門完全不碰這裡，繼續用上面的 WIN_W/WIN_H。
# ─────────────────────────────────────────────────────────────────────────────

SKELETON_WIN_W       = 704   # 視窗預設寬度
SKELETON_WIN_H       = 900   # 視窗預設高度
SKELETON_MIN_W       = 640   # minsize 寬
SKELETON_MIN_H       = 640   # minsize 高

SKELETON_CONTENT_COL = 640   # 內容欄設計寬度（行長保護基準）
SKELETON_CONTENT_PAD = 32    # 內容欄左右 padding（640+32+32=704=預設視窗寬）
SKELETON_CONTENT_MAX = 720   # 視窗拉寬時內容欄封頂——超過此寬度後多出的空間
                              # 全部變成左右留白，不再讓文字行長失控變長

SKELETON_TOPBAR_H      = 56  # 頂列（狀態點＋文字、模式 pill、導覽圖示）
SKELETON_STATUS_SLOT_H = 40  # 狀態槽（LevelMeter／Progress／Banner 共用一格）
SKELETON_STREAM_H      = 724 # 轉錄流容器
SKELETON_BOTTOMBAR_H   = 52  # 底列（統計文字 + 複製全部主鈕）
# 28（標題列，系統繪製不計）+ 56+40+724+52 = 900 = SKELETON_WIN_H

# StatusSlot LevelMeter 幾何：取代舊「waveform」模式 712×160 大卡片，把同一顆
# 46-bar 三態儀表（idle_color／energy_color／process_color，數學沿用
# WaveformEngine，見 tokens.py + waveform.py）壓進狀態槽的 640×32 橫條。
# bar:gap = 9:5（舊版是 ~12:2 近乎密實），壓扁版空間不夠畫峰值帽／白色高光，
# 兩者一律關閉（見 _draw_chamber_bars）。
LEVEL_METER_W       = 640
LEVEL_METER_H       = 32
LEVEL_METER_GAP     = 5.0
LEVEL_METER_X0      = 1.0
LEVEL_METER_DRAW_W  = LEVEL_METER_W - LEVEL_METER_X0   # 639 = 46×9 + 45×5，整除對齊

# ── 設計 Token（統一從 tokens.py 匯入，此模組內不重複定義任何 hex 色碼）────────
from tokens import (
    # Surfaces
    BG, SURF_1, SURF_2, SURF_3, SURF_4,
    # Text
    TEXT_1, TEXT_2, TEXT_3, TEXT_4,
    # Accents
    ACCENT, ACCENT_HV, ACCENT_BG,
    SUCCESS, SUCCESS_DIM,
    DANGER, DANGER_DIM,
    WARN,
    INDIGO, INDIGO_HV,
    energy_color, idle_color, process_color, WAVE_DIM, MARK_BG,
    # Typography + spacing
    FONT_FAMILY_UI, FONT_FAMILY_TEXT, FONT_FAMILY_MONO,
    SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL, SPACE_2XL,
    # Motion
    BREATHE_IDLE_MS, BREATHE_RECORDING_MS, BREATHE_PROCESSING_MS,
    ROTATE_PROCESSING_MS, RENDER_TICK_MS,
    # v2.28.0 主視窗骨架重寫新增
    CHROME, LINE, ROW_HI, ICON, SCROLL,
    PILL_ON, PILL_ON_FG, PILL_OFF, PILL_OFF_FG,
    BTN_BG, BTN_FG, BTN_DIS, BTN_DIS_FG,
    RED_BG, RED_LINE, RED_TEXT,
    # Aperture 主視窗內容層新增（UtteranceBlockV2 卡片 + 空狀態／最近區）
    CARD, CARD_HI, LINE_HI, HAIR,
    META, META_HI, TEXT_BODY, TEXT_BODY_2,
    CYAN_TEXT, MARK, CHIP_BG,
    SEG_BG, SEG_LINE, SEG_ON, SEG_ON_FG,
    KEY_BG, KEY_LINE, SKEL, SKEL_2,
)

# ─────────────────────────────────────────────────────────────────────────────
#  Ambient Chamber — 幾何與動畫常數
# ─────────────────────────────────────────────────────────────────────────────

CHAMBER_SIZE   = 280                        # Canvas 寬高（像素，正方形）
CHAMBER_CENTER = CHAMBER_SIZE // 2          # 圓心座標 (140, 140)
DISC_RADIUS    = 60                         # 中央可點擊圓盤的半徑

RING_RADII_5   = (80, 96, 112, 128, 140)    # 閒置 / 錄音：5 層同心圓半徑
RING_RADII_4   = (80, 100, 120, 140)        # 處理中：4 層（少一層 = 視覺「收斂」感）
RING_STROKE    = 2                           # 同心圓線寬（像素）

# 各狀態下同心圓的透明度（由內到外遞減）
RING_ALPHA_IDLE       = (0.25, 0.18, 0.12, 0.07, 0.03)
RING_ALPHA_RECORDING  = (0.35, 0.24, 0.15, 0.08, 0.04)
RING_ALPHA_PROCESSING = (0.30, 0.18, 0.10, 0.05)

# RMS 音量驅動的圓環擴張 + 漣漪發射
RMS_EXPAND_GAIN   = 0.18   # 最大擴張比例：RMS=1.0 時半徑增加 18%
RMS_RIPPLE_THR    = 0.15   # 漣漪觸發閾值：RMS > 0.15 且 > 上一幀 × 1.5 時發射
RIPPLE_R0         = 140    # 漣漪起始半徑（像素）
RIPPLE_R1         = 180    # 漣漪終止半徑（像素）
RIPPLE_DURATION   = 1.2    # 漣漪持續時間（秒）
RIPPLE_ALPHA0     = 0.4    # 漣漪起始透明度
RIPPLE_MAX        = 3      # 同時存在的最大漣漪數，防止畫面過於混亂

# 處理中狀態的旋轉粒子環
PROC_PARTICLES        = 12  # 粒子總數（均勻分布在環上）
PROC_PARTICLE_RADIUS  = 4   # 每個粒子的半徑（像素）


# ─────────────────────────────────────────────────────────────────────────────
#  Aperture Spectral Bands 波形視覺常數（v2.25.0）
# ─────────────────────────────────────────────────────────────────────────────
# 錄音態改用能量色溫 bar 視覺，取代舊三態光場的紅色脈衝＋漣漪（D1 決策：
# DANGER 紅收回，只留給真正的錯誤）。數學完全照抄 /tmp/aperture_engine.js
# drawBands() 的 L3 core 段（bar + 高光 + 峰值帽）與削波髮絲線；L1 back
# （模糊背層）／L2 bloom（發光）兩層刻意不做——tkinter Canvas 沒有
# blur／lighter 合成，單幀物件數也撐不住 20 FPS，還會排擠語音引擎 CPU。
# 逃生門：cfg.record_visual == "chamber" 時完全不建立 / 不呼叫這條路徑。

import tokens as _tokens_mod   # 讀 _THEME（tokens 於 import 時鎖定）

WAVE_N_BARS_MAIN = 46      # 主視窗 Ambient Chamber 的 bar 數
WAVE_N_BARS_MINI = 13      # Mini HUD 的 bar 數（極簡波形）
WAVE_GAP         = 2.0     # bar 間距（像素）
WAVE_CORE_ALPHA  = 0.96    # L3 核心 bar 不透明度（JS 原值 dark=0.98／light=0.92
                           # 取中間值單一常數；tkinter 無 alpha channel，靠
                           # blend() 對 SURF_1 混色模擬半透明）
MINI_WAVE_W = 100          # Mini HUD 波形 canvas 寬（像素）
MINI_WAVE_H = 28           # Mini HUD 波形 canvas 高（像素）
# 白色鏡面高光只在深色主題畫（參考實作 `if (v > 0.22 && !light)`）。
# tokens 在 import 時鎖定 theme，這裡跟著同一個來源、不另外讀設定檔。
_IS_LIGHT_THEME = getattr(_tokens_mod, "_THEME", "dark") == "light"

# ─────────────────────────────────────────────────────────────────────────────
#  三態統一（v2.27.0）── 主視窗 Ambient Chamber 改「寬條」，idle／processing
#  併入同一個 canvas + 同一支繪圖函式（見 _draw_aperture_bars）。
# ─────────────────────────────────────────────────────────────────────────────

WAVE_CHAMBER_W = 712       # 主視窗 chamber canvas 寬（像素）——46 bar 在此寬度
                           # 下每根 ≈13.5px，比例才對（280px 下只有 4.1px）
WAVE_CHAMBER_H = 160       # 主視窗 chamber canvas 高（像素）

# 錄音態大波形（v2.28.0）：疊在轉錄流容器上的「華麗版」波形，寬度跟著
# _blocks_container 目前的實際寬度走（見 _show_big_wave 的 relwidth=1.0），
# 只有高度是固定值——240 是設計規格給定的視覺份量，不需要跟著視窗縮放。
BIG_WAVE_H = 240

# 處理態掠掃光帶：接不上真實進度時的等速 fallback 週期（見 _processing_sweep_progress）
# 閒置行波振幅重映射（Aperture 2z：閒置地板 1.6%→4.6%）。
#   引擎的地板公式 0.016 + 0.014×sin 給的是 1.6%→3.0%（那是參考實作的
#   **錄音態**「bar 不歸零」地板）。閒置態要更明顯的呼吸感，所以在 gui 的
#   idle 分支把 swing 放大：0.016 + (v−0.016) × (0.030/0.014) → 1.6%→4.6%。
WAVE_IDLE_BASE        = 0.016
WAVE_IDLE_SWING_SCALE = 0.030 / 0.014   # ≈ 2.143

PROCESS_SWEEP_CYCLE_MS = 1900.0
# 光帶前緣「亮帶」半寬，單位是 bar index（1.0 = 前後各約 1 根 bar 的亮區）
PROCESS_SWEEP_BAND_HALF_BARS = 1.0


def _draw_spectral_bars(
    canvas: "tk.Canvas",
    engine: WaveformEngine,
    width: int,
    height: int,
    elapsed_ms: float,
    big: bool,
) -> None:
    """畫 Spectral Bands 波形（L3 核心層）到指定 canvas。

    照抄 aperture_engine.js drawBands() 的 L3 core 段（bar + 高光 + 峰值帽）
    與削波髮絲線，數學不變；只有「圓角」因 tkinter 沒有對應原生功能改用
    直角矩形（create_rectangle）。L1 back（模糊背層）／L2 bloom（發光）
    兩層刻意不做，同一顆函式給主視窗（46 bar）與 mini HUD（13 bar）共用。

    big=True 才畫峰值帽（照抄 JS 的 `rig.big` 旗標）；mini HUD 用 big=False
    省掉峰值帽物件量，維持「極簡波形」。呼叫端須自行先 canvas.delete("all")。

    Args:
        canvas: 目標 tk.Canvas。
        engine: 已呼叫過 update() 的 WaveformEngine，取 bars / peaks / clipping。
        width, height: canvas 尺寸（像素）。
        elapsed_ms: 狀態經過的毫秒數，驅動削波髮絲線的閃爍相位。
        big: 是否畫峰值帽（主視窗 True／mini HUD False）。
    """
    n = engine.n_bars
    bar_w = max(2.0, (width - (n - 1) * WAVE_GAP) / n)
    cy = height / 2.0
    margin = 10.0 if big else 3.0
    max_h = height / 2.0 - margin

    bars = engine.bars
    peaks = engine.peaks

    for i in range(n):
        x0 = i * (bar_w + WAVE_GAP)
        x1 = x0 + bar_w

        v = bars[i]
        h = max(1.2, v * max_h)
        color = blend(energy_color(v), SURF_1, WAVE_CORE_ALPHA)
        canvas.create_rectangle(x0, cy - h, x1, cy + h, fill=color, outline="")

        # 高光：v > 0.22 時在 bar 中線加一道白色細線（照抄 JS，不分 big/mini）。
        # 只在深色主題畫——參考實作寫的是 `if (v > 0.22 && !light)`：白色高光是
        # 「深色介質裡的鏡面反射」語意，淺色主題（墨水濃度斜坡）上會變成一道
        # 白色切痕、把 bar 割成兩半。PNG 預覽實際看到這個問題才補上此條件。
        if v > 0.22 and not _IS_LIGHT_THEME:
            hi_alpha = min(0.6, (v - 0.22) * 1.3)
            hi_color = blend("#FFFFFF", SURF_1, hi_alpha)
            canvas.create_rectangle(
                x0, cy - 0.6, x1, cy + 0.6, fill=hi_color, outline=""
            )

        # 峰值帽：只有 big（主視窗）才畫，照抄 JS 的 `rig.big` 旗標
        if big and peaks[i] > 0.08:
            ph = peaks[i] * max_h
            peak_color = blend(energy_color(peaks[i]), SURF_1, 0.42)
            canvas.create_rectangle(
                x0, cy - ph - 2.4, x1, cy - ph - 0.6, fill=peak_color, outline=""
            )
            canvas.create_rectangle(
                x0, cy + ph + 0.6, x1, cy + ph + 2.4, fill=peak_color, outline=""
            )

    # 削波髮絲線：上下各一條琥珀線，閃爍頻率照抄 JS（sin(now*0.012)）
    if engine.clipping:
        alpha = 0.35 + 0.35 * (0.5 + 0.5 * math.sin(elapsed_ms * 0.012))
        clip_color = blend(WARN, SURF_1, alpha)
        canvas.create_rectangle(
            0, cy - max_h - 1.2, width, cy - max_h - 0.1, fill=clip_color, outline=""
        )
        canvas.create_rectangle(
            0, cy + max_h + 0.1, width, cy + max_h + 1.2, fill=clip_color, outline=""
        )


# ─────────────────────────────────────────────────────────────────────────────
#  三態統一繪圖函式（v2.27.0）── 主視窗 Ambient Chamber 專用
#  「三態不是三個畫面，是同一個儀表的三種讀數」：idle／recording／processing
#  共用同一個 canvas、同一份 46 bar 幾何（bar_w／cy／max_h）、同一支
#  create_rectangle 繪製流程。三態差異完全收斂成兩個參數（values 高度來源、
#  color_fn 顏色查表），peaks/clipping/highlight 只有 recording 態會用到。
#
#  注意：這支函式**不是** _draw_spectral_bars 的取代品——後者是 Mini HUD
#  （13 bar 極簡波形）專用，兩者刻意保持獨立、互不相依，避免這裡的三態
#  統一改動波及已經做完的 Mini HUD（v2.25.0）。
# ─────────────────────────────────────────────────────────────────────────────

def _autohide_scrollbar(scroll_frame: "ctk.CTkScrollableFrame") -> None:
    """讓 CTkScrollableFrame 的捲動條在「內容裝得下」時自動隱藏（macOS 原生行為）。

    為什麼需要：CTkScrollableFrame 的捲動條永遠顯示，而內容不需要捲動時
    thumb 會**撐滿整個軌道**——看起來就是一條從上到下的直線，使用者回報
    「右邊為什麼有一條線做區隔，感覺很怪」就是這個。設計規格寫的是
    「捲動條寬 6 圓角 3」＝一個代表捲動位置的**滑塊**，不是分隔線。

    做法：掛在內部 canvas 的 <Configure> 上，比對 scrollregion 高度與可視
    高度，用 pack_forget()／pack() 收放捲動條。CTkScrollableFrame 內部用
    grid 佈局捲動條，所以要用 grid_remove()／grid() 才不會破壞版面。

    全段包 try/except：這裡碰的是 customtkinter 的內部屬性（_scrollbar /
    _parent_canvas），未來版本改結構時只會失去自動隱藏、不會讓 App 掛掉。
    """
    try:
        canvas = scroll_frame._parent_canvas
        bar = scroll_frame._scrollbar
    except Exception:
        return

    def _sync(_evt=None) -> None:
        try:
            region = canvas.bbox("all")
            if region is None:
                return
            content_h = region[3] - region[1]
            visible_h = canvas.winfo_height()
            # +2 容差：內容剛好等高時不要因為捨入誤差閃出捲動條
            if content_h <= visible_h + 2:
                bar.grid_remove()
            else:
                bar.grid()
        except Exception:
            pass

    canvas.bind("<Configure>", lambda e: canvas.after_idle(_sync), add="+")
    scroll_frame.bind("<Configure>", lambda e: canvas.after_idle(_sync), add="+")
    canvas.after(80, _sync)   # 首次繪製後同步一次（此時 winfo_height 才有值）


def _draw_aperture_bars(
    canvas: "tk.Canvas",
    n_bars: int,
    width: int,
    height: int,
    values: list,
    color_fn,
    *,
    big: bool = True,
    show_highlight: bool = True,
    peaks: Optional[list] = None,
    clipping: bool = False,
    elapsed_ms: float = 0.0,
    gap: float = WAVE_GAP,
    x0: float = 0.0,
) -> None:
    """主視窗 chamber 三態統一繪圖：同一份 bar 幾何、同一套 fill 邏輯。

    Args:
        values: 長度 n_bars、每根 bar 的高度比例 0..1。idle／recording 讀
            WaveformEngine 的即時 bars；processing 讀「錄音剛結束那一刻」
            的凍結快照（不再呼叫 engine.update()）。
        color_fn: (i, v) -> hex 色碼，呼叫端決定顏色語意（idle_color／
            energy_color 依振幅查表；processing 依「有沒有被光帶掃到」）。
            回傳的是查表後的「原色」，blend() 混色統一在這支函式內做一次，
            不需要呼叫端各自處理。
        peaks / clipping: 只有 recording 態會傳非 None／True；idle／
            processing 天生沒有這兩個概念，呼叫端留預設值即可。
        show_highlight: 白色鏡面高光（v > 0.22）只有 recording 態開啟——
            idle／processing 刻意關閉，維持設計要求的「乾淨、不搶戲」。
        gap / x0: bar 間距與繪圖起點偏移（viewport 像素）。預設值 = 舊行為
            （gap=WAVE_GAP、x0=0）——v2.28.0 主視窗骨架重寫的 StatusSlot
            LevelMeter 需要不同的 bar:gap 密度，加這兩個參數而非另開一支
            函式（幾何以外的邏輯完全共用，沒有理由複製一份）。

    呼叫端須自行先 canvas.delete("all")。
    """
    n = n_bars
    bar_w = max(2.0, (width - (n - 1) * gap) / n)
    cy = height / 2.0
    margin = 10.0 if big else 3.0
    max_h = height / 2.0 - margin

    for i in range(n):
        bx0 = x0 + i * (bar_w + gap)
        bx1 = bx0 + bar_w

        v = values[i]
        h = max(1.2, v * max_h)
        color = blend(color_fn(i, v), SURF_1, WAVE_CORE_ALPHA)
        canvas.create_rectangle(bx0, cy - h, bx1, cy + h, fill=color, outline="")

        if show_highlight and v > 0.22 and not _IS_LIGHT_THEME:
            hi_alpha = min(0.6, (v - 0.22) * 1.3)
            hi_color = blend("#FFFFFF", SURF_1, hi_alpha)
            canvas.create_rectangle(
                bx0, cy - 0.6, bx1, cy + 0.6, fill=hi_color, outline=""
            )

        if big and peaks is not None and peaks[i] > 0.08:
            ph = peaks[i] * max_h
            peak_color = blend(energy_color(peaks[i]), SURF_1, 0.42)
            canvas.create_rectangle(
                bx0, cy - ph - 2.4, bx1, cy - ph - 0.6, fill=peak_color, outline=""
            )
            canvas.create_rectangle(
                bx0, cy + ph + 0.6, bx1, cy + ph + 2.4, fill=peak_color, outline=""
            )

    if clipping:
        alpha = 0.35 + 0.35 * (0.5 + 0.5 * math.sin(elapsed_ms * 0.012))
        clip_color = blend(WARN, SURF_1, alpha)
        canvas.create_rectangle(
            x0, cy - max_h - 1.2, x0 + width, cy - max_h - 0.1, fill=clip_color, outline=""
        )
        canvas.create_rectangle(
            x0, cy + max_h + 0.1, x0 + width, cy + max_h + 1.2, fill=clip_color, outline=""
        )


def _processing_sweep_progress(elapsed_ms: float, use_real: bool, known_frac: float) -> float:
    """處理態掠掃光帶的位置，0（最左）→1（最右）。

    真實進度（use_real=True）：直接回 known_frac——這是 LocalAgreement-2
    在錄音期間就已經 commit 的邊界（哪段兩輪 ASR 都同意），不是估的。
    只有「這次錄音真的走過 LA 路徑、而且已經 commit 過東西」才會是 True，
    見呼叫端 _transition_to_processing 怎麼決定。

    等速 fallback（use_real=False）：fixed_chunk 路徑，或錄音太短、LA 從
    沒機會 commit 過任何東西（一次轉完），接不上真實進度就不硬湊——退回
    週期 PROCESS_SWEEP_CYCLE_MS 的等速掠掃，无限循環（不知道還要處理多久，
    用循環動畫表示「還在跑」比停在一個假數字誠實）。
    """
    if use_real:
        return 0.0 if known_frac < 0.0 else 1.0 if known_frac > 1.0 else known_frac
    cycle_pos = elapsed_ms % PROCESS_SWEEP_CYCLE_MS
    return cycle_pos / PROCESS_SWEEP_CYCLE_MS


def _processing_bar_color(i: int, v: float, n_bars: int, progress: float) -> str:
    """處理態單一 bar 的顏色：光帶前緣最亮、已掃過的依振幅上色、還沒掃到的 WAVE_DIM。

    progress（0..1，見 _processing_sweep_progress）換算成 bar-index 空間的
    連續位置 sweep_idx，跟每根 bar 的中心比較距離，分三段：
      還沒掃到（bar 中心在 sweep_idx 前緣之後）        → WAVE_DIM（扁平中性灰）
      光帶前緣（±PROCESS_SWEEP_BAND_HALF_BARS 內）      → process_color(1.0)（最亮）
      已經掃過（bar 中心在 sweep_idx 後緣之前）          → process_color(v)（依凍結
                                                            振幅取色，保留輪廓資訊）
    """
    sweep_idx = progress * n_bars
    center = i + 0.5
    if center > sweep_idx + PROCESS_SWEEP_BAND_HALF_BARS:
        return WAVE_DIM
    if center > sweep_idx - PROCESS_SWEEP_BAND_HALF_BARS:
        return process_color(1.0)
    return process_color(v)


# ─────────────────────────────────────────────────────────────────────────────
#  Reduced-motion detection (macOS system preference)
# ─────────────────────────────────────────────────────────────────────────────

def system_reduce_motion() -> bool:
    """讀取系統「減少動態效果」偏好設定（啟動時讀取一次）。

    依 macOS 慣例，偏好設定變更需要重新啟動 App 才生效。
    任何錯誤（鍵不存在、超時）都回傳 False（不限制動畫）。

    Windows 分支：Windows 的對應設定在登錄檔（Registry）
    `SPI_GETCLIENTAREAANIMATION`，屬錦上添花、非必要，這裡先回 False
    （不限制動畫）而非硬造登錄檔讀取邏輯。
    """
    if IS_MAC:
        try:
            r = subprocess.run(
                ["defaults", "read", "com.apple.universalaccess", "reduceMotion"],
                capture_output=True, text=True, timeout=0.5,
            )
            return r.stdout.strip() == "1"
        except Exception:
            return False
    # Windows/Linux：尚未實作系統偏好讀取，預設不限制動畫
    return False


def resolve_reduce_motion(pref: str) -> bool:
    """A3（v2.7.0）：依使用者偏好決定最終 reduce_motion 旗標。

    Args:
        pref: "auto" / "always" / "never"（未知值視為 "auto"）

    Returns:
        True → 渲染迴圈關閉呼吸光圈 / 漣漪 / 粒子環旋轉
    """
    if pref == "always":
        return True
    if pref == "never":
        return False
    # "auto" 與其他未知值都 fallback 到系統偏好
    return system_reduce_motion()


# v2.22.0 VAD 對齊切窗（治 streaming 腰斬詞的根）─────────────────────────────
# fixed_chunk streaming 每滿 10s 就硬切一段送轉錄，切點與語意無關（實測 195 個
# 接縫 100% 落在句中）。這裡不跑完整 Silero VAD（太重、每秒 tick 都要跑不划算），
# 改用簡單 RMS 法：只在「最後 SEARCH_WINDOW_S 秒」裡找一個能量最低的靜音窗，
# 找到就把切點挪去那裡（該點之後的音留給下一段開頭，一個 sample 都不丟）。
# v2.24.0：2.5 → 4.0。實測 v2.23.1 後 206 筆切窗、所有命中切點都 ≥7.64s（理論下限
#   10s 觸發 − 2.5s 窗 = 7.5s 幾乎完全吻合）——演算法只看得到每個 12s 週期的最後
#   37.5%、前段的自然停頓根本沒機會被檢查，這是命中率卡在 41.5% 的結構主因。
#   拉到 4s 讓可視範圍擴到 6–12s；再往前要動「開始搜尋的時機」屬更大改動、先不做。
STREAM_CUT_SEARCH_WINDOW_S = 4.0   # 只在 buffer 最後 4s 內找靜音切點
STREAM_CUT_RMS_WINDOW_MS = 80      # RMS 計算視窗
# v2.23.1：門檻改「最低點(floor)→中位數(hi)之間的比例位置」，不再是「中位數×比例」。
#   舊公式 threshold = median×0.3 要求 SNR > 3.33 才可能成立；背景噪音把停頓的
#   音量墊高時，數學上永遠達不到 → 實測 7/20、7/23 兩天 0/18 全撞 hard cap、
#   7/24（安靜）7/7 全中，整體只有 28%。錨點（median）從來就不代表「噪音底」。
STREAM_CUT_RMS_THRESHOLD_RATIO = 0.3   # threshold = floor + 0.3 × (median − floor)
STREAM_CUT_SPREAD_MIN = 1.6        # median/floor 動態範圍下限；低於此視為「整段音量太平」→ 不信任、退回 hard cap
STREAM_CUT_MIN_QUIET_WINDOWS = 3   # 需連續 3 個視窗（80ms×3 = 240ms）都安靜才算真停頓（濾音節間的短促凹陷）


def find_silence_cut_point(
    audio,
    sample_rate: int = 16_000,
    search_window_s: float = STREAM_CUT_SEARCH_WINDOW_S,
    rms_window_ms: int = STREAM_CUT_RMS_WINDOW_MS,
    threshold_ratio: float = STREAM_CUT_RMS_THRESHOLD_RATIO,
    spread_min: float = STREAM_CUT_SPREAD_MIN,
    min_quiet_windows: int = STREAM_CUT_MIN_QUIET_WINDOWS,
) -> Optional[int]:
    """在 audio 最後 search_window_s 秒內找「真正的停頓」當切點。

    純函式、不碰任何 Tk / 狀態，方便單元測試。

    v2.23.1 演算法（取代 v2.22.0 的「中位數 × 比例」相對門檻）：
      1. 把 audio 切成 rms_window_ms 小視窗，逐一算 RMS。
      2. floor = 全段最小 RMS（噪音底的估計）、hi = 全段中位數（講話音量）。
      3. **動態範圍檢查**：hi / floor < spread_min → 整段音量太平（高噪音、
         或根本沒停過）→ 回 None、退回 hard cap。寧可不切也不要誤切。
      4. threshold = floor + threshold_ratio × (hi − floor)
         —— 錨在「這段音訊自己的噪音底」，不是中位數，所以不會被背景噪音打敗。
      5. **持續性檢查**：只有「連續 ≥ min_quiet_windows 個視窗」都低於門檻才
         算真停頓（濾掉語音音節之間的短促凹陷），取該區段最安靜的視窗中點當切點。
      6. 都找不到 → 回 None（呼叫端 fallback 到 hard cap 硬切）。

    為何改：舊公式 threshold = median × 0.3 隱含「SNR > 3.33 才可能成立」，
    背景噪音把停頓音量墊高時數學上永遠達不到——實測 7/20、7/23 兩天 0/18
    全部撞 hard cap、7/24（安靜）7/7 全中，整體只有 28%。合成音訊模擬
    （SNR 1.2–20 × 停頓 300–1200ms）證實新演算法在 SNR ≥ 1.5 找到停頓率 100%
    （舊演算法 SNR ≤ 3 為 0%），且「連續講話沒停頓」的誤切率維持 0%。

    Args:
        audio: 1-D numpy float32 陣列。
        sample_rate: 取樣率（Hz）。
        search_window_s: 只在音訊尾端這麼多秒內找切點。
        rms_window_ms: 逐視窗 RMS 計算的視窗大小。
        threshold_ratio: 門檻在 floor→hi 之間的比例位置。
        spread_min: hi/floor 動態範圍下限，低於此不信任這段有真停頓。
        min_quiet_windows: 需連續幾個視窗都安靜才算真停頓。

    Returns:
        切點的 sample index（int），或 None（找不到可信的停頓）。
    """
    import numpy as np

    n = len(audio)
    win = max(1, int(sample_rate * rms_window_ms / 1000))
    if n < win * 2:
        return None   # 太短，不夠切兩個視窗

    # 逐視窗 RMS（不重疊、簡單快速）
    n_windows = n // win
    trimmed = audio[: n_windows * win].reshape(n_windows, win)
    rms = np.sqrt(np.mean(trimmed.astype(np.float64) ** 2, axis=1))

    hi = float(np.median(rms))
    if hi <= 0:
        return None   # 全段靜音之類的異常情況，不特別處理
    floor = float(np.min(rms))

    # 動態範圍檢查：floor 太接近中位數 = 整段音量很平（高噪音或全程講話）
    #   → 這種音訊裡「最安靜的地方」也只是相對安靜、不是真停頓，不要切。
    #   floor == 0（數位靜音）代表確實有絕對安靜處，視為無限大 spread、直接通過。
    if floor > 0 and (hi / floor) < spread_min:
        return None

    threshold = floor + threshold_ratio * (hi - floor)

    # 只看最後 search_window_s 秒對應的視窗範圍
    search_samples = int(sample_rate * search_window_s)
    search_start_window = max(0, (n - search_samples) // win)

    quiet = rms[search_start_window:] < threshold
    if len(quiet) == 0:
        return None

    # 持續性檢查：找「連續 ≥ min_quiet_windows 個安靜視窗」的區段，
    #   在所有合格區段中取「最安靜的那個視窗」當切點。
    best_local_idx: Optional[int] = None
    best_rms_val: Optional[float] = None
    run_start: Optional[int] = None

    def _consider(start: int, end: int) -> None:
        """把 quiet[start:end] 這個合格區段納入比較（end 為 exclusive）。"""
        nonlocal best_local_idx, best_rms_val
        seg = rms[search_start_window + start:search_start_window + end]
        local = int(np.argmin(seg)) + start
        val = float(rms[search_start_window + local])
        if best_rms_val is None or val < best_rms_val:
            best_rms_val = val
            best_local_idx = local

    for i, is_quiet in enumerate(quiet):
        if is_quiet:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and (i - run_start) >= min_quiet_windows:
                _consider(run_start, i)
            run_start = None
    # 收尾：區段一路延伸到搜尋窗結尾
    if run_start is not None and (len(quiet) - run_start) >= min_quiet_windows:
        _consider(run_start, len(quiet))

    if best_local_idx is None:
        return None   # 沒有夠長的連續安靜區段 → 沒有可信的停頓

    cut_point = (search_start_window + best_local_idx) * win + win // 2
    return int(cut_point)


# ─────────────────────────────────────────────────────────────────────────────
#  AppWindow
# ─────────────────────────────────────────────────────────────────────────────

class AppWindow(ctk.CTkFrame):
    """應用程式根框架——Apple MacBook Pro 美學風格。

    包含完整的 UI 狀態機（idle → recording → processing → idle）、
    錄音管線（recorder → transcriber → ollama_client）、
    快捷鍵管理（pynput）、自動貼上（auto_paste）等所有核心邏輯。
    """

    # 穩定性（Fix 1 / 2026-05-21、Fix 7 / 2026-05-22 簡化）：
    # NSEvent monitor 心跳檢查週期。NSEvent 後端不會被 idle disable、
    # 不會被 TCC race 殺掉、不會自己 GC，watchdog 幾乎不會 fire；
    # 但仍保留作為極端情境的兜底（例如系統權限被使用者中途撤銷）。
    # 5 秒：權衡 detect latency 與 µs 級檢查成本。
    HOTKEY_WATCHDOG_INTERVAL_MS = 5000

    # v2.17.4：定期跑 dummy ASR 防 MLX weights 被 macOS swap out。
    # 實機觀察：35 分鐘閒置後第一次 ASR 從 RTF 0.2 飆到 4.85（24x 慢）—— macOS
    # 把 3.4GB Qwen3-ASR weights page out 到磁碟、首次推論要 page-in。
    # 背景定期跑保活、強迫 weights 留在 active page。
    # 只在 state=idle 跑（避免跟 user 的 transcribe 撞 lock）。
    #
    # v2.20.2：5 min → 90s — real-world audit log（5.3hr idle + 61 ping 仍 cold load 12s）
    # 證明 5min 間隔不足以抵擋 macOS unified memory page-out。90s 比較保守、
    # 同時也是經驗值：M 系列 unified memory 大概 60-120s 不被 access 就會被 OS 標 page-out 候選。
    MLX_KEEPALIVE_INTERVAL_MS = 90 * 1000   # 90 秒

    # Fix 18 / 2026-05-23（Layer 2）：每 N 毫秒無條件 force-restart NSEvent monitor。
    # 即使 monitor 物件還 alive、其底層 ObjC block 仍可能在閒置 1hr+ 後被 invalidate
    # 而 watchdog 偵測不到（_monitor_global is not None 仍成立）。10 分鐘是
    # 跟 pynput 時期 Layer 3 同樣節奏的 belt-and-suspenders；錄音中跳過避免吞 stop。
    HOTKEY_FORCE_RESTART_INTERVAL_MS = 600_000

    # Fix 18 / 2026-05-23（Layer 3）：監聽 monitor "alive" 但事件靜默的閾值。
    # 超過此秒數沒收到任何 modifier 事件 + App active + idle → log 警告（不主動修，
    # Layer 2 force-restart 已兜底）。純診斷用，未來看 log 趨勢決定要不要調整。
    HOTKEY_SILENT_THRESHOLD_S = 300.0

    # Fix 10 / 2026-05-23：NSEvent handler 擱置的 tap 觸發輪詢間隔。
    # 詳見 _hotkey_tap / _poll_pending_tap 的 docstring（PyObjC + Tk GIL 衝突修法）。
    # 20ms 是 50 Hz 輪詢，使用者按鍵感受不到延遲；CPU 成本忽略不計。
    PENDING_TAP_POLL_MS = 20

    # Fix 17 / 2026-05-23：Cocoa observer / AppleEvent handler 擱置動作輪詢間隔。
    # 50ms（20 Hz）對「click Dock icon → 視窗彈出」UX 足夠快，看不出延遲。
    # 詳見 _poll_cocoa_pending_actions docstring。
    COCOA_POLL_MS = 50

    # 穩定性（Fix 4 / 2026-05-21）：processing 狀態超時自癒上限。
    # MLX 對 large-v3-turbo：RTF ≈ 0.1，60s 音訊 → 6s 推論；cold start +5-10s；
    # 60s 有 ~40s 緩衝，避免誤殺正常推論。若使用者誤殺再放大或設計可設定。
    PROCESSING_TIMEOUT_MS = 60_000

    # Fix 9 / 2026-05-22（P2-A）：動態 timeout = max(60s, audio_len_s × RTF_BUDGET)
    # CPU faster-whisper backend 對 5 分鐘音訊 RTF ≈ 0.3-0.5，需要 90-150s 推論；
    # 60s 固定 timeout 會 100% 誤殺。給 1.0x 即時當上限，等同 60s 音訊配 60s 推論。
    PROCESSING_TIMEOUT_BASE_MS = 60_000
    PROCESSING_TIMEOUT_RTF_BUDGET = 1.0

    # v2.16.0 Streaming 轉錄常數（chunk 邊講邊轉、放開後接 tail 立即出結果）
    STREAM_TICK_MS = 1000               # 每秒檢查 buffer 是否有新 chunk
    STREAM_CHUNK_SAMPLES = 10 * 16_000  # 10s × 16kHz = 一個 chunk 的 sample 數
    # 短於這個 threshold 不啟動 streaming（chunk 開銷 > 收益）
    STREAM_MIN_ENABLE_SAMPLES = 12 * 16_000   # 12 秒
    # _run_transcription 等所有 pending chunks 完成的最大時間
    STREAM_CHUNK_JOIN_TIMEOUT_S = 30.0

    # v2.22.0 VAD 對齊切窗：buffer 滿 10s 後不立刻硬切，先在最後 2.5s 內找
    # 靜音切點；連續講話找不到停頓 → 延後到 hard cap 12s 強制切（保底、
    # 行為同 chunk_cut_mode="fixed" 的舊路徑）。
    STREAM_HARD_CAP_SAMPLES = 12 * 16_000   # 12s × 16kHz

    # v2.21.0：class-level 預設、防禦性 net。__init__ 會覆蓋成 instance attr。
    # 保證即使在繞過 __init__ 的情境（如測試 stub AppWindow.__new__）下這些屬性
    # 也存在、不會在被測方法裡撞 AttributeError。
    #   _la_buffer (v2.20.0 LA-2)、_pipeline_* (v2.20.3 N3 pipeline timing)、
    #   _hotkey_health_tick_count (v2.20.3 N6)、_last_ping_at (v2.20.3 N5)
    # v2.24.0 錄音看門狗：連續無語音太久 → 提醒 → 自動停止（防忘記關錄音）。
    #   8/2 實案：凌晨按下錄音去睡、錄了 10h07m（97.7% 寂靜）、停止瞬間 MLX 當機、
    #   App 停擺 10 小時；8/5 實案：忘記關 97 分鐘、旁人對話整段進了資料庫。
    WATCHDOG_WARN_NO_VOICE_S = 8 * 60    # 連續 8 分鐘無語音 → 系統通知提醒
    WATCHDOG_STOP_NO_VOICE_S = 15 * 60   # 連續 15 分鐘無語音 → 自動停止（內容保留）

    _last_voice_at = None        # v2.24.0：最近一次偵測到語音的 perf_counter 時間
    _watchdog_warned = False     # v2.24.0：本輪錄音是否已發過 8 分鐘提醒
    _la_buffer = None
    _pipeline_summary_emitted = False
    _pipeline_t0 = None
    _pipeline_defer_emit = False           # v2.18.2 raw-paste defer emit
    _pipeline_polish_mode = "blocking"     # v2.18.2
    _hotkey_health_tick_count = 0
    _last_ping_at = None

    # v2.27.0 三態統一：同上緣故的 class-level 防禦性 net。_transition_to_processing
    # 會讀 self._wave_engine / self._proc_sweep_* 決定要不要凍結 bars 快照與
    # 掠掃光帶進度；bare stub（AppWindow.__new__）沒跑過 _build_record_card
    # 就不會有這些 instance attr，靠這裡的 class default 補上、避免撞
    # AttributeError（呼應本區塊上面 v2.21.0 的既有慣例）。
    _wave_engine = None
    _frozen_bars = None
    _proc_sweep_use_real = False
    _proc_sweep_known_frac = 0.0

    # v2.28.0 主視窗骨架重寫：同上緣故的 class-level 防禦性 net。_set_status /
    # _apply_polish_button_style 等共用 helper 會先看 self._skeleton_mode 決定
    # 走新骨架分支還是 chamber 逃生門分支；bare stub（AppWindow.__new__，
    # tests/ 既有慣例）沒跑過 _build_ui 就不會有這個 instance attr。
    # 預設 False（不是真實預設值 True）是刻意的：既有 test stub 只 mock
    # 了 chamber 逃生門那組舊 widget（_status_dot/_status_label 等），
    # 這裡的 class default 選「跟既有 test fixture 相容的值」，而不是
    # 「跟真實執行狀態相符的值」——真正跑起來的 AppWindow 一定會經過
    # _build_ui() 把這個值蓋成正確的 True/False，class default 只是防炸網。
    _skeleton_mode = False

    def __init__(self, master: ctk.CTk, cfg: Config) -> None:
        super().__init__(master, fg_color=BG, corner_radius=0)
        self.pack(fill="both", expand=True)

        self.cfg = cfg
        # v2.20.3 N8：config hash 更新（給 audit_log 每筆 entry 自動帶 snapshot 標記）
        try:
            import audit_log
            audit_log.set_config_hash(self.cfg)
        except Exception:
            pass
        self.recorder    = AudioRecorder()
        # F1（v2.12.0）：啟動時依 cfg.input_device 設裝置；失敗回退系統預設
        if cfg.input_device:
            try:
                if not self.recorder.set_device_by_name(cfg.input_device):
                    log.warning(
                        f"RECORD: startup input_device '{cfg.input_device}' "
                        f"找不到，回退到系統預設"
                    )
            except Exception:
                log_error("startup_set_device_failed", device=cfg.input_device)
        # v2.21.0 Phase M：註冊 CoreAudio 麥克風熱插拔監聽。
        #   _on_audio_devices_changed 在 CoreAudio 執行緒被呼叫 → marshal 回主執行緒。
        if hasattr(self.recorder, "start_device_monitor"):
            try:
                self.recorder.start_device_monitor(self._on_audio_devices_changed)
            except Exception:
                log_error("start_device_monitor_failed")
        self.transcriber = Transcriber()
        # v2.19.0：可疑音檔保留設定推給 transcriber（Agent T 還沒寫完時 hasattr 防炸）
        if hasattr(self.transcriber, "set_suspicious_capture"):
            try:
                self.transcriber.set_suspicious_capture(
                    cfg.suspicious_audio_capture,
                    cfg.suspicious_audio_max_size_mb,
                )
            except Exception:
                log_error("set_suspicious_capture_init_failed")
        # v2.19.x：Silero VAD v5 設定（神經前置過濾、擋鍵盤/雜音；hasattr 防舊版 transcriber）
        if hasattr(self.transcriber, "set_silero_vad"):
            try:
                self.transcriber.set_silero_vad(
                    cfg.silero_vad_enabled,
                    cfg.silero_vad_threshold,
                )
            except Exception:
                log_error("set_silero_vad_init_failed")
        # v2.20.1：Pinyin guard 預設關（regression 從 real-world 抓到）
        if hasattr(self.transcriber, "set_pinyin_guard"):
            try:
                self.transcriber.set_pinyin_guard(
                    getattr(cfg, "pinyin_guard_enabled", False)
                )
            except Exception:
                log_error("set_pinyin_guard_init_failed")
        self.ollama      = OllamaClient()
        # 用設定檔同步 Ollama 參數（base_url / model / enabled / timeout）
        self.ollama.apply_app_config(cfg)
        # v2.18.0：Vertex AI Gemini polish client（雲端、duck-type 兼容 Ollama）
        # lazy init、enabled 只在 polish_backend="vertex" 時 True
        try:
            from vertex_polish import VertexPolishClient
            self.vertex = VertexPolishClient()
            self.vertex.apply_app_config(cfg)
        except Exception:
            log_error("vertex_client_init_failed")
            self.vertex = None
        # polish 路由器：依 polish_backend 回對應 client（Ollama / Vertex / None）
        self._refresh_polish_backend()
        self.hotkey_mgr  = HotkeyManager(
            on_tap_cb=self._hotkey_tap,
        )

        # Fix 10 / 2026-05-23：NSEvent handler 擱置的 tap 計數。
        # _hotkey_tap 從 NSEvent monitor handler（PyGILState_Ensure context）被呼叫，
        # 絕對不能進入 Tk；只能遞增此計數。實際 toggle 由 _poll_pending_tap 在
        # Tk mainloop iteration（PyEval_SaveThread/RestoreThread context）中分派。
        self._pending_tap_count: int = 0

        # Fix 17 / 2026-05-23：Cocoa observer / AppleEvent handler 擱置動作。
        # 同 Fix 10 根因（PyObjC + Tk GIL 衝突），改成 list.append（pure Python，
        # GIL 由 PyGILState_Ensure 拿著、安全），實際分派由 _poll_cocoa_pending_actions
        # 在 Tk mainloop iteration 跑。
        self._cocoa_pending_actions: list = []

        # Fix 18 / 2026-05-23（Layer 2）：上次 force-restart NSEvent monitor 時間戳。
        # 詳見 HOTKEY_FORCE_RESTART_INTERVAL_MS 與 _hotkey_watchdog。
        self._last_hotkey_force_restart: float = time.monotonic()

        # v2.14.1 — 自動 restore 抑制旗標（epoch seconds、>now() = 抑制中）
        # 按熱鍵時設成 now+1.5s；期間 Cocoa BecomeActive / WillBecomeActive / Poll
        # 等「app 變 active」事件不會把 minimized 視窗 deiconify 拉回前景，
        # 讓「最小化 + 熱鍵錄音」可以真的背景跑、不打擾 user。
        # Dock icon 點擊走 AppleEventReopen 路徑、不受此影響、永遠正常 restore。
        self._suppress_auto_restore_until: float = 0.0

        # State
        self._state:       str   = "idle"
        self._rec_start:   float = 0.0

        # 錄音當下的前景 app：
        #   _frontmost_app —— 永遠抓（preset 路由用）；與 auto_paste 無關
        #   _paste_target  —— 只在 auto_paste=True 時才同步填值（⌘V 目標）
        # A1 修法：拆開兩個概念，讓關掉 auto-paste 也能用情境路由
        self._frontmost_app: Optional[str] = None
        self._paste_target:  Optional[str] = None

        # Streaming
        self._stream_samples: int       = 0
        self._stream_chunks:  list[str] = []   # 按 chunk index 排序、placeholder "" 占位
        self._stream_tick_id            = None
        # v2.16.0 streaming：dispatch / complete 計數、_run_transcription 用來等
        # 所有背景 chunk 推論完成才合併。Index-based 寫進 _stream_chunks 確保順序。
        self._stream_dispatched: int    = 0
        self._stream_completed:  int    = 0
        # Generation tag：防快速連續錄音時、前一輪未完成 chunk 污染新輪 counter。
        # _transition_to_recording += 1；chunk thread 只在 generation 還匹配時
        # 寫結果 / 增 _completed、否則 silent drop（資料來自上一輪 recording、
        # 對新輪無意義）。
        self._stream_generation: int    = 0
        # v2.17.0 streaming polish：每個 ASR chunk 完 → 立即 dispatch Ollama polish
        # 平行進行。User 放開時 polish 已大致完成、_start_polish 只需 polish tail。
        # 體感：22s polish → ~5-7s。Index-based slots、parallel to _stream_chunks。
        self._stream_polished: list[str]    = []
        self._stream_polish_dispatched: int = 0
        self._stream_polish_completed:  int = 0

        # v2.19.x LocalAgreement-2 streaming（experimental、躲在 config flag 後）
        # cfg.streaming_algo == "local_agreement" 時、_transition_to_recording 會
        # 建一個 LocalAgreementBuffer；fixed_chunk path 保持 None、不受影響。
        # 詳見 streaming_local_agreement.py。
        self._la_buffer = None
        # LA path 在背景 thread 跑 process_tick；in_flight 避免 tick 重疊
        self._la_in_flight: bool = False
        # LA finalize 在背景跑、與 _run_transcription 共用「等 ASR 完成」邏輯；
        # _run_transcription 直接用這顆結果合併、不再額外跑 transcribe(tail)
        self._la_finalize_done: bool = False
        self._la_finalize_text: str = ""

        # B2（v2.7.0）：processing timeout self-heal 的 after id；快速連續錄音
        # 避免 N 個 pending callback 並存（之前每次 _transition_to_processing
        # 都 schedule 一個新的卻不 cancel 舊的）。
        self._processing_timeout_id     = None

        # Polish（AI 潤飾）狀態追蹤
        # _polish_generation：每次新轉錄 +1，讓遲到的潤飾結果可以被識別丟棄
        # _last_raw         ：Whisper 產出的原文（C1：永不被 preset 剝除汙染）
        # _last_llm_input   ：實際送給 LLM 的輸入（可能已剝除關鍵字）
        # _last_polished    ：LLM 回的潤飾版（未完成時為 None）
        # _polish_busy      ：避免「潤飾中」時手動按 潤飾 鈕重複觸發
        self._polish_generation: int           = 0
        self._last_raw:          str           = ""
        self._last_llm_input:    Optional[str] = None
        self._last_polished:     Optional[str] = None
        self._polish_busy:       bool          = False
        # toggle chip 狀態：True=顯示潤飾版、False=顯示原文（Whisper 原文）
        # 新一段轉錄進來會重置為 True；沒有潤飾版時兩顆都灰態
        self._showing_polished:  bool          = True

        # 結果標題三段式狀態（C2：取代字串切割手術）
        #   _title_base    —— "轉錄結果  (Xs · lang · model)"
        #   _title_preset  —— 非 default preset 的 display_name，其他為 None
        #   _title_status  —— "潤飾中…" / "已潤飾 · 1.2s" / "原文（潤飾失敗）"/ None
        self._title_base:    str           = "轉錄結果"
        self._title_preset:  Optional[str] = None
        self._title_status:  Optional[str] = None

        # ── Pipeline timing（按下結束 → 貼上完成 端到端拆解）─────────────
        # 設計目標：讓 log 可以回答以下問題：
        #   1. 純轉譯（無 AI）：從按下結束熱鍵到字貼上總共幾秒？
        #   2. 純轉譯：轉譯本身耗時 vs 貼上耗時分別多少？
        #   3. 有 AI 潤飾：轉譯 → 潤飾 → 貼上 三段各耗時？
        #   4. 端到端總時間（不含錄音時長本身）
        # 計時錨點都是 time.perf_counter()；None 表示該階段尚未發生。
        self._pipeline_t0:            Optional[float] = None  # 按下結束的瞬間
        self._pipeline_transcribe_at: Optional[float] = None  # 轉譯完成相對 t0
        self._pipeline_polish_at:     Optional[float] = None  # 潤飾完成相對 t0
        self._pipeline_summary_emitted: bool          = False # 防止重複 emit
        # v2.18.2：raw paste strategy bug fix（Agent 4 P1-1）
        # raw 策略：先貼原文、polish 完背景跑、不再覆蓋。第一次貼上時
        # _do_auto_paste 不應觸發 emit、否則 polish_s 永遠是 0。改成 defer
        # 到 polish 完成（_finish_polish）再一起 emit。
        self._pipeline_defer_emit:    bool             = False
        self._pipeline_deferred_paste: dict            = {}    # 暫存 paste 結果
        # v2.18.2：streaming polish 跟 blocking polish 區分（Agent 4 P2）
        # streaming = polish 在錄音期間就跑、polish_s 只反映 join + tail
        # blocking  = polish 在 ASR 完成後才跑、polish_s 反映實際耗時
        self._pipeline_polish_mode:   str              = "blocking"

        self._build_ui()
        self._start_hotkey_listener()
        # 診斷（Task B）：mainloop 第一個 idle tick 抵達時間戳，
        # 配合 hotkey_manager.py 的 listener_fully_started / first_press_received
        # 可以判斷「首次按鍵丟失」是否落在 listener 啟動 vs. tk 進 mainloop 的 gap
        self.after(0, lambda: log.info(
            f"GUI: first idle tick reached at t={time.monotonic():.3f}"
        ))
        # 穩定性（Fix 1 / 2026-05-21、Fix 7 / 2026-05-22 簡化）：
        # 每 5 秒檢查 NSEvent monitor 是否還活著。NSEvent 不會自己死，
        # 但保留 watchdog 框架兜底極端情境。
        self.after(self.HOTKEY_WATCHDOG_INTERVAL_MS, self._hotkey_watchdog)
        # Fix 10 / 2026-05-23：tap poller —— 詳見 _hotkey_tap docstring。
        self.after(self.PENDING_TAP_POLL_MS, self._poll_pending_tap)
        # IS_MAC：以下整段（Dock icon 恢復視窗的 4 層保險絲）都是 macOS 專屬。
        # Windows 沒有 Dock、沒有 Space 概念，工作列（taskbar）點圖示的視窗
        # 恢復行為由 Windows 視窗管理員原生處理，Tk 的 deiconify/lift/focus_force
        # 已經足夠，這整段 Cocoa observer 邏輯 Windows 分支直接跳過（no-op）。
        if IS_MAC:
            # Fix 17 / 2026-05-23：Cocoa observer poller —— 詳見 _cocoa_pending_actions。
            self.after(self.COCOA_POLL_MS, self._poll_cocoa_pending_actions)
            # 穩定性（Fix 5 / 2026-05-22）：macOS Dock icon 點擊把最小化的視窗叫回來。
            # 兩條保險絲（Apple Event 處理在 shim Python.app 上可能不完整）：
            #   1. ::tk::mac::ReopenApplication — Tk-on-macOS 的官方 idiom，但需要
            #      AppleEvent handler 註冊完整才會觸發
            #   2. <<Activate>> 虛擬事件 — App 取得焦點時觸發，較可靠的 fallback
            #      （只在 iconic 狀態才 deiconify，避免每次 focus 都干擾）
            try:
                self.createcommand('::tk::mac::ReopenApplication', self._on_dock_reopen)
            except Exception:
                pass
            self.bind("<<Activate>>", self._on_app_activate)
            # Fix 5c / 2026-05-22：實測診斷顯示 NSApp delegate 為 None — Tk 沒設好
            # NSApplicationDelegate，AppleEvent kAEReopenApplication 沒人接收，所以
            # 上面兩條（::tk::mac::ReopenApplication + <<Activate>>）都不會觸發。
            # 改用 PyObjC 直接掛 Cocoa 層的 NSApplicationDidBecomeActiveNotification，
            # 不依賴 Tk 中介。失敗只 log，不阻擋啟動。
            self._install_cocoa_activation_observer()
            # Plan C / Fix 5c v3：終極保險絲 — 每 500ms 輪詢 NSApp.isActive 狀態轉換。
            # 若上面 4 層（Tk createcommand / <<Activate>> / Cocoa Did+Will Active）
            # 都沒觸發，這層會在 user 重新 focus 我們時最多 500ms 內恢復視窗。
            # 純 Python 計時器，不依賴任何 macOS 通知 — 連通知系統壞掉都救得到。
            self._last_nsapp_active = False  # boot 時非 active
            self.after(500, self._poll_window_visibility)
        self.after(1500, self._warmup_model)
        # 開啟著的話再去探 Ollama；即使 Ollama 離線也不會卡 UI 建構。
        self.after(2000, self._refresh_ollama_health)
        # v2.17.1：Ollama 預載 model 進 VRAM（背景 thread、不卡 UI）
        # 配合 keep_alive=-1、模型載入後永久駐留、user 第一次用就快
        self.after(2500, self._warmup_ollama)
        # v2.17.4：定期跑 dummy ASR 保活 MLX weights（防 macOS swap out）
        # 第一次 5 分鐘後跑、之後 each tick 自己排程下次
        self.after(self.MLX_KEEPALIVE_INTERVAL_MS, self._mlx_keepalive_tick)
        # v2.20.3 N5：記下上次 keepalive ping 結束時間（用 perf_counter，單調）。
        # 用來算 since_last_ping_s（兩次 ping 間隔；首次為 None → 不 emit since_last）
        self._last_ping_at: Optional[float] = None
        # v2.20.3 N6：hotkey 健康 snapshot 計數器（每 5 分鐘 fire 一次 audit event）
        # 60 ticks × 5s = 300s = 5 min
        self._hotkey_health_tick_count: int = 0

        # #4 字典：初次載入並餵給 Transcriber
        self._reload_dictionary()

        # #2 Prompt 熱重載：啟動 mtime 背景輪詢
        def _on_reload(name):
            # 熱重載只是替換 module-level 字串，OllamaClient 與 Transcriber
            # 都已走動態查詢路徑，不必重啟 pipeline
            self.after(0, lambda: self._show_toast(f"已重新載入 {name}.py"))
        self._prompt_reloader = PromptReloader(on_reload=_on_reload)
        if self.cfg.prompt_hot_reload:
            self._prompt_reloader.start()

        # Phase 3.2 歷史紀錄：每次轉錄完成會 insert，潤飾完成後 update_polish
        # _current_history_id：最近一筆 insert 的 rowid，供 _finish_polish 更新
        self.history_store: Optional[HistoryStore] = None
        self._current_history_id: Optional[int] = None
        if self.cfg.history_enabled:
            try:
                self.history_store = HistoryStore()
            except Exception:
                log_error("history_store_init_failed")
        # Aperture 空狀態的「最近」區需要 self.history_store：_build_ui()
        # 在上面這段賦值之前就跑過一次（_show_placeholder 已建好空狀態
        # plate），當時 self.history_store 這個 instance attr 還不存在
        # （_build_recent_block 用 getattr 防炸，直接回 None）。這裡補一次
        # 重建，讓空狀態抓到剛剛才存在的 history_store——不然要等使用者錄
        # 一段又清除才會刷新，冷啟動永遠看不到「最近」。
        if self._skeleton_mode and not self._utterance_blocks:
            self._clear_placeholder()
            self._show_placeholder()
        # 啟動 1 分鐘後跑一次保留策略清理（不阻塞 UI 建構）
        self.after(60_000, self._run_history_retention)

        # Phase 4.3 浮動 mini 錄音窗（lazy 建立；toggle off 時為 None）
        self._mini_window: Optional[MiniRecordingWindow] = None
        if self.cfg.mini_recording_window:
            self._ensure_mini_window()

    # ═══════════════════════════════════════════════════════════════════════
    #  BUILD UI
    # ═══════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        """依 cfg.record_visual 分派版面：新骨架（預設）或 chamber 逃生門。

        v2.28.0 主視窗骨架重寫——逃生門要求「cfg.record_visual == 'chamber'
        時完全退回舊版面……新骨架一行都不執行」，所以在最上層直接分派成
        兩條完全獨立的建置序列，而不是在既有五個 _build_* 方法內部各自加
        if/else（那樣兩條路徑會互相污染，稽核也麻煩）。
        """
        self._skeleton_mode = getattr(self.cfg, "record_visual", "waveform") != "chamber"
        if self._skeleton_mode:
            self._build_ui_skeleton()
        else:
            self._build_ui_legacy()

    def _build_ui_legacy(self) -> None:
        """cfg.record_visual == "chamber" 逃生門：完全原封不動的舊版面。"""
        self._build_topbar()
        self._build_record_card()
        self._build_result_card()
        self._build_action_bar()
        self._build_status_bar()

    def _build_ui_skeleton(self) -> None:
        """v2.28.0 主視窗骨架重寫（預設路徑）：「這個視窗是閱讀器、不是錄音器」。

        內容欄 640pt（視窗拉寬時封頂 720pt，見 SKELETON_CONTENT_MAX）水平置中，
        由上到下四段：頂列 56 / 狀態槽 40 / 轉錄流 724 / 底列 52。
        """
        self.configure(fg_color=BG)
        # customtkinter 的 place() 不接受 width/height（會直接 raise）——
        # 必須在建構子先給定初始寬度，之後 resize 改用 .configure(width=...)。
        self._content = ctk.CTkFrame(
            self, fg_color=BG, corner_radius=0,
            width=SKELETON_CONTENT_COL + 2 * SKELETON_CONTENT_PAD,
        )
        self._content.place(relx=0.5, rely=0, anchor="n", relheight=1.0)
        # pack_propagate 預設 True：容器會依「pack 進去的子元件」自動改自己
        # 的尺寸，蓋掉上面剛設好的 width——鎖 False 讓內容欄寬度永遠只聽
        # width= 這個設定值（初始 704，之後由 _on_skeleton_root_resize 動態調）。
        self._content.pack_propagate(False)
        self.bind("<Configure>", self._on_skeleton_root_resize, add="+")

        self._build_topbar_v2()
        self._build_status_slot()
        self._build_stream_v2()
        self._build_bottombar_v2()

    def _on_skeleton_root_resize(self, event) -> None:
        """視窗拉寬時內容欄跟著長，但封頂 SKELETON_CONTENT_MAX（行長保護）；
        超過的寬度全部變成左右留白。

        這支 bind 只掛在 self 上（見 _build_ui_skeleton），但 customtkinter
        的 CTkFrame.bind() 會把事件實際轉綁到內部的 `!ctkcanvas` 子元件，
        所以 event.widget 恆等於那個內部 canvas、不會等於 self 本尊——不能
        用 `is self` 判斷（永遠 False）。因為這支 handler 只綁在 self 上、
        沒有任何地方把它綁到別的 widget，收到的事件本來就只會是 self 自己
        的尺寸變化（內部 canvas 是 1:1 貼齊 self 的），不需要再過濾來源。
        """
        content_w = min(event.width - 2 * SKELETON_CONTENT_PAD, SKELETON_CONTENT_MAX)
        content_w = max(content_w, 200)   # 安全下限，避免視窗被硬拖到極端小時算出負值
        self._content.configure(width=int(content_w))

    # ═══════════════════════════════════════════════════════════════════════
    #  v2.28.0 主視窗骨架重寫 —— 新版四段式版面（頂列／狀態槽／轉錄流／底列）
    #  以下方法只在 self._skeleton_mode 為 True 時被呼叫（見 _build_ui）。
    #  逃生門（record_visual == "chamber"）走再下面「Top bar」以降的舊方法，
    #  兩條路徑完全獨立、互不呼叫、互不影響。
    # ═══════════════════════════════════════════════════════════════════════

    # ── 共用 helper：頂列狀態點 + 兩行文字 ────────────────────────────────────

    def _status_sub_text(self) -> str:
        """頂列狀態副文：「模型 · 裝置 · 語言」（10.5pt mono，取整為 11）。"""
        model  = self._model_var.get() if hasattr(self, "_model_var") else self.cfg.model
        device = getattr(self.recorder, "_device_name", None) or "系統預設"
        lang   = self._lang_var.get() if hasattr(self, "_lang_var") else self.cfg.language
        return f"{model} · {device} · {lang}"

    def _set_status(self, word: str, color: str) -> None:
        """更新頂列狀態點（fg_color）＋主文（狀態字）＋副文（模型/裝置/語言）。

        只給 skeleton 模式用——chamber 逃生門的 _status_dot/_status_label
        是舊版 CTkLabel，configure 參數不同（text_color 而非 fg_color、
        文字格式也不同），兩條路徑在各自呼叫點自行用
        `if self._skeleton_mode: ... else: ...` 分岔，不共用這支函式。
        """
        self._status_dot.configure(fg_color=color)
        self._status_label.configure(text=word)
        self._status_sub_label.configure(text=self._status_sub_text())

    # ── 共用 helper：模式 pill（自動貼上／潤飾共用同一套樣式）─────────────────

    def _pill_font(self) -> ctk.CTkFont:
        """pill 共用字型（快取一次；_style_pill 的動態寬度量測也用它）。"""
        if getattr(self, "_pill_font_cache", None) is None:
            self._pill_font_cache = ctk.CTkFont(FONT_FAMILY_TEXT, 12)   # 規格 11.5、取整
        return self._pill_font_cache

    def _style_pill(self, btn: ctk.CTkButton, on: bool, label: str) -> None:
        """套用模式 pill 開／關樣式：形狀＋字元雙重編碼（●／○），不靠顏色。

        寬度依文字動態量測（CTkButton 沒有 CSS 那種 auto-fit padding，
        用字型 measure() 算出精確寬度 + 兩側 padding，比硬寫死一個數字
        更貼近規格的「padding 0 11 / padding 0 10 + border 1」語意）。
        """
        font = self._pill_font()
        if on:
            text = f"●{label}"
            pad = 11
            btn.configure(
                text=text, font=font, image=None,
                fg_color=PILL_ON, text_color=PILL_ON_FG, hover_color=PILL_ON,
                border_width=0, corner_radius=13, height=26,
                width=font.measure(text) + pad * 2,
            )
        else:
            text = f"○{label}"
            pad = 10
            btn.configure(
                text=text, font=font, image=None,
                fg_color="transparent", text_color=PILL_OFF_FG, hover_color=ROW_HI,
                border_width=1, border_color=PILL_OFF, corner_radius=13, height=26,
                width=font.measure(text) + pad * 2 + 2,   # +2：border 左右各 1pt
            )

    # ── 頂列 56pt ──────────────────────────────────────────────────────────

    def _nav_icon_btn(self, parent, icon_name: str, command) -> ctk.CTkButton:
        """頂列導覽圖示鈕：28×28、radius 7、hover 底 ROW_HI。"""
        return ctk.CTkButton(
            parent, text="", image=get_icon(icon_name, 14, ICON),
            width=28, height=28, corner_radius=7,
            fg_color="transparent", hover_color=ROW_HI,
            command=command,
        )

    def _build_topbar_v2(self) -> None:
        """頂列 56pt：取代舊 _build_topbar + _build_status_bar（狀態列併進來）。

        左：狀態點 + 兩行文字｜中：flex｜右：兩顆模式 pill + 分隔線 + 三顆
        導覽圖示（歷史／設定／⋯選單）。Canvas 物件 0（全 CTkFrame/Label/Button）。
        """
        bar = ctk.CTkFrame(self._content, height=SKELETON_TOPBAR_H,
                            corner_radius=0, fg_color=CHROME)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=(18, 20))

        # 左：狀態點 + 兩行文字
        left = ctk.CTkFrame(inner, fg_color="transparent")
        left.pack(side="left", fill="y")

        self._status_dot = ctk.CTkFrame(
            left, width=8, height=8, corner_radius=4, fg_color=ACCENT,
        )
        self._status_dot.pack(side="left", padx=(0, SPACE_MD))

        text_wrap = ctk.CTkFrame(left, fg_color="transparent")
        text_wrap.pack(side="left")
        self._status_label = ctk.CTkLabel(
            text_wrap, text="就緒", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13, "bold"),   # 規格 12.5 semibold、取整
            text_color=TEXT_1,
        )
        self._status_label.pack(anchor="w")
        self._status_sub_label = ctk.CTkLabel(
            text_wrap, text=self._status_sub_text(), anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11),
            text_color=TEXT_3,
        )
        self._status_sub_label.pack(anchor="w", pady=(3, 0))

        # 中：flex spacer（最小 24pt，視窗縮到 SKELETON_MIN_W 仍不重疊）
        ctk.CTkFrame(inner, fg_color="transparent", width=24).pack(
            side="left", fill="x", expand=True
        )

        # 右：pill × 2 + 分隔線 + 導覽圖示 × 3——用內部 side="left" 保持
        # 左到右閱讀順序，整包再貼齊右邊界。
        right = ctk.CTkFrame(inner, fg_color="transparent")
        right.pack(side="right")

        pill_wrap = ctk.CTkFrame(right, fg_color="transparent")
        pill_wrap.pack(side="left")
        self._ap_btn = ctk.CTkButton(pill_wrap, command=self._toggle_auto_paste)
        self._ap_btn.pack(side="left")
        self._ollama_btn = ctk.CTkButton(pill_wrap, command=self._on_ollama)
        self._ollama_btn.pack(side="left", padx=(6, 0))
        self._refresh_ap_btn_style()
        self._apply_polish_button_style(enabled=self.cfg.ollama_enabled, healthy=False)

        # 分隔線左右各 gap4——padx 只給在分隔線這一側，nav_wrap 不再疊加，
        # 避免 4+4 變 8（規格明確寫「gap 4 → 分隔線 → gap 4」，不是 4→8）。
        ctk.CTkFrame(right, width=1, height=20, fg_color=LINE).pack(
            side="left", padx=4
        )

        nav_wrap = ctk.CTkFrame(right, fg_color="transparent")
        nav_wrap.pack(side="left")
        self._nav_icon_btn(nav_wrap, "history", self._open_history).pack(
            side="left", padx=(0, 2)
        )
        self._nav_icon_btn(nav_wrap, "settings", self._open_settings).pack(
            side="left", padx=(0, 2)
        )
        self._menu_btn = self._nav_icon_btn(
            nav_wrap, "more-horizontal", self._open_overflow_menu
        )
        self._menu_btn.pack(side="left")

        # ── 相容殼 ──────────────────────────────────────────────────────────
        # 以下幾顆 widget 在新骨架沒有對應的可見元件，但深處狀態機有幾十個
        # 既有呼叫點會 .configure() 它們（計時器／貼上目標／熱鍵提示已是
        # Mini HUD 的主場；模型/語言下拉選單併入設定視窗，見 SettingsWindow）。
        # 刻意不逐一改那些呼叫點——那些邏輯不屬於這次「版面層」的範圍——
        # 改成建立不 pack 的殼，讓呼叫落空但不炸。真正驅動轉錄的
        # _model_var / _lang_var 仍是「活」的 StringVar（.get() 被 pipeline
        # 讀取來決定實際轉錄用的模型/語言），不是殼。
        self._model_var  = ctk.StringVar(value=self.cfg.model)
        self._lang_var   = ctk.StringVar(value=self.cfg.language)
        self._model_menu = ctk.CTkOptionMenu(self, values=list(MODEL_INFO.keys()), variable=self._model_var)
        self._lang_menu  = ctk.CTkOptionMenu(self, values=list(LANGUAGE_OPTIONS.keys()), variable=self._lang_var)
        self._hotkey_status = ctk.CTkLabel(self, text="")

    def _open_overflow_menu(self) -> None:
        """⋯ 溢出選單：清除本次結果 / 匯出本次為 .txt / 重新載入模型 / 關於。"""
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="清除本次結果", command=self._on_clear)
        menu.add_command(label="匯出本次為 .txt", command=self._on_save)
        menu.add_command(label="重新載入模型", command=self._reload_model_from_menu)
        menu.add_separator()
        menu.add_command(label="關於", command=self._open_settings)
        try:
            x = self._menu_btn.winfo_rootx()
            y = self._menu_btn.winfo_rooty() + self._menu_btn.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _reload_model_from_menu(self) -> None:
        """⋯ 選單「重新載入模型」：重跑一次目前選定模型的 warmup。"""
        log_action("overflow_reload_model_clicked")
        self._show_toast("正在重新載入模型…")
        self._warmup_model()

    # ── 狀態槽 40pt ────────────────────────────────────────────────────────

    def _build_status_slot(self) -> None:
        """狀態槽 40pt：同一個槽輪流顯示 5 種 payload（常態 LevelMeter／模型
        暖機 Progress／banner）。取代舊 _build_record_card 的錄音卡片——這裡
        不再可點擊，錄音已有主場（R⌘ + 迷你條），這個視窗是閱讀器。
        """
        slot = ctk.CTkFrame(self._content, height=SKELETON_STATUS_SLOT_H,
                             corner_radius=0, fg_color=CHROME)
        slot.pack(fill="x")
        slot.pack_propagate(False)
        ctk.CTkFrame(self._content, height=1, fg_color=LINE, corner_radius=0).pack(fill="x")
        self._status_slot = slot
        self._status_slot_active = "meter"

        # Payload 1：LevelMeter（常態，繪製邏輯見 _draw_chamber_bars）
        self._chamber_w = LEVEL_METER_W
        self._chamber_h = LEVEL_METER_H
        self._chamber = tk.Canvas(
            slot, width=LEVEL_METER_W, height=LEVEL_METER_H,
            bg=CHROME, highlightthickness=0, bd=0,
        )
        # 不綁 <Enter>/<Leave>/<Motion>/<ButtonPress-1>/<ButtonRelease-1>——
        # 這個 canvas 不再可點擊，錄音已有主場（R⌘ + 迷你條）。
        self._chamber.pack(pady=4)

        # Payload 2：Progress（模型暖機，見 _warmup_model / _warmup_progress_tick）
        self._warmup_progress = ctk.CTkProgressBar(
            slot, width=LEVEL_METER_W, height=4, corner_radius=2,
            mode="determinate", progress_color=WARN, fg_color=SURF_3,
        )
        self._warmup_progress.set(0.0)
        self._warmup_progress_anim_id: Optional[str] = None

        # Payload 3：Banner（麥克風無法啟動／轉錄失敗共用一種外觀，內容不同）
        self._banner = ctk.CTkFrame(slot, fg_color="transparent")
        ctk.CTkFrame(
            self._banner, width=6, height=6, corner_radius=3, fg_color=RED_TEXT,
        ).pack(side="left", padx=(20, 10))
        self._banner_label = ctk.CTkLabel(
            self._banner, text="", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12, "bold"), text_color=RED_TEXT,
        )
        self._banner_label.pack(side="left", fill="x", expand=True)
        self._banner_btn = ctk.CTkButton(
            self._banner, text="", height=24, corner_radius=6,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
            fg_color=RED_LINE, text_color=RED_TEXT,
            hover_color=blend(RED_TEXT, RED_LINE, 0.25),
        )
        # _banner_btn 刻意不在這裡 pack——沒有 action 的 banner 不該顯示空鈕，
        # 由 _status_slot_show_banner() 依有沒有給 action 決定要不要 pack。

        # ── 相容殼：計時器／貼上目標／熱鍵提示已是 Mini HUD 的主場，新骨架
        # 不再顯示；_transition_to_recording 等處仍有既有呼叫點，說明同
        # _build_topbar_v2。
        self._timer_label  = ctk.CTkLabel(self, text="")
        self._target_label = ctk.CTkLabel(self, text="")
        self._hotkey_hint  = ctk.CTkLabel(self, text="")

        # 三態 LevelMeter 的引擎狀態（同舊版 _build_record_card 的等價設定，
        # 只是不再綁點擊事件、canvas 尺寸換成 LEVEL_METER_W/H）。
        self._state_start_time = time.perf_counter()
        self._ripples: list[Ripple] = []
        self._prev_rms = 0.0
        self._reduce_motion = resolve_reduce_motion(
            getattr(self.cfg, "reduce_motion_pref", "auto")
        )
        self._wave_engine: Optional[WaveformEngine] = WaveformEngine(n_bars=WAVE_N_BARS_MAIN)
        self._wave_last_tick = time.perf_counter()
        self._frozen_bars: Optional[list] = None
        self._proc_sweep_use_real = False
        self._proc_sweep_known_frac = 0.0

        # 啟動渲染迴圈（idle／recording／processing 三態都靠這個 tick 驅動）
        self._render_tick()

    def _status_slot_set(self, mode: str) -> None:
        """StatusSlot payload 切換：meter／progress／banner 三選一互斥顯示。"""
        if getattr(self, "_status_slot_active", None) == mode:
            return
        self._chamber.pack_forget()
        self._warmup_progress.pack_forget()
        self._banner.pack_forget()
        if mode == "banner":
            self._status_slot.configure(fg_color=RED_BG)
            self._banner.pack(fill="both", expand=True)
        else:
            self._status_slot.configure(fg_color=CHROME)
            if mode == "progress":
                self._warmup_progress.pack(pady=18)
            else:
                self._chamber.pack(pady=4)
        self._status_slot_active = mode

    def _status_slot_show_meter(self) -> None:
        """StatusSlot 換回常態 LevelMeter。"""
        self._status_slot_set("meter")

    def _status_slot_show_progress(self) -> None:
        """StatusSlot 換成模型暖機 Progress。"""
        self._status_slot_set("progress")
        if self._warmup_progress_anim_id is None:
            self._warmup_progress_tick()

    def _warmup_progress_tick(self) -> None:
        """暖機 Progress bar 動畫：transcriber.warmup() 不回報中間進度，沒有
        真實完成百分比可用。比照 _processing_sweep_progress 的既有哲學——
        寧可誠實地跑一個等速循環動畫表示「還在跑」，也不要編一個假的完成
        百分比。只在 StatusSlot 目前仍顯示 progress payload 時繼續排程。
        """
        if getattr(self, "_status_slot_active", None) != "progress":
            self._warmup_progress_anim_id = None
            return
        cycle_s = 1.6
        frac = (time.perf_counter() % cycle_s) / cycle_s
        try:
            self._warmup_progress.set(frac)
        except tk.TclError:
            self._warmup_progress_anim_id = None
            return
        self._warmup_progress_anim_id = self.after(50, self._warmup_progress_tick)

    def _status_slot_show_banner(
        self, message: str,
        action_label: Optional[str] = None, action_cb=None,
    ) -> None:
        """StatusSlot 換成 Banner，顯示 message；給了 action_label + action_cb
        才顯示右側動作鈕（麥克風無法啟動→設定；轉錄失敗→開啟日誌）。
        """
        self._banner_label.configure(text=message)
        if action_label and action_cb is not None:
            # padding 0 11：跟 pill 同一套「字型 measure() + 兩側 padding」
            # 動態量寬，不用 CTkButton 預設的 140px 死板寬度。
            btn_font = self._banner_btn.cget("font")
            self._banner_btn.configure(
                text=action_label, command=action_cb,
                width=btn_font.measure(action_label) + 11 * 2,
            )
            self._banner_btn.pack(side="right", padx=(10, 20))
        else:
            self._banner_btn.pack_forget()
        self._status_slot_set("banner")

    # ── 轉錄流容器 724pt ───────────────────────────────────────────────────

    def _build_stream_v2(self) -> None:
        """轉錄流容器 724pt：滿版 CTkScrollableFrame，內含 UtteranceBlockV2
        卡片、空狀態／「最近」區。取代整個舊 ResultCard（含 header／divider）。
        捲動起始位置＝頂端（最新段在最上，沿用 v2.8.0 既有決定）。
        """
        self._blocks_container = ctk.CTkScrollableFrame(
            self._content,
            corner_radius=0,
            fg_color=BG,
            scrollbar_fg_color=BG,
            scrollbar_button_color=SCROLL,
            scrollbar_button_hover_color=SCROLL,
        )
        # padding 16 32 16 32：CTkScrollableFrame 沒有分邊 padding 參數，
        # 用 pack 的 padx/pady 模擬（左右 32=SPACE_2XL、上下 16=SPACE_LG）。
        self._blocks_container.pack(fill="both", expand=True, padx=SPACE_2XL, pady=SPACE_LG)
        # 捲動條寬 6 radius 3——公開建構參數只能改顏色，寬度/圓角要動內部
        # _scrollbar（CTkScrollbar 實例）才行；guard 起來避免未來 customtkinter
        # 版本改內部結構時整段炸掉（沿用本檔既有的 _parent_canvas 存取慣例）。
        try:
            self._blocks_container._scrollbar.configure(width=6, corner_radius=3)
        except Exception:
            pass
        # v2.28.0：內容裝得下時自動收起捲動條（見 _autohide_scrollbar docstring）
        _autohide_scrollbar(self._blocks_container)
        self._utterance_blocks: list[UtteranceBlock] = []

        # 內容層新增（skeleton 模式限定；chamber 逃生門的 _display_result
        # 走 legacy 分支，完全不會碰這三個）：
        #   _pending_block   — 轉錄中骨架卡（_transition_to_processing 建立、
        #                       _display_result_skeleton／失敗處理收尾）
        #   _time_separators — 已插入的時間分隔線，_on_clear 要一併清掉
        #   _recent_below_frame — 有內容時（≤520pt）貼在所有 block 下方的
        #                       「最近」區容器；先建好、預設不 pack
        self._pending_block: Optional["UtteranceBlockV2"] = None
        self._time_separators: list[ctk.CTkFrame] = []
        self._recent_below_frame = ctk.CTkFrame(self._blocks_container, fg_color="transparent")

        self._wraplength_debounce_id: Optional[str] = None
        self._blocks_container.bind("<Configure>", self._on_blocks_container_resize, add="+")
        self._placeholder_label: Optional[ctk.CTkBaseClass] = None
        self._show_placeholder()

        # ── 大波形（錄音態專屬，v2.28.0）───────────────────────────────────
        # 疊在轉錄流之上，而不是塞進它內部：後者是會捲動的
        # CTkScrollableFrame，塞進去的子元件會跟著使用者捲動位移，疊加的
        # 波形也會跟著跑掉。改成用 place() 蓋在它**外層** frame
        # （_blocks_container._parent_frame）上、relwidth/relheight 都是 1，
        # 整塊蓋滿——不受內部捲動影響，也不必去動版面。詳細理由（含試過
        # 「把轉錄流 pack_forget 讓位」踩到的三個 Tk 坑）見 _show_big_wave。
        #
        # 引擎共用（見 _draw_chamber_bars 的呼叫點）：這裡不自己 new 一份
        # WaveformEngine、也不自己呼叫 update()。大波形跟上方 640×32 的
        # 小電平條讀同一份 self._wave_engine 狀態——兩邊各自 update() 的話，
        # 包絡的 attack/release 是對時間積分的，幾幀之後兩邊的高度會慢慢
        # 對不上，看起來像兩個不同步的波形，而不是同一顆儀表的兩種呈現。
        self._big_wave_canvas = tk.Canvas(
            self._content, height=BIG_WAVE_H, bg=BG, highlightthickness=0, bd=0,
        )
        self._big_wave_renderer: Optional[GlowWaveRenderer] = None
        # PhotoImage 沒有其他 Python 參考就會被 GC 回收、畫面變空白——這是
        # tkinter 的經典坑，必須存成 instance attribute 保留參考，不能只是
        # 局部變數丟給 create_image/itemconfig 就不管（見 _update_big_wave）。
        self._big_wave_photo = None
        self._big_wave_image_id: Optional[int] = None
        # _draw_chamber_bars 靠這個旗標決定要不要多畫這一份，不隱藏時
        # 停止渲染就會在背景空轉（見需求：不另開一支 tick）。
        self._big_wave_active = False

        # ── 相容殼：舊 ResultCard header（標題 + 原文/潤飾 toggle chip）在
        # 新骨架被砍掉（本階段只做容器）；_rebuild_result_title /
        # _apply_toggle_style 等既有邏輯仍會 .configure() 它們——那些是
        # UtteranceBlock 層級的邏輯（下一階段做卡片時才會重新設計進卡片
        # 本身），這裡不動，只補殼避免呼叫落空時炸掉。
        self._result_title     = ctk.CTkLabel(self, text="")
        self._seg_raw_btn      = ctk.CTkButton(self, text="")
        self._seg_polished_btn = ctk.CTkButton(self, text="")

    def _refresh_stream_stats(self) -> None:
        """底列左側統計文字「本次 N 段 · M 字」+ 右側複製全部主鈕的
        enable/disable 狀態，兩者共用同一份「目前有沒有結果」判斷。
        """
        if not self._skeleton_mode:
            return
        n = len(self._utterance_blocks)
        chars = sum(len(b.get_current_text()) for b in self._utterance_blocks)
        self._stream_stats_label.configure(
            text=f"本次 {n} 段 · {chars:,} 字",
            text_color=TEXT_3 if n == 0 else TEXT_2,
        )
        if n == 0:
            self._copy_all_btn.configure(state="disabled", fg_color=BTN_DIS, text_color=BTN_DIS_FG)
        else:
            self._copy_all_btn.configure(state="normal", fg_color=BTN_BG, text_color=BTN_FG)
        # 「最近」區顯示規則跟著 block 增減一起重算（單一 choke point，
        # 所有會改動 _utterance_blocks 的地方都已經呼叫這支）。
        self._refresh_recent_section()

    def _on_copy_all(self) -> None:
        """底列主鈕「複製全部」：依畫面顯示順序（最新在上）串接所有段落
        目前顯示文字（尊重各自的原文／潤飾狀態），整份複製到剪貼簿。

        跟舊版「複製」（_on_copy，只複製最新一段）是不同動作——新骨架的
        主鈕文字明講「全部」，語意就該是全部，不能掛羊頭賣狗肉沿用舊
        handler；但不動 UtteranceBlock，只用它既有的 get_current_text()
        公開方法。
        """
        if not self._utterance_blocks:
            log_action("copy_all_clicked_empty")
            return
        parts = [b.get_current_text().strip() for b in self._utterance_blocks]
        text = "\n\n".join(p for p in parts if p)
        if not text:
            log_action("copy_all_clicked_empty")
            return
        try:
            import pyperclip
            pyperclip.copy(text)
            log_action("copy_all_succeeded", text_len=len(text), blocks=len(parts))
            self._show_toast(f"已複製全部 {len(self._utterance_blocks)} 段")
        except Exception as e:
            log_error("copy_all_failed", text_len=len(text))
            self._show_toast(f"複製失敗: {e}")

    # ── 底列 52pt ──────────────────────────────────────────────────────────

    def _build_bottombar_v2(self) -> None:
        """底列 52pt：取代舊 _build_action_bar 的六顆按鈕。左：統計文字；
        右：主鈕「複製全部」。原六顆按鈕去向——自動貼上/潤飾→頂列 pill；
        複製/存檔→移到卡片（下一階段）；歷史/設定→頂列圖示；清除→⋯選單。
        """
        ctk.CTkFrame(self._content, height=1, fg_color=LINE, corner_radius=0).pack(fill="x")
        bar = ctk.CTkFrame(self._content, height=SKELETON_BOTTOMBAR_H,
                            corner_radius=0, fg_color=CHROME)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=20)

        self._stream_stats_label = ctk.CTkLabel(
            inner, text="", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 12),   # 規格 11.5、取整
            text_color=TEXT_3,
        )
        self._stream_stats_label.pack(side="left", fill="y")

        btn_font = ctk.CTkFont(FONT_FAMILY_TEXT, 13, "bold")   # 規格 12.5 semibold、取整
        btn_text = "複製全部"
        self._copy_all_btn = ctk.CTkButton(
            inner, text=btn_text,
            image=get_icon("copy", 14, BTN_FG),
            compound="left",
            height=32, corner_radius=8,
            font=btn_font,
            width=btn_font.measure(btn_text) + 14 + 7 + 16 * 2,   # icon+gap+左右padding 16
            fg_color=BTN_BG, text_color=BTN_FG, hover_color=BTN_BG,
            command=self._on_copy_all,
        )
        self._copy_all_btn.pack(side="right")

        self._refresh_stream_stats()

    # ── Top bar ──────────────────────────────────────────────────────────────

    def _build_topbar(self) -> None:
        """建立頂部工具列：左側品牌 logo、右側語言與模型選單。"""
        bar = ctk.CTkFrame(self, height=60, corner_radius=0, fg_color=SURF_1)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # Thin bottom separator line
        ctk.CTkFrame(self, height=1, fg_color=SURF_3, corner_radius=0).pack(fill="x")

        # Left: title
        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side="left", padx=SPACE_XL, fill="y")

        ctk.CTkLabel(
            left,
            text="🎙",
            font=ctk.CTkFont(size=20),
        ).pack(side="left", padx=(0, 8))

        ctk.CTkLabel(
            left,
            text="Whisper Pro",
            font=ctk.CTkFont("SF Pro Display", 17, "bold"),
            text_color=TEXT_1,
        ).pack(side="left")

        # Right: language + model selectors
        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.pack(side="right", padx=20, fill="y")

        for label_txt, var_attr, menu_attr, values, init_val, cb in [
            ("語言", "_lang_var", "_lang_menu",
             list(LANGUAGE_OPTIONS.keys()), cfg_val(self.cfg.language), self._on_language_change),
            ("模型", "_model_var", "_model_menu",
             list(MODEL_INFO.keys()), cfg_val(self.cfg.model), self._on_model_change),
        ]:
            grp = ctk.CTkFrame(right, fg_color="transparent")
            grp.pack(side="left", padx=(0, 16), pady=18)

            ctk.CTkLabel(
                grp, text=label_txt,
                font=ctk.CTkFont("SF Pro Text", 12),
                text_color=TEXT_3,
            ).pack(side="left", padx=(0, 4))

            var = ctk.StringVar(value=init_val)
            setattr(self, var_attr, var)

            menu = ctk.CTkOptionMenu(
                grp,
                values=values,
                variable=var,
                width=108 if var_attr == "_lang_var" else 148,
                height=30,
                corner_radius=8,
                fg_color=SURF_2,
                button_color=SURF_2,
                button_hover_color=SURF_3,
                dropdown_fg_color=SURF_1,
                text_color=TEXT_1,
                font=ctk.CTkFont("SF Pro Text", 13),
                command=cb,
            )
            menu.pack(side="left")
            setattr(self, menu_attr, menu)

    # ── Record card ──────────────────────────────────────────────────────────

    def _build_record_card(self) -> None:
        """建立錄音卡片：Ambient Chamber 畫布、計時器、自動貼上目標標籤、快捷鍵提示。"""
        card = ctk.CTkFrame(
            self, corner_radius=16,
            fg_color=SURF_1,
        )
        card.pack(fill="x", padx=SPACE_LG, pady=(SPACE_MD + 2, SPACE_SM - 2))

        # v2.27.0 三態統一：canvas 尺寸依 record_visual 決定——bar 模式（預設）
        # 用 712×160 寬條；chamber 逃生門維持舊 280×280 正方形（舊三態光場的
        # 同心圓／粒子環比例只在正方形下才對）。self._wave_visual 是這支
        # AppWindow 唯一的模式旗標，下面 engine 建立、_draw_chamber 分派、
        # _in_disc 熱區計算都共用它，避免同一個 getattr 判斷散落多處。
        self._wave_visual = getattr(self.cfg, "record_visual", "waveform") != "chamber"
        self._chamber_w = WAVE_CHAMBER_W if self._wave_visual else CHAMBER_SIZE
        self._chamber_h = WAVE_CHAMBER_H if self._wave_visual else CHAMBER_SIZE

        # Ambient chamber — single canvas replacing waveform + outer ring + button
        self._chamber = tk.Canvas(
            card,
            width=self._chamber_w, height=self._chamber_h,
            bg=SURF_1, highlightthickness=0, bd=0,
        )
        self._chamber.pack(pady=(SPACE_MD, SPACE_XS))

        # Canvas 事件綁定
        # 注意：tkinter 的 <Button-1> 與 <ButtonPress-1> 是同一件事，
        # 同時綁定時後者會覆蓋前者。一律使用 press/release 明確區分。
        self._chamber.bind("<Enter>",           self._on_chamber_enter)
        self._chamber.bind("<Leave>",           self._on_chamber_leave)
        self._chamber.bind("<Motion>",          self._on_chamber_motion)
        self._chamber.bind("<ButtonPress-1>",   self._on_chamber_press)
        self._chamber.bind("<ButtonRelease-1>", self._on_chamber_release)

        # 錄音計時器：只在錄音中顯示；SF Mono 等寬字型防止數字跳動
        self._timer_label = ctk.CTkLabel(
            card, text="",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 18, "bold"),
            text_color=TEXT_2,
        )
        self._timer_label.pack(pady=(0, SPACE_XS))

        # Auto-paste target label (reused)
        self._target_label = ctk.CTkLabel(
            card, text="",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
            text_color=INDIGO,
        )
        self._target_label.pack()

        # 快捷鍵提示文字：依目前狀態動態切換（「按下…」/「再按…停止」/「轉錄中…」）
        self._hotkey_hint = ctk.CTkLabel(
            card,
            text=f"按下 {self.cfg.format_hotkey_display()} 即時錄音",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
            text_color=TEXT_3,
        )
        self._hotkey_hint.pack(pady=(0, SPACE_MD + 2))

        # 預先渲染三種狀態的圖示（tk.Canvas 需要 PhotoImage，不接受 CTkImage）
        self._icon_mic_idle = get_canvas_icon("mic",    36, TEXT_2)
        self._icon_square   = get_canvas_icon("square", 28, TEXT_1)
        self._icon_mic_proc = get_canvas_icon("mic",    36, blend(WARN, SURF_2, 0.6))

        # Chamber 動畫狀態變數
        self._state_start_time = time.perf_counter()
        self._ripples: list[Ripple] = []
        self._prev_rms         = 0.0
        self._pressed          = False
        self._hovering         = False
        # A3（v2.7.0）：reduce_motion 解析改成 config-aware（"auto" / "always" /
        # "never"）。SettingsWindow 改 pref 後呼叫 _refresh_reduce_motion() 立即
        # 套用、不需重啟。getattr fallback 為了向前相容沒升級的舊 config。
        self._reduce_motion    = resolve_reduce_motion(
            getattr(self.cfg, "reduce_motion_pref", "auto")
        )

        # v2.25.0 Aperture 波形視覺 / v2.27.0 三態統一：cfg.record_visual ==
        # "chamber" 時完全不建立（逃生門，新程式碼路徑一行都不執行）。
        # engine 現在橫跨 idle／recording／processing 三態共用、永遠不會被
        # reset 以外的方式重建（「沒有任何元件被建立或銷毀」）。
        self._wave_engine: Optional[WaveformEngine] = None
        self._wave_last_tick = time.perf_counter()
        # processing 態的凍結快照（_transition_to_processing 存最後一幀
        # engine.bars）；idle／recording 不讀這個欄位。
        self._frozen_bars: Optional[list] = None
        # processing 態掠掃光帶是否餵真實 LA 進度（見 _processing_sweep_progress）
        self._proc_sweep_use_real = False
        self._proc_sweep_known_frac = 0.0
        if self._wave_visual:
            self._wave_engine = WaveformEngine(n_bars=WAVE_N_BARS_MAIN)

        # 啟動渲染迴圈（在視窗存活期間持續執行，每 50ms 更新一次 Canvas）
        self._render_tick()

    # ── Result card ──────────────────────────────────────────────────────────

    def _build_result_card(self) -> None:
        """建立轉錄結果卡片：標題列（含原文/潤飾切換）、文字區、清除按鈕。"""
        card = ctk.CTkFrame(
            self, corner_radius=16,
            fg_color=SURF_1,
        )
        card.pack(fill="both", expand=True, padx=SPACE_LG, pady=(0, 6))

        # Header
        hdr = ctk.CTkFrame(card, fg_color="transparent", height=46)
        hdr.pack(fill="x", padx=20, pady=(14, 0))
        hdr.pack_propagate(False)

        # Title with leading icon
        title_wrap = ctk.CTkFrame(hdr, fg_color="transparent")
        title_wrap.pack(side="left")

        ctk.CTkLabel(
            title_wrap, text="",
            image=get_icon("file-text", 16, TEXT_1),
            width=18,
        ).pack(side="left", padx=(0, 8))

        self._result_title = ctk.CTkLabel(
            title_wrap, text="轉錄結果",
            font=ctk.CTkFont("SF Pro Display", 14, "bold"),
            text_color=TEXT_1, anchor="w",
        )
        self._result_title.pack(side="left")

        ctk.CTkButton(
            hdr, text="清除", width=64, height=26,
            image=get_icon("x", 12, TEXT_3),
            compound="left",
            fg_color="transparent",
            border_width=1, border_color=SURF_3,
            text_color=TEXT_3,
            hover_color=SURF_2,
            font=ctk.CTkFont("SF Pro Text", 12),
            corner_radius=6,
            command=self._on_clear,
        ).pack(side="right")

        # 原文 / 潤飾 切換 chip（分段控制）
        # 只在「最新一段」成功產出潤飾版時可用；無潤飾時兩顆都變灰。
        # 狀態由 self._showing_polished 與 self._last_polished 決定。
        toggle_wrap = ctk.CTkFrame(hdr, fg_color="transparent")
        toggle_wrap.pack(side="right", padx=(0, SPACE_SM))

        _seg_common = dict(
            width=52, height=26, corner_radius=6,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
            border_width=1,
        )
        self._seg_raw_btn = ctk.CTkButton(
            toggle_wrap, text="原文",
            command=lambda: self._set_showing_polished(False),
            **_seg_common,
        )
        self._seg_raw_btn.pack(side="left", padx=(0, 2))
        self._seg_polished_btn = ctk.CTkButton(
            toggle_wrap, text="潤飾",
            command=lambda: self._set_showing_polished(True),
            **_seg_common,
        )
        self._seg_polished_btn.pack(side="left")
        # 初始樣式：沒有任何結果也沒有潤飾版，兩顆都灰
        self._apply_toggle_style()

        # Divider
        ctk.CTkFrame(card, height=1, fg_color=SURF_3, corner_radius=0).pack(
            fill="x", padx=20, pady=(6, 0)
        )

        # Fix 19 Path B / 2026-05-23 — Speakly-style 獨立區塊容器。
        # 取代原本的單一 CTkTextbox（多段累積 + 分隔線），改成 ScrollableFrame
        # 內含一串 UtteranceBlock。每段獨立顯示時間戳、文字、per-block 動作。
        self._blocks_container = ctk.CTkScrollableFrame(
            card,
            corner_radius=0,
            fg_color="transparent",
            scrollbar_button_color=SURF_3,
            scrollbar_button_hover_color=SURF_4,
        )
        self._blocks_container.pack(fill="both", expand=True, padx=SPACE_SM, pady=(4, 10))
        # 區塊列表（v2.8.0 Bug 3 反轉後：最新在 [0]、最舊在 [-1]）
        self._utterance_blocks: list[UtteranceBlock] = []

        # D3-S7（v2.10.0）：動態 wraplength。視窗 resize 時更新所有 block 的
        # wraplength = container.width - container padding - block border 預留。
        # debounce 80ms 避免 resize 拖曳時 fire 過頻。
        # v2.13.2 Bug E：**必須加 add="+"**！否則會覆蓋 CTkScrollableFrame 內部
        # bind("<Configure>", scrollregion 更新) 的 handler（其原始碼 line 75）→
        # scrollregion 永遠停在初始值、scrollbar 拖不動、看不到新加的 block。
        self._wraplength_debounce_id: Optional[str] = None
        self._blocks_container.bind("<Configure>", self._on_blocks_container_resize, add="+")
        # 佔位文字（沒有任何 block 時顯示）
        self._placeholder_label: Optional[ctk.CTkLabel] = None
        self._show_placeholder()

    # ── Action bar ───────────────────────────────────────────────────────────

    def _build_action_bar(self) -> None:
        """建立動作列：複製、存檔、自動貼上、AI 潤飾、設定五顆按鈕。"""
        # Top separator
        ctk.CTkFrame(self, height=1, fg_color=SURF_3, corner_radius=0).pack(fill="x")

        bar = ctk.CTkFrame(self, height=58, corner_radius=0, fg_color="transparent")
        bar.pack(fill="x")
        bar.pack_propagate(False)

        row = ctk.CTkFrame(bar, fg_color="transparent")
        row.pack(expand=True, pady=13)

        # 幽靈按鈕共用樣式（透明底色 + 細邊框，低調不搶眼）
        ghost = dict(
            height=32, corner_radius=8,
            font=ctk.CTkFont("SF Pro Text", 13),
            fg_color=SURF_1,
            border_width=1, border_color=SURF_3,
            text_color=TEXT_2,
            hover_color=SURF_2,
        )

        # 幽靈按鈕圖示尺寸（15px 在 13pt 字型旁的視覺平衡最佳）
        icon_size = 15

        ctk.CTkButton(
            row, text="複製", width=96,
            image=get_icon("copy", icon_size, TEXT_2),
            compound="left",
            command=self._on_copy, **ghost,
        ).pack(side="left", padx=SPACE_XS)

        ctk.CTkButton(
            row, text="存檔", width=96,
            image=get_icon("download", icon_size, TEXT_2),
            compound="left",
            command=self._on_save, **ghost,
        ).pack(side="left", padx=SPACE_XS)

        # Auto-paste chip — icon colour tracks active state
        ap_on = self.cfg.auto_paste
        self._ap_btn = ctk.CTkButton(
            row,
            text="自動貼上",
            image=get_icon("keyboard", icon_size,
                           TEXT_1 if ap_on else TEXT_3),
            compound="left",
            width=116, height=32, corner_radius=8,
            font=ctk.CTkFont("SF Pro Text", 13),
            fg_color=INDIGO if ap_on else SURF_1,
            border_width=1,
            border_color=INDIGO if ap_on else SURF_3,
            text_color=TEXT_1 if ap_on else TEXT_3,
            hover_color=INDIGO_HV if ap_on else SURF_2,
            command=self._toggle_auto_paste,
        )
        self._ap_btn.pack(side="left", padx=SPACE_XS)

        # AI 潤飾按鈕初始外觀依 cfg 設定，不在此時做網路探測（會阻塞 UI 建構）。
        # 實際 Ollama 連線狀態由 _refresh_ollama_health() 在啟動 2 秒後非同步更新。
        self._ollama_btn = ctk.CTkButton(
            row, text="潤飾", width=96,
            image=get_icon("sparkles", icon_size, TEXT_3),
            compound="left",
            state="disabled",
            fg_color=SURF_1,
            hover_color=SURF_2,
            border_width=1,
            border_color=SURF_3,
            text_color=TEXT_3,
            height=32, corner_radius=8,
            font=ctk.CTkFont("SF Pro Text", 13),
            command=self._on_ollama,
        )
        self._ollama_btn.pack(side="left", padx=SPACE_XS)
        # 依目前 cfg 立即套一次外觀（啟動時僅是視覺狀態）
        self._apply_polish_button_style(enabled=self.cfg.ollama_enabled, healthy=False)

        ctk.CTkButton(
            row, text="歷史", width=96,
            image=get_icon("history", icon_size, TEXT_2),
            compound="left",
            command=self._open_history, **ghost,
        ).pack(side="left", padx=SPACE_XS)

        ctk.CTkButton(
            row, text="設定", width=96,
            image=get_icon("settings", icon_size, TEXT_2),
            compound="left",
            command=self._open_settings, **ghost,
        ).pack(side="left", padx=SPACE_XS)

    # ── Status bar ───────────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        """建立底部狀態列：狀態點、狀態文字、右側快捷鍵顯示。"""
        ctk.CTkFrame(self, height=1, fg_color=SURF_3, corner_radius=0).pack(fill="x")
        bar = ctk.CTkFrame(self, height=32, corner_radius=0, fg_color=SURF_1)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=6)

        self._status_dot = ctk.CTkLabel(
            inner, text="●", text_color=SUCCESS,
            font=ctk.CTkFont(size=8),
        )
        self._status_dot.pack(side="left")

        self._status_label = ctk.CTkLabel(
            inner, text="  模型載入中…",
            font=ctk.CTkFont("SF Pro Text", 12),
            text_color=TEXT_3,
        )
        self._status_label.pack(side="left")

        # 計時器已移至 Chamber 下方（見 _build_record_card）；
        # 狀態列不再重複顯示 mm:ss，避免資訊重複。

        self._hotkey_status = ctk.CTkLabel(
            inner, text=self.cfg.format_hotkey_display(),
            font=ctk.CTkFont("SF Pro Text", 12),
            text_color=TEXT_3,
        )
        self._hotkey_status.pack(side="right", padx=SPACE_LG)

    # ═══════════════════════════════════════════════════════════════════════
    #  STATE MACHINE
    # ═══════════════════════════════════════════════════════════════════════

    def _transition_to_recording(self) -> None:
        """狀態機：idle → recording。啟動麥克風錄音並更新全部 UI。

        Fix Cluster C / 2026-05-23：先 start recorder、失敗就不進 recording state。
        之前 `self.recorder.start()` 在 UI / state 切換**之後**呼叫且不檢查回傳值
        → 麥克風被別 App 占用 / 拔線 / 權限拒絕時 UI 卡 recording、按停止得到
        「沒有偵測到音訊」、使用者以為麥克風壞了。改成 start-first guard + toast。
        """
        if self._state != "idle":
            log.debug(f"_transition_to_recording ignored (state={self._state})")
            return

        # Cluster C：先 start recorder（會 lazy-init device）、失敗就回退 idle
        if not self.recorder.start():
            log.warning("RECORD: start() failed; staying idle")
            log_action("recording_start_failed_no_device")
            try:
                self._show_toast("⚠ 麥克風無法啟動（請確認權限或裝置）")
            except Exception:
                pass
            # v2.28.0 骨架重寫：把這個錯誤同時升級成 StatusSlot 的持久 Banner
            # （toast 幾秒就消失，使用者切開別的視窗回來就看不到了）。現有
            # recorder.start() 回傳值分不出「權限被拒」vs「裝置不見了」兩種
            # 原因，訊息故意寫成兩者皆可的通用版本，不假裝能分辨。
            if self._skeleton_mode:
                try:
                    self._status_slot_show_banner(
                        "麥克風無法啟動，請確認權限或裝置",
                        action_label="設定", action_cb=self._open_settings,
                    )
                except Exception:
                    pass
            return

        # v2.21.4：若 recorder 因指定裝置（AirPods）未就緒而退回系統預設、提示使用者
        #   為何不是用 AirPods 收音（避免困惑）。
        if getattr(self.recorder, "_started_with_fallback", False):
            try:
                self._show_toast("指定麥克風未就緒，已改用系統預設裝置錄音")
            except Exception:
                pass

        # v2.19.0：錄音真正開始 → 生成新 pipeline_id，貫穿整條 pipeline。
        # （錄音啟動失敗時就不生 pid、避免 dead pid 污染 log）
        _pipe_new_id()
        _pipe_event("hotkey_pressed", hotkey=str(self.cfg.hotkey))

        log_state(
            "idle->recording",
            model=self._model_var.get(),
            language=self._lang_var.get(),
            auto_paste=self.cfg.auto_paste,
        )
        self._state            = "recording"
        self._state_start_time = time.perf_counter()
        self._rec_start        = self._state_start_time
        self._stream_samples   = 0
        self._stream_chunks    = []
        self._ripples.clear()
        self._prev_rms         = 0.0
        # v2.25.0 Aperture 波形：新錄音開始，engine 歸零 + tick 基準重設
        if self._wave_engine is not None:
            self._wave_engine.reset()
            self._wave_last_tick = self._state_start_time
        # v2.24.0 看門狗：錄音起點視為「最後聽到語音」的起算點
        self._last_voice_at    = time.perf_counter()
        self._watchdog_warned  = False

        # Capture frontmost app —— 背景執行緒執行，避免 2 秒 osascript timeout
        # 卡住 recorder.start()。此資訊同時被 preset 路由（A1）與 auto-paste 用；
        # 所以**永遠抓**，不再綁 auto_paste 設定。
        self._paste_target  = None
        self._frontmost_app = None
        self._target_label.configure(text="")

        def _capture_frontmost():
            # Bug B（v2.12.0）：擴大自我識別清單，避免把 Whisper Pro 自己當成貼上目標。
            # NSWorkspace 在 .app bundle 模式下會回「Whisper Pro」；dev 模式回「Python」。
            # 兩種情境都該排除，但**不要**直接 return（會錯過真實 frontmost）；
            # 改成：只跳過「我自己」的判斷，仍把 frontmost 紀錄起來（_frontmost_app 用於 preset
            # 路由），但不設 paste_target（避免貼到自己）。
            SELF_APP_NAMES = {"Python", "python3", "WhisperPro", "Whisper Pro", "whisper_pro"}
            app = _ap.get_frontmost_app()
            if not app:
                return
            def _apply():
                if self._state != "recording":
                    return
                self._frontmost_app = app   # 永遠記錄（preset 路由用）
                # paste_target 排除自我，避免貼到自己
                if self.cfg.auto_paste and app not in SELF_APP_NAMES:
                    self._paste_target = app
                    self._target_label.configure(text=f"→ {app}")
                elif app in SELF_APP_NAMES:
                    # 自我前景時不顯示 → 標籤
                    log.debug(f"AUTO-PASTE: frontmost is self ({app!r}), skip paste_target")
            self.after(0, _apply)
        threading.Thread(target=_capture_frontmost, daemon=True).start()

        # UI：Chamber 渲染迴圈會在下次 tick 時自動依新狀態重繪，不需手動觸發
        self._timer_label.configure(text="00:00")
        self._hotkey_hint.configure(
            text=f"再按 {self.cfg.format_hotkey_display()} 停止錄音"
        )
        self._model_menu.configure(state="disabled")
        self._lang_menu.configure(state="disabled")
        self._set_status("錄音中", DANGER)

        self._update_timer()
        # v2.16.0：streaming 轉錄啟用（邊講邊轉、長段語音放開後幾乎立即出結果）
        # 重置計數器、generation 自增、排程第一次 tick。短錄音
        # (< STREAM_MIN_ENABLE_SAMPLES) _stream_tick 內部會自動跳過 dispatch。
        self._stream_dispatched = 0
        self._stream_completed  = 0
        # v2.17.0 streaming polish 計數器 reset
        self._stream_polished = []
        self._stream_polish_dispatched = 0
        self._stream_polish_completed  = 0
        self._stream_generation += 1

        # v2.19.x LocalAgreement-2 path（experimental、躲在 config flag 後、預設關）。
        # 預設值 cfg.streaming_algo == "fixed_chunk" → _la_buffer 維持 None、
        # _stream_tick 維持原 fixed-chunk 邏輯不變。
        # local_agreement 時建 buffer、_stream_tick 入口會 route 到 _stream_tick_la()。
        algo = getattr(self.cfg, "streaming_algo", "fixed_chunk")
        if algo == "local_agreement":
            try:
                from streaming_local_agreement import LocalAgreementBuffer
                min_s = float(getattr(self.cfg, "streaming_la_min_chunk_s", 5.0))
                max_s = int(getattr(self.cfg, "streaming_la_max_buffer_s", 120))
                self._la_buffer = LocalAgreementBuffer(
                    transcriber=self.transcriber,
                    language=self.cfg.get_whisper_language(),
                    model_size=self.cfg.model,
                    chinese_variant=getattr(self.cfg, "chinese_variant", "off"),
                    min_chunk_samples=int(min_s * 16_000),
                    max_buffer_samples=int(max_s * 16_000),
                )
                self._la_in_flight = False
                self._la_finalize_done = False
                self._la_finalize_text = ""
                log.info(
                    f"STREAMING: LocalAgreement-2 enabled "
                    f"(min_chunk={min_s}s, max_buf={max_s}s)"
                )
            except Exception:
                log_error("la_buffer_init_failed")
                self._la_buffer = None   # fallback 退回 fixed_chunk path
        else:
            self._la_buffer = None

        self._stream_tick_id = self.after(self.STREAM_TICK_MS, self._stream_tick)

        # Phase 4.3 mini 視窗：開啟錄音 HUD
        if self._mini_window is not None:
            self._mini_window.show_recording()

        # 大波形（錄音態專屬）：疊上轉錄流。chamber 逃生門沒有這顆 canvas，
        # 只在 skeleton 模式做（見 _build_stream_v2）。
        if self._skeleton_mode:
            self._show_big_wave()

    def _transition_to_processing(self, audio_duration_s: float = 0.0) -> None:
        """狀態機：recording → processing。停止麥克風並在背景執行緒跑 Whisper。

        Fix 9 / 2026-05-22（P2-A）：timeout 改成動態（依音訊長度推算）。
        呼叫端可顯式傳 `audio_duration_s`（測試用）；否則內部 stop 後從
        `full_audio` 長度自己算（生產路徑）。取兩者大值作為 timeout，
        保證 BASE 60s 為下限。
        """
        if self._state != "recording":
            log.debug(f"_transition_to_processing ignored (state={self._state})")
            return
        duration = time.perf_counter() - self._rec_start
        log_state("recording->processing", duration_s=f"{duration:.2f}")
        self._state            = "processing"
        self._state_start_time = time.perf_counter()

        # v2.27.0 三態統一：凍結最後一幀 engine.bars 快照，處理態畫「剛剛
        # 那段錄音的輪廓」不再繼續 update() engine（bars 屬性本身就回複本，
        # 這裡存的是獨立 list、不受後續 idle 態 update() 影響）。
        if self._wave_engine is not None:
            self._frozen_bars = self._wave_engine.bars

        # 大波形只在錄音態顯示；處理中收回，露出轉錄流的骨架卡（見下方
        # UtteranceBlockV2 pending block）。
        if self._skeleton_mode:
            self._hide_big_wave()

        # Pipeline timing 起點：按下結束熱鍵 / 點停止按鈕的這一瞬間。
        # 從這裡開始算「使用者等多久才看到貼上的字」。
        self._pipeline_t0            = self._state_start_time
        # v2.18.2：重置 defer + polish_mode（每次新 pipeline 都清乾淨）
        self._pipeline_defer_emit    = False
        self._pipeline_deferred_paste = {}
        self._pipeline_polish_mode   = "blocking"
        self._pipeline_transcribe_at = None
        self._pipeline_polish_at     = None
        self._pipeline_summary_emitted = False

        # v2.20.3 N3：pipeline timing breakdown 起點 milestone。
        # hotkey_release 是 anchor（user 放開的瞬間 ≈ recording 結束的瞬間）。
        # 後續 milestone（transcribe_start/done、polish_*、paste_*）皆相對於此 anchor。
        try:
            import pipeline_id as _pid  # noqa: PLC0415 — local import 防 cycle
            _pid.clear_events()         # 開新 pipeline 先清舊
            _pid.record_event("hotkey_release")
        except Exception:
            pass

        if self._stream_tick_id is not None:
            self.after_cancel(self._stream_tick_id)
            self._stream_tick_id = None

        full_audio = self.recorder.stop()

        # 動態 timeout：取呼叫端傳入值 / 實際錄音長度 / recording 階段 elapsed
        # 三者最大；至少 BASE 60s 為下限，避免 CPU backend + 長音檔誤殺。
        measured_audio_s = max(
            audio_duration_s,
            len(full_audio) / 16_000.0 if len(full_audio) else 0.0,
            duration,
        )
        dynamic_timeout_ms = max(
            self.PROCESSING_TIMEOUT_BASE_MS,
            int(measured_audio_s * self.PROCESSING_TIMEOUT_RTF_BUDGET * 1000),
        )
        # 穩定性（Fix 4 / 2026-05-21、Fix 9 / 2026-05-22）：記錄 processing
        # 進入時間並排程動態自癒檢查。
        # B2（v2.7.0）：cancel 上一輪 pending 的 timeout callback，避免快速
        # 連續錄音累積 N 個 alive 直到各自 timeout 觸發。
        self._processing_started_at = time.monotonic()
        self._processing_timeout_ms = dynamic_timeout_ms
        if self._processing_timeout_id is not None:
            try:
                self.after_cancel(self._processing_timeout_id)
            except Exception:
                pass  # Tk 已不認得這個 id，無害
        self._processing_timeout_id = self.after(
            dynamic_timeout_ms, self._processing_timeout_check
        )

        # UI：Chamber 渲染迴圈下一 tick 會自動切換到 WARN 配色 + 粒子旋轉環
        self._timer_label.configure(text="")
        self._hotkey_hint.configure(text="轉錄中…")
        self._target_label.configure(text="")
        if self._skeleton_mode:
            # v2.28.0 骨架重寫：狀態點語意收窄——PROC(=INDIGO) 專職「處理中」，
            # WARN 讓給「暖機中」（見 _warmup_model）。
            self._set_status("處理中", INDIGO)
            # v2.28.0：錄音一停就在轉錄流頂端插一張「轉錄中」骨架卡（固定
            # 高度 88，避免真正內容填入時版面跳動）；_display_result_skeleton
            # 完成後會就地把它填成 ready／failed，不會有「骨架卡消失、新卡片
            # 彈出」的閃爍。狀態機不允許 processing 中再開始新錄音，同一時間
            # 只會有一個 pending block，不用擔心覆蓋舊的。
            self._clear_placeholder()
            self._pending_block = UtteranceBlockV2(
                self._blocks_container,
                on_copy=self._on_block_copy, on_save=self._on_block_save,
            )
            prev_top = self._utterance_blocks[0] if self._utterance_blocks else None
            if prev_top is not None:
                prev_top.highlight_as_latest(False)
                self._pending_block.pack(fill="x", pady=(0, 8), before=prev_top)
            else:
                self._pending_block.pack(fill="x", pady=(0, 8))
            try:
                self._blocks_container.update_idletasks()
                self._blocks_container._parent_canvas.yview_moveto(0.0)
            except Exception:
                pass
        else:
            self._status_dot.configure(text_color=WARN)
            self._status_label.configure(text="  轉錄中，請稍候…")

        tail  = full_audio[self._stream_samples:]
        model = self._model_var.get()
        lang  = self.cfg.get_whisper_language()
        audio = tail if len(tail) > 800 else full_audio

        # v2.19.x LocalAgreement-2 path：把剩餘 audio 餵給 buffer + 起 finalize。
        # _run_transcription 內部會偵測 _la_buffer 非 None 改走 LA 合併路徑、
        # 不再從 audio 跑 transcribe(tail)。即便如此仍把整段 audio 傳進去——
        # _run_transcription 還是會跑一個 transcribe 拿 segment / language（finalize
        # 完才有完整輸出、但這條路徑保險絲還是讓 fixed-chunk 邏輯能正常回 result）。
        # 簡化：LA path 把整段累積進 buffer，finalize 內部會跑最後一次 ASR、結果
        # 就是完整文字。
        if self._la_buffer is not None:
            # 把錄音停下後的最後一段（_stream_samples 之後）也餵進 buffer
            try:
                if len(tail):
                    self._la_buffer.add_audio(tail)
                    self._stream_samples = len(full_audio)
            except Exception:
                log_error("la_final_add_audio_failed")
            self._start_la_finalize()

        # v2.27.0 三態統一：處理態掠掃光帶盡量餵真實進度。只有「這次錄音真的
        # 走過 LA 路徑、而且錄音期間已經 commit 過東西」才算數——LA 沒機會
        # commit（fixed_chunk 模式、或錄音太短一次轉完）就誠實退回等速掠掃，
        # 不硬湊假進度（見 _processing_sweep_progress docstring）。
        self._proc_sweep_use_real = False
        self._proc_sweep_known_frac = 0.0
        if self._la_buffer is not None:
            try:
                total_s = self._la_buffer.buffer_seconds
                committed_s = self._la_buffer.committed_audio_end_s
                if total_s > 0 and committed_s > 0:
                    self._proc_sweep_use_real = True
                    self._proc_sweep_known_frac = committed_s / total_s
            except Exception:
                pass

        # v2.20.3 N3：把背景 ASR thread 起點記成一個 milestone。
        # 跟 hotkey_release 的差距 ≈「錄音收尾 + UI 切狀態 + thread spin-up」。
        try:
            import pipeline_id as _pid  # noqa: PLC0415
            _pid.record_event("transcribe_start")
        except Exception:
            pass

        threading.Thread(
            target=self._run_transcription,
            args=(audio, model, lang),
            daemon=True,
        ).start()

        # Phase 4.3 mini 視窗：切到處理中色（琥珀）
        if self._mini_window is not None:
            self._mini_window.show_processing()

    def _transition_to_idle(self, result: Optional[TranscriptionResult] = None) -> None:
        """狀態機：任何狀態 → idle。恢復 UI 操作性；若 result 非 None 則顯示結果。"""
        if result is not None:
            log_state(
                "processing->idle",
                text_len=len(result.text),
                language=result.language,
                elapsed_s=f"{result.elapsed_seconds:.2f}",
                audio_s=f"{result.duration_seconds:.2f}",
            )
        else:
            log_state("->idle")
        self._state            = "idle"
        self._state_start_time = time.perf_counter()
        self._ripples.clear()

        # B2（v2.7.0）：processing 正常結束 → cancel 對應 timeout self-heal
        # callback，避免 pending after 一直留到 timeout_ms 才 fire（no-op，
        # 但會 hold reference 在 Tk command table 裡）。
        if self._processing_timeout_id is not None:
            try:
                self.after_cancel(self._processing_timeout_id)
            except Exception:
                pass
            self._processing_timeout_id = None

        # Phase 4.3 mini 視窗：閒置時隱藏（不 destroy，下次錄音再 show）
        if self._mini_window is not None:
            self._mini_window.hide()

        # 大波形只在錄音態顯示；idle 是任何狀態都可能進來的收斂點（見本
        # 函式 docstring「任何狀態 → idle」），這裡保險再收一次。
        # _transition_to_processing 通常已經收過，place_forget() 對已經
        # 收起的 canvas 呼叫是無害的 no-op。
        if self._skeleton_mode:
            self._hide_big_wave()

        self._timer_label.configure(text="")
        self._hotkey_hint.configure(
            text=f"按下 {self.cfg.format_hotkey_display()} 即時錄音"
        )
        self._model_menu.configure(state="normal")
        self._lang_menu.configure(state="normal")
        self._target_label.configure(text="")

        model = self._model_var.get()
        if self._skeleton_mode:
            # v2.28.0 骨架重寫：狀態點語意收窄——CYAN(=ACCENT) 專職「就緒」，
            # 從閒置態原本的 SUCCESS(綠) 讓出來（SUCCESS 只留給「已貼上」toast
            # 與權限已授權，見 tokens.py Aperture 第二輪語意收窄）。
            self._set_status("就緒", ACCENT)
        else:
            self._status_dot.configure(text_color=SUCCESS)
            self._status_label.configure(text=f"  就緒 ({model})")

        if result is not None:
            self._display_result(result)

    # ═══════════════════════════════════════════════════════════════════════
    #  STREAMING
    # ═══════════════════════════════════════════════════════════════════════

    def _stream_tick(self) -> None:
        """v2.16.0 Streaming 中段轉錄：每秒檢查 recorder buffer，累積夠
        STREAM_CHUNK_SAMPLES（10s）就 dispatch 一個 chunk 到背景 thread
        跑 user 選的主模型（不再是 small）。結果 index-based 寫進
        _stream_chunks 維持順序。

        關鍵設計：
          • Index-based slots：dispatch 時先 append placeholder ""，背景
            thread 完成寫進指定 index、避免 race。
          • _transcription_lock 在 transcribe() 內、序列化所有 chunks
            （Qwen3-ASR Session 不支援並發、序列化是 must）。10s chunk
            推論 ~5s < 10s 間隔、不會塞車。
          • dispatched / completed 計數：_run_transcription 用 deadline
            wait 確保最終合併前所有 chunks 完成。
          • 短錄音 (< STREAM_MIN_ENABLE_SAMPLES = 12s) 完全不 dispatch、
            直接走原 end-to-end 路徑（streaming overhead > 收益）。
          • v2.22.0 VAD 對齊切窗：累積 ≥10s 後不再無腦硬切，交給
            _decide_stream_cut_length() 判斷（詳見該方法 docstring）。
            cfg.chunk_cut_mode="fixed" 可退回舊的「滿 10s 就切」行為。
        """
        self._stream_tick_id = None
        if self._state != "recording":
            return

        # v2.24.0 錄音看門狗（每秒 tick 順路檢查、零額外成本）
        self._recording_watchdog_check()
        if self._state != "recording":
            return   # 看門狗剛自動停止 → 本輪 tick 到此為止

        # v2.19.x LocalAgreement-2 path：route 到 _stream_tick_la()。
        # _la_buffer 非 None 表示這輪錄音用 LA 演算法、fixed-chunk 邏輯整段跳過。
        if self._la_buffer is not None:
            self._stream_tick_la()
            return

        snap = self.recorder.get_buffer_snapshot()
        available = len(snap) - self._stream_samples

        # 短錄音門檻：buffer 還沒到 12s 就先不 streaming
        # （即使現在到 10s、user 馬上停的話、整段只 ~11s、走端到端比較合算）
        # 但若已開始 streaming 就繼續（_stream_dispatched > 0）
        if (
            self._stream_dispatched == 0
            and len(snap) < self.STREAM_MIN_ENABLE_SAMPLES
        ):
            # 排下一輪 tick、繼續累積
            self._stream_tick_id = self.after(
                self.STREAM_TICK_MS, self._stream_tick
            )
            return

        # 累積 ≥ 10s 新 audio？ → 決定切點（VAD 對齊 or 舊 fixed 行為）
        if available >= self.STREAM_CHUNK_SAMPLES:
            cut_len = self._decide_stream_cut_length(snap, available)
            if cut_len is not None:
                chunk_end = self._stream_samples + cut_len
                chunk = snap[self._stream_samples:chunk_end]
                self._stream_samples = chunk_end
                self._dispatch_stream_chunk(chunk)
            # cut_len is None → 還沒到 hard cap、且沒找到靜音，繼續累積等下次 tick

        # 排下次 tick
        if self._state == "recording":
            self._stream_tick_id = self.after(
                self.STREAM_TICK_MS, self._stream_tick
            )

    def _recording_watchdog_check(self) -> None:
        """v2.24.0 錄音看門狗：連續無語音太久 → 提醒 → 自動停止（防忘記關錄音）。

        訊號來源：fixed_chunk streaming 的 chunk 轉錄結果——有內容就刷新
        `_last_voice_at`（`_dispatch_stream_chunk` 內）。任何超過 8 分鐘的錄音
        必然已進入 streaming（12s 就啟動），所以訊號一定存在。

        兩段式（參數依近 2.5 週實測：最長合法錄音 15 分鐘、多數 <5 分鐘）：
          • 連續 8 分鐘無語音 → 系統通知 + toast 提醒（只發一次）。用系統通知
            是因為兩次實案（8/2 睡著、8/5 在別的 App 工作）都證明 Mini HUD 的
            被動顯示不夠——人根本不在看。
          • 連續 15 分鐘無語音 → 自動停止。走 `_try_stop()` 標準停止路徑、
            已錄內容照常轉錄存檔（不丟棄），使用者要繼續只需再按一次熱鍵。

        只在 fixed_chunk 路徑生效：LA 路徑（`_la_buffer` 非 None、預設關）的
        chunk 訊號機制不同、`_last_voice_at` 不會被刷新，直接跳過避免誤停。
        """
        try:
            if not getattr(self.cfg, "recording_watchdog", True):
                return
            if self._la_buffer is not None:
                return
            last = self._last_voice_at
            if last is None:
                return
            quiet_s = time.perf_counter() - last

            if quiet_s >= self.WATCHDOG_STOP_NO_VOICE_S:
                log_action("watchdog_auto_stop", quiet_min=round(quiet_s / 60, 1))
                self._notify_system(
                    "Whisper Pro",
                    "太久沒偵測到語音，已自動停止錄音（已錄內容有保留）",
                )
                self._show_toast("⚠ 太久沒偵測到語音，已自動停止錄音")
                self._try_stop()
            elif (
                quiet_s >= self.WATCHDOG_WARN_NO_VOICE_S
                and not self._watchdog_warned
            ):
                self._watchdog_warned = True
                log_action("watchdog_warn", quiet_min=round(quiet_s / 60, 1))
                self._notify_system(
                    "Whisper Pro",
                    "麥克風還在錄音中——已 8 分鐘未偵測到語音（忘記關了嗎？）",
                )
                self._show_toast("🎙 還在錄音中（久未偵測到語音）")
        except Exception:
            log_error("recording_watchdog_failed")

    def _notify_system(self, title: str, message: str) -> None:
        """發系統通知（使用者不在本 App / 不在電腦前也看得到）。

        mac 用 osascript display notification（Popen 射後不理、不卡主執行緒）；
        Windows 暫無實作（toast 仍會顯示）。失敗靜默——通知是輔助、非關鍵路徑。
        """
        try:
            if IS_MAC:
                safe_t = title.replace('"', '\\"')
                safe_m = message.replace('"', '\\"')
                subprocess.Popen(
                    ["osascript", "-e",
                     f'display notification "{safe_m}" with title "{safe_t}"'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    def _decide_stream_cut_length(self, snap, available: int) -> Optional[int]:
        """v2.22.0 VAD 對齊切窗：決定這次要切多長（相對 _stream_samples 的
        sample 數），或回傳 None 表示這次先不切、繼續累積。

        邏輯（chunk_cut_mode 讀自 self.cfg，預設 "vad_aligned"）：
          • "fixed"：完全比照舊行為 —— 累積 ≥ 10s 就切滿 10s，不找靜音。
          • "vad_aligned"（預設）：
              - 還沒到 hard cap（12s）：在目前累積的音訊「最後 2.5 秒」內找
                能量最低的靜音窗；找到就切在那裡（切點後的音留給下一段）。
                找不到就回 None，讓 _stream_tick 繼續累積等下一秒。
              - 已達 hard cap（12s）：不能再等了，最後再試一次找靜音、
                找不到就強制在 hard cap 處切（保底、行為同舊 fixed path）。

        audit log：每次真正切下去都記一筆 chunk_cut（cut_at_s / mode /
        found_silence），方便日後驗收「切點有沒有落在真正的停頓」。
        """
        mode = getattr(self.cfg, "chunk_cut_mode", "vad_aligned")

        if mode == "fixed":
            cut_len = self.STREAM_CHUNK_SAMPLES
            log_action(
                "chunk_cut",
                cut_at_s=round((self._stream_samples + cut_len) / 16_000, 2),
                mode="fixed",
                found_silence=False,
            )
            return cut_len

        # vad_aligned：只在目前累積範圍（從 _stream_samples 開始）搜尋
        buffer_tail = snap[self._stream_samples:self._stream_samples + available]
        hard_cap_reached = available >= self.STREAM_HARD_CAP_SAMPLES

        cut_point = find_silence_cut_point(buffer_tail, sample_rate=16_000)

        if cut_point is not None:
            log_action(
                "chunk_cut",
                cut_at_s=round((self._stream_samples + cut_point) / 16_000, 2),
                mode="vad_aligned",
                found_silence=True,
            )
            return cut_point

        if not hard_cap_reached:
            # 連續講話沒停頓、還沒到 hard cap → 先不切，等下一秒累積更多
            return None

        # 到 hard cap 仍找不到停頓 → 強制切在 hard cap（保底、同舊行為）
        cut_len = self.STREAM_HARD_CAP_SAMPLES
        log_action(
            "chunk_cut",
            cut_at_s=round((self._stream_samples + cut_len) / 16_000, 2),
            mode="vad_aligned",
            found_silence=False,
        )
        return cut_len

    def _dispatch_stream_chunk(self, audio) -> None:
        """送一個 chunk 到背景 thread 跑 transcribe，結果寫進 _stream_chunks[idx]。

        Index-based slot pattern：避免完成順序不一致引發的 race。實際上
        transcribe() 內 _transcription_lock 已序列化、但這個 pattern 更穩。
        """
        idx = len(self._stream_chunks)
        self._stream_chunks.append("")   # placeholder 占位、保順序
        self._stream_dispatched += 1

        model = self.cfg.model
        lang = self.cfg.get_whisper_language()
        cv = getattr(self.cfg, "chinese_variant", "off")

        gen = self._stream_generation   # 捕捉 dispatch 當下的 generation

        def _process(audio=audio, idx=idx, gen=gen):
            try:
                r = self.transcriber.transcribe(
                    audio, model_size=model, language=lang, chinese_variant=cv,
                )
                text = r.text if (
                    r.text and not r.text.startswith("（")
                ) else ""
                # Generation guard：若 user 已開新 recording、上輪 chunk 結果丟棄
                # 否則寫進 index（單一 writer per slot、不需鎖）
                if self._stream_generation == gen:
                    self._stream_chunks[idx] = text
                    # v2.24.0 看門狗：這個 chunk 有真實語音 → 刷新「最後聽到語音」
                    #   時間戳（float 賦值原子、背景執行緒直接寫安全）。
                    if text:
                        self._last_voice_at = time.perf_counter()
                    log.info(
                        f"STREAMING: chunk[{idx}] done "
                        f"({len(text)} chars, RTF={(r.elapsed_seconds or 0)/(r.duration_seconds or 1):.2f})"
                    )
                    # v2.17.0：ASR chunk 完 → 立即 dispatch streaming polish
                    # （只在 Ollama enabled 且 chunk text 非空）
                    if self.cfg.ollama_enabled and text:
                        self._dispatch_stream_polish(idx, text, gen)
                else:
                    log.info(f"STREAMING: chunk[{idx}] dropped (generation mismatch)")
            except Exception:
                log_error("stream_chunk_failed", idx=idx)
            finally:
                # Generation guard 同樣套用 counter
                if self._stream_generation == gen:
                    self._stream_completed += 1

        threading.Thread(target=_process, daemon=True).start()

    def _dispatch_stream_polish(self, idx: int, text: str, gen: int) -> None:
        """v2.17.0：把單一 ASR chunk 立即 dispatch 給 Ollama polish。

        關鍵設計：
          • 一律用 default polish prompt（OLLAMA_POLISH_PROMPT）。preset 路由
            的 action presets（翻英文 / 條列 / 會議紀錄）需要看到全文 context、
            chunk-level polish 沒意義；若 user 最終 preset 是 action、
            _start_polish 會 fallback 走 full-text polish、捨棄 streamed 結果。
          • Index-based slot pattern + generation guard（同 ASR streaming）
          • Polish 失敗（timeout / Ollama 掛掉）→ slot 留原文當保險
        """
        from prompts import OLLAMA_POLISH_PROMPT

        # 擴展 list 到 idx+1（slot pattern）
        while len(self._stream_polished) <= idx:
            self._stream_polished.append("")
        self._stream_polish_dispatched += 1
        dict_terms = self._dictionary_terms if self.cfg.dictionary_enabled else None

        def _polish_thread(text=text, idx=idx, gen=gen):
            try:
                # v2.18.0：透過 self.polish router 自動路由到 Ollama / Vertex
                # polish == None（backend=off）→ 跳過 streaming polish
                client = self.polish
                if client is None:
                    return
                resp = client.process(
                    text,
                    prompt_template=OLLAMA_POLISH_PROMPT,
                    dictionary_terms=dict_terms,
                    preset_name="default",
                )
                polished = resp.text if (resp and resp.text and not resp.error) else text
                if self._stream_generation == gen:
                    self._stream_polished[idx] = polished
                    log.info(
                        f"STREAM_POLISH: chunk[{idx}] done "
                        f"({len(polished)} chars, {(resp.elapsed_seconds or 0):.2f}s)"
                    )
                else:
                    log.info(f"STREAM_POLISH: chunk[{idx}] dropped (generation mismatch)")
            except Exception:
                log_error("stream_polish_failed", idx=idx)
                # 失敗時保險絲：slot 留原文（避免合併時這段消失）
                if self._stream_generation == gen and idx < len(self._stream_polished):
                    self._stream_polished[idx] = text
            finally:
                if self._stream_generation == gen:
                    self._stream_polish_completed += 1

        threading.Thread(target=_polish_thread, daemon=True).start()

    # ═══════════════════════════════════════════════════════════════════════
    #  v2.19.x LocalAgreement-2 STREAMING（experimental）
    # ═══════════════════════════════════════════════════════════════════════
    #
    # 與 fixed-chunk path 的對照：
    #   fixed-chunk：每 10s 切一個獨立 chunk、各自轉、concat → 邊界切碎 + 重複病
    #   LocalAgreement-2：累積整段、重轉未 commit 尾段、跟上輪取 LCP commit
    #
    # 介面：
    #   _stream_tick_la()       — 路由自 _stream_tick；feed audio + dispatch tick
    #   _dispatch_la_process()  — 背景 thread 跑 buffer.process_tick()，更新 UI
    #   _start_la_finalize()    — _transition_to_processing 呼叫；背景跑 finalize
    #
    # 與 streaming polish 的關係：
    #   暫時不整合（避免 race）。LA path commit 進來只更新 UI + audit、polish 等
    #   錄音結束 finalize 之後跑一次（blocking polish）。Streaming polish 在
    #   v2.17.0 跑 per-chunk Ollama 呼叫，每個 commit 都觸發會大幅增加 Ollama 負載
    #   且 commit 與 chunk 邊界不對齊、polish 出來再 concat 容易斷句。
    # TODO（future work / 下個 session）：
    #   • 整合 streaming polish on LA—— commit 累積到一個門檻（例如句末符號 / N 字
    #     以上）再 dispatch polish。Index-based slot pattern 仍可用、但 idx 不再
    #     對應 chunk 而對應「commit 批次」。
    #   • _dispatch_la_process 改 priority queue / single worker thread、避免 LA
    #     tick 重疊 + transcribe() 的 _transcription_lock 排隊。
    #   • Display commit incrementally to UI（目前是錄音結束才一次性 display）。

    def _stream_tick_la(self) -> None:
        """LocalAgreement-2 streaming tick（路由自 _stream_tick）。

        流程：
          1. 從 recorder 拿新累積 audio（用 _stream_samples 當 cursor、跟 fixed-chunk
             共用）feed 給 _la_buffer.add_audio()
          2. 若上一輪 process_tick 還在跑（_la_in_flight=True）→ 排下輪即可，不重疊
             dispatch（transcribe() 的 _transcription_lock 會排隊、但這樣會堆積太多）
          3. dispatch 背景 thread 跑 process_tick；完成回主執行緒更新 UI（目前是
             只寫 log；UI display 留給錄音結束一次性 display）
          4. 排下次 tick
        """
        snap = self.recorder.get_buffer_snapshot()
        # 用 _stream_samples 當 cursor 拿增量、feed 進 buffer
        # （_stream_samples 在 _transition_to_recording 已被 reset 為 0）
        new_audio = snap[self._stream_samples:]
        if len(new_audio):
            try:
                self._la_buffer.add_audio(new_audio)
                self._stream_samples = len(snap)
            except Exception:
                log_error("la_add_audio_failed")

        # 沒在 in-flight 就 dispatch
        if not self._la_in_flight:
            self._dispatch_la_process()

        # 排下次 tick（_state 已在 _stream_tick 入口檢查過、這裡再保險）
        if self._state == "recording":
            self._stream_tick_id = self.after(
                self.STREAM_TICK_MS, self._stream_tick
            )

    def _dispatch_la_process(self) -> None:
        """背景 thread 跑 _la_buffer.process_tick()。

        Generation guard：_transition_to_recording 會把 _stream_generation +=1，
        若 dispatch 期間 user 已開新一輪錄音（generation 變了 / _la_buffer 變了 /
        state 變了）→ 結果 silent drop。
        """
        if self._la_buffer is None:
            return
        self._la_in_flight = True
        gen = self._stream_generation
        la_buf = self._la_buffer

        def _process(gen=gen, la_buf=la_buf):
            try:
                new_commit = la_buf.process_tick()
                # Generation guard
                if (
                    self._stream_generation == gen
                    and self._la_buffer is la_buf
                ):
                    if new_commit:
                        log.info(
                            f"LA: stream tick commit (+{len(new_commit)} chars, "
                            f"total={len(la_buf.committed_text)})"
                        )
            except Exception:
                log_error("la_dispatch_process_failed")
            finally:
                # 即使 generation 不匹配也要 clear，避免下輪卡住
                # （但只 clear 自己這輪、用 buffer identity 比對）
                if self._la_buffer is la_buf:
                    self._la_in_flight = False

        threading.Thread(target=_process, daemon=True).start()

    def _start_la_finalize(self) -> None:
        """錄音結束時呼叫；背景 thread 跑 _la_buffer.finalize() 取完整文字。

        結果寫進 self._la_finalize_text，_la_finalize_done=True，_run_transcription
        等待這個 flag 後直接用、不再額外跑 transcribe(tail)。

        為什麼不在 _transition_to_processing 直接 join：finalize 內部會跑一次 ASR、
        需要 _transcription_lock；可能花幾秒；要在背景跑、不阻塞主執行緒切 UI。
        """
        if self._la_buffer is None:
            return
        gen = self._stream_generation
        la_buf = self._la_buffer
        self._la_finalize_done = False
        self._la_finalize_text = ""

        def _finalize(gen=gen, la_buf=la_buf):
            try:
                # 等任何 in-flight process_tick 跑完（簡單 spin、最多幾秒）
                t_wait = time.time()
                while self._la_in_flight:
                    if time.time() - t_wait > 15.0:
                        log.warning("LA: finalize gave up waiting for in-flight tick")
                        break
                    time.sleep(0.05)
                text = la_buf.finalize()
                # Generation guard
                if (
                    self._stream_generation == gen
                    and self._la_buffer is la_buf
                ):
                    self._la_finalize_text = text
                    self._la_finalize_done = True
                    log.info(
                        f"LA: finalize complete ({len(text)} chars)"
                    )
            except Exception:
                log_error("la_finalize_failed")
                # 失敗也要設 done，否則 _run_transcription 永遠等不到
                if self._la_buffer is la_buf:
                    self._la_finalize_done = True

        threading.Thread(target=_finalize, daemon=True).start()

    # ═══════════════════════════════════════════════════════════════════════
    #  TRANSCRIPTION
    # ═══════════════════════════════════════════════════════════════════════

    def _run_transcription(self, audio, model: str, lang) -> None:
        """背景執行緒：呼叫 Whisper 做最終轉錄，完成後 marshal 到主執行緒。

        若有中段串流結果，將其與最終文字合併後一起回傳。

        穩定性（Fix 3 / 2026-05-21）：整段包 try/except；若 transcribe()
        throw（模型載入失敗 / OOM / VAD 異常），_on_transcription_done 不會
        被排程，狀態機會卡在 processing 永不歸位。改在這裡 marshal
        _on_transcription_failed 回主執行緒把狀態切回 idle。
        """
        try:
            # v2.19.x LocalAgreement-2 path：完全跳過 fixed-chunk merge 邏輯。
            # _start_la_finalize 已在背景跑 finalize、會跑最後一次 ASR 並把完整
            # commit 寫進 _la_finalize_text。我們在這裡 spin 等 finalize 完、
            # 把結果包成 TranscriptionResult。整段 audio 在 _transition_to_processing
            # 已餵進 buffer、不再呼叫 self.transcriber.transcribe(audio)。
            if self._la_buffer is not None:
                deadline = time.time() + self.STREAM_CHUNK_JOIN_TIMEOUT_S
                while not self._la_finalize_done:
                    if time.time() > deadline:
                        log.warning(
                            "LA: timeout waiting for finalize, using whatever "
                            "committed_text we have"
                        )
                        break
                    time.sleep(0.05)
                # 取完整文字（finalize 成功 → _la_finalize_text；timeout → buffer
                # 的 committed_text 當保險絲）
                la_text = (
                    self._la_finalize_text
                    if self._la_finalize_done and self._la_finalize_text
                    else self._la_buffer.committed_text
                )
                # 估算 duration（buffer 累積總長）；elapsed 用 0（finalize 內部已 log）
                duration_s = self._la_buffer.buffer_seconds
                result = TranscriptionResult(
                    text=la_text.strip() or "（未偵測到語音內容）",
                    language=lang or "",
                    duration_seconds=duration_s,
                    elapsed_seconds=0.0,
                    segments=[],
                )
                log.info(
                    f"LA: _run_transcription using LA result "
                    f"({len(la_text)} chars, duration={duration_s:.1f}s)"
                )
                self.after(0, self._on_transcription_done, result)
                return

            # 一律用使用者選的模型做最終轉錄，不再因短音檔退化到 transcribe_fast
            # （那條路徑寫死 small，會嚴重拖垮品質）。
            # v2.14.0：傳 chinese_variant 給 Qwen3-ASR 用（Whisper backend ignore）
            # tail audio：_transition_to_processing 已把 _stream_samples 後的尾段
            # 取出來；若無 streaming 就是整段 full_audio
            result = self.transcriber.transcribe(
                audio,
                model_size=model,
                language=lang,
                chinese_variant=getattr(self.cfg, "chinese_variant", "off"),
            )

            # v2.16.0：等所有背景 streaming chunks 完成寫入 _stream_chunks
            # 才合併。transcribe() 已序列化（_transcription_lock）、tail 完成
            # 表示鎖剛被釋放、之前 chunks 已寫完；但 _stream_completed 計數
            # 在 transcribe 之外才 +=、最壞情況差一個 chunk、deadline wait 保險。
            if self._stream_dispatched > 0:
                deadline = time.time() + self.STREAM_CHUNK_JOIN_TIMEOUT_S
                while self._stream_completed < self._stream_dispatched:
                    if time.time() > deadline:
                        log.warning(
                            f"STREAMING: timeout waiting for chunks "
                            f"(done={self._stream_completed}/{self._stream_dispatched})"
                        )
                        break
                    time.sleep(0.05)
                log.info(
                    f"STREAMING: merged {self._stream_dispatched} chunks "
                    f"+ tail (tail len={len(result.text)})"
                )

            prior = list(self._stream_chunks)
            if prior:
                tail = result.text if result.text != "（未偵測到語音內容）" else ""
                # v2.21.3 接縫縫合：移除 10 秒切窗在接縫補的假句號（實測 14/14 接縫
                #   的句號都是假的、甚至把「三千六」切成「月三。千六」）。tail 原樣保留。
                try:
                    from transcriber import stitch_streaming_seams
                    combined = stitch_streaming_seams(prior, tail)
                except Exception:
                    log_error("stream_seam_stitch_failed")
                    combined = "".join(prior) + tail
                # v2.21.0 Phase B3：盲拼接的接縫會產生重複（都都/一一律/句尾詞重疊）。
                # 重用 transcriber 既有的 n-gram dedupe（低風險、跟單段轉錄同一套邏輯）。
                try:
                    from transcriber import _dedupe_repetitive_ngrams
                    combined = _dedupe_repetitive_ngrams(combined)
                except Exception:
                    log_error("stream_merge_dedupe_failed")
                result = result.__class__(
                    text=combined.strip() or "（未偵測到語音內容）",
                    language=result.language,
                    duration_seconds=result.duration_seconds,
                    elapsed_seconds=result.elapsed_seconds,
                    segments=result.segments,
                )
            self.after(0, self._on_transcription_done, result)
        except Exception as e:
            log_error("transcription_failed", model=model, error=str(e))
            self.after(0, self._on_transcription_failed, str(e))

    def _on_transcription_failed(self, err_msg: str) -> None:
        """主執行緒：轉錄失敗時把狀態從 processing 切回 idle，顯示 toast。

        Fix 3 / 2026-05-21：沿用既有 _transition_to_idle(result) 路徑，
        不新增狀態；用一個帶失敗訊息的空 TranscriptionResult 觸發回 idle。

        Fix 9 / 2026-05-22（P1-B）：state != processing 時直接 drop。情境：
        _processing_timeout_check 已先 force-idle，背景 thread 隨後 throw，
        若不擋第二段錄音的 UI 會被「⚠ 轉錄失敗」toast 干擾。
        """
        if self._state != "processing":
            log.warning(
                f"_on_transcription_failed dropped: state={self._state} "
                f"(expected 'processing'); stale callback after force-idle"
            )
            log_action("transcription_failed_late_dropped", state=self._state)
            return
        empty = TranscriptionResult(
            text="（轉錄失敗，請查看 log）",
            language="",
            duration_seconds=0.0,
            elapsed_seconds=0.0,
            segments=[],
        )
        self._transition_to_idle(empty)
        # v2.18.2：補 pipeline_timing emit（Agent 4 P1-2）
        # 失敗 case 最需要觀察、不能 silent drop。path 標 failed_asr
        self._emit_pipeline_timing(
            paste_outcome="skipped",
            paste_target=None,
            text_len=0,
            failure_reason="asr",
        )
        self._show_toast("⚠ 轉錄失敗")
        # v2.28.0 骨架重寫：toast 幾秒就消失，轉錄失敗這種「使用者可能沒看到」
        # 的錯誤額外升級成 StatusSlot 的持久 Banner。
        if self._skeleton_mode:
            try:
                self._status_slot_show_banner(
                    "轉錄失敗，請查看日誌",
                    action_label="開啟日誌",
                    action_cb=lambda: _open_path_in_os_default(
                        os.path.expanduser("~/.whisper_app/logs")
                    ),
                )
            except Exception:
                pass

    def _processing_timeout_check(self) -> None:
        """processing 狀態超過動態 timeout 沒結束 → 強制切回 idle（Fix 4 / 2026-05-21、
        Fix 9 / 2026-05-22）。

        對應 _transition_to_processing 排程的 after。若狀態早已正常切回 idle 則
        no-op；若超時則 log_warning + log_action + 強制 idle + toast。
        Fix 9：timeout 上限改成依音訊長度動態算，比較 elapsed 用 instance 上記的
        `_processing_timeout_ms`，無記則 fallback BASE 60s。
        """
        # B2（v2.7.0）：callback fire 後 id 已自動 consume，清掉以利下次判斷
        self._processing_timeout_id = None
        if self._state != "processing":
            return   # 已正常結束，無事
        elapsed = time.monotonic() - getattr(self, "_processing_started_at", 0)
        timeout_s = getattr(self, "_processing_timeout_ms", self.PROCESSING_TIMEOUT_BASE_MS) / 1000
        if elapsed >= timeout_s:
            log.warning(f"STATE: processing stuck >{elapsed:.0f}s — force idle")
            log_action("state_processing_timeout_recovered", elapsed_s=f"{elapsed:.1f}")
            empty = TranscriptionResult(
                text="（轉錄超時，請重試）",
                language="",
                duration_seconds=0.0,
                elapsed_seconds=elapsed,
                segments=[],
            )
            self._transition_to_idle(empty)
            # v2.18.2：補 pipeline_timing emit（Agent 4 P1-2、跟 ASR fail 同邏輯）
            self._emit_pipeline_timing(
                paste_outcome="skipped",
                paste_target=None,
                text_len=0,
                failure_reason="asr_timeout",
            )
            self._show_toast("⏱ 轉錄超時 — 已自動恢復")

    def _on_transcription_done(self, result: TranscriptionResult) -> None:
        """主執行緒：轉錄完成後決定是否走潤飾流程，並觸發剪貼簿 / 自動貼上。

        Fix 9 / 2026-05-22（P1-B）：state != processing 時直接 drop。情境：
        _processing_timeout_check 已先 force-idle 並 toast「轉錄超時」，使用
        者已開始第二段錄音（state=recording），背景 Whisper thread 慢慢回來
        若不擋會覆寫 UI / timer 並把 _state 強制改成 idle，與 recorder 失同步。
        """
        if self._state != "processing":
            log.warning(
                f"_on_transcription_done dropped: state={self._state} "
                f"(expected 'processing'); likely stale callback from timed-out inference"
            )
            log_action("transcription_late_dropped", state=self._state)
            return

        # Pipeline timing 打點：轉譯完成（含背景 thread 排程 + after(0) 回主執行緒）
        if self._pipeline_t0 is not None:
            self._pipeline_transcribe_at = time.perf_counter() - self._pipeline_t0

        # v2.20.3 N3：milestone「transcribe_done」（細到 ms 給 session_summary 用）
        try:
            import pipeline_id as _pid  # noqa: PLC0415
            _pid.record_event("transcribe_done")
        except Exception:
            pass

        # v2.19.0：pipeline event「transcribe_done」
        _pipe_event(
            "transcribe_done",
            text_len=len(result.text or ""),
            elapsed_s=float(result.elapsed_seconds or 0.0),
        )

        self._transition_to_idle(result)
        self._show_toast(f"轉錄完成 · {result.elapsed_seconds:.1f}s")

        text  = result.text
        valid = bool(text) and text not in (
            "（未偵測到語音內容）",
            "（沒有偵測到音訊，請確認麥克風是否正常運作）",
        )

        # 每次新轉錄都 +1，遲到的潤飾結果可據此丟棄。
        self._polish_generation += 1
        gen = self._polish_generation
        # C1：_last_raw 永遠是 Whisper 原文、絕不被路由剝除覆寫
        self._last_raw         = text if valid else ""
        self._last_llm_input   = None
        self._last_polished    = None
        self._showing_polished = True          # 預設顯示潤飾版（有的話）
        self._apply_toggle_style()             # 新結果暫無潤飾版 → 兩顆灰

        # Phase 3.2：有效轉錄寫入歷史（polished_text 先 NULL，潤飾完再 update）
        self._current_history_id = None
        if valid and self.history_store is not None:
            try:
                self._current_history_id = self.history_store.insert(
                    duration_s    = result.duration_seconds,
                    raw_text      = text,
                    model_whisper = self._model_var.get(),
                    target_app    = self._frontmost_app,
                    preset_used   = "default",   # 走潤飾路徑時會在 _finish_polish 更新
                    language      = result.language or None,
                )
                if self._current_history_id:
                    log_action("history_saved",
                               id=self._current_history_id,
                               has_polish_pending=self.cfg.ollama_enabled)
            except Exception:
                log_error("history_insert_on_transcription_done")

        # 「最近」區去重用：這筆剛存進歷史的 id 記回 latest block，
        # _build_recent_block 才知道要排除掉、避免同一段內容卡片與
        # 「最近」列重複出現。_transition_to_idle 上面已經呼叫過
        # _display_result（→ block 已存在於 _utterance_blocks[0]）。
        if self._skeleton_mode and self._current_history_id is not None and self._utterance_blocks:
            self._utterance_blocks[0].history_id = self._current_history_id

        # 決定路徑：能潤飾就走潤飾流程，失敗自動降級回原文。
        # 規劃書 6.4「策略 B」：等潤飾完再貼，因此 auto-paste 也延到潤飾後。
        # v2.13.0 / 2026-05-24：修「Ollama 啟用但 health_ok=None 跳過 polish」。
        # 根因：D5-S7 TTL 30s 過期會回 None；剛 enable Ollama health check 還沒
        # 回來也是 None；舊邏輯 `is True` 排除 None → user 雖然開了 Ollama
        # 但每次 cache 過期或剛啟用都不會 polish、直接貼 Whisper 原文（含「措置率」
        # 這種同音錯字）。改用 `is not False` 放寬：None / True 都試一下；
        # 真的 False（確定沒跑）才略過。process() 內部 ConnectionError fallback
        # 自然會降級回原文，使用者不會卡住。
        # v2.21.0 Phase B4：把 polish_backend="off" 納入判斷。
        #   舊邏輯只看 ollama_enabled、沒看 polish_backend → off + ollama_enabled=True
        #   時仍走 polish path、下游 `self.polish or self.ollama` 又 fallback 回
        #   ollama → off 名存實亡。加 polish_backend != "off" 讓 off 真的不跑 LLM。
        take_polish_path = (
            valid
            and self.cfg.ollama_enabled
            and getattr(self.cfg, "polish_backend", "local") != "off"
            and self.ollama.health_ok is not False
        )

        if take_polish_path:
            # 複製原文到剪貼簿的行為保留（使用者可能立即想 ⌘V 原文）；
            # 潤飾完成後會再覆蓋一次成為潤飾版。
            if self.cfg.auto_copy:
                try:
                    import pyperclip
                    pyperclip.copy(text)
                except Exception:
                    pass

            target = self._paste_target if self.cfg.auto_paste else None
            self._paste_target = None

            # Bug D（v2.13.0）：raw 策略 → 立刻貼 Whisper 原文（不等 3-20s polish）
            # polish 結果照樣跑、UI 會顯示「已潤飾」，但不再 paste 第二次（避免貼到
            # user 已切走的視窗）。預設仍是 wait（品質優先）。
            paste_strategy = getattr(self.cfg, "ollama_paste_strategy", "wait")
            if paste_strategy == "raw" and self.cfg.auto_paste and target:
                # v2.18.2：raw 策略 defer pipeline emit、等 polish 完成 emit 才
                # 能完整記 polish_s（Agent 4 P1-1）
                self._pipeline_defer_emit = True
                self._do_auto_paste(text, target)
                target = None   # 告訴 _start_polish/_finish_polish 不要再 paste 第二次

            self._start_polish(gen, text, target)
            return

        # 不走潤飾：沿用原有「auto-copy + auto-paste 原文」流程。
        if self.cfg.auto_copy and valid:
            try:
                import pyperclip
                pyperclip.copy(text)
            except Exception:
                pass

        if self.cfg.auto_paste and valid and self._paste_target:
            target = self._paste_target
            self._paste_target = None
            # macOS 26.4+ TSM 強制主執行緒：pynput keyboard.Controller 模擬 ⌘V
            # 必須在主 thread 呼叫，否則 TSMGetInputSourceProperty 會觸發
            # dispatch_assert_queue_fail 直接閃退。詳見 CLAUDE.md §9.6。
            self._do_auto_paste(text, target)
        else:
            # 沒走 auto-paste（auto_paste 關 / 無 target / 無效文字）也要 emit
            # pipeline summary — 此時 paste_s 是 0、total_s ≈ transcribe_s
            self._emit_pipeline_timing(
                paste_outcome="skipped",
                paste_target=None,
                text_len=len(text) if valid else 0,
            )

    # ── 潤飾管線 ────────────────────────────────────────────────────────────

    def _start_polish(self, gen: int, raw_text: str, target: Optional[str]) -> None:
        """啟動背景潤飾；完成時將於主執行緒回呼 _finish_polish。

        Fix Cluster B / 2026-05-23：抓 target_block ref 一起傳進 _finish_polish，
        讓 _finish_polish 對齊「block identity」而非只判 generation。覆蓋三種 race：
        (1) auto-polish 與手動 `_on_ollama` 並存寫進同一個 latest block
        (2) `_repolish_from_history` 跑時使用者錄新音、polish 寫進新 block
        (3) `_on_block_delete` 刪 latest 時 polish 仍跑、寫進倒數第二段
        """
        self._polish_busy = True

        # A1：preset 路由一律用 _frontmost_app（不受 auto_paste 影響）
        if self.cfg.preset_routing_enabled:
            selection = _presets.select_preset(
                raw_text,
                self._frontmost_app,
                enabled=self.cfg.preset_overrides,
            )
        else:
            selection = _presets.PresetSelection(
                preset=_presets.PRESETS["default"],
                text=raw_text,
                matched_reason="routing_disabled",
            )

        preset      = selection.preset
        llm_input   = selection.text
        preset_name = preset.name
        # C1：記錄實際送 LLM 的輸入但不汙染 _last_raw
        self._last_llm_input = llm_input

        # v2.19.0：pipeline event「polish_dispatch」(polish_done 由 client 自己印)
        backend_name = getattr(self.cfg, "polish_backend", "local")
        _pipe_event(
            "polish_dispatch",
            backend=backend_name,
            preset=preset_name,
        )

        # v2.20.3 N3：milestone「polish_start」
        try:
            import pipeline_id as _pid  # noqa: PLC0415
            _pid.record_event("polish_start")
        except Exception:
            pass

        # Cluster B：抓「當下 latest」block ref 一起傳進 _finish_polish。
        # polish 跑回來時這個 ref 可能已被 delete（不在 list 內）或不再是 latest，
        # _finish_polish 用 identity 比對來決定是否安全 set_polished。
        target_block = self._utterance_blocks[0] if self._utterance_blocks else None
        if self._skeleton_mode and target_block is not None:
            # Aperture 卡片：segmented「潤飾」格切到載入態，讓使用者看得出
            # AI 正在跑、不是卡住或忘記按。
            target_block.set_polish_pending()

        # C2：三段式標題狀態（preset 名稱不會被後續 status 吃掉）
        self._title_preset = preset.display_name if preset_name != "default" else None
        self._title_status = "潤飾中…"
        self._rebuild_result_title()

        dict_terms = self._dictionary_terms if self.cfg.dictionary_enabled else None

        # v2.17.0：streaming polish 條件 — 只 default preset 用、action preset
        # (翻英文/條列/會議紀錄) 走原 full-text path（需看全文 context）
        use_streaming_polish = (
            self._stream_polish_dispatched > 0
            and preset_name == "default"
        )
        # v2.18.2：標記 polish_mode 給 _emit_pipeline_timing 用（Agent 4 P2）
        # streaming = polish_s 只反映 join + tail（實際 polish 在錄音期間就跑完）
        # blocking  = polish_s 反映完整 polish 工作量
        self._pipeline_polish_mode = "streaming" if use_streaming_polish else "blocking"

        def _run():
            if use_streaming_polish:
                # 等所有背景 polish chunks 完成（30s deadline 保險）
                deadline = time.time() + self.STREAM_CHUNK_JOIN_TIMEOUT_S
                while self._stream_polish_completed < self._stream_polish_dispatched:
                    if time.time() > deadline:
                        log.warning(
                            f"STREAM_POLISH: timeout waiting "
                            f"(done={self._stream_polish_completed}/{self._stream_polish_dispatched})"
                        )
                        break
                    time.sleep(0.05)

                streamed_polished = "".join(p for p in self._stream_polished if p)
                streamed_raw      = "".join(c for c in self._stream_chunks   if c)
                # 推算 tail = llm_input 比 streamed_raw 多出來的尾段
                # （length-based、簡單但可能因 corrections / opencc 微差）
                tail_raw = llm_input[len(streamed_raw):] if len(llm_input) > len(streamed_raw) else ""

                if tail_raw.strip() and self.polish is not None:
                    # v2.21.0 Phase B4：off 模式 self.polish=None、不再 fallback 回
                    # ollama（take_polish_path 已擋 off、這裡 defensive）
                    client = self.polish
                    tail_resp = client.process(
                        tail_raw,
                        prompt_template=preset.resolve_prompt(),
                        dictionary_terms=dict_terms,
                        preset_name=preset_name,
                    )
                    tail_polished = tail_resp.text if (
                        tail_resp and tail_resp.text and not tail_resp.error
                    ) else tail_raw
                elif tail_raw.strip():
                    # polish=None（off）：直接用原文尾段、不跑 LLM
                    tail_polished = tail_raw
                else:
                    tail_polished = ""

                combined_polished = streamed_polished + tail_polished
                log.info(
                    f"STREAM_POLISH: merged {self._stream_polish_dispatched} chunks "
                    f"+ tail (final {len(combined_polished)} chars)"
                )
                # 合成 OllamaResponse（沿用 _finish_polish 既有 contract）
                from ollama_client import OllamaResponse
                resp = OllamaResponse(
                    text=combined_polished,
                    model=self.cfg.ollama_model,
                    done=True,
                    error=None,
                    elapsed_seconds=0.0,
                    preset_name=preset_name,
                )
            else:
                # v2.21.0 Phase B4：透過 polish router；off 模式 self.polish=None
                # 不再 fallback 回 ollama（take_polish_path 已擋 off、這裡 defensive）
                client = self.polish
                if client is None:
                    from ollama_client import OllamaResponse
                    resp = OllamaResponse(
                        text=llm_input, model="", done=True, error=None,
                        elapsed_seconds=0.0, preset_name=preset_name,
                    )
                else:
                    resp = client.process(
                        llm_input,
                        prompt_template=preset.resolve_prompt(),
                        dictionary_terms=dict_terms,
                        preset_name=preset_name,
                    )
            # 把 raw_text（= Whisper 原文）+ target_block 交給 _finish_polish
            self.after(0, self._finish_polish, gen, raw_text, target, resp, target_block)

        threading.Thread(target=_run, daemon=True).start()

    def _finish_polish(
        self,
        gen: int,
        raw_text: str,
        target: Optional[str],
        resp,
        target_block=None,
    ) -> None:
        """主執行緒：套用潤飾結果（或降級回原文）+ 觸發自動貼上。

        Fix Cluster B：target_block identity 比對 — 只在「block 仍在 list 內
        且仍是 latest」時才 set_polished。否則 silently drop（避免寫進不相干 block）。
        """
        # 若期間又開始新轉錄，這次結果直接丟棄，避免蓋到新內容
        if gen != self._polish_generation:
            self._polish_busy = False
            return

        self._polish_busy = False

        # Pipeline timing 打點：潤飾完成（含背景 polish thread 排程 + after(0) 回主）
        # 注意：若 polish 失敗走 fallback（paste_text = raw_text），仍視為「完成這個階段」
        if self._pipeline_t0 is not None:
            self._pipeline_polish_at = time.perf_counter() - self._pipeline_t0

        # v2.20.3 N3：milestone「polish_done」
        try:
            import pipeline_id as _pid  # noqa: PLC0415
            _pid.record_event("polish_done")
        except Exception:
            pass

        if resp.error:
            # 降級：提示錯誤，title 狀態轉為「原文（潤飾失敗）」，auto-paste 貼原文
            # 注意：preset 名稱（若有）刻意保留，讓使用者知道是哪條 preset 炸
            self._title_status = "原文（潤飾失敗）"
            self._rebuild_result_title()
            self._show_toast(f"AI 潤飾失敗：{resp.error}")
            paste_text = raw_text
            # Aperture 卡片：segmented「潤飾」格顯示失敗提示、降級回原文顯示。
            # 只要求 block 還在 list 內（不要求仍是 latest）——即使使用者已經
            # 錄了下一段，這張舊卡片自己潤飾失敗的事實不會變，仍可以標示。
            if self._skeleton_mode and target_block is not None and target_block in self._utterance_blocks:
                target_block.set_polish_failed()
        else:
            # 成功：把潤飾版寫進 target_block，但只在 block identity 仍成立時才寫
            polished = resp.text
            # Cluster B：identity check — target_block 還在 list 內？還是 latest？
            block_ok = (
                target_block is not None
                and target_block in self._utterance_blocks
                and target_block is self._utterance_blocks[0]
            )
            if block_ok:
                target_block.set_polished(polished)
                self._last_polished = polished
            else:
                # block 已被刪除 / 不再是 latest（user 又錄新音、又刪掉等）
                # → 不寫進別人的 block；但仍把 paste_text 設成 polished 供 auto-paste
                log_action(
                    "polish_dropped_block_changed",
                    target_alive=(target_block in self._utterance_blocks),
                    is_latest=(target_block is self._utterance_blocks[0]
                               if self._utterance_blocks else False),
                )
            self._showing_polished = True
            self._apply_toggle_style()          # 現在有潤飾版了 → 兩顆點亮
            self._title_status = f"已潤飾 · {resp.elapsed_seconds:.1f}s"
            self._rebuild_result_title()
            self._show_toast(f"AI 潤飾完成 · {resp.elapsed_seconds:.1f}s")

            # 剪貼簿覆蓋成潤飾版（如果開啟 auto_copy）
            if self.cfg.auto_copy:
                try:
                    import pyperclip
                    pyperclip.copy(polished)
                except Exception:
                    pass

            # Phase 3.2：回頭更新歷史紀錄（潤飾結果 + preset + model）
            if self._current_history_id is not None and self.history_store is not None:
                try:
                    self.history_store.update_polish(
                        self._current_history_id,
                        polished_text = polished,
                        preset_used   = resp.preset_name,
                        model_ollama  = resp.model,
                    )
                except Exception:
                    log_error("history_update_polish_on_finish",
                              id=self._current_history_id)

            paste_text = polished

        # 自動貼上（策略 B：等潤飾完才貼）
        # macOS 26.4+ TSM 強制主執行緒：pynput keyboard.Controller 模擬 ⌘V
        # 必須在主 thread 呼叫，否則 TSMGetInputSourceProperty 會觸發
        # dispatch_assert_queue_fail 直接閃退。詳見 CLAUDE.md §9.6。
        if target:
            self._do_auto_paste(paste_text, target)
        else:
            # 沒貼上的情境（auto_paste 關 / target 已被 raw 策略消耗 / polish 失敗
            # 仍想 emit 完整 timing） — 在此 emit pipeline summary
            # v2.18.2：raw 策略下 _do_auto_paste 已被 defer、現在 polish 完成
            # 把 defer flag 關掉、用之前暫存的 paste 結果一起 emit（這樣 polish_s
            # 才會被記、path 才會是 with_ai_polish）
            if self._pipeline_defer_emit and self._pipeline_deferred_paste:
                deferred = self._pipeline_deferred_paste
                self._pipeline_defer_emit = False  # 解除 defer、允許 emit
                self._pipeline_deferred_paste = {}
                self._emit_pipeline_timing(
                    paste_outcome=deferred.get("paste_outcome", "skipped"),
                    paste_target=deferred.get("paste_target"),
                    text_len=deferred.get("text_len", len(paste_text)),
                )
            else:
                self._emit_pipeline_timing(
                    paste_outcome="skipped",
                    paste_target=None,
                    text_len=len(paste_text),
                )

    # ── 原文 / 潤飾 分段切換 ─────────────────────────────────────────────

    def _apply_toggle_style(self) -> None:
        """依目前狀態重繪兩顆分段按鈕。

        狀態矩陣：
          _last_polished is None           → 兩顆都灰（無潤飾可切）
          _showing_polished == True        → 「潤飾」active
          _showing_polished == False       → 「原文」active
        """
        has_polished = self._last_polished is not None

        def _style(active: bool, enabled: bool):
            if not enabled:
                return dict(
                    state="disabled",
                    fg_color="transparent",
                    border_color=SURF_3,
                    text_color=TEXT_4,
                    hover_color=SURF_1,
                )
            if active:
                return dict(
                    state="normal",
                    fg_color=SURF_2,
                    border_color=ACCENT,
                    text_color=TEXT_1,
                    hover_color=SURF_3,
                )
            return dict(
                state="normal",
                fg_color="transparent",
                border_color=SURF_3,
                text_color=TEXT_3,
                hover_color=SURF_2,
            )

        self._seg_raw_btn.configure(
            **_style(active=not self._showing_polished, enabled=has_polished)
        )
        self._seg_polished_btn.configure(
            **_style(active=self._showing_polished, enabled=has_polished)
        )

    def _set_showing_polished(self, show_polished: bool) -> None:
        """切換最新一段 block 的顯示內容（原文 / 潤飾）。

        Fix 19 Path B / 2026-05-23：直接呼叫 latest block 的 `set_showing_polished`，
        block 內部會更新 label 內容與 showing_polished state。原本 textbox mark 比對
        `expect_current` 的踩壞防護不再需要（block 自己有 raw/polished 兩個獨立欄位）。
        """
        if self._last_polished is None:
            return  # 沒有潤飾版可切
        if show_polished == self._showing_polished:
            return  # 已經是這個狀態
        if not self._utterance_blocks:
            return

        self._utterance_blocks[0].set_showing_polished(show_polished)
        self._showing_polished = show_polished
        self._apply_toggle_style()

    def _rebuild_result_title(self) -> None:
        """由 _title_base / _title_preset / _title_status 三段拼出標題。

        C2 修法：取代 _append/_replace_result_title_suffix 的字串切割手術。
        單一入口、單一真相來源，preset 與 status 能共存不互相吃。
        """
        parts: list[str] = [self._title_base]
        segments: list[str] = []
        if self._title_preset:
            segments.append(self._title_preset)
        if self._title_status:
            segments.append(self._title_status)
        if segments:
            parts.append("  · " + "  · ".join(segments))
        self._result_title.configure(text="".join(parts))

    def _do_auto_paste(self, text: str, target: str) -> None:
        """主執行緒：呼叫 auto_paste，完成後以 toast 通知使用者。

        必須在主執行緒呼叫——macOS 26.4+ TSM 對 pynput keyboard.Controller
        強制主執行緒（背景 thread 會觸發 dispatch_assert_queue_fail 閃退）。
        典型耗時 ~380ms（osascript activate + 180ms 緩衝 + ⌘V），可接受的
        UI 短暫凍結，遠優於閃退。詳見 CLAUDE.md §9.6。
        """
        # v2.19.0：pipeline event「paste_dispatch」+ 量測 paste latency
        _pipe_event("paste_dispatch", target_app=target or "")
        # v2.20.3 N3：milestone「paste_start」（dispatch ⌘V 之前的瞬間）
        try:
            import pipeline_id as _pid  # noqa: PLC0415
            _pid.record_event("paste_start")
        except Exception:
            pass
        _t_paste_start = time.perf_counter()
        success = _ap.paste_to_app(text, target)
        _paste_latency = time.perf_counter() - _t_paste_start
        _pipe_record_paste_latency(_paste_latency)
        # v2.20.3 N3：milestone「paste_complete」
        try:
            import pipeline_id as _pid  # noqa: PLC0415
            _pid.record_event("paste_complete")
        except Exception:
            pass

        if success:
            # v2.27.0：綠色語意收窄後唯一的落點——這是真正的成功事件。
            # 900ms（比預設 2800ms 短）：只是一個確認回饋，不需要長時間佔著畫面。
            self._show_toast(f"⌨  已貼入 {target}", color=SUCCESS, duration_ms=900)
        else:
            self._show_toast("⌨  自動貼上失敗（請確認輔助使用權限）")
        # Pipeline 終點：emit 完整 timing summary（不論成功 / 失敗都要記）
        self._emit_pipeline_timing(paste_outcome="success" if success else "failed",
                                   paste_target=target,
                                   text_len=len(text))
        # v2.19.0：整條 pipeline 結束、清掉 thread-local pid
        # （raw paste 策略下 emit 會 defer、pipeline_done 也要 defer 以對齊；
        #  但 defer 結束會走到 _finish_polish 內 emit、那條路徑也清；
        #  此處走通常路徑、只要 _pipeline_summary_emitted 為 True 就清）
        if self._pipeline_summary_emitted:
            _pipe_event("pipeline_done")
            _pipe_clear()

    def _emit_pipeline_timing(
        self,
        paste_outcome: str = "skipped",
        paste_target: Optional[str] = None,
        text_len: int = 0,
        failure_reason: Optional[str] = None,
    ) -> None:
        """輸出「按下結束熱鍵 → 貼上完成」端到端 pipeline 拆解 log。

        分四個階段，皆相對於 `_pipeline_t0`（按下結束的那一刻）：
          transcribe   ：轉譯完成
          polish       ：潤飾完成（無 AI 時為 None）
          paste        ：貼上完成（無 auto_paste 時為當下呼叫點）
          total        ：以最後階段為準

        路徑：
          no_ai           ：純轉譯 → 貼上
          with_ai_polish  ：轉譯 → AI 潤飾 → 貼上
          failed_*        ：v2.18.2 新增。如 failed_asr / failed_asr_timeout
                            （ASR 失敗或超時、polish 從未跑、log 仍有資料）

        Args:
            paste_outcome:  "success" / "failed" / "skipped"
            paste_target:   貼上目標 App 名稱（沒貼則 None）
            text_len:       最終輸出字數
            failure_reason: 失敗原因（如 "asr" / "asr_timeout"）；非 None 時
                            path 標 "failed_{reason}"、polish_s 設 0
        """
        if self._pipeline_summary_emitted:
            return   # 防重複（例如 polish + paste 兩段都觸發）
        if self._pipeline_t0 is None:
            return   # 某些 edge case（手動觸發 polish 沒有 pipeline）
        # v2.18.2：raw paste 策略 defer emit、第一次 paste 不算結束
        # 等 polish 完成（_finish_polish）再一併 emit
        if self._pipeline_defer_emit and failure_reason is None:
            # 暫存 paste 結果、等 polish emit 時帶上
            self._pipeline_deferred_paste = dict(
                paste_outcome=paste_outcome,
                paste_target=paste_target,
                text_len=text_len,
            )
            return
        self._pipeline_summary_emitted = True

        now      = time.perf_counter()
        t_total  = now - self._pipeline_t0
        t_trans  = self._pipeline_transcribe_at
        t_polish = self._pipeline_polish_at

        # 各階段耗時（不是相對 t0，而是該階段自己花了多久）
        transcribe_s = t_trans if t_trans is not None else 0.0
        if failure_reason is not None:
            polish_s = 0.0
            paste_s  = 0.0
            path     = f"failed_{failure_reason}"
        elif t_polish is not None and t_trans is not None:
            polish_s = t_polish - t_trans
            paste_s  = t_total  - t_polish
            path     = "with_ai_polish"
        else:
            polish_s = 0.0
            paste_s  = t_total - (t_trans if t_trans is not None else 0.0)
            path     = "no_ai"

        log_action(
            "pipeline_timing",
            path=path,
            transcribe_s=f"{transcribe_s:.2f}",
            polish_s=f"{polish_s:.2f}",
            paste_s=f"{paste_s:.2f}",
            total_s=f"{t_total:.2f}",
            paste_outcome=paste_outcome,
            paste_target=paste_target or "",
            text_len=text_len,
            polish_mode=self._pipeline_polish_mode,
        )
        # v2.20.3 N3：拿 thread-local timeline 各 milestone（ms）一起 emit。
        # 同時把 durations 餵給 session_summary 累積、session 結束時印中位數。
        durations: dict = {}
        try:
            import pipeline_id as _pid  # noqa: PLC0415
            durations = _pid.get_durations()
            if durations:
                try:
                    import session_summary  # noqa: PLC0415
                    session_summary.record_pipeline_timing(durations)
                except Exception:
                    pass
        except Exception:
            durations = {}

        # v2.19.0：pipeline 終點 event + 清 thread-local pid
        # 任何 emit 路徑（success / skipped / failed_*）都會走到這、統一收尾。
        # 注意：_do_auto_paste 也會呼叫 _pipe_event("pipeline_done") + _pipe_clear()，
        # 重複呼叫無害（set_current(None) 是 idempotent）。
        _pipe_event(
            "pipeline_done",
            path=path,
            total_s=round(t_total, 3),
            text_len=text_len,
            durations=durations,
        )
        # v2.20.3 N3：清 timeline（與 thread-local pid 同步釋放）
        try:
            import pipeline_id as _pid  # noqa: PLC0415
            _pid.clear_events()
        except Exception:
            pass
        _pipe_clear()

    def _display_result(self, result: TranscriptionResult) -> None:
        """新增一段結果，取代之前在 textbox 累積插入文字的做法。

        Fix 19 Path B / 2026-05-23：
          • 每段獨立 block（時間戳 / 時長 / 文字 / 動作圖示）
          • 自動把前一段的「最新」邊框取消
          • 新 block pack 在頂端 + 自動 scroll-to-top 讓使用者看到
          • 標題仍顯示「最近一段的元資料」（時長 / 語言 / 模型）

        v2.28.0：skeleton 模式（cfg.record_visual != "chamber"）分派給
        _display_result_skeleton（新版 UtteranceBlockV2 卡片，含轉錄中骨架卡
        收尾、失敗紅卡、時間分隔線）；chamber 逃生門走下面原封不動的舊路徑。
        """
        dur   = float(result.duration_seconds)
        lang  = (result.language.upper() if result.language else "?")
        model = self._model_var.get()
        # C2：重設三段式標題狀態（以最新段為準）
        self._title_base   = f"轉錄結果  ({int(dur)}s · {lang} · {model})"
        self._title_preset = None
        self._title_status = None
        self._rebuild_result_title()

        if self._skeleton_mode:
            self._display_result_skeleton(result, dur=dur, lang=lang, model=model)
            return

        # ── chamber 逃生門：原封不動的舊路徑 ──────────────────────────────
        # 清掉佔位符
        self._clear_placeholder()

        # Bug 3（v2.8.0 / 2026-05-23）：_utterance_blocks 反轉為「新→舊」順序，
        # [0] = 最新、[-1] = 最舊（與 UI 視覺順序一致：最新在最上）。
        # 把前一段「最新」邊框取消（[0] = 上一段最新）。
        if self._utterance_blocks:
            self._utterance_blocks[0].highlight_as_latest(False)

        # 建立新 block 並 pack 在最頂端
        import datetime as _dt
        timestamp_iso = _dt.datetime.now().strftime("%H:%M")
        block = UtteranceBlock(
            self._blocks_container,
            raw_text=result.text,
            timestamp_iso=timestamp_iso,
            duration_s=dur,
            language=lang,
            model=model,
            on_copy=self._on_block_copy,
            on_delete=self._on_block_delete,
        )
        # pack(before=existing_first) 插到現有最頂端 widget 之前
        if self._utterance_blocks:
            block.pack(
                fill="x", padx=SPACE_XS, pady=(0, 8),
                before=self._utterance_blocks[0],
            )
        else:
            block.pack(fill="x", padx=SPACE_XS, pady=(0, 8))
        block.highlight_as_latest(True)
        self._utterance_blocks.insert(0, block)

        # D3-S5（v2.10.0 / 2026-05-23）：auto-scroll 兩段式
        # 第一段：強制 layout 跑完（update_idletasks）→ 確保 _parent_canvas
        # 已知道新 block 的高度。
        # 第二段：after(150ms) 再 scroll 一次保險（macOS Tk 某些版本第一輪
        # idle 還沒 commit layout 就 scroll 會跳到舊位置）。Bug 3 反轉後現在
        # scroll 到頂（moveto 0.0），但 layout 沒算完 scroll 還是失效。
        def _scroll_to_top():
            try:
                self._blocks_container.update_idletasks()
                self._blocks_container._parent_canvas.yview_moveto(0.0)
            except Exception:
                pass
        # 立刻試一次（多數情況夠）
        self.after(0, _scroll_to_top)
        # 150ms 後再試一次（保險：layout 慢於預期）
        self.after(150, _scroll_to_top)
        self._refresh_stream_stats()

    # ── Aperture 卡片路徑（skeleton 模式限定）──────────────────────────────

    _FAIL_TEXTS = (
        "（轉錄失敗，請查看 log）",
        "（轉錄超時，請重試）",
    )

    def _display_result_skeleton(
        self, result: TranscriptionResult, *, dur: float, lang: str, model: str,
    ) -> None:
        """skeleton 模式的 _display_result：優先重用 _transition_to_processing
        建立的轉錄中骨架卡（_pending_block），把它就地填成 ready／failed，
        避免「骨架卡消失、新卡片彈出」的版面跳動；沒有 pending block（例如
        測試直接呼叫、或 pending 已被其他流程清掉）才新建一張。
        """
        self._clear_placeholder()

        import datetime as _dt
        now = _dt.datetime.now()
        timestamp_iso = now.strftime("%H:%M")
        epoch = now.timestamp()

        is_fail = result.text in self._FAIL_TEXTS

        if self._pending_block is not None:
            block = self._pending_block
            self._pending_block = None
        else:
            block = UtteranceBlockV2(
                self._blocks_container, on_copy=self._on_block_copy, on_save=self._on_block_save,
            )

        block.timestamp_iso = timestamp_iso
        if is_fail:
            block.set_failed(result.text)
        else:
            corrections = list(dict.fromkeys(getattr(result, "corrections", None) or []))
            block.set_result(
                raw_text=result.text, timestamp_iso=timestamp_iso,
                duration_s=dur, language=lang, model=model, corrections=corrections,
            )

        self._stream_insert_latest_block(block, epoch)

    def _stream_insert_latest_block(self, block, epoch: float) -> None:
        """把 block 插入 _blocks_container 最頂端、標為最新、必要時插入時間
        分隔線（相鄰兩段間隔 > 10 分鐘）。供 _display_result_skeleton 與
        _repolish_from_history 共用（都是 skeleton 模式限定）。
        """
        block.timestamp_epoch = epoch
        prev_top = self._utterance_blocks[0] if self._utterance_blocks else None

        if prev_top is not None:
            gap_s = epoch - getattr(prev_top, "timestamp_epoch", epoch)
            prev_top.highlight_as_latest(False)
            if gap_s > 600:
                sep = self._make_time_separator(gap_s)
                sep.pack(fill="x", pady=(0, 8), before=prev_top)
                self._time_separators.append(sep)
                block.pack(fill="x", pady=(0, 8), before=sep)
            else:
                block.pack(fill="x", pady=(0, 8), before=prev_top)
        else:
            block.pack(fill="x", pady=(0, 8))

        block.highlight_as_latest(True)
        self._utterance_blocks.insert(0, block)

        # 兩段式 auto-scroll（同 legacy 路徑的既有做法，見上方 _scroll_to_top）
        def _scroll_to_top():
            try:
                self._blocks_container.update_idletasks()
                self._blocks_container._parent_canvas.yview_moveto(0.0)
            except Exception:
                pass
        self.after(0, _scroll_to_top)
        self.after(150, _scroll_to_top)
        self._refresh_stream_stats()

    def _make_time_separator(self, gap_s: float) -> ctk.CTkFrame:
        """時間分隔：高 20、左右 1pt HAIR 線、中間文字（間隔 N 分鐘/小時）。

        letter-spacing .12em 未實作——tkinter 無原生支援，且用細空格模擬
        字距會讓數字（分鐘數）可讀性變差，改用正常字距。
        """
        row = ctk.CTkFrame(self._blocks_container, fg_color="transparent", height=20)
        row.pack_propagate(False)

        m = max(1, int(gap_s // 60))
        label = f"間隔 {m} 分鐘" if m < 60 else f"間隔 {m // 60} 小時"

        ctk.CTkFrame(row, height=1, fg_color=HAIR).pack(
            side="left", fill="x", expand=True, padx=(0, 10)
        )
        ctk.CTkLabel(
            row, text=label,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 10), text_color=TEXT_4,
        ).pack(side="left")
        ctk.CTkFrame(row, height=1, fg_color=HAIR).pack(
            side="left", fill="x", expand=True, padx=(10, 0)
        )
        return row

    # ═══════════════════════════════════════════════════════════════════════
    #  AMBIENT CHAMBER — render loop + draw + events
    # ═══════════════════════════════════════════════════════════════════════

    def _render_tick(self) -> None:
        """主渲染迴圈——動態 tick rate、依 state + 視窗可見性節省 CPU。

        v2.17.3：原本固定 50ms (20 FPS)、閒置 + 視窗最小化時仍狂吃 CPU
        （user 實機看到 Whisper Pro 閒置 33% CPU）。改成：
          • idle + 視窗 iconic/withdrawn → 1000ms（看不到、最低 1 FPS）
          • idle + 視窗顯示 + reduce_motion → 500ms（呼吸週期 6s、肉眼無感）
          • idle + 視窗顯示 + 動畫開 → 200ms（5 FPS、呼吸還是平滑）
          • recording → 50ms（RMS 電平要 20 FPS）
          • processing → 50ms（12 格旋轉、20 FPS）

        預期 CPU 改善：idle 33% → 5-10%。
        """
        try:
            self._draw_chamber()
        except tk.TclError:
            # App 關閉時 Canvas 已被銷毀，靜默結束迴圈
            return

        # 動態 interval 選擇
        state = self._state
        if state == "recording" or state == "processing":
            tick = RENDER_TICK_MS   # 50ms = 20 FPS（要看到 RMS / 旋轉）
        else:
            # idle 狀態：依視窗可見性 + 動畫偏好降頻
            try:
                wm_state = self.winfo_toplevel().wm_state()
                window_visible = wm_state == "normal"
            except Exception:
                window_visible = True   # 取不到視窗狀態安全假設可見
            if not window_visible:
                tick = 1000   # 看不到、1 FPS 撐著就好
            elif self._reduce_motion:
                tick = 500    # reduce-motion 偏好 → 大幅降頻
            else:
                tick = 200    # 一般 idle → 5 FPS（呼吸 6s 週期仍平滑）

        self.after(tick, self._render_tick)

    def _draw_chamber_bars(self) -> None:
        """三態統一的 Aperture bar 視覺（46 bar，idle／recording／processing 共用）。

        只在 cfg.record_visual != "chamber" 時被 _draw_chamber() 呼叫（逃生
        門）。v2.28.0 主視窗骨架重寫後，這支函式畫的是 StatusSlot 的壓扁版
        LevelMeter（640×32，見 LEVEL_METER_* 常數），不再是舊版 712×160 大
        record card——固定 big=False／show_highlight=False（32px 高度畫不下
        峰值帽與白色高光，會糊成一團），bar:gap 密度也改用 LEVEL_METER_GAP
        （9:5，比舊版更疏）。同一個 canvas、同一份 46 bar 幾何、同一支
        _draw_aperture_bars()，三態的差異只在「bar 高度從哪來」與「顏色怎麼查」：

          idle       —— engine 用 rms=0 持續 update()，bars 落在行波地板附近
                        （呼吸律動），顏色查 idle_color（無彩度）。
          recording  —— engine 用麥克風 RMS 持續 update()（v2.25.0 已完成），
                        顏色查 energy_color（能量色溫）。
          processing —— 不再呼叫 update()，直接讀「錄音剛結束那一刻」的
                        凍結快照 self._frozen_bars；顏色依「有沒有被掠掃
                        光帶掃到」決定（process_color 三級亮度 / WAVE_DIM）。
        """
        now = time.perf_counter()
        c   = self._chamber
        c.delete("all")

        state = self._state
        elapsed_ms = (now - self._state_start_time) * 1000.0

        if state == "processing":
            values = self._frozen_bars if self._frozen_bars is not None \
                else [0.0] * WAVE_N_BARS_MAIN
            progress = _processing_sweep_progress(
                elapsed_ms, self._proc_sweep_use_real, self._proc_sweep_known_frac,
            )

            def color_fn(i, v, _progress=progress):
                return _processing_bar_color(i, v, WAVE_N_BARS_MAIN, _progress)

            peaks, clipping, show_highlight = None, False, False
        else:
            rms = (
                self.recorder.get_rms_level()
                if state == "recording" and self.recorder.is_recording()
                else 0.0
            )
            dt_ms = max(0.0, (now - self._wave_last_tick) * 1000.0)
            self._wave_last_tick = now
            self._wave_engine.update(rms, dt_ms)
            values = self._wave_engine.bars

            if state == "recording":
                # 色溫吃「整體音量」而不是「該根 bar 的高度」。
                #
                # 原本是 `energy_color(v)`（每根 bar 各自依高度查色）。在頻譜
                # 改成中心對稱之前，最高的 bar 永遠在最左邊，看起來像一條由左
                # 到右的漸層、還算合理；改成中央最高之後，同一段條就變成
                # 「兩端青、中間橘」的彩虹——而琥珀／橘在這套設計裡的語意是
                # **削波警告**，等於普通音量就在對使用者謊報破音。
                #
                # 改吃 rms 之後，整條同一個色溫、只有高度在動，語意才對得上：
                # 顏色＝多大聲、高度＝頻譜形狀。大波形（GlowWaveRenderer）從
                # 一開始就是這個規則，兩邊現在一致。
                _lvl_col = energy_color(rms)
                color_fn = lambda i, v, _c=_lvl_col: _c  # noqa: E731
                # show_highlight 固定 False（見本函式 docstring：壓扁版 32px
                # 高度畫不下白色高光）——tuple 第三個位置沿用既有結構、只是
                # 值不再是 True，維持跟 idle 分支同一種「三元組」讀法。
                peaks, clipping, show_highlight = (
                    self._wave_engine.peaks, self._wave_engine.clipping, False,
                )
                # 大波形只在顯示時才畫（不隱藏時就別再多做這份工——見
                # _big_wave_active 的旗標定義）。讀的是上面 update() 之後
                # 同一幀的 engine 狀態，不重複呼叫 update()。
                if self._big_wave_active:
                    self._update_big_wave(rms)
            else:  # idle
                # 閒置行波振幅：把引擎的「錄音態地板」1.6%→3.0% 線性映射到
                #   設計規格的閒置範圍 1.6%→4.6%（Aperture 第二輪 2z 表格）。
                #   為什麼不改引擎公式：那條 0.016 + 0.014×sin 是參考實作給
                #   **錄音態**用的「bar 不歸零」地板，兩者用途不同；改引擎會
                #   連帶動到錄音態的視覺。只在 idle 分支重映射最外科手術。
                #   注意 max(0.0, ...)：引擎在衰減期 bar 會短暫低於地板值，
                #   直接放大會變負數（實測 -0.82%）。只放大「高於地板」的部分。
                values = [
                    WAVE_IDLE_BASE
                    + max(0.0, v - WAVE_IDLE_BASE) * WAVE_IDLE_SWING_SCALE
                    for v in values
                ]
                color_fn = lambda i, v: idle_color(v)  # noqa: E731
                peaks, clipping, show_highlight = None, False, False

        _draw_aperture_bars(
            c, WAVE_N_BARS_MAIN, LEVEL_METER_DRAW_W, self._chamber_h,
            values, color_fn,
            big=False, show_highlight=show_highlight,
            peaks=peaks, clipping=clipping,
            elapsed_ms=elapsed_ms,
            gap=LEVEL_METER_GAP, x0=LEVEL_METER_X0,
        )

    # ── 大波形（錄音態專屬，v2.28.0）────────────────────────────────────────

    def _show_big_wave(self) -> None:
        """錄音開始：疊大波形蓋住轉錄流卡片。

        呼叫端（_transition_to_recording）已經用 `if self._skeleton_mode:`
        擋過——chamber 逃生門沒有 _big_wave_canvas 這顆 widget（見
        _build_stream_v2，只有新骨架會建），這裡不重複判斷。
        """
        # 疊在轉錄流的**外層** frame 上，整塊蓋滿（relwidth/relheight 都是 1）。
        #
        # 為什麼不是疊在 `self._blocks_container` 上：CTkScrollableFrame 交給你
        # 的是「裡層」frame，高度會跟著內容縮——錄音時裡面幾乎沒東西，實測只剩
        # 244pt（版面配置是 718pt），疊上去會被頂到畫面上三分之一。外層
        # `_parent_frame` 才是真正佔住版面那一格的 widget（實測 y=113 高=718）。
        #
        # 為什麼是「蓋滿」而不是「把轉錄流 pack_forget 掉讓位」：後者實測連續
        # 踩到三個 Tk pack 的坑——① pack() 回來會排到順序最後，轉錄流跑到底列
        # 後面、整塊位移 53pt ② CTkScrollableFrame 的 winfo_manager() 不轉發給
        # 外層，防重複 pack 的判斷式永遠成立 ③ 轉錄流一消失，底列就往上遞補到
        # 畫面頂端。蓋一張不透明畫布完全不動版面，這三個坑一次消失。
        # 波形本身只佔中間 BIG_WAVE_H 一條（renderer 的 band_h），其餘是底色。
        self._big_wave_canvas.place(
            in_=self._blocks_container._parent_frame,
            relx=0.5, rely=0.5, anchor="center", relwidth=1.0, relheight=1.0,
        )
        # 不能用 self._big_wave_canvas.lift()／.tkraise()：tkinter 的
        # Canvas 把這兩個名字都改綁成 tag_raise()（畫布「物件」疊放順序，
        # 要吃 tagOrId 參數），蓋掉了 Misc 原本「視窗」疊放順序的版本，
        # 呼叫不帶參數會直接炸 TclError。要疊到 _blocks_container 上面，
        # 得繞開 Canvas 的覆寫、直接呼叫 Misc 版本。
        tk.Misc.lift(self._big_wave_canvas)

        self._big_wave_active = True

    def _hide_big_wave(self) -> None:
        """收回大波形。

        先關旗標再 place_forget()：_draw_chamber_bars 靠 _big_wave_active
        判斷要不要多畫這一份，關掉旗標才是真的「停止重繪」，不是只把
        canvas 藏起來但背景仍在空跑（place_forget() 本身不會讓
        _draw_chamber_bars 停止呼叫 _update_big_wave）。
        """
        self._big_wave_active = False
        self._big_wave_canvas.place_forget()

    def _update_big_wave(self, rms: float) -> None:
        """畫大波形的一幀。只從 _draw_chamber_bars() 的 recording 分支、
        且 self._big_wave_active 為 True 時呼叫——讀的是同一幀
        self._wave_engine.update() 之後的狀態，不重複呼叫 update()（見
        _build_stream_v2「大波形」註解：兩邊各自 update 會漸漸不同步）。
        """
        w = self._big_wave_canvas.winfo_width()
        h = self._big_wave_canvas.winfo_height()
        if w <= 1 or h <= 1:
            # 剛 place() 完、geometry manager 這一輪還沒跑到這顆 canvas，
            # 量到的是預設 1×1——跳過這一幀，下一次 tick 幾乎立刻就會補上，
            # 不值得為了這個等 update_idletasks()。
            return
        if (
            self._big_wave_renderer is None
            or self._big_wave_renderer.width != w
            or self._big_wave_renderer.height != h
        ):
            # 只有尺寸變了（首次顯示／視窗被拉寬）才重建，不是每幀都重建。
            # band_h=BIG_WAVE_H：畫布蓋滿整個轉錄流區（遮住底下卡片），
            # 但波形本身只佔中間這一條——不然 700pt 高的 bar 會變成一面牆。
            self._big_wave_renderer = GlowWaveRenderer(
                w, h, n_bars=WAVE_N_BARS_MAIN, band_h=BIG_WAVE_H,
            )
        img = self._big_wave_renderer.render(
            self._wave_engine.bars, level=rms, clipping=self._wave_engine.clipping,
        )
        self._big_wave_photo = ImageTk.PhotoImage(img)
        if self._big_wave_image_id is None:
            self._big_wave_image_id = self._big_wave_canvas.create_image(
                0, 0, anchor="nw", image=self._big_wave_photo
            )
        else:
            self._big_wave_canvas.itemconfig(
                self._big_wave_image_id, image=self._big_wave_photo
            )

    def _draw_chamber(self) -> None:
        """Render ambient rings + central disc + icon for the current state."""
        # v2.27.0 三態統一：bar 模式時 idle／recording／processing 三態全部
        # 走 _draw_chamber_bars()（同一個 canvas、同一支繪圖函式）。early
        # return 讓 cfg.record_visual == "chamber" 逃生門完全沿用下面原本
        # 的舊三態光場程式碼，一行新程式碼都不執行。
        if self._wave_engine is not None:
            self._draw_chamber_bars()
            return

        now = time.perf_counter()
        c   = self._chamber
        c.delete("all")

        state = self._state
        rm    = self._reduce_motion

        # ─── 依狀態決定配色與幾何參數 ───────────────────────────────────────
        if state == "idle":
            color  = ACCENT
            period = BREATHE_IDLE_MS / 1000.0
            alphas = RING_ALPHA_IDLE
            radii  = RING_RADII_5
            rms    = 0.0
        elif state == "recording":
            color  = DANGER
            period = BREATHE_RECORDING_MS / 1000.0
            alphas = RING_ALPHA_RECORDING
            radii  = RING_RADII_5
            rms    = self.recorder.get_rms_level() if self.recorder.is_recording() else 0.0
        elif state == "processing":
            color  = WARN
            period = BREATHE_PROCESSING_MS / 1000.0
            alphas = RING_ALPHA_PROCESSING
            radii  = RING_RADII_4
            rms    = 0.0
        else:
            return

        # ─── 呼吸縮放係數（reduce-motion 模式下固定為 1.0）─────────────────
        if rm:
            scale = 1.0
        else:
            phase = ((now - self._state_start_time) % period) / period
            b     = breathe(phase)
            if state == "idle":
                scale = 0.97 + b * 0.06                        # 0.97 ↔ 1.03
            elif state == "recording":
                scale = 1.0 + b * 0.04 + rms * RMS_EXPAND_GAIN
            else:  # processing
                scale = 0.98 + b * 0.04

        if self._pressed and state == "idle":
            scale *= 0.97

        cx = cy = CHAMBER_CENTER

        # ─── 漣漪效果（僅錄音狀態 + 非減少動態模式）────────────────────────
        if state == "recording" and not rm:
            if rms > RMS_RIPPLE_THR and rms > self._prev_rms * 1.5:
                self._ripples.append(Ripple(
                    start_time=now,
                    duration=RIPPLE_DURATION,
                    r0=RIPPLE_R0, r1=RIPPLE_R1, a0=RIPPLE_ALPHA0,
                ))
                if len(self._ripples) > RIPPLE_MAX:
                    self._ripples = self._ripples[-RIPPLE_MAX:]

            alive: list[Ripple] = []
            for rip in self._ripples:
                st = rip.state(now)
                if st is None:
                    continue
                r_rip, a_rip = st
                col = blend(DANGER, SURF_1, a_rip)
                c.create_oval(
                    cx - r_rip, cy - r_rip, cx + r_rip, cy + r_rip,
                    outline=col, width=RING_STROKE,
                )
                alive.append(rip)
            self._ripples = alive
        self._prev_rms = rms

        # ─── 環境同心圓（由外到內繪製，讓內圓覆蓋在外圓上方）──────────────
        for radius, a in zip(reversed(radii), reversed(alphas)):
            r = radius * scale
            col = blend(color, SURF_1, a)
            c.create_oval(
                cx - r, cy - r, cx + r, cy + r,
                outline=col, width=RING_STROKE,
            )

        # ─── 處理中旋轉粒子環 ────────────────────────────────────────────────
        if state == "processing":
            outer_r = radii[-1] * scale
            if rm:
                head = 0.0
            else:
                t_norm = ((now - self._state_start_time) %
                          (ROTATE_PROCESSING_MS / 1000.0)) \
                         / (ROTATE_PROCESSING_MS / 1000.0)
                head = ease_in_out_cubic(t_norm) * 360.0

            pr = PROC_PARTICLE_RADIUS
            for i in range(PROC_PARTICLES):
                ang = math.radians(head + i * (360.0 / PROC_PARTICLES))
                px = cx + outer_r * math.cos(ang)
                py = cy + outer_r * math.sin(ang)
                # 亮度漸層：領頭粒子最亮，沿環方向逐漸暗淡（彗星尾巴效果）
                idx_from_head = (i % PROC_PARTICLES) / PROC_PARTICLES
                a_p = 1.0 - idx_from_head * 0.85              # 1.0 → 0.15
                if rm:
                    a_p = 0.6
                col_p = blend(WARN, SURF_1, a_p)
                c.create_oval(
                    px - pr, py - pr, px + pr, py + pr,
                    fill=col_p, outline="",
                )

        # ─── 中央可點擊圓盤 ──────────────────────────────────────────────────
        dr = DISC_RADIUS * (scale if self._pressed and state == "idle" else 1.0)
        if state == "idle":
            disc_fill   = SURF_2
            disc_border = ACCENT if self._hovering else SURF_4
            disc_width  = 1.5
        elif state == "recording":
            disc_fill   = blend(DANGER, SURF_1, 0.12)
            disc_border = DANGER
            disc_width  = 2.0
        else:  # processing
            disc_fill   = SURF_2
            disc_border = WARN
            disc_width  = 1.5

        c.create_oval(
            cx - dr, cy - dr, cx + dr, cy + dr,
            fill=disc_fill, outline=disc_border, width=disc_width,
        )

        # ─── 中央狀態圖示（閒置=麥克風、錄音=停止方塊、處理中=暗麥克風）────
        if state == "idle":
            icon = self._icon_mic_idle
        elif state == "recording":
            icon = self._icon_square
        else:
            icon = self._icon_mic_proc

        c.create_image(cx, cy, image=icon)

    # ── Canvas event handlers ────────────────────────────────────────────

    def _in_disc(self, x: int, y: int) -> bool:
        """以畢氏定理判斷座標是否落在可點擊熱區內（半徑 DISC_RADIUS，圓心
        永遠是目前 canvas 的正中央）。

        v2.27.0：三態統一後 canvas 寬高依模式不同（bar 模式 712×160、chamber
        逃生門 280×280），熱區圓心不能再寫死 CHAMBER_CENTER——改用實際
        self._chamber_w / _chamber_h 算中心。chamber 模式數值不變
        （280/2 = 140 = CHAMBER_CENTER），行為 100% 相容、無回歸。
        """
        dx = x - self._chamber_w / 2
        dy = y - self._chamber_h / 2
        return dx * dx + dy * dy <= DISC_RADIUS * DISC_RADIUS

    def _on_chamber_enter(self, event) -> None:
        """滑鼠進入 Canvas 時，若進入 disc 且為 idle 狀態則設 hover 效果。"""
        self._hovering = self._in_disc(event.x, event.y) and self._state == "idle"

    def _on_chamber_leave(self, event) -> None:
        """滑鼠離開 Canvas 時清除 hover / pressed 狀態與自訂游標。"""
        self._hovering = False
        self._pressed  = False
        try:
            self._chamber.configure(cursor="")
        except tk.TclError:
            pass

    def _on_chamber_motion(self, event) -> None:
        """滑鼠在 Canvas 移動時，動態切換 hover 狀態與手形游標。"""
        inside = self._in_disc(event.x, event.y)
        self._hovering = inside and self._state == "idle"
        try:
            self._chamber.configure(cursor="hand2" if self._hovering else "")
        except tk.TclError:
            pass

    def _on_chamber_press(self, event) -> None:
        """滑鼠在 disc 內按下時設 pressed 旗標（視覺按壓回饋）。"""
        # idle → 即將開始錄音；recording → 即將停止（轉 processing）
        if self._in_disc(event.x, event.y) and self._state in ("idle", "recording"):
            self._pressed = True
        print(f"CHAMBER: press  at ({event.x},{event.y}) state={self._state} pressed={self._pressed}")

    def _on_chamber_release(self, event) -> None:
        """滑鼠在 disc 內放開時觸發錄音切換（按下且放開都在 disc 內才算點擊）。"""
        was_pressed = self._pressed
        self._pressed = False
        print(f"CHAMBER: release at ({event.x},{event.y}) state={self._state} was_pressed={was_pressed}")
        # 只在「按下時命中 disc、放開時還在 disc 內、狀態允許」時觸發
        if (
            was_pressed
            and self._in_disc(event.x, event.y)
            and self._state in ("idle", "recording")
        ):
            self._on_record_btn()

    def _update_timer(self) -> None:
        """每秒更新錄音計時器標籤（MM:SS），非 recording 狀態自動停止。"""
        if self._state != "recording":
            self._timer_label.configure(text="")
            return
        elapsed = int(time.perf_counter() - self._rec_start)
        mm, ss  = divmod(elapsed, 60)
        self._timer_label.configure(text=f"{mm:02d}:{ss:02d}")
        # Phase 4.3 mini 視窗：同步計時器
        if self._mini_window is not None:
            self._mini_window.update_timer(elapsed)
        self.after(1000, self._update_timer)

    # ═══════════════════════════════════════════════════════════════════════
    #  EVENT HANDLERS
    # ═══════════════════════════════════════════════════════════════════════

    # Tail padding：停錄音前多等 N 毫秒，抓住使用者說完尾字前放開熱鍵/點按鈕
    # 的短暫餘音，避免最後一兩個字被切掉。
    TAIL_PADDING_MS = 300

    def _on_record_btn(self) -> None:
        """Chamber 點擊 → 依目前狀態切換錄音（idle↔recording）。"""
        log_action("record_button_clicked", state=self._state)
        if   self._state == "idle":
            self._transition_to_recording()
        elif self._state == "recording":
            # Tail padding — 多錄 300ms 再停，抓尾音避免漏字
            self.after(self.TAIL_PADDING_MS, self._try_stop)

    def _hotkey_tap(self) -> None:
        """HotkeyManager callback：tap 觸發（完整按下→放開算 1 次）。

        ⚠️ Fix 10 / 2026-05-23 — **絕對不能在這裡呼叫 self.after() 或任何 Tk 函式**。

        理由（PyObjC + Tk 同執行緒 GIL 衝突）：
          Fix 7 換成 NSEvent monitor 後，這個 callback 在「主執行緒 +
          PyObjC PyGILState_Ensure context」下被呼叫。Tk 用的是
          PyEval_SaveThread/RestoreThread 來管理 GIL，兩套 API 在同執行緒
          混用會讓 Tk 後續 Tcl_ServiceEvent 拿到 stale thread state →
          PyEval_RestoreThread 觸發 fatal_error → SIGSEGV at NULL+16。
          崩潰堆疊：Tcl_ServiceEvent → PythonCmd → PyEval_RestoreThread →
          fatal_error。100% 重現：每按一次熱鍵都崩。

        修法：純 Python 屬性遞增（GIL 已被 PyGILState_Ensure 拿著、不需要 Tk），
        實際 toggle 動作由 _poll_pending_tap 在「Tk mainloop iteration」context
        裡跑（PyEval 系列 GIL 管理乾淨）。20ms 輪詢延遲使用上感受不到。

        Tap toggle 語意：每次 tap 等同於點一下螢幕上的錄音鈕，
        idle → 開始錄音；recording → 停止；processing → 忽略。
        實際分支邏輯沿用 `_on_record_btn`，不在這裡重複實作。
        """
        # 純 Python int 遞增；不呼叫任何 Tk / PyObjC bridge / threading 同步原語
        self._pending_tap_count += 1
        # v2.14.1：標記「熱鍵剛剛按過」、抑制接下來 1.5s 內的 Cocoa BecomeActive
        # 自動 restore（避免按熱鍵時 minimized 視窗跳出來打擾 user）。
        # time.time() 是純 CPython 內部呼叫，無 Tk/PyObjC bridge，PyGIL context 安全。
        self._suppress_auto_restore_until = time.time() + 1.5

    def _poll_pending_tap(self) -> None:
        """Tk-side poller：在 mainloop iteration 中安全地把 NSEvent handler
        擱置的 tap 觸發實際分派到 _on_hotkey_tap。

        Fix 10 / 2026-05-23 配對：詳見 _hotkey_tap docstring。
        """
        try:
            count = self._pending_tap_count
            if count > 0:
                self._pending_tap_count = 0
                # 同一輪 poll 內最多 fire 1 次，避免使用者連按時把 toggle 跑成
                # 「start→stop→start」這種非預期狀態（每 20ms 跑一次已經夠快）
                self._on_hotkey_tap()
        except Exception:
            log_error("poll_pending_tap_failed")
        finally:
            self.after(self.PENDING_TAP_POLL_MS, self._poll_pending_tap)

    def _on_hotkey_tap(self) -> None:
        """主執行緒：快捷鍵 tap → 直接重用 chamber 按鈕的 toggle handler。

        v2.16.2 background recording fix（v2.16.1 deactivate-only 不足、user 實測
        仍 Space 切換）：
          根因：_transition_to_recording 內的 MiniHUD `deiconify()` 會讓 Tk 內部
          呼叫 `[NSApp activateIgnoringOtherApps:YES]` → macOS 排程 Space 切換
          帶 user 到 Whisper Pro 所在 Space。單純 NSApp.deactivate() 是「被動」
          動作、macOS 已經排程 Space switch 不會撤回。

          v2.16.2 修法：主動「重新 activate user 原本的 app」（NSRunningApplication.
          activateWithOptions_）。macOS 看到另一個 app 被 activate → 取消對我們
          的 Space switch、留 user 在原 app/Space。

          步驟：
            1. _on_record_btn 之前 snapshot frontmost（NSWorkspace 50ns、不阻塞）
            2. _on_record_btn 結束後 NSApp.deactivate() + 主動 prev_app.activate
            3. 若 prev_app 是 self（Whisper Pro 已在前景）跳過、不要 self-deactivate

          副作用（acceptable）：若 user 在 Whisper Pro 視窗按熱鍵但 frontmost
          抓不到自己（osascript / NSWorkspace 抓到 dev shell）→ 會被 deactivate
          → 焦點短暫跳走。罕見、recording 仍正常進行。
        """
        # Snapshot frontmost app BEFORE 任何 UI / activation 動作
        # （NSWorkspace.frontmostApplication() 是 in-process call、無 osascript 開銷）
        # IS_MAC 專屬：Windows 沒有 Space 概念，不需要 frontmost snapshot / re-activate。
        prev_app = None
        if IS_MAC:
            try:
                from AppKit import NSWorkspace
                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                if app:
                    name = (app.localizedName() or "")
                    # 排除自我（避免 self-reactivate 變成 no-op、或下方 deactivate 後反而把自己拉回來）
                    if "WhisperPro" not in name and "Whisper Pro" not in name:
                        prev_app = app
            except Exception:
                log_error("hotkey_snapshot_frontmost_failed")

        log_action("hotkey_triggered_toggle", combo=self.cfg.hotkey, state=self._state)
        self._on_record_btn()

        # 雙保險：deactivate 自己 + 主動 reactivate 原 app
        # 後者是關鍵——macOS 看到另一個 app 被明確 activate 才會取消對我們的 Space switch
        # IS_MAC 專屬：Windows 沒有 Space 切換問題，跳過。
        if IS_MAC:
            try:
                from AppKit import NSApplication
                NSApplication.sharedApplication().deactivate()
                if prev_app is not None:
                    # NSApplicationActivateAllWindows = 1 << 0 = 1（帶所有視窗回前）
                    # 0 表示不帶（更乾淨）；用 0 避免把 user app 額外視窗也拉前
                    prev_app.activateWithOptions_(0)
                    log.debug(f"BG_RECORD: re-activated '{prev_app.localizedName()}' to prevent Space switch")
            except Exception:
                log_error("nsapp_deactivate_failed")

    def _try_stop(self) -> None:
        """延遲後真的停錄音。只在仍為 recording 狀態才執行（防重入）。"""
        if self._state == "recording":
            self._transition_to_processing()

    def _on_copy(self) -> None:
        """複製按鈕：複製「最新一段」block 的目前顯示文字（原文或潤飾）。

        Fix 19 Path B / 2026-05-23 — block-based 重構後不再用 Tk mark，直接從
        `self._utterance_blocks[0]` 取目前顯示內容。語意跟 Path A 一致：
        每次錄完音點複製 = 拿剛剛那一段。
        """
        if not self._utterance_blocks:
            log_action("copy_clicked_empty")
            return
        latest = self._utterance_blocks[0]
        text = latest.get_current_text().strip()
        if not text:
            log_action("copy_clicked_empty")
            return
        try:
            import pyperclip
            pyperclip.copy(text)
            log_action("copy_succeeded", text_len=len(text), scope="latest_block")
            self._show_toast("已複製最後一段")
        except Exception as e:
            log_error("copy_failed", text_len=len(text))
            self._show_toast(f"複製失敗: {e}")

    # D3-S7（v2.10.0）：動態 wraplength ────────────────────────────────────

    def _on_blocks_container_resize(self, event=None) -> None:
        """blocks_container resize 時 debounce 80ms 後更新所有 block 的 wraplength。"""
        if self._wraplength_debounce_id is not None:
            try:
                self.after_cancel(self._wraplength_debounce_id)
            except Exception:
                pass
        self._wraplength_debounce_id = self.after(80, self._apply_wraplength_to_blocks)

    def _apply_wraplength_to_blocks(self) -> None:
        """計算當前 container 寬度並套用到所有 block 的 text label。

        Bug C（v2.13.0）：加 width-change guard 避免 layout 抖動。
        - winfo_width < 100：container 還沒 realized、不動
        - 與上次差距 < 10px：寬度沒實質變化、不動（避免每次 scroll 都觸發
          wraplength→block height→container Configure 死循環，scrollbar 被打斷）
        - 動作完 → 主動觸發 CTkScrollableFrame 內部 canvas 重算 scrollregion，
          確保 scrollbar 認得新的 content 高度
        """
        self._wraplength_debounce_id = None
        try:
            width = self._blocks_container.winfo_width()
            if width < 100:
                # 容器還沒 realized（初始化 / withdraw 時 winfo_width 可能 = 1）
                return
            new_wrap = max(280, width - (SPACE_XS * 2) - (SPACE_MD * 2) - 20)
            last_wrap = getattr(self, "_last_wraplength", -1)
            if abs(new_wrap - last_wrap) < 10:
                # 變動太小、skip 避免 layout 抖動
                return
            self._last_wraplength = new_wrap
            for blk in self._utterance_blocks:
                try:
                    blk.update_wraplength(new_wrap)
                except Exception:
                    pass
            # Bug C：主動觸發 CTkScrollableFrame 重新計算 scrollregion
            # block 高度變了，內部 canvas 必須知道才能正確顯示 scrollbar
            try:
                inner_canvas = self._blocks_container._parent_canvas
                inner_canvas.update_idletasks()
                inner_canvas.configure(scrollregion=inner_canvas.bbox("all"))
            except Exception:
                pass
        except Exception:
            log_error("blocks_wraplength_update_failed")

    def _on_block_copy(self, block: "UtteranceBlock") -> None:
        """Per-block 複製圖示：直接複製這個 block 的目前顯示文字。"""
        text = block.get_current_text().strip()
        if not text:
            return
        try:
            import pyperclip
            pyperclip.copy(text)
            log_action("copy_succeeded", text_len=len(text), scope="block")
            self._show_toast("已複製這一段")
        except Exception as e:
            log_error("copy_failed", text_len=len(text))
            self._show_toast(f"複製失敗: {e}")

    def _on_block_save(self, block: "UtteranceBlockV2") -> None:
        """Aperture 卡片存檔圖示：只存這一段目前顯示的文字（跟舊版全域「存
        檔」不同——那個存的是全部 block 串接，見 _on_save／_get_result_text）。
        """
        text = block.get_current_text().strip()
        if not text:
            log_action("save_clicked_empty", scope="block")
            return
        path = fd.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("文字檔", "*.txt"), ("所有檔案", "*.*")],
            title="儲存這一段",
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                log_action("save_succeeded", path=path, text_len=len(text), scope="block")
                self._show_toast("已儲存")
            except Exception as e:
                log_error("save_file_failed", path=path, scope="block")
                self._show_toast(f"儲存失敗: {e}")
        else:
            log_action("save_cancelled", scope="block")

    def _on_block_delete(self, block: "UtteranceBlock") -> None:
        """Per-block 刪除圖示：把這個 block 從畫面與列表中移除。

        若刪掉的剛好是最新 block 且還有其他 block → 把新的「最新」標示出來、
        重置 toggle / polish 狀態（_last_raw / _last_polished 對齊新的 latest）。
        若刪到剩 0 個 → 顯示佔位符 + 完整重置 toggle 狀態。
        """
        if block not in self._utterance_blocks:
            return
        was_latest = (block is self._utterance_blocks[0])
        self._utterance_blocks.remove(block)
        try:
            block.destroy()
        except Exception:
            pass
        log_action("block_deleted", remaining=len(self._utterance_blocks))
        self._refresh_stream_stats()

        if not self._utterance_blocks:
            # 全清空 → 重置標題、toggle、顯示 placeholder
            self._title_base   = "轉錄結果"
            self._title_preset = None
            self._title_status = None
            self._rebuild_result_title()
            self._last_raw         = ""
            self._last_llm_input   = None
            self._last_polished    = None
            self._showing_polished = True
            self._apply_toggle_style()
            self._show_placeholder()
            return

        if was_latest:
            # 新的最新一段 = 之前的倒數第二段
            new_latest = self._utterance_blocks[0]
            for b in self._utterance_blocks:
                b.highlight_as_latest(b is new_latest)
            # toggle / polish 狀態跟著對齊新的 latest
            self._last_raw         = new_latest.raw_text
            self._last_polished    = new_latest.polished_text
            self._showing_polished = new_latest.showing_polished
            self._apply_toggle_style()

    def _on_save(self) -> None:
        """存檔按鈕：開啟 Save As 對話框，將結果寫成 .txt 檔。"""
        text = self._get_result_text()
        if not text:
            log_action("save_clicked_empty")
            return
        path = fd.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("文字檔", "*.txt"), ("所有檔案", "*.*")],
            title="儲存轉錄結果",
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                log_action("save_succeeded", path=path, text_len=len(text))
                self._show_toast("已儲存")
            except Exception as e:
                log_error("save_file_failed", path=path)
                self._show_toast(f"儲存失敗: {e}")
        else:
            log_action("save_cancelled")

    def _on_clear(self) -> None:
        """清除按鈕：銷毀所有 UtteranceBlock、重置標題與 toggle、顯示佔位符。"""
        log_action("clear_clicked", blocks_cleared=len(self._utterance_blocks))
        # 銷毀所有 block 並清空列表
        for b in list(self._utterance_blocks):
            try:
                b.destroy()
            except Exception:
                pass
        self._utterance_blocks.clear()
        if self._skeleton_mode:
            # 時間分隔線不在 _utterance_blocks 裡，要另外清；轉錄中骨架卡若
            # 剛好還沒收尾（使用者在 processing 途中按清除）也一併砍掉——
            # _display_result_skeleton 發現 _pending_block 是 None 時會自己
            # 新建一張，不會因此炸掉。
            for sep in self._time_separators:
                try:
                    sep.destroy()
                except Exception:
                    pass
            self._time_separators.clear()
            if self._pending_block is not None:
                try:
                    self._pending_block.destroy()
                except Exception:
                    pass
                self._pending_block = None
        # 重置三段式標題
        self._title_base   = "轉錄結果"
        self._title_preset = None
        self._title_status = None
        self._rebuild_result_title()
        # 重置 toggle 狀態：清空後無最新段可切
        self._last_raw         = ""
        self._last_llm_input   = None
        self._last_polished    = None
        self._showing_polished = True
        self._apply_toggle_style()
        self._show_placeholder()
        self._refresh_stream_stats()

    def _toggle_auto_paste(self) -> None:
        """自動貼上按鈕：切換 auto_paste 開關並即時更新按鈕樣式。"""
        old = self.cfg.auto_paste
        self.cfg.auto_paste = not self.cfg.auto_paste
        self.cfg.save()
        log_settings("changed", field="auto_paste", old=old, new=self.cfg.auto_paste)
        on = self.cfg.auto_paste
        self._refresh_ap_btn_style()
        self._show_toast("自動貼上已開啟" if on else "自動貼上已關閉")

    def _refresh_ap_btn_style(self) -> None:
        """自動貼上按鈕外觀，依 self._skeleton_mode 分派——skeleton 走二態
        pill（形狀＋字元編碼，見 _style_pill）；chamber 逃生門維持舊版
        幽靈按鈕（實色 vs 描邊 + icon 變色）。共用於初始建置與切換後刷新，
        避免同一段配色邏輯散落在 _build_topbar(_v2) / _toggle_auto_paste /
        _on_settings_saved 三處各寫一份。"""
        on = self.cfg.auto_paste
        if self._skeleton_mode:
            self._style_pill(self._ap_btn, on, "自動貼上")
            return
        self._ap_btn.configure(
            fg_color=INDIGO if on else SURF_1,
            border_color=INDIGO if on else SURF_3,
            text_color=TEXT_1 if on else TEXT_3,
            hover_color=INDIGO_HV if on else SURF_2,
            image=get_icon("keyboard", 15, TEXT_1 if on else TEXT_3),
        )

    def _on_ollama(self) -> None:
        """手動按「潤飾」鈕：對「最新一段 block」執行一次潤飾。

        Fix 19 Path B / 2026-05-23 行為微調：原本「整坨結果替換」在 block 化
        UI 之後失去意義（沒有單一 textbox 可整坨替換）。改成跟 auto-polish
        一致 → 對最新 block 的 raw_text 跑潤飾、結果寫進該 block 的 polished_text。
        舊段落不動。
        """
        if not self.cfg.ollama_enabled:
            log_action("ollama_clicked_disabled")
            self._show_toast("AI 潤飾未啟用，請到「設定」開啟")
            return
        if self.ollama.health_ok is False:
            log_action("ollama_clicked_offline")
            self._show_toast("無法連線 Ollama 服務，請確認 ollama serve 已啟動")
            self._refresh_ollama_health()
            return
        if self._polish_busy:
            log_action("ollama_clicked_busy")
            self._show_toast("目前已在潤飾中，請稍候…")
            return
        if not self._utterance_blocks:
            log_action("ollama_clicked_empty")
            return

        latest = self._utterance_blocks[0]
        text = latest.raw_text.strip()
        if not text:
            log_action("ollama_clicked_empty")
            return
        log_action("ollama_clicked", text_len=len(text), scope="latest_block")

        # Fix Cluster B：抓 target_block ref + 增 generation、讓「polish 跑時又錄新音 / 刪掉
        # latest block」這類 race 跟自動 polish 用同一套 identity 保護。
        self._polish_generation += 1
        gen = self._polish_generation
        target_block = latest
        if self._skeleton_mode:
            target_block.set_polish_pending()

        self._ollama_btn.configure(state="disabled", text="處理中…")
        self._polish_busy = True

        def _run():
            result = self.ollama.process(text)
            self.after(0, _done, result)

        def _done(result):
            self._polish_busy = False
            # 用當前 cfg 重繪按鈕，避免 state 卡在 disabled
            self._apply_polish_button_style(
                enabled=self.cfg.ollama_enabled,
                healthy=(self.ollama.health_ok is True),
            )
            # Cluster B：generation 對齊（防新轉錄）+ block identity（防 delete / 不再 latest）
            if gen != self._polish_generation:
                log_action("manual_polish_dropped_gen_mismatch")
                return
            if result.error:
                self._show_toast(f"AI 潤飾失敗：{result.error}")
                if self._skeleton_mode and target_block in self._utterance_blocks:
                    target_block.set_polish_failed()
                return
            block_ok = (
                target_block in self._utterance_blocks
                and target_block is self._utterance_blocks[0]
            )
            if block_ok:
                target_block.set_polished(result.text)
                self._last_polished    = result.text
                self._showing_polished = True
                self._apply_toggle_style()
                self._show_toast(f"AI 潤飾完成 · {result.elapsed_seconds:.1f}s")
            else:
                log_action("manual_polish_dropped_block_changed")

        threading.Thread(target=_run, daemon=True).start()

    # ── Ollama 健康狀態管理 ─────────────────────────────────────────────────

    # ── #4 字典 ─────────────────────────────────────────────────────────────

    def _reload_dictionary(self) -> None:
        """從檔案重新載字典，同步更新 transcriber。"""
        if not self.cfg.dictionary_enabled:
            self.transcriber.set_dictionary_terms([])
            self._dictionary_terms: list[str] = []
            return
        path = self._dictionary_path()
        _dictionary.ensure_file(path)
        terms = _dictionary.load_terms(path)
        self._dictionary_terms = terms
        self.transcriber.set_dictionary_terms(terms)
        log.info(f"DICTIONARY: loaded {len(terms)} terms from {path}")

    def _dictionary_path(self):
        """回傳字典檔路徑（cfg 自訂或預設）。"""
        from pathlib import Path
        custom = (self.cfg.dictionary_path or "").strip()
        if custom:
            return Path(custom).expanduser()
        return _dictionary.DEFAULT_PATH

    def _refresh_ollama_health(self) -> None:
        """非同步探測 Ollama 服務，探測結果更新按鈕外觀。

        設計原則：永遠不在主執行緒做 HTTP I/O；callback 用 self.after(0) 切回。
        """
        if not self.cfg.ollama_enabled:
            # 沒啟用就不需要探測，直接把外觀設為「關閉」
            self._apply_polish_button_style(enabled=False, healthy=False)
            return

        def _on_result(ok: bool):
            # _on_result 在 requests 執行緒，UI 更新必須 marshal 回主執行緒
            self.after(0, lambda: self._apply_polish_button_style(
                enabled=self.cfg.ollama_enabled,
                healthy=ok,
            ))

        self.ollama.health_check_async(on_result=_on_result)

    def _apply_polish_button_style(self, enabled: bool, healthy: bool) -> None:
        """把潤飾按鈕依樣式規則畫出來。

        v2.28.0 骨架重寫：skeleton 模式走二態 pill——跟自動貼上 pill 同一種
        語意，pill 只反映「使用者有沒有打開這個功能」（enabled），不看
        healthy；服務離線的提醒改由 _on_ollama() 點擊當下的 toast 負責，
        不佔用 pill 的視覺語言（形狀＋字元編碼，沒有第三態）。chamber 逃生
        門維持舊版「啟用 × 連線」四種組合的三態 ghost button，不動。
        """
        if self._skeleton_mode:
            self._style_pill(self._ollama_btn, enabled, "潤飾")
            return
        if not enabled:
            # 未啟用：完全灰態、不可按；點擊會顯示提示 toast（保留 state=normal 才能觸發）
            self._ollama_btn.configure(
                state="normal",
                text="潤飾",
                image=get_icon("sparkles", 15, TEXT_3),
                fg_color=SURF_1,
                hover_color=SURF_2,
                border_color=SURF_3,
                text_color=TEXT_3,
            )
            return

        if not healthy:
            # 啟用但服務不可達：用警示色，點擊會提示使用者
            self._ollama_btn.configure(
                state="normal",
                text="潤飾（離線）",
                image=get_icon("sparkles", 15, WARN),
                fg_color=SURF_1,
                hover_color=SURF_2,
                border_color=WARN,
                text_color=WARN,
            )
            return

        # 啟用且健康：正常可用
        self._ollama_btn.configure(
            state="normal",
            text="潤飾",
            image=get_icon("sparkles", 15, TEXT_1),
            fg_color=ACCENT,
            hover_color=ACCENT_HV,
            border_color=ACCENT,
            text_color=TEXT_1,
        )

    def _on_model_change(self, value: str) -> None:
        """頂部模型選單變更時：更新 cfg 並刷新狀態列文字。

        v2.15.0：模型切換 → 背景 warmup（跟 _on_settings_saved 同樣模式、
        修補 v2.14.0 只覆蓋 SettingsWindow 路徑、漏掉主視窗 dropdown 的 bug）。
        對 qwen3-asr-large 特別重要——3.4GB 首次下載 + cold load 加起來會
        遠超過 60s _processing_timeout_check 的 fail-safe、後果是「轉錄超時」。
        """
        old = self.cfg.model
        self.cfg.model = value
        self.cfg.save()
        log_settings("changed", field="model", old=old, new=value, source="dropdown")
        if value != old:
            # 顯示「暖機中」直到 _warmup_model 完成才改回「就緒」
            # （避免 user 在 1.7B 下載 3.4GB 期間誤以為已 ready、按熱鍵卡 60s timeout）
            self._status_label.configure(text=f"  暖機中 ({value})...")
            log.info(f"WARMUP: model changed ({old} → {value}) via top dropdown, scheduling warmup")
            self.after(1500, self._warmup_model)
        else:
            self._status_label.configure(text=f"  就緒 ({value})")

    def _on_language_change(self, value: str) -> None:
        """頂部語言選單變更時：更新 cfg 儲存（下次錄音生效）。"""
        old = self.cfg.language
        self.cfg.language = value
        self.cfg.save()
        log_settings("changed", field="language", old=old, new=value, source="dropdown")

    # ═══════════════════════════════════════════════════════════════════════
    #  SETTINGS
    # ═══════════════════════════════════════════════════════════════════════

    def _open_settings(self) -> None:
        """開啟設定視窗（modal）。"""
        log_action("settings_opened")
        SettingsWindow(self, self.cfg, self._on_settings_saved)

    # ── v2.21.0 Phase M：麥克風熱插拔處理 ───────────────────────────────────
    def _on_audio_devices_changed(self) -> None:
        """CoreAudio 裝置變動回呼（在 CoreAudio 執行緒）。marshal 回主執行緒處理。"""
        try:
            self.after(0, self._handle_device_change)
        except Exception:
            pass   # App 關閉中、root 已銷毀

    def _handle_device_change(self) -> None:
        """主執行緒：麥克風插/拔後的處理。

        - 重新初始化 PortAudio 拿最新清單（錄音中會自動跳過）
        - 若使用者選定的裝置（cfg.input_device）已不在 → 退回系統預設 + 提示
        錄音中不動裝置（避免打斷當下錄音）、等這次錄完下次生效。
        """
        try:
            if self.recorder.is_recording():
                return
            devices = self.recorder.refresh_portaudio()
            names = {d["name"] for d in devices}
            # v2.21.0：用 recorder「當下實際在用」的裝置名判斷（不是 cfg），
            #   這樣只在錄音用的那支被拔時 fallback、且 fallback 後 _device_name=None
            #   不會在後續裝置變動時重複跳 toast。
            active = getattr(self.recorder, "_device_name", None)
            if active and active not in names:
                # 使用中的裝置被拔 → runtime 退回系統預設（但**不**寫死 config，
                #   保留使用者偏好：下次啟動 / 重新插回時仍會用回原裝置）
                log_action("mic_device_removed_fallback", device=active)
                self.recorder._device_index = None
                self.recorder._device_name = None
                self._show_toast(f"⚠ 麥克風「{active}」已移除，已切回系統預設")
            elif active:
                # v2.21.5 BUGFIX：裝置還在、但 refresh_portaudio 的 PortAudio re-init
                #   會讓**所有裝置 index 位移**。舊 _device_index 指到錯的裝置 → 下次
                #   錄音錄到錯裝置、收到雜訊/靜音 → 顯示「未偵測到語音內容」且不報錯
                #   （串流有開成功、不丟例外，fallback 也不會觸發）。這正是「插新裝置
                #   後沒重選就一直錄音失敗」的根因。必須用名字重新解析回正確 index
                #   （這就是當初存 _device_name 的目的、見 recorder.py 註解）。
                if self.recorder.set_device_by_name(active):
                    log_action("mic_device_reindexed", device=active,
                               index=self.recorder._device_index)
                else:
                    # 理論上不會走到（active 已在 names 內）；保險退回系統預設。
                    self.recorder._device_index = None
                    self.recorder._device_name = None
                    log_action("mic_device_reindex_failed_fallback", device=active)
            else:
                log_action("mic_device_changed", count=len(devices))
        except Exception:
            log_error("handle_device_change_failed")

    def _do_theme_relaunch(self, on_failure=None) -> None:
        """v2.6.0 主題切換後執行：[pre-flight] → toast → 800ms → cleanup → spawn → exit。

        Eng Review Issue 1 / 2026-05-23：cleanup-first 順序避免新舊 process 共存
        race window（雙 NSEvent monitor / mic 衝突 / config.json race）。

        Fix Cluster D-1 / 2026-05-23：spawn 失敗時呼叫 on_failure callback（讓
        SettingsWindow rollback config.theme），避免使用者下次啟動莫名變新主題。

        Bug 1（v2.8.0 / 2026-05-23）：pre-flight 檢查 .app bundle 是否存在。不存在
        就**不要**跑 cleanup（否則 App 會半殘：hotkey 死、mini HUD 不見、history
        關掉），直接 rollback + toast 提示。

        SettingsWindow 已 save config + destroy 自己；本函式由 AppWindow 跑收尾。
        """
        log_action("theme_relaunch_started")

        # Bug 1 pre-flight：spawn 不可能就不要 cleanup（保住 App 仍可用）
        try:
            import main as _main
            app_bundle_exists = _main._APP_BUNDLE.exists()
        except Exception:
            app_bundle_exists = False

        if not app_bundle_exists:
            log.warning(
                f"THEME: 無 .app bundle ({getattr(_main, '_APP_BUNDLE', '?')})，"
                f"略過 cleanup 直接 rollback 並提示使用者手動重啟"
            )
            if on_failure is not None:
                try:
                    on_failure()
                except Exception:
                    log_error("theme_relaunch_on_failure_callback_failed")
            try:
                self._show_toast("無 .app bundle、請手動關閉並重新啟動 App（執行 build_app.sh 後即可自動重啟）")
            except Exception:
                pass
            log_error("theme_relaunch_no_app_bundle")
            return

        # Step 0：toast 通知（fire-and-forget）
        try:
            self._show_toast("主題切換中、~2 秒…")
        except Exception:
            pass

        def _do_relaunch_sequence():
            # Step A：cleanup 完整跑完（on_close 內含 hotkey_mgr.stop / recorder.stop /
            # mini HUD destroy / history.db close 等所有資源釋放）
            try:
                self.on_close()
            except Exception:
                log_error("theme_relaunch_cleanup_failed")

            # Step B：spawn 新 process
            try:
                import main as _main
                spawned = _main._relaunch_app()
            except Exception:
                log_error("theme_relaunch_spawn_failed")
                spawned = False

            # Step C：舊 process 終止（.app 路徑用 sys.exit；execv 路徑不會走到這）
            if spawned:
                import sys
                log.info("THEME: cleanup done, new instance spawned, exiting")
                sys.exit(0)
            else:
                # Cluster D-1：全部失敗 → 回滾 config.theme，不 exit、讓使用者手動處理
                # 注意：能走到這代表 .app bundle 存在但 open -n 失敗（罕見）。
                if on_failure is not None:
                    try:
                        on_failure()
                    except Exception:
                        log_error("theme_relaunch_on_failure_callback_failed")
                try:
                    self._show_toast("重啟失敗、主題已回滾、請手動關閉並重啟 App")
                except Exception:
                    pass
                log_error("theme_relaunch_all_paths_failed")

        # 800ms 後執行 relaunch sequence（給 toast 時間顯示）
        self.after(800, _do_relaunch_sequence)

    def _open_history(self) -> None:
        """開啟歷史紀錄視窗（Phase 3.2）。"""
        log_action("history_opened")
        if self.history_store is None:
            self._show_toast("歷史紀錄未啟用（請到設定開啟）")
            return
        HistoryWindow(self, self.history_store, on_repolish=self._repolish_from_history)

    def _repolish_from_history(self, entry) -> None:
        """從歷史視窗重新潤飾一筆舊紀錄。

        流程：設 _current_history_id 指到舊筆 → 走 _start_polish → 潤飾完成後
        `_finish_polish` 會 update_polish 回寫同一筆，不會再 insert 新紀錄。
        目標貼上位置 target=None（使用者在歷史視窗按的，不猜目標）。
        """
        if not self.cfg.ollama_enabled:
            self._show_toast("AI 潤飾未啟用")
            return
        if self.ollama.health_ok is False:
            self._show_toast("Ollama 服務未啟動")
            self._refresh_ollama_health()
            return
        if self._polish_busy:
            self._show_toast("目前已在潤飾中，請稍候…")
            return

        # 更新 AppWindow 狀態指向歷史紀錄
        self._polish_generation += 1
        gen = self._polish_generation
        self._current_history_id = entry.id
        self._last_raw         = entry.raw_text
        self._last_llm_input   = None
        self._last_polished    = None
        self._showing_polished = True

        # Fix 19 Path B / 2026-05-23：把歷史原文當成一個新 block append 進結果區，
        # _finish_polish 完成時會更新「最新 block」（= 剛 append 的這個）的 polished_text。
        # 之前是塞回 textbox 整坨清空再 insert；block 化後改成 append + 標示最新。
        self._clear_placeholder()

        import datetime as _dt
        try:
            ts = _dt.datetime.fromtimestamp(entry.timestamp).strftime("%H:%M")
        except Exception:
            ts = _dt.datetime.now().strftime("%H:%M")

        if self._skeleton_mode:
            block = UtteranceBlockV2(
                self._blocks_container,
                on_copy=self._on_block_copy, on_save=self._on_block_save,
            )
            block.set_result(
                raw_text=entry.raw_text, timestamp_iso=f"{ts}（歷史）",
                duration_s=float(entry.duration_s or 0.0),
                language=entry.language or "?", model=entry.model_whisper or "?",
            )
            block.history_id = entry.id
            # _stream_insert_latest_block 內部已含「插頂端 + 標最新 + 兩段式
            # scroll + _refresh_stream_stats」，不用再重複那串邏輯。
            self._stream_insert_latest_block(block, epoch=float(entry.timestamp))
        else:
            if self._utterance_blocks:
                self._utterance_blocks[0].highlight_as_latest(False)
            block = UtteranceBlock(
                self._blocks_container,
                raw_text=entry.raw_text,
                timestamp_iso=f"{ts} (history)",
                duration_s=float(entry.duration_s or 0.0),
                language=entry.language or "?",
                model=entry.model_whisper or "?",
                on_copy=self._on_block_copy,
                on_delete=self._on_block_delete,
            )
            # Bug 3（v2.8.0）：插最頂 + insert(0)
            if self._utterance_blocks:
                block.pack(
                    fill="x", padx=SPACE_XS, pady=(0, 8),
                    before=self._utterance_blocks[0],
                )
            else:
                block.pack(fill="x", padx=SPACE_XS, pady=(0, 8))
            block.highlight_as_latest(True)
            self._utterance_blocks.insert(0, block)
            # D3-S5：兩段式 scroll（同 _display_result）
            def _scroll_to_top_history():
                try:
                    self._blocks_container.update_idletasks()
                    self._blocks_container._parent_canvas.yview_moveto(0.0)
                except Exception:
                    pass
            self.after(0, _scroll_to_top_history)
            self.after(150, _scroll_to_top_history)
            self._refresh_stream_stats()

        self._apply_toggle_style()
        self._frontmost_app = entry.target_app   # 讓 preset 路由走原本的 app

        log_action("history_repolish", id=entry.id)
        self._start_polish(gen, entry.raw_text, target=None)

    # ── Phase 4.3 mini 錄音窗 ──────────────────────────────────────────────

    def _ensure_mini_window(self) -> None:
        """Lazy 建立 MiniRecordingWindow；toggle on 時呼叫。

        Bug A（v2.12.0）：成功 / 失敗都 log 一行，方便 user log 回報時定位。
        """
        if self._mini_window is not None:
            return
        try:
            self._mini_window = MiniRecordingWindow(self)
            # log 是否升級成 NSPanel level（決定能否跨 Space 可見）
            ns_ok = getattr(self._mini_window, "_ns_window", None) is not None
            log.info(
                f"MINI_HUD: instance created (panel_level_upgraded={ns_ok})。"
                f"{'跨 Space / 全螢幕可見' if ns_ok else '退化模式，僅同 Space 可見'}"
            )
        except Exception:
            log_error("mini_window_init_failed")
            self._mini_window = None

    def _destroy_mini_window(self) -> None:
        """Toggle off 時銷毀 mini 視窗。"""
        if self._mini_window is None:
            return
        try:
            self._mini_window.close()
        except Exception:
            log_error("mini_window_close_failed")
        self._mini_window = None

    def _run_history_retention(self) -> None:
        """Phase 3.2：啟動後 1 分鐘跑一次保留策略清理。

        若 `history_retention_days > 0`，刪除 N 天前的紀錄。
        失敗只 log_error，不影響 UI。
        """
        if self.history_store is None:
            return
        days = max(0, int(self.cfg.history_retention_days or 0))
        if days <= 0:
            return
        try:
            removed = self.history_store.delete_before(days)
            if removed > 0:
                log.info(f"HISTORY retention: removed {removed} rows older than {days}d")
        except Exception:
            log_error("history_retention_failed", days=days)

    def _on_settings_saved(self, cfg: Config) -> None:
        """設定視窗儲存後：同步 cfg、重啟 listener（若 hotkey 有變），刷新所有 UI。"""
        old_hotkey = self.cfg.hotkey
        old_model  = self.cfg.model   # v2.14.0：模型切換要重 warmup（見下方）
        # 逐欄位紀錄變動
        for field_name in ("model", "language", "hotkey", "auto_copy",
                           "auto_paste", "append_results", "ollama_enabled"):
            ov, nv = getattr(self.cfg, field_name), getattr(cfg, field_name)
            if ov != nv:
                log_settings("changed", field=field_name, old=ov, new=nv,
                             source="settings_window")
        self.cfg = cfg
        # v2.20.3 N8：config 變動後重算 hash（後續 audit entry 會帶新 snapshot 標記）
        try:
            import audit_log
            audit_log.set_config_hash(self.cfg)
        except Exception:
            pass
        # 只有 hotkey 真正變更才重啟 pynput listener——反覆 stop/start 在 macOS
        # 上曾與 MLX/Metal 並存時觸發 native 層不穩定，能避免就避免。
        # PR #8 修正：延遲 100ms 再重啟，讓 SettingsWindow.destroy() 與舊 Listener
        # 的 CFRunLoop teardown 完成，避免 macOS Cocoa native race 造成閃退。
        if cfg.hotkey != old_hotkey:
            self.after(100, self._start_hotkey_listener)
        self._hotkey_hint.configure(text=f"按下 {cfg.format_hotkey_display()} 即時錄音")
        self._hotkey_status.configure(text=cfg.format_hotkey_display())
        self._model_var.set(cfg.model)
        # v2.14.0：模型切換 → 背景 warmup，避免下次按錄音 cold start ~7-8s
        # （Qwen3-ASR 第一次跑要 1.87s session load + ~5s Metal shader 編譯；
        # 跟 App 啟動時的 self.after(1500, self._warmup_model) 同 delay）
        if cfg.model != old_model:
            log.info(f"WARMUP: model changed ({old_model} → {cfg.model}), scheduling warmup")
            self.after(1500, self._warmup_model)
        self._lang_var.set(cfg.language)
        # F1（v2.12.0）：麥克風來源變更 → 套到 recorder。下次 start() 時用新 device。
        # 不需要 stream restart，因為當前若在錄音中、user 該手動停止再換。
        try:
            if cfg.input_device:
                ok = self.recorder.set_device_by_name(cfg.input_device)
                if not ok:
                    log.warning(
                        f"RECORD: input_device '{cfg.input_device}' 找不到、"
                        f"回退到系統預設"
                    )
                log_settings("device_applied", device=cfg.input_device, found=ok)
            else:
                # input_device=None → 用系統預設
                self.recorder._device_index = None
                log_settings("device_applied", device="(system default)")
        except Exception:
            log_error("settings_apply_device_failed")
        self._refresh_ap_btn_style()
        # Ollama 設定同步：把新 cfg 推給 client，然後重新非同步探測一次
        self.ollama.apply_app_config(cfg)
        # v2.18.0：Vertex 設定同步 + polish backend router 更新
        if self.vertex is not None:
            try:
                self.vertex.apply_app_config(cfg)
            except Exception:
                log_error("vertex_apply_app_config_failed")
        self._refresh_polish_backend()
        # v2.21.4：存檔後若選了 Vertex 但 Project ID 空、明確告知（否則使用者會
        #   「以為開了 AI 潤飾、其實每次都靜默降回原文」完全感知不到）。
        if (getattr(cfg, "polish_backend", "local") == "vertex"
                and not (getattr(cfg, "vertex_project_id", "") or "").strip()):
            self._show_toast("Vertex 潤飾需要 Project ID，目前未設定、潤飾已停用")
        self._refresh_ollama_health()

        # v2.19.0：可疑音檔保留設定也同步推給 transcriber
        # hasattr 防 Agent T 還在進行時（setter 還沒實作）的 race
        if hasattr(self.transcriber, "set_suspicious_capture"):
            try:
                self.transcriber.set_suspicious_capture(
                    cfg.suspicious_audio_capture,
                    cfg.suspicious_audio_max_size_mb,
                )
            except Exception:
                log_error("set_suspicious_capture_apply_failed")

        # v2.19.x：Silero VAD 設定也同步推給 transcriber（套用設定 / 匯入設定後生效）
        if hasattr(self.transcriber, "set_silero_vad"):
            try:
                self.transcriber.set_silero_vad(
                    cfg.silero_vad_enabled,
                    cfg.silero_vad_threshold,
                )
            except Exception:
                log_error("set_silero_vad_apply_failed")

        # v2.20.1：Pinyin guard 設定同步（預設關、user 改了 config.json 也能即時生效）
        if hasattr(self.transcriber, "set_pinyin_guard"):
            try:
                self.transcriber.set_pinyin_guard(
                    getattr(cfg, "pinyin_guard_enabled", False)
                )
            except Exception:
                log_error("set_pinyin_guard_apply_failed")

        # 字典：重新載入（可能 enabled 變了，或路徑變了）
        self._reload_dictionary()

        # Prompt 熱重載：啟動／停止 watcher
        reloader = getattr(self, "_prompt_reloader", None)
        if reloader is not None:
            if cfg.prompt_hot_reload:
                # start() 是 idempotent 嗎？不是，若已在跑會建第二個 thread。
                # 簡單策略：先 stop 舊的（若有）再 start
                reloader.stop()
                # 新建一個實例以確保 _stop Event 乾淨
                def _on_reload(name):
                    self.after(0, lambda: self._show_toast(f"已重新載入 {name}.py"))
                self._prompt_reloader = PromptReloader(on_reload=_on_reload)
                self._prompt_reloader.start()
            else:
                reloader.stop()

        # Phase 3.2：歷史紀錄啟用狀態變動 → lazy 建立/釋放 store
        if cfg.history_enabled and self.history_store is None:
            try:
                self.history_store = HistoryStore()
                log.info("HISTORY: store lazy-initialized after settings change")
            except Exception:
                log_error("history_store_lazy_init_failed")
        elif not cfg.history_enabled and self.history_store is not None:
            # 停用 → 只是停寫入；既有 DB 檔保留
            self.history_store = None
            log.info("HISTORY: store disabled (DB file preserved)")

        # Phase 4.3：mini 視窗 toggle 變動
        if cfg.mini_recording_window and self._mini_window is None:
            self._ensure_mini_window()
        elif not cfg.mini_recording_window and self._mini_window is not None:
            self._destroy_mini_window()

        # A3（v2.7.0）：reduce_motion 偏好變更立即套用（下一個 render tick 生效）
        self._reduce_motion = resolve_reduce_motion(
            getattr(cfg, "reduce_motion_pref", "auto")
        )

    # ═══════════════════════════════════════════════════════════════════════
    #  HELPERS
    # ═══════════════════════════════════════════════════════════════════════

    def _start_hotkey_listener(self) -> None:
        """（重）啟動 pynput 全域快捷鍵監聽器；pynput 不可用時靜默略過。"""
        if not is_pynput_available():
            return
        self.hotkey_mgr.restart(self.cfg.hotkey)

    def _hotkey_watchdog(self) -> None:
        """週期性檢查熱鍵監聽器還活著嗎；死了就 restart（Layer 1，跨平台）。

        存活判斷依後端分流：
          • macOS（NSEvent 後端）：monitor 物件不為 None 視為活著。
          • Windows（pynput Listener 後端）：_pynput_listener 存在且 is_alive()。

        Layer 2（10 分鐘定時 force-restart）+ Layer 3（靜默診斷）是 macOS NSEvent
        專屬補救（monitor 閒置 ~60min 後 ObjC block 失效但物件仍非 None，靠定時
        force-restart 兜底）；Windows 的 pynput Listener 沒有這個失效問題，只跑
        Layer 1。詳見下方 monitor_alive 分流與 `elif IS_MAC` 註解。
        """
        try:
            mgr = self.hotkey_mgr
            # 監聽器存活判斷依後端平台分流（Mac 分支表達式維持 byte-identical）：
            #   • Windows（pynput Listener 後端）：listener 物件存在且背景執行緒還活著。
            #     Windows 後端從不設 _monitor_global/_monitor_local（那是 NSEvent 專屬），
            #     若沿用舊判斷會恆判為「死」→ watchdog 每 5s 無限重啟熱鍵（真 bug）。
            #   • macOS（NSEvent 後端）：monitor 物件不為 None 視為活著（原邏輯不變）。
            if IS_WINDOWS:
                listener = getattr(mgr, "_pynput_listener", None)
                monitor_alive = listener is not None and listener.is_alive()
            else:
                monitor_alive = (
                    getattr(mgr, "_monitor_global", None) is not None
                    or getattr(mgr, "_monitor_local", None) is not None
                )

            # v2.20.3 N6：每 5 分鐘（60 ticks × 5s）寫一筆 hotkey_health snapshot 給
            # audit log，記下 monitor 是否還活著、目前 pressed 集合、ns held modifiers、
            # combo_active 等狀態。用途：事後抓「user 反映 hotkey 不靈」的根因 —
            # 若 snapshot 顯示 monitor 已死 / pressed 卡住，就知道是哪層出問題。
            self._hotkey_health_tick_count += 1
            if self._hotkey_health_tick_count >= 60:
                self._hotkey_health_tick_count = 0
                try:
                    import audit_log  # noqa: PLC0415
                    # 安全取值：mgr 任何 attr 可能因版本差異不存在 → 用 getattr fallback。
                    # set 要轉 list 才能 JSON serialize；元素轉 str 避免 PyObjC Key 型別炸。
                    pressed_set_raw = getattr(mgr, "_pressed", set()) or set()
                    ns_held_raw = getattr(mgr, "_ns_held_modifiers", set()) or set()
                    pressed_list = [str(k) for k in pressed_set_raw]
                    # ns_held_modifiers 元素是 keycode int，直接轉 list 即可
                    try:
                        ns_held_list = sorted(int(x) for x in ns_held_raw)
                    except Exception:
                        ns_held_list = [str(x) for x in ns_held_raw]
                    # last event 時間（相對「現在 ago 多少秒」）
                    last_event_at_raw = getattr(mgr, "_last_event_at", None)
                    last_event_s_ago: Optional[float] = None
                    if isinstance(last_event_at_raw, (int, float)) and last_event_at_raw > 0:
                        last_event_s_ago = round(
                            time.monotonic() - float(last_event_at_raw), 1
                        )
                    # watchdog 狀態用簡單分類：monitor 死 = degraded、else normal
                    watchdog_state = "normal" if monitor_alive else "degraded"
                    audit_log.write_event(
                        "hotkey_health",
                        "",
                        monitor_alive=bool(monitor_alive),
                        pressed_set=pressed_list,
                        ns_held_modifiers=ns_held_list,
                        combo_active=bool(getattr(mgr, "_combo_active", False)),
                        last_event_s_ago=last_event_s_ago,
                        watchdog_state=watchdog_state,
                    )
                except Exception:
                    log_error("hotkey_health_snapshot_failed")
            if not monitor_alive:
                log.warning("HOTKEY: watchdog restarting hotkey listener (reason=monitor_missing)")
                log_action("hotkey_listener_auto_restarted", reason="monitor_missing")
                mgr.restart(self.cfg.hotkey)
                self._last_hotkey_force_restart = time.monotonic()
            elif IS_MAC:
                # Layer 2（10 分鐘定時 force-restart）+ Layer 3（靜默診斷）是 macOS
                # NSEvent 後端專屬的補救：monitor 物件閒置 ~60min 後 ObjC block 會失效但
                # 物件仍非 None（is_not_None 偵測不到）→ 只能靠定時 force-restart 兜底。
                # Windows 的 pynput Listener（SetWindowsHookEx）沒有這個失效問題，不需要
                # 這兩層；Windows watchdog 只保留上面的 Layer 1（listener 死了才重啟）。
                now = time.monotonic()

                # Fix 18 / 2026-05-23（Layer 2）：每 10 分鐘 force-restart。
                # 實機觀察：閒置 ~60 min 後 NSEvent monitor 物件仍 alive 但 ObjC
                # block 失效，handler 收不到事件。watchdog 用 is_not_None 偵測不到。
                # 唯一可靠的修法是定時 force restart。state guard：錄音中跳過，
                # 避免 50ms restart window 吞掉使用者的 stop tap。
                since_restart = now - self._last_hotkey_force_restart
                if (
                    self._state == "idle"
                    and since_restart * 1000.0 > self.HOTKEY_FORCE_RESTART_INTERVAL_MS
                ):
                    # Fix Cluster G / 2026-05-23：force-restart 不撞使用者剛好按鍵的瞬間。
                    # 若 mgr 內部 _pressed 不為空或 _combo_active=True，表示有 in-flight
                    # press 還沒 release。restart 會清空 _pressed → 後續 release 不 fire。
                    # 跳過本輪、下個 5s watchdog tick 再試（最多延 5 次 = +25s 可接受）。
                    pressed_count = len(getattr(mgr, "_pressed", ()))
                    combo_active  = getattr(mgr, "_combo_active", False)
                    if pressed_count > 0 or combo_active:
                        log.info(
                            f"HOTKEY: watchdog deferring force-restart "
                            f"(in-flight: pressed={pressed_count}, combo_active={combo_active})"
                        )
                    else:
                        log.info(
                            f"HOTKEY: watchdog force-restart (reason=periodic, "
                            f"since_last={since_restart:.0f}s)"
                        )
                        log_action("hotkey_listener_auto_restarted", reason="periodic")
                        mgr.restart(self.cfg.hotkey)
                        self._last_hotkey_force_restart = now

                # Fix 18 / 2026-05-23（Layer 3）：靜默偵測（純診斷 log）。
                # monitor 物件還在但 5 分鐘沒任何事件 + idle → 嫌疑 H1/H2 發生中。
                # 不主動修復（Layer 2 兜底），只記錄供未來 log 分析。每次符合
                # 條件都 log 太吵 → 5 分鐘節流（per watchdog cycle 觸發一次即可）。
                last_event_at = getattr(mgr, "_last_event_at", 0.0)
                if (
                    last_event_at > 0
                    and self._state == "idle"
                ):
                    silence_s = now - last_event_at
                    if (
                        silence_s > self.HOTKEY_SILENT_THRESHOLD_S
                        and silence_s % self.HOTKEY_SILENT_THRESHOLD_S < (
                            self.HOTKEY_WATCHDOG_INTERVAL_MS / 1000.0
                        )
                    ):
                        log.info(
                            f"HOTKEY: diagnostic — monitor alive but silent "
                            f"for {silence_s:.0f}s (Layer 2 will force-restart at "
                            f"{self.HOTKEY_FORCE_RESTART_INTERVAL_MS / 1000.0:.0f}s)"
                        )
        except Exception:
            log_error("hotkey_watchdog_failed")
        finally:
            self.after(self.HOTKEY_WATCHDOG_INTERVAL_MS, self._hotkey_watchdog)

    # ── 視窗復原 helper（Fix 5d / 2026-05-22）─────────────────────────
    # 根因：AppWindow 是 CTkFrame，不是 Toplevel。所有 wm_state/deiconify/iconify
    # 方法必須在 self.winfo_toplevel()（真正的 Tk root）上呼叫，不能在 self 上。
    # 之前 5 層保險絲全部在 self 呼叫 → 全部 AttributeError silent crash。
    def _is_auto_restore_suppressed(self) -> bool:
        """判斷現在該不該抑制「app active → 自動 deiconify 視窗」的反射動作。

        v2.14.1 新增。理由：按熱鍵會讓 macOS 把 app 變 active（NSEvent monitor
        受理 event 的副作用），原本 Cocoa observer 會把 minimized 視窗拉回前景
        ——這跟 user「最小化背景錄音」的意圖相反。

        抑制條件（任一）：
          • 1.5s 內按過熱鍵（_hotkey_tap 標記）
          • 當前 state 為 recording / processing（明示背景處理中）

        Dock icon 點擊走 AppleEventReopen 路徑、**不**經過此判斷、永遠 restore。
        """
        if time.time() < self._suppress_auto_restore_until:
            return True
        if self._state in ("recording", "processing"):
            return True
        return False

    def _restore_root_if_minimized(self, source: str) -> None:
        """共用 helper：根視窗若在 iconic/icon/withdrawn 狀態 → 強制 deiconify + lift。

        必須走 winfo_toplevel() 拿到真正的 Tk root，因為 AppWindow 是 CTkFrame
        本身沒有 wm_state/deiconify 等視窗管理方法。

        B3（v2.7.0）：含 'icon' legacy（Tk 文件列出，雖 macOS 26.4 實測都是
        'iconic'，零成本防禦避免未來 Tk 版本變更時漏判）。
        """
        try:
            top = self.winfo_toplevel()
            state = top.wm_state()  # 'normal' / 'iconic' / 'icon' / 'withdrawn'
            if state in ("iconic", "icon", "withdrawn"):
                top.deiconify()
                top.lift()
                log_action("dock_reopen_recovered", source=source, state=state)
        except Exception as e:
            log_error("dock_reopen_failed", source=source, error=str(e))

    def _on_dock_reopen(self) -> None:
        """macOS：點 Dock icon 把最小化的視窗叫回來（Fix 5 / 2026-05-22）。

        B1（v2.7.0）：DRY 重構，delegate 到 _restore_root_if_minimized；
        ReopenApplication 路徑額外 focus_force 才能搶回 keyboard focus。
        """
        self._restore_root_if_minimized("ReopenApplication")
        try:
            self.winfo_toplevel().focus_force()
        except Exception as e:
            log_error("dock_reopen_focus_failed", error=str(e))

    def _on_app_activate(self, event=None) -> None:
        """macOS Fix 5b：App 取得焦點時若視窗是最小化狀態，自動 deiconify。

        v2.14.1：熱鍵剛按過 / 錄音中 → 抑制（user 要背景錄音、不要視窗跳出）。
        """
        if self._is_auto_restore_suppressed():
            return
        self._restore_root_if_minimized("Activate")

    def _install_cocoa_activation_observer(self) -> None:
        """Fix 5c：用 PyObjC 掛 Cocoa 層通知，繞過 Tk 的 AppleEvent 投遞。

        實測診斷顯示我們 process 的 NSApp.delegate 為 None — kAEReopenApplication
        沒有人接收。Tk 也沒有把 reopen 事件正確轉成 <<Activate>>。所以從
        Cocoa NSNotificationCenter 直接訂閱 NSApplicationDidBecomeActive，
        不依賴 Tk / AppleEvent / delegate。

        當任何方式讓本 App 取得焦點（Dock 圖示／視窗縮圖、Cmd+Tab、Mission
        Control、Spotlight）→ 通知抵達 → 主執行緒 deiconify。

        IS_MAC 專屬：PyObjC/Cocoa 通知系統，Windows 無對應概念（no-op）。
        """
        if not IS_MAC:
            return
        try:
            import objc
            from Foundation import NSObject, NSNotificationCenter, NSBundle
            from AppKit import (
                NSApplication,
                NSApplicationDidBecomeActiveNotification,
                NSApplicationWillBecomeActiveNotification,
                NSApplicationWillHideNotification,
            )

            # === Runtime 診斷（Fix 5c v2 / 2026-05-22）===
            # 根因確認：bash launcher exec Python 可能讓 LaunchServices 沒
            # 把 PID 綁到 shim Python.app 的 bundle id → AppleEvent 寄不到。
            # 印出 runtime bundle id + delegate 狀態確認假設。
            try:
                bid = NSBundle.mainBundle().bundleIdentifier()
                delegate = NSApplication.sharedApplication().delegate()
                log.info(
                    f"COCOA DIAG: bundle_id={bid!r} "
                    f"delegate={type(delegate).__name__ if delegate else 'None'}"
                )
            except Exception as e:
                log.warning(f"COCOA DIAG: failed - {e}")

            # 用 closure 把 self 帶進 Objective-C method（避免 NSObject 子類保
            # 留 tk 物件參照造成循環引用）
            tk_root = self

            class _ActivationObserver(NSObject):
                # Fix 17 / 2026-05-23 — 不在 PyObjC context 裡呼叫 tk_root.after()。
                # 跟 Fix 10 同款根因：PyObjC PyGILState_Ensure 與 Tk PyEval_SaveThread
                # 衝突，導致 Tcl_ServiceEvent → PyEval_RestoreThread → SIGSEGV at NULL+16。
                # 改成純 Python list.append（GIL 已由 PyGILState_Ensure 拿著，安全），
                # 實際分派由 _poll_cocoa_pending_actions 在 Tk mainloop iteration
                # 跑（PyEval_SaveThread/RestoreThread context 乾淨）。
                def appBecameActive_(self, notification):
                    try:
                        tk_root._cocoa_pending_actions.append("BecomeActive")
                    except Exception:
                        pass

                def appWillBecomeActive_(self, notification):
                    try:
                        tk_root._cocoa_pending_actions.append("WillBecomeActive")
                    except Exception:
                        pass

                def appWillHide_(self, notification):
                    # Fix 16：攔截 macOS「點 frontmost app dock icon = hide」標準行為。
                    # 詳見 _handle_cocoa_will_hide docstring。
                    try:
                        tk_root._cocoa_pending_actions.append("WillHide")
                    except Exception:
                        pass

            self._cocoa_activation_observer = _ActivationObserver.alloc().init()
            nc = NSNotificationCenter.defaultCenter()
            # 雙保險：DidBecomeActive（事後）+ WillBecomeActive（事前）
            # 後者是 Plan B 重點 — SwiftUI 同類 bug 驗證唯一有效的 workaround
            nc.addObserver_selector_name_object_(
                self._cocoa_activation_observer,
                b"appBecameActive:",
                NSApplicationDidBecomeActiveNotification,
                NSApplication.sharedApplication(),
            )
            nc.addObserver_selector_name_object_(
                self._cocoa_activation_observer,
                b"appWillBecomeActive:",
                NSApplicationWillBecomeActiveNotification,
                NSApplication.sharedApplication(),
            )
            # Fix 16：觀察 WillHide 攔截「click frontmost app dock icon = hide」
            nc.addObserver_selector_name_object_(
                self._cocoa_activation_observer,
                b"appWillHide:",
                NSApplicationWillHideNotification,
                NSApplication.sharedApplication(),
            )
            log.info("COCOA: observers installed (Did + Will BecomeActive + WillHide)")

            # ── Layer 6（Fix 13 / 2026-05-23）：kAEReopenApplication AppleEvent ──
            # 補洞理由：黃色鈕 minimize 後再點 Dock icon，本 App 全程仍是
            # frontmost（NSApp.isActive 不轉換）→ Layer 3/4 BecomeActive 不 fire、
            # Layer 5 Poll 沒 transition 也不 fire。但 macOS 一定會送
            # kAEReopenApplication AppleEvent 給程序——此層直接掛到
            # NSAppleEventManager 接這個 event，不依賴 NSApp.isActive 轉換、
            # 不依賴 NSApplicationDelegate（後者可能是 TKApplication 但沒轉發）。
            # 失敗只 log，不阻擋啟動。
            try:
                from Foundation import NSAppleEventManager
                # FourCharCode: 'aevt' = kCoreEventClass, 'rapp' = kAEReopenApplication
                #   'a'=0x61 'e'=0x65 'v'=0x76 't'=0x74 → 0x61657674
                #   'r'=0x72 'a'=0x61 'p'=0x70 'p'=0x70 → 0x72617070
                _kCoreEventClass     = 0x61657674
                _kAEReopenApplication = 0x72617070

                class _ReopenHandler(NSObject):
                    def handleReopenEvent_withReplyEvent_(self, event, reply):
                        # Fix 17：純 Python append，不呼叫 tk_root.after()（GIL 衝突）
                        try:
                            tk_root._cocoa_pending_actions.append("AppleEventReopen")
                        except Exception:
                            pass

                self._cocoa_reopen_handler = _ReopenHandler.alloc().init()
                aem = NSAppleEventManager.sharedAppleEventManager()
                aem.setEventHandler_andSelector_forEventClass_andEventID_(
                    self._cocoa_reopen_handler,
                    b"handleReopenEvent:withReplyEvent:",
                    _kCoreEventClass,
                    _kAEReopenApplication,
                )
                log.info("COCOA: kAEReopenApplication handler installed (Layer 6)")
            except Exception as e:
                log_error("cocoa_reopen_handler_install_failed", error=str(e))
        except Exception as e:
            log_error("cocoa_observer_install_failed", error=str(e))

    def _poll_cocoa_pending_actions(self) -> None:
        """Fix 17 / 2026-05-23 — Cocoa observer / AppleEvent handler 動作 poller。

        在 Tk mainloop iteration 跑（PyEval_SaveThread/RestoreThread context 乾淨），
        把 PyObjC observer / handler 從 PyGILState context 擱置的動作分派出去。
        詳見 _cocoa_pending_actions 註解與 Fix 10 模式。

        同一輪 poll 內同類動作只執行一次（dedupe），避免短時間連點 Dock icon
        把同一個 restore 邏輯跑多次。
        """
        try:
            if self._cocoa_pending_actions:
                actions = self._cocoa_pending_actions
                self._cocoa_pending_actions = []
                seen: set = set()
                for action in actions:
                    if action in seen:
                        continue
                    seen.add(action)
                    if action == "BecomeActive":
                        # v2.14.1：抑制熱鍵 / 錄音中的 active → restore 反射
                        # AppleEventReopen（Dock 點擊）不受影響、見下方分支
                        if self._is_auto_restore_suppressed():
                            continue
                        self._restore_root_if_minimized("CocoaActive")
                    elif action == "WillBecomeActive":
                        if self._is_auto_restore_suppressed():
                            continue
                        self._restore_root_if_minimized("CocoaWillActive")
                    elif action == "AppleEventReopen":
                        # Dock icon 點擊明確路徑、永遠 restore（user 主動要視窗）
                        self._restore_root_if_minimized("AppleEventReopen")
                    elif action == "WillHide":
                        self._handle_cocoa_will_hide()
        except Exception as e:
            log_error("poll_cocoa_pending_failed", error=str(e))
        finally:
            self.after(self.COCOA_POLL_MS, self._poll_cocoa_pending_actions)

    def _handle_cocoa_will_hide(self) -> None:
        """Fix 16 / 17 — 攔截 macOS「點 frontmost app dock icon = hide app」。

        判斷邏輯：WillHide 抵達時若有 iconic 視窗 → 是「minimize 後點 Dock」
        的標準行為，立刻 unhide + deiconify 把視窗叫回來。state 不是 iconic
        時跳過，避免影響真正的 ⌘H / Hide Others 行為。

        ⚠️ 此方法只能從 _poll_cocoa_pending_actions 呼叫（Tk mainloop context），
        絕對不能從 NSNotification observer 直接呼叫（PyObjC + Tk GIL 衝突 → crash）。
        """
        try:
            top = self.winfo_toplevel()
            state = top.wm_state()
            if state == "iconic":
                from AppKit import NSApplication
                # NSApp.unhide 取消 hide；同時 deiconify 把 iconic 視窗叫回
                NSApplication.sharedApplication().unhide_(None)
                top.deiconify()
                top.lift()
                log_action(
                    "dock_reopen_recovered",
                    source="CancelHide",
                    state="hide+iconic",
                )
        except Exception as e:
            log_error("cancel_hide_failed", error=str(e))

    def _poll_window_visibility(self) -> None:
        """Plan C / Fix 5c v3：每 500ms 輪詢，偵測「App 重新 active + 視窗 iconic」。

        終極保險絲：如果上面所有通知層（Tk + Cocoa）都沒觸發，這層用純
        Python 計時器確保 user 重新 focus 我們時視窗一定能在 500ms 內恢復。

        關鍵：只在 NSApp.isActive 從 False → True 轉換時動作。這避免：
        - User 主動 minimize 時誤觸發（那時 NSApp 還是 active，不算轉換）
        - 我們是 active 期間偶發的 iconic 假狀態（極罕見）
        """
        try:
            from AppKit import NSApplication
            now_active = bool(NSApplication.sharedApplication().isActive())
            transitioned_to_active = (not self._last_nsapp_active) and now_active
            self._last_nsapp_active = now_active

            if transitioned_to_active:
                # v2.14.1：熱鍵 / 錄音中抑制（與 Cocoa observer 路徑一致）
                if not self._is_auto_restore_suppressed():
                    self._restore_root_if_minimized("Poll")
        except Exception as e:
            log_error("poll_visibility_failed", error=str(e))
        finally:
            self.after(500, self._poll_window_visibility)

    def _warmup_model(self) -> None:
        """背景預熱 Whisper 模型（延遲 1.5s 後執行，避免阻礙 UI 初始化）。

        v2.28.0 骨架重寫：skeleton 模式下這是唯一會觸發 StatusSlot「Progress」
        payload 的地方（AMBER 暖機中），完成後（成功或失敗）都要把 StatusSlot
        換回常態 LevelMeter——三個入口都會排程到這裡（App 啟動、頂列模型
        下拉選單、設定視窗儲存），單點處理不用在每個呼叫端各自複製一份。
        """
        model = self._model_var.get()
        log.info(f"WARMUP: starting for model={model}")
        if self._skeleton_mode:
            self._set_status("暖機中", WARN)
            self._status_slot_show_progress()

        def _load():
            try:
                self.transcriber.warmup(model)
                backend = self.transcriber.active_backend()
                label   = "⚡ Metal" if backend == "mlx" else "CPU"
                log.info(f"WARMUP: complete (model={model} backend={backend})")
                def _done_ok():
                    if self._skeleton_mode:
                        self._set_status("就緒", ACCENT)
                        self._status_slot_show_meter()
                    else:
                        self._status_label.configure(
                            text=f"  就緒 ({model} · {label})"
                        )
                        self._status_dot.configure(text_color=SUCCESS)
                self.after(0, _done_ok)
            except Exception:
                log_error("warmup_failed", model=model)
                def _done_fail():
                    if self._skeleton_mode:
                        self._set_status("錯誤", DANGER)
                        self._status_slot_show_meter()
                    else:
                        self._status_label.configure(text="  模型載入失敗")
                        self._status_dot.configure(text_color=DANGER)
                self.after(0, _done_fail)

        threading.Thread(target=_load, daemon=True).start()

    def _refresh_polish_backend(self) -> None:
        """v2.18.0：依 cfg.polish_backend 設定 self.polish 指向 Ollama / Vertex / Hybrid。

        - "local"  → self.polish = self.ollama
        - "vertex" → self.polish = self.vertex
        - "hybrid" → self.polish = self.hybrid（v2.19.x：rule + pinyin + 可選 Gemini）
        - "off"    → self.polish = None（_start_polish 會 short-circuit）

        ollama_enabled / vertex 的 health_ok 仍由各自 client 管。
        """
        backend = getattr(self.cfg, "polish_backend", "local")
        if backend == "vertex" and self.vertex is not None:
            # v2.21.4 防呆：純 Vertex 後端必須有 project_id，否則每次潤飾都會在
            #   vertex_polish._ensure_client raise RuntimeError、靜默降級回原文——
            #   使用者「以為開了 AI 潤飾、其實每次都沒跑」完全感知不到。空 ID 就直接
            #   停用潤飾、log 標明，避免假性啟用 + 反覆 raise。
            if not (getattr(self.cfg, "vertex_project_id", "") or "").strip():
                self.polish = None
                log.warning(
                    "POLISH: backend=vertex 但 vertex_project_id 為空、潤飾已停用"
                    "（請在設定填入 Project ID）"
                )
            else:
                self.polish = self.vertex
                log.info(f"POLISH: backend = vertex (model={self.cfg.vertex_model})")
        elif backend == "hybrid":
            # v2.19.x：lazy init hybrid client（沒裝 google-genai 也能跑 Layer 1+2）
            if not hasattr(self, "hybrid") or self.hybrid is None:
                try:
                    from hybrid_polish import HybridPolishClient
                    self.hybrid = HybridPolishClient()
                except Exception:
                    log_error("hybrid_polish_init_failed")
                    self.hybrid = None
            if self.hybrid is not None:
                try:
                    self.hybrid.apply_app_config(self.cfg)
                except Exception:
                    log_error("hybrid_polish_apply_config_failed")
                self.polish = self.hybrid
                log.info(
                    f"POLISH: backend = hybrid (use_gemini="
                    f"{getattr(self.cfg, 'hybrid_use_gemini', True)}, "
                    f"vertex_project={'set' if self.cfg.vertex_project_id else 'unset'})"
                )
            else:
                self.polish = None
                log.warning("POLISH: backend=hybrid requested but init failed; polish disabled")
        elif backend == "off":
            self.polish = None
            log.info("POLISH: backend = off (no polish)")
        else:
            self.polish = self.ollama
            log.info(f"POLISH: backend = local Ollama (model={self.cfg.ollama_model})")

    def _mlx_keepalive_tick(self) -> None:
        """v2.17.4：每 5 分鐘背景跑一次 dummy ASR、防 MLX weights 被 swap。

        實機觀察：閒置 35 分鐘後第一次 ASR RTF 從 0.2 飆到 4.85（24x 慢）。
        macOS 把 3.4GB MLX weights 當作「不常用」page out 到 swap、首次推論
        要從磁碟 page in 全部權重。

        修法：每 5 分鐘背景跑一次 1.5s sine wave 推論（沿用 transcriber.warmup
        既有路徑）、強迫 macOS VM 把 weights 留在 active page。

        Guard：
          • state != idle → 跳過（避免跟 user transcribe 撞 _transcription_lock）
          • Qwen3-ASR session 還沒載入 → 跳過（不需要的 warmup）
          • 失敗 silent、log warning
        """
        try:
            if self._state == "idle":
                model = self._model_var.get()
                # 只對已載入的模型 keepalive（轉錄 lock 內、不撞用戶操作）
                def _ping():
                    # v2.20.3 N5：量 ping 自身耗時（≈ 模型 warmup 推論時間）
                    _t_start = time.perf_counter()
                    try:
                        # v2.21.6：非阻塞探測 _transcription_lock——拿不到就跳過這次
                        #   ping（90s 後還有下一次）。舊行為是 ping 跟使用者「先搶先贏」
                        #   平等競爭：實測 26813 筆 ping 佔鎖中位 1.5s、最糟 49s，
                        #   使用者停止錄音撞上 ping 就得排隊等（碰撞率 ~1.7%）。
                        #   背景保養永遠讓位給使用者輸入。
                        #   （lock 是 threading.Lock 非 RLock、probe 後必須先放掉
                        #   再呼叫 warmup——中間的微小 race 視窗可接受。）
                        _lk = getattr(self.transcriber, "_transcription_lock", None)
                        if _lk is not None:
                            if not _lk.acquire(blocking=False):
                                log.info("MLX_KEEPALIVE: lock busy, skip this ping")
                                return
                            _lk.release()
                        # v2.20.2：沿用 transcriber.warmup() —— 內部已換成
                        # _make_keepalive_audio()（1.5s formant-based 假人聲、低音量）。
                        # 走 session.transcribe() 直呼叫、不過 Transcriber.transcribe()
                        # 外層的 RMS gate / Silero VAD，所以不會被擋掉。
                        self.transcriber.warmup(model)
                        # v2.21.6：補呼叫 mark_keepalive_ping()——此方法 v2.20.3 定義後
                        #   從未被呼叫、audit 的 seconds_since_last_keepalive_ping 欄位
                        #   一直是 None（壞掉的觀測性、151 筆離群分析全失真）。
                        try:
                            self.transcriber.mark_keepalive_ping()
                        except Exception:
                            pass
                        elapsed_ms = int((time.perf_counter() - _t_start) * 1000)
                        log.info(
                            f"MLX_KEEPALIVE: ping done (model={model}, "
                            f"payload=formant-1.5s, elapsed_ms={elapsed_ms})"
                        )
                        # v2.20.3 N5：寫 keepalive_ping audit event 含記憶體 snapshot。
                        # 用途：監測 RSS / MLX Metal cache 隨時間漂移、抓 leak/swap-out。
                        try:
                            import audit_log    # noqa: PLC0415
                            import psutil       # noqa: PLC0415
                            proc = psutil.Process()
                            rss_mb = proc.memory_info().rss / 1024 / 1024
                            # v2.21.6 量測修正：舊版把 get_active_memory()（模型權重
                            #   常駐量、~3.5GB 恆定）誤標成 "cache"，導致 v2.21.4 的
                            #   cache 上限「看起來沒生效」其實是量錯東西。現在分開記：
                            #   cache = 可回收暫存池（set_cache_limit 管的）、
                            #   active = 權重常駐量（set_wired_limit 釘的）。
                            metal_mb: Optional[float] = None
                            active_mb: Optional[float] = None
                            try:
                                import mlx.core as mx  # noqa: PLC0415
                                metal_mb = (
                                    mx.get_cache_memory() / 1024 / 1024
                                    if hasattr(mx, "get_cache_memory")
                                    else mx.metal.get_cache_memory() / 1024 / 1024
                                )
                                active_mb = (
                                    mx.get_active_memory() / 1024 / 1024
                                    if hasattr(mx, "get_active_memory")
                                    else mx.metal.get_active_memory() / 1024 / 1024
                                )
                            except Exception:
                                metal_mb = None
                                active_mb = None
                            # since_last_ping_s：首次 ping 為 None（不知道間隔）
                            now_pc = time.perf_counter()
                            prev = self._last_ping_at
                            since_last: Optional[float] = (
                                round(now_pc - prev, 2) if prev is not None else None
                            )
                            self._last_ping_at = now_pc
                            audit_log.write_event(
                                "keepalive_ping",
                                "",
                                elapsed_ms=elapsed_ms,
                                process_rss_mb=round(rss_mb, 1),
                                mlx_metal_cache_mb=(
                                    round(metal_mb, 1) if metal_mb is not None else None
                                ),
                                mlx_active_mb=(
                                    round(active_mb, 1) if active_mb is not None else None
                                ),
                                since_last_ping_s=since_last,
                                model=model,
                            )
                        except Exception:
                            # 觀測性失敗不能影響 keepalive 本身
                            pass
                    except Exception:
                        log_error("mlx_keepalive_ping_failed", model=model)
                threading.Thread(target=_ping, daemon=True).start()
        except Exception:
            log_error("mlx_keepalive_tick_failed")
        finally:
            # 永遠重新排程下次（即使這次失敗）
            self.after(self.MLX_KEEPALIVE_INTERVAL_MS, self._mlx_keepalive_tick)

    def _warmup_ollama(self) -> None:
        """v2.17.1：背景預載 Ollama polish model 進 VRAM。

        延遲 2.5s 跑（避開 Whisper warmup 跟 health_check）、用獨立 thread。
        Ollama 沒裝 / 沒啟動 / 模型沒下載：silent fail、user 第一次 polish
        仍會等 cold load（fallback 體驗、不會崩）。

        配合 _OLLAMA_KEEP_ALIVE=-1：模型載入後永久駐留 VRAM、user 用任何
        polish 都不會 cold load。
        """
        if not self.cfg.ollama_enabled:
            return
        log.info(f"OLLAMA_WARMUP: starting for model={self.cfg.ollama_model}")

        def _load():
            try:
                ok = self.ollama.warmup()
                if ok:
                    log.info("OLLAMA_WARMUP: complete (model loaded into VRAM, persistent)")
                else:
                    log.warning(
                        "OLLAMA_WARMUP: failed (Ollama not running or model missing) — "
                        "polish 第一次使用會 cold load"
                    )
            except Exception:
                log_error("ollama_warmup_failed")

        threading.Thread(target=_load, daemon=True).start()

    def _show_placeholder(self) -> None:
        """沒有任何 UtteranceBlock 時，顯示置中的佔位符。

        skeleton 模式：Aperture 空狀態組合（R⌘ 鍵帽 + 標題 + 說明 + 「最近」
        4 筆，見 _build_empty_state_v2）——「空狀態不是空的」，用歷史紀錄
        填掉，而不是單純一句「尚無內容」。chamber 逃生門維持原本的單行文字。
        """
        if self._placeholder_label is not None or self._utterance_blocks:
            return
        if self._skeleton_mode:
            self._placeholder_label = self._build_empty_state_v2()
            self._placeholder_label.pack(fill="x", pady=(SPACE_XL, 0))
            return
        self._placeholder_label = ctk.CTkLabel(
            self._blocks_container,
            text="（等待第一次錄音...）",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 14),
            text_color=TEXT_4,
        )
        self._placeholder_label.pack(pady=40)

    def _clear_placeholder(self) -> None:
        """新增 block 之前清掉佔位符。"""
        if self._placeholder_label is not None:
            try:
                self._placeholder_label.destroy()
            except Exception:
                pass
            self._placeholder_label = None

    # ── Aperture 空狀態＋「最近」區（skeleton 模式限定）─────────────────────

    def _build_empty_state_v2(self) -> ctk.CTkFrame:
        """空狀態組合：R⌘ 鍵帽（依目前設定的熱鍵動態顯示）+ 標題 + 說明，
        下方接「最近」4 筆（history_store 未啟用或沒有紀錄時該區塊不顯示）。
        """
        wrap = ctk.CTkFrame(self._blocks_container, fg_color="transparent")

        plate = ctk.CTkFrame(wrap, fg_color="transparent")
        plate.pack(fill="x", pady=(34, 40))

        # R⌘ 鍵帽：74×60 radius 11 + 下緣多露 3px 的同色底框做出「立體感」
        # （CTkFrame 的 corner_radius 是四角統一套用、無法只加粗某一邊
        # border，用兩片堆疊模擬鍵帽厚度，比較貼近真實鍵帽的視覺）。
        keycap_wrap = ctk.CTkFrame(plate, fg_color="transparent", width=74, height=63)
        keycap_wrap.pack_propagate(False)
        keycap_wrap.pack()
        ctk.CTkFrame(
            keycap_wrap, width=74, height=60, corner_radius=11, fg_color=KEY_LINE,
        ).place(x=0, y=3)
        keycap = ctk.CTkFrame(
            keycap_wrap, width=74, height=60, corner_radius=11,
            fg_color=KEY_BG, border_width=1, border_color=KEY_LINE,
        )
        keycap.place(x=0, y=0)
        ctk.CTkLabel(
            keycap, text=self.cfg.format_hotkey_display(),
            font=ctk.CTkFont(FONT_FAMILY_MONO, 20), text_color=TEXT_1,
        ).place(relx=0.5, rely=0.46, anchor="center")

        ctk.CTkLabel(
            plate, text="按一下開始，再按一下結束",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 16, "bold"), text_color=TEXT_1,
        ).pack(pady=(20, 0))
        ctk.CTkLabel(
            plate, text="轉錄會出現在這裡，並依設定自動貼到最前景的輸入框",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13), text_color=TEXT_3,
        ).pack(pady=(7, 0))

        recent = self._build_recent_block(wrap, count=4, exclude_ids=set())
        if recent is not None:
            recent.pack(fill="x")

        return wrap

    def _refresh_recent_section(self) -> None:
        """依「本次內容高度 ≤ 520pt」規則決定要不要在 blocks 下方顯示「最近」
        區（3 筆）。空狀態（無 block）的 4 筆是 _build_empty_state_v2 自己的
        一部分，不歸這支管。
        """
        if not self._skeleton_mode:
            return
        frame = self._recent_below_frame
        for w in frame.winfo_children():
            w.destroy()
        frame.pack_forget()
        if not self._utterance_blocks:
            return
        try:
            self._blocks_container.update_idletasks()
            total_h = sum(b.winfo_height() for b in self._utterance_blocks)
        except Exception:
            total_h = 0
        if total_h > 520:
            return
        exclude_ids = {
            getattr(b, "history_id", None) for b in self._utterance_blocks
        } - {None}
        inner = self._build_recent_block(frame, count=3, exclude_ids=exclude_ids)
        if inner is not None:
            inner.pack(fill="x")
            frame.pack(fill="x", pady=(SPACE_LG, 0))

    def _build_recent_block(
        self, parent, count: int, exclude_ids: set,
    ) -> Optional[ctk.CTkFrame]:
        """「最近」eyebrow +最多 count 筆歷史列 +「全部歷史 ›」連結。

        history_store 未啟用、初始化尚未跑到（_build_ui 早於 __init__ 設定
        self.history_store，見 __init__ 補的那次重建）、或沒有可顯示的紀錄
        時回 None，呼叫端不 pack。
        """
        store = getattr(self, "history_store", None)
        if store is None:
            return None
        try:
            entries = store.list_recent(limit=count + len(exclude_ids))
        except Exception:
            return None
        entries = [e for e in entries if e.id not in exclude_ids][:count]
        if not entries:
            return None

        box = ctk.CTkFrame(parent, fg_color="transparent")

        head = ctk.CTkFrame(box, fg_color="transparent")
        head.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            head, text="最近", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11), text_color=TEXT_4,
        ).pack(side="left")
        link = ctk.CTkLabel(
            head, text="全部歷史 ›", anchor="e", cursor="hand2",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11), text_color=CYAN_TEXT,
        )
        link.pack(side="right")
        link.bind("<Button-1>", lambda e: self._open_history())

        for entry in entries:
            self._build_recent_row(box, entry)

        return box

    def _build_recent_row(self, parent, entry) -> None:
        """「最近」區單一列：高 42 radius 7、時間欄寬 74、gap 14、
        摘要單行截斷、hover 底 ROW_HI、點擊開啟歷史紀錄視窗。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=7, height=42)
        row.pack(fill="x", pady=(0, 1))
        row.pack_propagate(False)

        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=12)

        import datetime as _dt
        ctk.CTkLabel(
            inner, text=_dt.datetime.fromtimestamp(entry.timestamp).strftime("%H:%M"),
            width=74, anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11), text_color=TEXT_3,
        ).pack(side="left")
        ctk.CTkLabel(
            inner, text=entry.summary(46), anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12), text_color=TEXT_2,
        ).pack(side="left", padx=(14, 0), fill="x", expand=True)

        def _on_enter(_e=None, r=row):
            r.configure(fg_color=ROW_HI)

        def _on_leave(_e=None, r=row):
            r.configure(fg_color="transparent")

        for w in (row, inner, *_walk_children(inner)):
            w.bind("<Enter>", _on_enter, add="+")
            w.bind("<Leave>", _on_leave, add="+")
            w.bind("<Button-1>", lambda e: self._open_history())

    def _get_result_text(self) -> str:
        """所有 block 串接（換行分隔）；給「存檔」用。"""
        if not self._utterance_blocks:
            return ""
        return "\n\n".join(b.get_current_text() for b in self._utterance_blocks)

    def _show_toast(
        self, message: str, color: Optional[str] = None, duration_ms: int = 2800,
    ) -> None:
        """在視窗右下角顯示一個浮動 toast，duration_ms 毫秒後自動消失。

        v2.27.0：綠色（SUCCESS）語意收窄後只留給「真正的成功事件」——目前
        唯一的落點是自動貼上成功。color 給定時 toast 邊框改該色搭配較短
        的 duration_ms；其餘呼叫端不傳就是原本的中性樣式，不受影響。
        """
        toast = ctk.CTkFrame(
            self, corner_radius=10,
            fg_color=SURF_2,
            border_width=1, border_color=(color or SURF_3),
        )
        ctk.CTkLabel(
            toast, text=message,
            font=ctk.CTkFont("SF Pro Text", 13),
            text_color=TEXT_1, padx=18, pady=10,
        ).pack()
        toast.place(relx=1.0, rely=1.0, x=-20, y=-52, anchor="se")
        self.after(duration_ms, toast.destroy)

    def on_close(self) -> None:
        """視窗關閉時：停止 pynput 監聽器、錄音、清 Cocoa observer，避免資源洩漏。

        Fix Cluster A / 2026-05-23：之前完全沒呼叫 `removeObserver_` 或
        `removeEventHandlerForEventClass_andEventID_`。theme switch 走 execv 路徑
        relaunch 時、process image 雖然 replace、但同 PID 下 AppKit / NSAppleEventManager
        留有 dangling block reference；連切 5 次主題後出現重複 Cocoa 事件 dispatch
        + 緩慢記憶體 leak。此修法在 on_close 顯式撤除 4 個觀察者 + 1 個 AppleEvent
        handler，配合 cleanup-first 順序確保 spawn 新 process 前完全乾淨。
        """
        log.info("GUI: on_close")
        try:
            self.hotkey_mgr.stop()
        except Exception:
            log_error("hotkey_mgr_stop_on_close_failed")
        # v2.21.0 Phase M：撤除 CoreAudio 麥克風監聽（防 dangling listener）
        if hasattr(self.recorder, "stop_device_monitor"):
            try:
                self.recorder.stop_device_monitor()
            except Exception:
                log_error("stop_device_monitor_on_close_failed")
        if self.recorder.is_recording():
            log.info("GUI: recorder still active on close — stopping")
            try:
                self.recorder.stop()
            except Exception:
                log_error("recorder_stop_on_close_failed")
        # Phase 4.3 mini 視窗也要 destroy，避免主視窗關了 HUD 還浮著
        self._destroy_mini_window()

        # Fix Cluster A：撤除 Cocoa NSNotification observer（3 個：Did/Will BecomeActive + WillHide）
        try:
            obs = getattr(self, "_cocoa_activation_observer", None)
            if obs is not None:
                from Foundation import NSNotificationCenter
                NSNotificationCenter.defaultCenter().removeObserver_(obs)
                self._cocoa_activation_observer = None
                log.info("COCOA: NSNotification observer removed on close")
        except Exception:
            log_error("cocoa_observer_remove_on_close_failed")

        # Fix Cluster A：撤除 NSAppleEventManager kAEReopenApplication handler
        try:
            reopen_handler = getattr(self, "_cocoa_reopen_handler", None)
            if reopen_handler is not None:
                from Foundation import NSAppleEventManager
                _kCoreEventClass      = 0x61657674   # 'aevt'
                _kAEReopenApplication = 0x72617070   # 'rapp'
                NSAppleEventManager.sharedAppleEventManager() \
                    .removeEventHandlerForEventClass_andEventID_(
                        _kCoreEventClass, _kAEReopenApplication,
                    )
                self._cocoa_reopen_handler = None
                log.info("COCOA: kAEReopenApplication handler removed on close")
        except Exception:
            log_error("cocoa_reopen_handler_remove_on_close_failed")


# ─────────────────────────────────────────────────────────────────────────────
#  SETTINGS WINDOW
# ─────────────────────────────────────────────────────────────────────────────

class SettingsWindow(ctk.CTkToplevel):
    """設定視窗（modal）。

    資訊架構（v2.28.0 重排）：兩層——側欄 6 分類（錄音／辨識／輸出／外觀／
    資料／關於，對應「錄音→辨識→輸出」主流程 + 3 個非流程類）× 每類最多
    5 列；最上面一個搜尋框可直接跳到任一列並高亮。AI 潤飾裡較深的欄位
    （Ollama 模型／Vertex AI／情境路由／Prompt 熱重載）收進「進階設定」
    對話框，主頁面維持不捲動。

    使用者按「儲存」時呼叫 on_save_cb(new_cfg)；按「取消」不修改原 cfg。
    """

    _WIN_W = 840
    _WIN_H = 680
    _SIDEBAR_W = 172
    _LEVEL_METER_W = 460
    _LEVEL_METER_H = 56
    # (分類 key, 側欄顯示文字, icons.py 圖示名稱) —— 順序即側欄由上到下順序
    _CATEGORIES = (
        ("recording",  "錄音", "mic"),
        ("stt",        "辨識", "sparkles"),
        ("output",     "輸出", "keyboard"),
        ("appearance", "外觀", "settings"),
        ("data",       "資料", "folder"),
        ("about",      "關於", "file-text"),
    )

    def __init__(self, parent, cfg: Config, on_save_cb) -> None:
        """初始化設定視窗，深拷貝 cfg 以避免使用者取消時汙染原始設定。"""
        super().__init__(parent)
        self._parent     = parent                 # AppWindow（訪問 _prompt_reloader 用）
        self.cfg         = Config(**cfg.__dict__)  # 深拷貝：取消時不修改原 cfg
        self._on_save_cb = on_save_cb
        self.title("設定")
        self.geometry(f"{self._WIN_W}x{self._WIN_H}")
        self.resizable(False, False)
        self.configure(fg_color=BG)

        # ── 搜尋 + 分類導覽狀態（v2.28.0 兩層 IA 重排）────────────────────────
        self._row_frames: dict[str, ctk.CTkFrame] = {}       # row_id → outer frame（搜尋高亮用）
        self._search_index: dict[str, tuple[str, str]] = {}  # 關鍵字 → (category, row_id)
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._active_category: Optional[str] = None

        # ── 輸入電平即時表（「錄音」分類唯一 Canvas；只在該頁可見時才跑）───────
        self._level_engine = WaveformEngine(n_bars=WAVE_N_BARS_MAIN)
        self._level_canvas: Optional[tk.Canvas] = None
        self._level_stream = None            # 獨立於 AudioRecorder 的最小 sd.InputStream
        self._level_stream_failed = False    # 本次停留該頁期間已失敗過一次，別狂 retry
        self._level_after_id: Optional[str] = None
        self._level_rms = 0.0
        self._level_last_tick = 0.0
        self._level_state_start = 0.0
        self.bind("<Destroy>", self._on_window_destroyed)

        self.grab_set()
        self._build()

    def _build(self) -> None:
        """建立搜尋列 + 側欄導覽 + 6 個分類頁 + 底部儲存／取消按鈕。"""
        # ── 搜尋列 ───────────────────────────────────────────────────────────
        search_bar = ctk.CTkFrame(self, height=52, fg_color=SURF_1, corner_radius=0)
        search_bar.pack(fill="x", side="top")
        search_bar.pack_propagate(False)
        self._search_var = ctk.StringVar(value="")
        search_entry = ctk.CTkEntry(
            search_bar, textvariable=self._search_var,
            placeholder_text="搜尋設定…（例如：字典、快捷鍵、主題）按 Enter 跳過去",
            height=32, corner_radius=8,
            fg_color=SURF_2, border_color=SURF_3, text_color=TEXT_1,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
        )
        search_entry.pack(fill="x", padx=SPACE_LG, pady=SPACE_SM)
        search_entry.bind("<Return>", self._run_search)

        ctk.CTkFrame(self, height=1, fg_color=SURF_3, corner_radius=0).pack(fill="x", side="top")

        # ── 主體：側欄 + 內容 ────────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, side="top")

        sidebar = ctk.CTkFrame(body, width=self._SIDEBAR_W, fg_color=SURF_1, corner_radius=0)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        for key, label, icon_name in self._CATEGORIES:
            btn = ctk.CTkButton(
                sidebar, text=f"  {label}",
                image=get_icon(icon_name, 16, TEXT_3),
                compound="left", anchor="w",
                width=self._SIDEBAR_W - 16, height=40, corner_radius=8,
                fg_color="transparent", hover_color=SURF_2,
                text_color=TEXT_3,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
                command=lambda k=key: self._select_category(k),
            )
            first = key == self._CATEGORIES[0][0]
            btn.pack(fill="x", padx=SPACE_SM, pady=(SPACE_SM if first else 2, 2))
            self._nav_buttons[key] = btn

        ctk.CTkFrame(body, width=1, fg_color=SURF_3, corner_radius=0).pack(side="left", fill="y")

        content = ctk.CTkFrame(body, fg_color="transparent")
        content.pack(side="left", fill="both", expand=True)

        builders = {
            "recording":  self._build_page_recording,
            "stt":        self._build_page_stt,
            "output":     self._build_page_output,
            "appearance": self._build_page_appearance,
            "data":       self._build_page_data,
            "about":      self._build_page_about,
        }
        for key, _label, _icon in self._CATEGORIES:
            page = ctk.CTkFrame(content, fg_color="transparent")
            self._pages[key] = page
            builders[key](page)

        self._select_category(self._CATEGORIES[0][0])

        # ── Buttons ──────────────────────────────────────────────────────────
        ctk.CTkFrame(self, height=1, fg_color=SURF_3, corner_radius=0).pack(fill="x", side="bottom")
        btn_bar = ctk.CTkFrame(self, height=60, fg_color=SURF_1, corner_radius=0)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)

        inner = ctk.CTkFrame(btn_bar, fg_color="transparent")
        inner.pack(side="right", padx=20, pady=SPACE_MD)

        ctk.CTkButton(
            inner, text="取消", width=88, height=36, corner_radius=8,
            fg_color=SURF_2, text_color=TEXT_2,
            hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            font=ctk.CTkFont("SF Pro Text", 14),
            command=self.destroy,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            inner, text="儲存", width=88, height=36, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HV,
            text_color=TEXT_1,
            font=ctk.CTkFont("SF Pro Text", 14, "bold"),
            command=self._save,
        ).pack(side="left")

    # ── 分類切換 ─────────────────────────────────────────────────────────────

    def _select_category(self, key: str) -> None:
        """切換側欄分類：顯示對應頁面、更新導覽按鈕高亮。

        離開「錄音」頁一定要停掉電平表（render loop + 麥克風串流）——那是
        設定視窗唯一允許跑 Canvas render loop 的分類頁，看不到就不該繼續
        佔用麥克風；進入「錄音」頁才重新啟動。
        """
        if key == self._active_category:
            return
        if self._active_category == "recording":
            self._stop_level_meter()
        for page in self._pages.values():
            page.pack_forget()
        self._pages[key].pack(fill="both", expand=True)
        for k, btn in self._nav_buttons.items():
            active = k == key
            btn.configure(
                fg_color=(SURF_2 if active else "transparent"),
                text_color=(TEXT_1 if active else TEXT_3),
            )
        self._active_category = key
        if key == "recording":
            self._start_level_meter()

    # ── 通用列 / 搜尋登記 helper ─────────────────────────────────────────────

    def _page_header(self, page: ctk.CTkFrame, title: str) -> None:
        """每個分類頁最上面的大標題（側欄已經有分類名，這裡再放一次是為了
        『搜尋跳過來、一眼確認自己在哪頁』）。"""
        ctk.CTkLabel(
            page, text=title, anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_UI, 17, "bold"), text_color=TEXT_1,
        ).pack(fill="x", padx=SPACE_LG, pady=(SPACE_LG, SPACE_SM))

    def _register_row(self, category: str, row_id: str, frame: ctk.CTkFrame, *terms: str) -> None:
        """登記 row frame（搜尋高亮用）與搜尋關鍵字（label 本身 + 自訂 alias）。"""
        self._row_frames[row_id] = frame
        for t in terms:
            if t:
                self._search_index[t] = (category, row_id)

    def _row(
        self, parent, category: str, row_id: str, label: str,
        desc: Optional[str], widget_fn, *, search_terms: tuple = (),
    ) -> tuple:
        """畫一列設定：左標籤（13pt）／說明（11pt TEXT_3，desc=None 時省略），
        右控制項（由 widget_fn 畫進已經 side="right" 的 wrap frame）。

        回傳 (outer, desc_label)：outer 讓呼叫端需要在列下方繼續 pack 額外
        內容時用（例如個人字典的路徑欄）；desc_label 給需要動態改文字的
        列用（例如模型說明會隨下拉選單即時更新）。desc 用 None（不是空
        字串）代表「這列沒有說明」——刻意跟空字串區分，這樣「說明文字
        剛好是空字串」的邊界情況（例如 config 損毀時 MODEL_INFO 查無此
        模型）仍然會建立一個空白 label，呼叫端存的 handle 才不會是 None
        而在後續 .configure() 時噴 AttributeError。
        """
        has_desc = desc is not None
        outer = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=8)
        outer.pack(fill="x", padx=SPACE_LG, pady=(0, SPACE_XS))

        top = ctk.CTkFrame(outer, fg_color="transparent")
        top.pack(fill="x", padx=SPACE_MD, pady=(SPACE_SM, 2 if has_desc else SPACE_SM))

        ctk.CTkLabel(
            top, text=label, anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13), text_color=TEXT_1,
        ).pack(side="left")

        wrap = ctk.CTkFrame(top, fg_color="transparent")
        wrap.pack(side="right")
        widget_fn(wrap)

        desc_label = None
        if has_desc:
            desc_label = ctk.CTkLabel(
                outer, text=desc, anchor="w", justify="left",
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 11), text_color=TEXT_3,
                wraplength=480,
            )
            desc_label.pack(fill="x", padx=SPACE_MD, pady=(0, SPACE_SM))

        self._register_row(category, row_id, outer, label, *search_terms)
        return outer, desc_label

    def _make_sw(self, var, accent: Optional[str] = None):
        """回傳一個 widget_fn：在給定 parent 裡放一顆樣式統一的 switch。"""
        style = dict(
            progress_color=(accent or ACCENT),
            button_color=TEXT_1, button_hover_color=TEXT_2, fg_color=SURF_3,
        )

        def fn(r):
            ctk.CTkSwitch(
                r, text="", variable=var, onvalue=True, offvalue=False, **style,
            ).pack(side="right")

        return fn

    def _var(self, attr: str, factory):
        """回傳已存在的 var；不存在才用 factory() 建一個新的。

        給「AI 潤飾進階設定」對話框用——那個對話框每次開啟都整個重建
        widget（跟 HotkeyBindDialog 同一套模式），但底層 var 要跨開關保留
        使用者還沒按主視窗「儲存」的暫時改動，不能每次重開都從 self.cfg
        重新蓋過去。
        """
        if getattr(self, attr, None) is None:
            setattr(self, attr, factory())
        return getattr(self, attr)

    # ── 分類頁：錄音 ─────────────────────────────────────────────────────────

    def _build_page_recording(self, page: ctk.CTkFrame) -> None:
        """錄音分類：全域快捷鍵、麥克風、輸入電平（即時）、靜音自動停止。"""
        self._page_header(page, "錄音")

        # 全域快捷鍵 --------------------------------------------------------
        def hotkey_row(r):
            self._hk_label = ctk.CTkLabel(
                r, text=format_hotkey(self.cfg.hotkey),
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 13, "bold"),
                fg_color=SURF_2, text_color=TEXT_1,
                corner_radius=8, padx=SPACE_MD, pady=SPACE_XS,
            )
            self._hk_label.pack(side="left", padx=(0, 8))
            ctk.CTkButton(
                r, text="重新綁定", width=80, height=28, corner_radius=8,
                fg_color=ACCENT_BG, hover_color=ACCENT,
                border_width=1, border_color=ACCENT, text_color=ACCENT_HV,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
                command=self._rebind_hotkey,
            ).pack(side="left")

        self._row(
            page, "recording", "recording_hotkey", "全域快捷鍵", None, hotkey_row,
            search_terms=("快捷鍵", "熱鍵", "hotkey", "重新綁定"),
        )

        # 麥克風來源 ----------------------------------------------------------
        # v2.21.0 Phase M：優先用 app window 的 recorder.refresh_portaudio() 重新
        # 初始化 PortAudio 拿「最新」裝置清單，插了新麥克風不用重開整個 App。
        from recorder import AudioRecorder as _AR
        try:
            _rec = getattr(self.master, "recorder", None)
            if _rec is not None and hasattr(_rec, "refresh_portaudio"):
                self._available_devices = _rec.refresh_portaudio()
            else:
                self._available_devices = _AR.list_devices()
        except Exception:
            self._available_devices = []
            log_error("settings_list_devices_failed")
        self._device_values = ["（系統預設）"] + [d["name"] for d in self._available_devices]
        _current_dev = self.cfg.input_device
        if _current_dev and _current_dev in (d["name"] for d in self._available_devices):
            initial = _current_dev
        else:
            initial = "（系統預設）"
        self._device_var = ctk.StringVar(value=initial)

        def device_row(r):
            ctk.CTkOptionMenu(
                r, values=self._device_values, variable=self._device_var,
                width=220, height=30, corner_radius=8,
                fg_color=SURF_2, button_color=SURF_2, button_hover_color=SURF_3,
                dropdown_fg_color=SURF_1, text_color=TEXT_1,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
                command=self._on_preview_device_changed,
            ).pack(side="right")

        self._row(
            page, "recording", "recording_mic", "麥克風來源",
            f"偵測到 {len(self._available_devices)} 個輸入裝置；「系統預設」會跟隨系統設定。",
            device_row, search_terms=("麥克風", "裝置", "輸入裝置", "mic", "microphone"),
        )

        # 輸入電平（即時）------------------------------------------------------
        level_outer = ctk.CTkFrame(page, fg_color="transparent")
        level_outer.pack(fill="x", padx=SPACE_LG, pady=(0, SPACE_XS))
        ctk.CTkLabel(
            level_outer, text="輸入電平（即時）", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13), text_color=TEXT_1,
        ).pack(fill="x", padx=SPACE_MD, pady=(SPACE_SM, 4))
        canvas_card = ctk.CTkFrame(
            level_outer, fg_color=SURF_1, corner_radius=8,
            border_width=1, border_color=SURF_3,
        )
        canvas_card.pack(fill="x", padx=SPACE_MD, pady=(0, 4))
        self._level_canvas = tk.Canvas(
            canvas_card, width=self._LEVEL_METER_W, height=self._LEVEL_METER_H,
            bg=SURF_1, highlightthickness=0, bd=0,
        )
        self._level_canvas.pack(padx=8, pady=8)
        ctk.CTkLabel(
            level_outer, text="離開此頁會自動停止讀取麥克風；跟正式錄音互不干擾。",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11), text_color=TEXT_3, anchor="w",
        ).pack(fill="x", padx=SPACE_MD, pady=(0, SPACE_SM))
        self._register_row(
            "recording", "recording_level", level_outer,
            "輸入電平（即時）", "電平表", "麥克風音量", "level", "meter",
        )

        # 靜音自動停止 ----------------------------------------------------------
        self._recording_watchdog_var = ctk.BooleanVar(
            value=getattr(self.cfg, "recording_watchdog", True)
        )
        self._row(
            page, "recording", "recording_watchdog", "靜音自動停止",
            "連續 8 分鐘沒偵測到語音會提醒；15 分鐘會自動停止錄音並保留內容（防止忘記關）。",
            self._make_sw(self._recording_watchdog_var, ACCENT),
            search_terms=("看門狗", "watchdog", "自動停止", "忘記關"),
        )

    # ── 分類頁：辨識 ─────────────────────────────────────────────────────────

    def _build_page_stt(self, page: ctk.CTkFrame) -> None:
        """辨識分類：模型、語言、邊錄邊轉錄門檻（VAD 靈敏度）、個人字典。"""
        self._page_header(page, "辨識")

        self._model_var = ctk.StringVar(value=self.cfg.model)

        def model_row(r):
            ctk.CTkOptionMenu(
                r, values=list(MODEL_INFO.keys()), variable=self._model_var,
                width=180, height=30, corner_radius=8,
                fg_color=SURF_2, button_color=SURF_2, button_hover_color=SURF_3,
                dropdown_fg_color=SURF_1, text_color=TEXT_1,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
                command=self._on_model_preview,
            ).pack(side="right")

        _, self._model_desc = self._row(
            page, "stt", "stt_model", "模型大小",
            MODEL_INFO.get(self.cfg.model, ""), model_row,
            search_terms=("模型", "whisper", "qwen"),
        )

        self._lang_var = ctk.StringVar(value=self.cfg.language)

        def lang_row(r):
            ctk.CTkOptionMenu(
                r, values=list(LANGUAGE_OPTIONS.keys()), variable=self._lang_var,
                width=112, height=30, corner_radius=8,
                fg_color=SURF_2, button_color=SURF_2, button_hover_color=SURF_3,
                dropdown_fg_color=SURF_1, text_color=TEXT_1,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
            ).pack(side="right")

        self._row(
            page, "stt", "stt_lang", "辨識語言", None, lang_row,
            search_terms=("語言", "language"),
        )

        # 邊錄邊轉錄門檻（Silero VAD 靈敏度）------------------------------------
        self._silero_vad_enabled_var = ctk.BooleanVar(
            value=getattr(self.cfg, "silero_vad_enabled", True)
        )
        self._silero_vad_threshold_var = ctk.StringVar(
            value=str(getattr(self.cfg, "silero_vad_threshold", 0.35))
        )

        def vad_row(r):
            ctk.CTkEntry(
                r, textvariable=self._silero_vad_threshold_var,
                width=56, height=30, corner_radius=8,
                fg_color=SURF_2, border_color=SURF_3, text_color=TEXT_1,
                font=ctk.CTkFont(FONT_FAMILY_MONO, 13), justify="right",
            ).pack(side="right", padx=(8, 0))
            self._make_sw(self._silero_vad_enabled_var, ACCENT)(r)

        self._row(
            page, "stt", "stt_vad", "邊錄邊轉錄門檻",
            "語音偵測靈敏度（Silero VAD）：數值越低，愈容易把小聲／背景雜音當語音送去轉錄；"
            "數值越高，只有明顯人聲才會被錄進結果。範圍 0.0–1.0，預設 0.35。",
            vad_row, search_terms=("VAD", "語音偵測", "門檻", "靈敏度", "雜音"),
        )

        # 個人字典 --------------------------------------------------------------
        self._dict_enabled_var = ctk.BooleanVar(value=self.cfg.dictionary_enabled)
        dict_outer, _ = self._row(
            page, "stt", "stt_dict", "個人字典",
            "字典術語會注入 Whisper 與 Ollama prompt，提升專有名詞辨識率"
            "（例：Claude／Cloud 同音字消歧）。",
            self._make_sw(self._dict_enabled_var, ACCENT),
            search_terms=("字典", "術語", "dictionary", "同音字"),
        )
        dict_detail = ctk.CTkFrame(dict_outer, fg_color=SURF_2, corner_radius=8)
        dict_detail.pack(fill="x", padx=SPACE_MD, pady=(0, SPACE_SM))

        path_row = ctk.CTkFrame(dict_detail, fg_color="transparent", height=40)
        path_row.pack(fill="x", padx=SPACE_MD, pady=(SPACE_SM, 2))
        path_row.pack_propagate(False)
        ctk.CTkLabel(
            path_row, text="字典檔路徑", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12), text_color=TEXT_2,
        ).pack(side="left")
        self._dict_path_var = ctk.StringVar(value=self.cfg.dictionary_path)
        ctk.CTkEntry(
            path_row, textvariable=self._dict_path_var,
            placeholder_text="(預設 ~/.whisper_app/dictionary.json)",
            width=220, height=28, corner_radius=6,
            fg_color=SURF_1, border_color=SURF_4, text_color=TEXT_3,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11),
        ).pack(side="right")

        status_row = ctk.CTkFrame(dict_detail, fg_color="transparent", height=36)
        status_row.pack(fill="x", padx=SPACE_MD, pady=(2, SPACE_SM))
        status_row.pack_propagate(False)
        self._dict_status_label = ctk.CTkLabel(
            status_row, text=self._compute_dict_status(), anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11), text_color=TEXT_3,
        )
        self._dict_status_label.pack(side="left")
        ctk.CTkButton(
            status_row, text="用預設編輯器開啟", width=140, height=26, corner_radius=6,
            fg_color=SURF_1, text_color=TEXT_1, hover_color=SURF_3,
            border_width=1, border_color=SURF_4,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
            command=self._open_dictionary_file,
        ).pack(side="right")

    # ── 分類頁：輸出 ─────────────────────────────────────────────────────────

    def _build_page_output(self, page: ctk.CTkFrame) -> None:
        """輸出分類：自動貼上、自動複製、AI 潤飾、貼上策略、追加錄音結果。

        Ollama 模型／Vertex AI／情境路由／Prompt 熱重載這些較深的欄位收進
        「AI 潤飾」列的「進階…」按鈕（見 _open_polish_advanced），這頁本身
        維持 5 列、不捲動。
        """
        self._page_header(page, "輸出")

        self._autopaste_var = ctk.BooleanVar(value=self.cfg.auto_paste)
        self._row(
            page, "output", "output_paste", "語音轉文字後自動貼上",
            "轉錄完成後自動模擬 ⌘V 貼到目前游標位置。",
            self._make_sw(self._autopaste_var, INDIGO),
            search_terms=("自動貼上", "貼上", "paste", "⌘V"),
        )

        self._autocopy_var = ctk.BooleanVar(value=self.cfg.auto_copy)
        self._row(
            page, "output", "output_copy", "轉錄後自動複製",
            "轉錄完成後自動把文字複製到剪貼簿。",
            self._make_sw(self._autocopy_var),
            search_terms=("自動複製", "複製", "copy", "剪貼簿"),
        )

        self._polish_backend_var = ctk.StringVar(
            value=getattr(self.cfg, "polish_backend", "local")
        )

        def backend_row(r):
            self._polish_backend_btns: dict[str, ctk.CTkButton] = {}
            for value, label in (
                ("local", "地端 Ollama"), ("vertex", "Vertex AI"),
                ("hybrid", "Hybrid"), ("off", "關閉"),
            ):
                btn = ctk.CTkButton(
                    r, text=label, width=74, height=30, corner_radius=8,
                    font=ctk.CTkFont(FONT_FAMILY_TEXT, 12), border_width=1,
                    command=lambda v=value: self._on_polish_backend_clicked(v),
                )
                btn.pack(side="left", padx=(0, 4))
                self._polish_backend_btns[value] = btn
            self._apply_polish_backend_chip_style()
            ctk.CTkButton(
                r, text="進階…", width=52, height=30, corner_radius=8,
                fg_color="transparent", border_width=1, border_color=SURF_3,
                text_color=ACCENT_HV, hover_color=SURF_2,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
                command=self._open_polish_advanced,
            ).pack(side="left", padx=(6, 0))

        self._row(
            page, "output", "output_ai", "AI 潤飾",
            "地端 Ollama／雲端 Vertex AI／Hybrid／關閉。模型、服務位址、情境路由、"
            "Prompt 熱重載等細節在「進階…」。",
            backend_row,
            search_terms=("AI 潤飾", "潤飾", "ollama", "vertex", "校正", "polish", "gemini"),
        )

        self._paste_strategy_var = ctk.StringVar(
            value=getattr(self.cfg, "ollama_paste_strategy", "wait")
        )

        def paste_strategy_row(r):
            self._paste_strategy_btns: dict[str, ctk.CTkButton] = {}
            for value, label in (("wait", "等潤飾"), ("raw", "先貼原文")):
                btn = ctk.CTkButton(
                    r, text=label, width=80, height=30, corner_radius=8,
                    font=ctk.CTkFont(FONT_FAMILY_TEXT, 13), border_width=1,
                    command=lambda v=value: self._on_paste_strategy_clicked(v),
                )
                btn.pack(side="left", padx=(0, 4))
                self._paste_strategy_btns[value] = btn
            self._apply_paste_strategy_chip_style()

        self._row(
            page, "output", "output_paste_strategy", "貼上策略",
            "等潤飾：品質優先，等 AI 校正完才貼上。先貼原文：立即貼 Whisper 原文，速度優先。",
            paste_strategy_row, search_terms=("貼上策略", "等潤飾", "先貼原文"),
        )

        self._append_var = ctk.BooleanVar(value=self.cfg.append_results)
        self._row(
            page, "output", "output_append", "追加錄音結果",
            "開啟：新結果接在舊結果後面。關閉：新結果覆蓋畫面上的舊結果。",
            self._make_sw(self._append_var),
            search_terms=("追加", "append", "覆蓋"),
        )

    def _open_polish_advanced(self) -> None:
        """開「AI 潤飾進階設定」：Ollama／Vertex AI 詳細欄位、情境路由、
        Prompt 熱重載。從輸出頁「AI 潤飾」列的「進階…」按鈕開啟。

        每次開啟都整個重建 widget（跟 HotkeyBindDialog 同一套模式），但底層
        StringVar／BooleanVar 用 self._var()「有就沿用、沒有才新建」，確保
        使用者在對話框裡改了、還沒按主視窗「儲存」就先關掉對話框再重開，
        剛剛的改動不會被蓋掉。關閉時把 self._vertex_frame 設回 None，
        _update_vertex_frame_visibility() 已有防呆、之後點主頁 chip 不會噴錯。
        """
        dlg = ctk.CTkToplevel(self)
        dlg.title("AI 潤飾進階設定")
        dlg.geometry("540x640")
        dlg.resizable(False, False)
        dlg.configure(fg_color=BG)
        dlg.transient(self)
        dlg.grab_set()

        def _on_close():
            self._vertex_frame = None
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", _on_close)
        dlg.bind("<Escape>", lambda e: _on_close())

        scroll = ctk.CTkScrollableFrame(dlg, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        def section(title: str) -> ctk.CTkFrame:
            ctk.CTkLabel(
                scroll, text=title.upper(), font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
                text_color=TEXT_3, anchor="w",
            ).pack(fill="x", padx=SPACE_LG, pady=(SPACE_LG, 6))
            f = ctk.CTkFrame(scroll, corner_radius=12, fg_color=SURF_1)
            f.pack(fill="x", padx=SPACE_LG, pady=(0, 4))
            return f

        def sep(parent) -> None:
            ctk.CTkFrame(parent, height=1, fg_color=SURF_3).pack(fill="x", padx=SPACE_LG, pady=0)

        # ── Ollama（地端）／Vertex AI（雲端）─────────────────────────────
        ai = section("Ollama ／ Vertex AI 詳細設定")

        self._vertex_frame = ctk.CTkFrame(ai, fg_color="transparent")
        # 先建好不 pack，由下面 _update_vertex_frame_visibility() 依 backend 決定顯隱

        vp_row = ctk.CTkFrame(self._vertex_frame, fg_color="transparent", height=52)
        vp_row.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
        vp_row.pack_propagate(False)
        ctk.CTkLabel(
            vp_row, text="GCP Project ID", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 14), text_color=TEXT_1,
        ).pack(side="left")
        self._vertex_project_id_var = self._var(
            "_vertex_project_id_var",
            lambda: ctk.StringVar(value=getattr(self.cfg, "vertex_project_id", "")),
        )
        ctk.CTkEntry(
            vp_row, textvariable=self._vertex_project_id_var,
            width=220, height=30, corner_radius=8,
            fg_color=SURF_2, border_color=SURF_3, text_color=TEXT_2,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 12),
            placeholder_text="my-gcp-project-123",
        ).pack(side="right")
        sep(self._vertex_frame)

        vm_row = ctk.CTkFrame(self._vertex_frame, fg_color="transparent", height=52)
        vm_row.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
        vm_row.pack_propagate(False)
        ctk.CTkLabel(
            vm_row, text="Vertex 模型", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 14), text_color=TEXT_1,
        ).pack(side="left")
        self._vertex_model_var = self._var(
            "_vertex_model_var",
            lambda: ctk.StringVar(value=getattr(self.cfg, "vertex_model", "gemini-2.5-flash")),
        )
        ctk.CTkOptionMenu(
            vm_row, variable=self._vertex_model_var,
            values=["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"],
            width=200, height=30, corner_radius=8,
            fg_color=SURF_2, button_color=SURF_3, button_hover_color=SURF_4,
            text_color=TEXT_1, font=ctk.CTkFont(FONT_FAMILY_MONO, 12),
        ).pack(side="right")

        ctk.CTkLabel(
            self._vertex_frame,
            text=(
                "⚠ 啟用後文字會傳到 Google Cloud（試用 credit 適用 Vertex AI 才會被抵扣）。\n"
                "需先在終端機跑 `gcloud auth application-default login` 設定憑證。"
            ),
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11), text_color=WARN,
            justify="left", anchor="w",
        ).pack(anchor="w", padx=SPACE_LG, pady=(6, 10))
        sep(self._vertex_frame)

        self._ollama_enabled_var = self._var(
            "_ollama_enabled_var", lambda: ctk.BooleanVar(value=self.cfg.ollama_enabled)
        )
        self._ollama_warmup_anchor = ctk.CTkFrame(ai, fg_color="transparent", height=50)
        self._ollama_warmup_anchor.pack(fill="x", padx=SPACE_LG, pady=2)
        self._ollama_warmup_anchor.pack_propagate(False)
        ctk.CTkLabel(
            self._ollama_warmup_anchor, text="啟用 Ollama 暖機", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 14), text_color=TEXT_1,
        ).pack(side="left")
        self._make_sw(self._ollama_enabled_var, ACCENT)(self._ollama_warmup_anchor)
        sep(ai)

        self._update_vertex_frame_visibility()

        model_row = ctk.CTkFrame(ai, fg_color="transparent", height=52)
        model_row.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
        model_row.pack_propagate(False)
        ctk.CTkLabel(
            model_row, text="模型名稱", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 14), text_color=TEXT_1,
        ).pack(side="left")
        self._ollama_model_var = self._var(
            "_ollama_model_var", lambda: ctk.StringVar(value=self.cfg.ollama_model)
        )
        self._ollama_model_menu_wrap = ctk.CTkFrame(model_row, fg_color="transparent")
        self._ollama_model_menu_wrap.pack(side="right")
        self._build_ollama_model_menu()
        ctk.CTkLabel(
            ai,
            text=(
                "速度建議（M 系列 Apple Silicon）：\n"
                "• qwen2.5:3b-instruct — 1-2 秒（推薦，中文校正夠用）\n"
                "• gemma3:4b — 1-3 秒（平衡）\n"
                "• gemma3:12b — 3-6 秒（品質優、但體感較慢）\n"
                "找不到模型？終端機跑 `ollama pull <名稱>` 後按 ↻ 重新偵測"
            ),
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11), text_color=TEXT_3,
            justify="left", anchor="w",
        ).pack(anchor="w", padx=SPACE_LG, pady=(0, 10))
        sep(ai)

        url_row = ctk.CTkFrame(ai, fg_color="transparent", height=52)
        url_row.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
        url_row.pack_propagate(False)
        ctk.CTkLabel(
            url_row, text="服務位址", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 14), text_color=TEXT_1,
        ).pack(side="left")
        self._ollama_url_var = self._var(
            "_ollama_url_var", lambda: ctk.StringVar(value=self.cfg.ollama_base_url)
        )
        ctk.CTkEntry(
            url_row, textvariable=self._ollama_url_var,
            width=200, height=30, corner_radius=8,
            fg_color=SURF_2, border_color=SURF_3, text_color=TEXT_3,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11),
        ).pack(side="right")
        sep(ai)

        diag_row = ctk.CTkFrame(ai, fg_color=SURF_2, corner_radius=8)
        diag_row.pack(fill="x", padx=SPACE_LG, pady=(8, 4))
        self._ollama_diag_title = ctk.CTkLabel(
            diag_row, text="正在診斷…", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13, "bold"), text_color=TEXT_1,
        )
        self._ollama_diag_title.pack(fill="x", padx=SPACE_MD, pady=(10, 2))
        self._ollama_diag_detail = ctk.CTkLabel(
            diag_row, text="", anchor="w", justify="left",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11), text_color=TEXT_3, wraplength=440,
        )
        self._ollama_diag_detail.pack(fill="x", padx=SPACE_MD, pady=(0, 4))
        self._ollama_diag_cmd_frame = ctk.CTkFrame(diag_row, fg_color="transparent")
        self._ollama_diag_cmd_frame.pack(fill="x", padx=SPACE_MD, pady=(0, 10))
        self._ollama_diag_cmd = ctk.CTkLabel(
            self._ollama_diag_cmd_frame, text="", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11), text_color=ACCENT, wraplength=320,
        )
        self._ollama_diag_cmd.pack(side="left", padx=(0, 8))
        self._ollama_diag_copy_btn = ctk.CTkButton(
            self._ollama_diag_cmd_frame, text="複製命令",
            width=84, height=24, corner_radius=6,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
            fg_color=SURF_3, hover_color=SURF_4, text_color=TEXT_2,
        )
        self._ollama_diag_copy_btn.pack(side="right")
        self._ollama_diag_copy_btn.pack_forget()
        self.after(100, self._refresh_ollama_diagnostic)

        test_row = ctk.CTkFrame(ai, fg_color="transparent", height=52)
        test_row.pack(fill="x", padx=SPACE_LG, pady=(4, 8))
        test_row.pack_propagate(False)
        self._ollama_test_status = ctk.CTkLabel(
            test_row, text="（尚未測試）", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12), text_color=TEXT_3,
        )
        self._ollama_test_status.pack(side="left")
        ctk.CTkButton(
            test_row, text="測試連線", width=100, height=30, corner_radius=8,
            fg_color=SURF_2, text_color=TEXT_1, hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
            command=self._test_ollama,
        ).pack(side="right")

        # ── 情境路由 (Phase 2) ────────────────────────────────────────────
        rout = section("情境路由（依前景 App／關鍵字自動選 Prompt）")

        self._routing_var = self._var(
            "_routing_var", lambda: ctk.BooleanVar(value=self.cfg.preset_routing_enabled)
        )
        routing_row = ctk.CTkFrame(rout, fg_color="transparent", height=50)
        routing_row.pack(fill="x", padx=SPACE_LG, pady=2)
        routing_row.pack_propagate(False)
        ctk.CTkLabel(
            routing_row, text="啟用情境自動切換", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 14), text_color=TEXT_1,
        ).pack(side="left")
        self._make_sw(self._routing_var, ACCENT)(routing_row)
        sep(rout)

        if not hasattr(self, "_preset_switch_vars"):
            self._preset_switch_vars: dict[str, ctk.BooleanVar] = {}
        for pname, preset in _presets.PRESETS.items():
            if pname == "default":
                continue
            if pname not in self._preset_switch_vars:
                default_on = self.cfg.preset_overrides.get(pname, True)
                self._preset_switch_vars[pname] = ctk.BooleanVar(value=default_on)
            prow = ctk.CTkFrame(rout, fg_color="transparent", height=44)
            prow.pack(fill="x", padx=SPACE_LG, pady=2)
            prow.pack_propagate(False)
            ctk.CTkLabel(
                prow, text=f"  ↳ {preset.display_name}", anchor="w",
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 13), text_color=TEXT_2,
            ).pack(side="left")
            self._make_sw(self._preset_switch_vars[pname], ACCENT)(prow)
            sep(rout)

        hot_row = ctk.CTkFrame(rout, fg_color="transparent", height=52)
        hot_row.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
        hot_row.pack_propagate(False)
        ctk.CTkLabel(
            hot_row, text="Prompt 熱重載", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 14), text_color=TEXT_1,
        ).pack(side="left")
        self._hot_reload_var = self._var(
            "_hot_reload_var", lambda: ctk.BooleanVar(value=self.cfg.prompt_hot_reload)
        )
        ctk.CTkSwitch(
            hot_row, text="", variable=self._hot_reload_var,
            onvalue=True, offvalue=False,
            progress_color=ACCENT, button_color=TEXT_1, button_hover_color=TEXT_2,
            fg_color=SURF_3,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            hot_row, text="立即重新載入", width=110, height=28, corner_radius=8,
            fg_color=SURF_2, text_color=TEXT_1,
            hover_color=SURF_3, border_width=1, border_color=SURF_3,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
            command=self._reload_prompts_now,
        ).pack(side="right", padx=(8, 0))
        self._hot_reload_status_label = ctk.CTkLabel(
            rout, text="", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11), text_color=TEXT_3,
        )
        self._hot_reload_status_label.pack(fill="x", padx=SPACE_LG, pady=(0, 10))

        ctk.CTkButton(
            dlg, text="完成", width=100, height=32, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HV, text_color=TEXT_1,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13, "bold"),
            command=_on_close,
        ).pack(side="bottom", pady=SPACE_MD)

    # ── 分類頁：外觀 ─────────────────────────────────────────────────────────

    def _build_page_appearance(self, page: ctk.CTkFrame) -> None:
        """外觀分類：主題、動態效果、迷你懸浮條、錄音視覺。"""
        self._page_header(page, "外觀")

        # 主題（v2.6.0）—— 點 chip 跟現在不同就彈 confirm dialog → 確認 → 立刻
        # save + 重啟，不跟其他設定一起等 Save 按鈕。
        self._theme_var = ctk.StringVar(value=self.cfg.theme)

        def theme_row(r):
            self._theme_btns: dict[str, ctk.CTkButton] = {}
            for value, label in (("dark", "深色"), ("light", "淺色")):
                btn = ctk.CTkButton(
                    r, text=label, width=72, height=30, corner_radius=8,
                    font=ctk.CTkFont(FONT_FAMILY_TEXT, 13), border_width=1,
                    command=lambda v=value: self._on_theme_clicked(v),
                )
                btn.pack(side="left", padx=(0, 4))
                self._theme_btns[value] = btn
            self._apply_theme_chip_style()

        self._row(
            page, "appearance", "appearance_theme", "主題",
            "深色：zinc + cyan（目前預設）。淺色：暖白 + Claude 珊瑚。"
            "切換需重新啟動 App（~2 秒、自動完成）。",
            theme_row, search_terms=("主題", "深色", "淺色", "theme", "dark", "light"),
        )

        self._reduce_motion_var = ctk.StringVar(
            value=getattr(self.cfg, "reduce_motion_pref", "auto")
        )

        def reduce_motion_row(r):
            self._reduce_motion_btns: dict[str, ctk.CTkButton] = {}
            for value, label in (
                ("auto", "跟系統"), ("always", "減少動態"), ("never", "完整動畫"),
            ):
                btn = ctk.CTkButton(
                    r, text=label, width=76, height=30, corner_radius=8,
                    font=ctk.CTkFont(FONT_FAMILY_TEXT, 13), border_width=1,
                    command=lambda v=value: self._on_reduce_motion_clicked(v),
                )
                btn.pack(side="left", padx=(0, 4))
                self._reduce_motion_btns[value] = btn
            self._apply_reduce_motion_chip_style()

        self._row(
            page, "appearance", "appearance_motion", "動態效果",
            "跟系統：依 macOS「減少動態效果」偏好（推薦）。減少動態：強制關閉呼吸光圈／"
            "粒子環／漣漪。完整動畫：即使系統開了 reduce motion 也跑完整動畫。",
            reduce_motion_row, search_terms=("動態效果", "動畫", "reduce motion", "呼吸光圈"),
        )

        self._mini_window_var = ctk.BooleanVar(value=self.cfg.mini_recording_window)
        self._row(
            page, "appearance", "appearance_mini", "迷你懸浮條",
            "錄音／處理中時在游標所在螢幕中下方顯示小型狀態視窗，跨 Space／全螢幕仍可見。",
            self._make_sw(self._mini_window_var, ACCENT),
            search_terms=("mini", "懸浮", "hud", "浮動視窗", "迷你"),
        )

        self._record_visual_var = ctk.StringVar(
            value=getattr(self.cfg, "record_visual", "waveform")
        )

        def visual_row(r):
            self._record_visual_btns: dict[str, ctk.CTkButton] = {}
            for value, label in (("waveform", "波形"), ("chamber", "光場")):
                btn = ctk.CTkButton(
                    r, text=label, width=70, height=30, corner_radius=8,
                    font=ctk.CTkFont(FONT_FAMILY_TEXT, 13), border_width=1,
                    command=lambda v=value: self._on_record_visual_clicked(v),
                )
                btn.pack(side="left", padx=(0, 4))
                self._record_visual_btns[value] = btn
            self._apply_record_visual_chip_style()

        self._row(
            page, "appearance", "appearance_visual", "錄音視覺",
            "波形：46 格頻譜長條、依音量變色（預設）。光場：同心圓呼吸光圈（v2.3 前風格）。"
            "變更於下次啟動 App 生效。",
            visual_row, search_terms=("錄音視覺", "波形", "光場", "aperture", "chamber"),
        )

    def _apply_record_visual_chip_style(self) -> None:
        """根據 _record_visual_var 重繪 2 顆 chip（跟主題／動態效果同一套樣式）。"""
        active = self._record_visual_var.get()
        for value, btn in self._record_visual_btns.items():
            if value == active:
                btn.configure(
                    fg_color=SURF_2, border_color=ACCENT,
                    text_color=TEXT_1, hover_color=SURF_3,
                )
            else:
                btn.configure(
                    fg_color="transparent", border_color=SURF_3,
                    text_color=TEXT_3, hover_color=SURF_2,
                )

    def _on_record_visual_clicked(self, value: str) -> None:
        """點 chip → 預覽切換，按 Save 才落地到 cfg（需重啟才生效）。"""
        if value == self._record_visual_var.get():
            return
        self._record_visual_var.set(value)
        self._apply_record_visual_chip_style()

    # ── 分類頁：資料 ─────────────────────────────────────────────────────────

    def _build_page_data(self, page: ctk.CTkFrame) -> None:
        """資料分類：歷史紀錄保留、可疑音檔保留、匯入匯出。"""
        self._page_header(page, "資料")

        self._history_enabled_var = ctk.BooleanVar(value=self.cfg.history_enabled)
        self._history_retention_var = tk.StringVar(value=str(self.cfg.history_retention_days))

        def history_row(r):
            ctk.CTkLabel(
                r, text="天（0=永久）", font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
                text_color=TEXT_3,
            ).pack(side="right", padx=(4, 0))
            ctk.CTkEntry(
                r, textvariable=self._history_retention_var,
                width=56, height=30, corner_radius=8,
                fg_color=SURF_2, border_color=SURF_3, text_color=TEXT_1,
                font=ctk.CTkFont(FONT_FAMILY_MONO, 13), justify="right",
            ).pack(side="right")
            self._make_sw(self._history_enabled_var, ACCENT)(r)

        self._row(
            page, "data", "data_history", "歷史紀錄保留",
            "每次轉錄（含潤飾）寫入 ~/.whisper_app/history.db；保留天數到期後自動清除舊紀錄。",
            history_row, search_terms=("歷史紀錄", "history", "保留天數"),
        )

        self._suspicious_audio_var = ctk.BooleanVar(
            value=getattr(self.cfg, "suspicious_audio_capture", False)
        )
        self._suspicious_audio_maxsize_var = tk.StringVar(
            value=str(getattr(self.cfg, "suspicious_audio_max_size_mb", 200))
        )

        def suspicious_row(r):
            ctk.CTkLabel(
                r, text="MB 上限", font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
                text_color=TEXT_3,
            ).pack(side="right", padx=(4, 0))
            ctk.CTkEntry(
                r, textvariable=self._suspicious_audio_maxsize_var,
                width=64, height=30, corner_radius=8,
                fg_color=SURF_2, border_color=SURF_3, text_color=TEXT_1,
                font=ctk.CTkFont(FONT_FAMILY_MONO, 13), justify="right",
            ).pack(side="right")
            self._make_sw(self._suspicious_audio_var, ACCENT)(r)

        self._row(
            page, "data", "data_suspicious", "可疑音檔保留",
            "觸發幻覺／去重／轉錄過慢時把錄音另存供除錯；超過上限自動清最舊的。預設關閉。",
            suspicious_row, search_terms=("可疑音檔", "debug", "除錯錄音", "幻覺"),
        )

        def ie_row(r):
            ctk.CTkButton(
                r, text="匯入設定…", width=110, height=28,
                image=get_icon("file-text", 14, ACCENT_HV), compound="left",
                fg_color="transparent", border_width=1, border_color=SURF_3,
                text_color=ACCENT_HV, hover_color=SURF_2,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 12), corner_radius=8,
                command=self._import_settings,
            ).pack(side="right")
            ctk.CTkButton(
                r, text="匯出設定…", width=110, height=28,
                image=get_icon("download", 14, ACCENT_HV), compound="left",
                fg_color="transparent", border_width=1, border_color=SURF_3,
                text_color=ACCENT_HV, hover_color=SURF_2,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 12), corner_radius=8,
                command=self._export_settings,
            ).pack(side="right", padx=(0, 8))

        self._row(
            page, "data", "data_importexport", "匯入匯出",
            "打包 config.json + dictionary.json 成 zip；不含歷史紀錄（隱私）與除錯日誌。",
            ie_row, search_terms=("匯入", "匯出", "備份", "export", "import"),
        )

    # ── 分類頁：關於 ─────────────────────────────────────────────────────────

    def _build_page_about(self, page: ctk.CTkFrame) -> None:
        """關於分類：版本、權限狀態、日誌位置。"""
        self._page_header(page, "關於")

        from _version import __version__ as _app_ver

        def version_row(r):
            ctk.CTkLabel(
                r, text=f"v{_app_ver}",
                font=ctk.CTkFont(FONT_FAMILY_MONO, 13), text_color=TEXT_2,
            ).pack(side="right")

        self._row(
            page, "about", "about_version", "版本", "Whisper Pro（Mac／Windows 雙棲版）。",
            version_row, search_terms=("版本", "version"),
        )

        def permission_row(r):
            granted = check_accessibility()
            self._permission_status_label = ctk.CTkLabel(
                r, text=("已授權" if granted else "未授權"),
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 13, "bold"),
                text_color=(SUCCESS if granted else DANGER),
            )
            self._permission_status_label.pack(side="right", padx=(0, 8))
            ctk.CTkButton(
                r, text="開啟權限引導", width=110, height=28, corner_radius=8,
                fg_color=SURF_2, text_color=TEXT_1, hover_color=SURF_3,
                border_width=1, border_color=SURF_3,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
                command=self._open_accessibility_guide,
            ).pack(side="right")

        self._row(
            page, "about", "about_permission", "權限狀態",
            "熱鍵運作需要 macOS「輔助使用」權限；Windows 不需額外授權，永遠顯示已授權。",
            permission_row, search_terms=("權限", "輔助使用", "accessibility"),
        )

        def logpath_row(r):
            ctk.CTkButton(
                r, text="開啟資料夾", width=110, height=28,
                image=get_icon("folder", 14, ACCENT_HV), compound="left",
                fg_color="transparent", border_width=1, border_color=SURF_3,
                text_color=ACCENT_HV, hover_color=SURF_2,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 12), corner_radius=8,
                command=lambda: _open_path_in_os_default(
                    os.path.expanduser("~/.whisper_app")
                ),
            ).pack(side="right")

        self._row(
            page, "about", "about_logs", "日誌位置",
            "~/.whisper_app 內含 config.json、logs/、dictionary.json；"
            "Whisper 模型快取另存在 ~/.cache/huggingface。",
            logpath_row, search_terms=("日誌", "log", "設定檔", "快取", "cache"),
        )

    def _open_accessibility_guide(self) -> None:
        """開啟輔助使用權限引導對話框（沿用 main.py 首次啟動用的同一個 dialog）。"""
        log_action("settings_accessibility_guide_opened")
        AccessibilityDialog(self)


    def _on_model_preview(self, value: str) -> None:
        """模型選單即時預覽：更新下方說明文字。"""
        self._model_desc.configure(text=MODEL_INFO.get(value, ""))

    def _rebind_hotkey(self) -> None:
        """開啟 HotkeyBindDialog，讓使用者重新綁定快捷鍵。"""
        log_action("rebind_hotkey_dialog_opened")
        HotkeyBindDialog(self, self.cfg.hotkey, self._apply_hotkey).focus()

    def _apply_hotkey(self, combo: str) -> None:
        """HotkeyBindDialog 完成後回呼：把新組合寫回暫存 cfg 並刷新標籤。"""
        old = self.cfg.hotkey
        self.cfg.hotkey = combo
        self._hk_label.configure(text=format_hotkey(combo))
        log_settings("pending", field="hotkey", old=old, new=combo,
                     note="pending_save_button")

    # ── v2.6.0 主題切換（外觀 section）─────────────────────────────────────

    def _apply_theme_chip_style(self) -> None:
        """根據目前 _theme_var 重繪 2 顆 chip：active 帶 ACCENT 邊框 + SURF_2 底色。"""
        active = self._theme_var.get()
        for value, btn in self._theme_btns.items():
            if value == active:
                btn.configure(
                    fg_color=SURF_2,
                    border_color=ACCENT,
                    text_color=TEXT_1,
                    hover_color=SURF_3,
                )
            else:
                btn.configure(
                    fg_color="transparent",
                    border_color=SURF_3,
                    text_color=TEXT_3,
                    hover_color=SURF_2,
                )

    # A3（v2.7.0）：動態效果 chip 樣式 / click handler ───────────────────────

    def _apply_reduce_motion_chip_style(self) -> None:
        """根據 _reduce_motion_var 重繪 3 顆 chip。"""
        active = self._reduce_motion_var.get()
        for value, btn in self._reduce_motion_btns.items():
            if value == active:
                btn.configure(
                    fg_color=SURF_2, border_color=ACCENT,
                    text_color=TEXT_1, hover_color=SURF_3,
                )
            else:
                btn.configure(
                    fg_color="transparent", border_color=SURF_3,
                    text_color=TEXT_3, hover_color=SURF_2,
                )

    def _on_reduce_motion_clicked(self, value: str) -> None:
        """點 chip 立即套用（不需 Save 按鈕）；同樣會在 _collect_and_save 寫回。"""
        if value == self._reduce_motion_var.get():
            return
        self._reduce_motion_var.set(value)
        self._apply_reduce_motion_chip_style()
        # live-apply：直接套用到 parent AppWindow（下一個 render tick 生效）
        try:
            parent = self._parent
            if hasattr(parent, "_reduce_motion"):
                parent._reduce_motion = resolve_reduce_motion(value)
        except Exception:
            log_error("reduce_motion_live_apply_failed")

    # Bug D（v2.13.0）：貼上策略 chip 樣式 / click handler ────────────────

    def _apply_paste_strategy_chip_style(self) -> None:
        """根據 _paste_strategy_var 重繪 2 顆 chip。"""
        active = self._paste_strategy_var.get()
        for value, btn in self._paste_strategy_btns.items():
            if value == active:
                btn.configure(
                    fg_color=SURF_2, border_color=ACCENT,
                    text_color=TEXT_1, hover_color=SURF_3,
                )
            else:
                btn.configure(
                    fg_color="transparent", border_color=SURF_3,
                    text_color=TEXT_3, hover_color=SURF_2,
                )

    def _on_paste_strategy_clicked(self, value: str) -> None:
        """點 chip → 預覽切換，按 Save 才落地到 cfg。"""
        if value == self._paste_strategy_var.get():
            return
        self._paste_strategy_var.set(value)
        self._apply_paste_strategy_chip_style()

    # ── v2.18.0 polish backend chip ────────────────────────────────────
    def _apply_polish_backend_chip_style(self) -> None:
        """根據 _polish_backend_var 重繪 3 顆 chip。"""
        active = self._polish_backend_var.get()
        for value, btn in self._polish_backend_btns.items():
            if value == active:
                btn.configure(
                    fg_color=SURF_2, border_color=ACCENT,
                    text_color=TEXT_1, hover_color=SURF_3,
                )
            else:
                btn.configure(
                    fg_color="transparent", border_color=SURF_3,
                    text_color=TEXT_3, hover_color=SURF_2,
                )

    def _on_polish_backend_clicked(self, value: str) -> None:
        """點 chip → 預覽切換，按 Save 才落地到 cfg。"""
        if value == self._polish_backend_var.get():
            return
        self._polish_backend_var.set(value)
        self._apply_polish_backend_chip_style()
        self._update_vertex_frame_visibility()

    def _update_vertex_frame_visibility(self) -> None:
        """vertex 後端被選中時顯示 GCP 設定欄位，否則隱藏。

        pack(before=anchor) 確保 vertex_frame 插在 Ollama 暖機 row 上面、
        而不是預設地附加到 ai section 最尾端。

        v2.28.0：AI 潤飾進階設定搬進獨立對話框後，_vertex_frame 只在該
        對話框開著時存在；對話框關閉時會把它設回 None，這裡多一層防呆
        （winfo_exists 檢查 + try/except），避免使用者在對話框關閉後又點
        主頁 chip 時對已銷毀的 widget 操作噴 TclError。
        """
        vf = getattr(self, "_vertex_frame", None)
        if vf is None:
            return
        try:
            if not vf.winfo_exists():
                return
            if self._polish_backend_var.get() == "vertex":
                if not vf.winfo_ismapped():
                    anchor = getattr(self, "_ollama_warmup_anchor", None)
                    if anchor is not None and anchor.winfo_exists():
                        vf.pack(fill="x", before=anchor)
                    else:
                        vf.pack(fill="x")
            else:
                if vf.winfo_ismapped():
                    vf.pack_forget()
        except Exception:
            log_error("update_vertex_frame_visibility_failed")

    def _on_theme_clicked(self, new_theme: str) -> None:
        """使用者點主題 chip。跟現在不同就彈 confirm dialog。"""
        if new_theme == self.cfg.theme:
            return   # 點現在主題的 chip → noop
        # 視覺先 preview（chip 跳到使用者選的；若取消會還原）
        self._theme_var.set(new_theme)
        self._apply_theme_chip_style()
        self._confirm_theme_switch(new_theme)

    def _confirm_theme_switch(self, new_theme: str) -> None:
        """彈 modal confirm dialog：警告錄音 / 潤飾進行中的狀態。

        確認 → save + relaunch。取消 → 視覺還原 + chip 變回原本主題。
        """
        # 偵測進行中狀態，警告文字加在 confirm body
        warnings: list[str] = []
        try:
            if getattr(self._parent, "_state", None) == "recording":
                warnings.append("• 進行中的錄音會被中斷")
            if getattr(self._parent, "_polish_busy", False):
                warnings.append("• AI 潤飾進行中、結果會遺失")
        except Exception:
            pass

        dlg = ctk.CTkToplevel(self)
        dlg.title("切換主題")
        dlg.geometry("360x220")
        dlg.resizable(False, False)
        dlg.configure(fg_color=BG)
        dlg.transient(self)
        dlg.grab_set()

        ctk.CTkLabel(
            dlg,
            text=f"切換到「{('淺色' if new_theme == 'light' else '深色')}」需要重新啟動 App",
            font=ctk.CTkFont(FONT_FAMILY_UI, 14, "bold"),
            text_color=TEXT_1,
            wraplength=320, justify="left",
        ).pack(padx=20, pady=(20, 8), anchor="w")

        body_text = "• 所有設定 / 歷史紀錄都會保留\n• Splash 過場後落地新主題"
        if warnings:
            body_text = "\n".join(warnings) + "\n" + body_text
        ctk.CTkLabel(
            dlg, text=body_text,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
            text_color=TEXT_2,
            wraplength=320, justify="left", anchor="w",
        ).pack(padx=20, pady=(0, 14), anchor="w")

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(side="bottom", fill="x", padx=20, pady=14)

        def on_cancel():
            # 還原視覺
            self._theme_var.set(self.cfg.theme)
            self._apply_theme_chip_style()
            log_action("theme_switch_cancelled", attempted=new_theme)
            dlg.destroy()

        def on_confirm():
            dlg.destroy()
            self._trigger_theme_relaunch(new_theme)

        ctk.CTkButton(
            btns, text="取消", width=90, height=32, corner_radius=8,
            fg_color="transparent", border_width=1, border_color=SURF_3,
            text_color=TEXT_2, hover_color=SURF_2,
            command=on_cancel,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btns, text="確認、重啟", width=120, height=32, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HV,
            text_color=TEXT_1,
            command=on_confirm,
        ).pack(side="right")

        # ESC 鍵 = 取消
        dlg.bind("<Escape>", lambda e: on_cancel())

    def _trigger_theme_relaunch(self, new_theme: str) -> None:
        """確認後：save config → 觸發 AppWindow 的 relaunch 流程 → destroy SettingsWindow。

        Eng Review Issue 1 順序：confirm 立刻 save、AppWindow 在 toast 後依
        cleanup → spawn → exit 順序執行，避免雙 instance 共存。

        Fix Cluster D-1 / 2026-05-23：relaunch 全部失敗時把 config.theme 回滾。
        原本 save 落地後 relaunch fail 只 log_error、config 已寫入磁碟回不去 →
        使用者下次啟動莫名變新主題。AppWindow `_do_theme_relaunch` 改成同步檢查
        spawn 是否成功，失敗時回呼此處 rollback。
        """
        old_theme = self.cfg.theme
        self.cfg.theme = new_theme
        try:
            self.cfg.save()
        except Exception:
            log_error("theme_save_failed")
            self._theme_var.set(old_theme)
            self._apply_theme_chip_style()
            return

        log_action("theme_switched", **{"from": old_theme, "to": new_theme})

        # 設 rollback callback：AppWindow 800ms 後 cleanup + spawn 若失敗、把 config 回滾。
        def _rollback():
            try:
                self.cfg.theme = old_theme
                self.cfg.save()
                log_action("theme_rollback_after_relaunch_fail",
                           **{"attempted": new_theme, "rolled_back_to": old_theme})
            except Exception:
                log_error("theme_rollback_save_failed")

        try:
            self._parent._do_theme_relaunch(on_failure=_rollback)
        except Exception:
            log_error("theme_trigger_relaunch_failed")
            _rollback()
            return

        # 關閉 SettingsWindow，AppWindow 會在 800ms 後正式進入 relaunch 流程
        try:
            self.destroy()
        except Exception:
            pass

    def _save(self) -> None:
        """收集所有 UI 欄位值，寫入暫存 cfg、呼叫 on_save_cb，然後關閉視窗。

        包 try/except + finally — 避免 Cocoa/MLX 並存時任何例外變成靜默
        SIGSEGV；同時保證 destroy() 一定被呼叫，不讓使用者被卡在設定視窗。
        """
        log_action("settings_save_clicked")
        try:
            self._collect_and_save()
        except Exception:
            log_error("settings_save_failed")
        finally:
            try:
                self.destroy()
            except Exception:
                log_error("settings_window_destroy_failed")

    def _collect_and_save(self) -> None:
        """從表單欄位蒐集所有值、呼叫 cfg.save() 並通知主視窗。

        v2.28.0 風險控管：Ollama 模型／Vertex AI／情境路由／Prompt 熱重載
        這批欄位的 var 只在使用者開過「AI 潤飾進階設定」對話框（輸出頁
        「AI 潤飾」列的「進階…」按鈕）才會被建立——多數使用者可能整個
        session 都不會打開它。這裡逐一用 hasattr 防呆：var 不存在就跳過
        （self.cfg 已經是 __init__ 深拷貝時的原值，不覆蓋＝值不變，不是
        « 遺漏儲存 »），絕對不能讓「使用者只是想改模型/麥克風/快捷鍵按
        儲存」這個最常見路徑因為 AttributeError 而整個設定視窗炸掉。
        """
        self.cfg.model          = self._model_var.get()
        self.cfg.language       = self._lang_var.get()
        # F1（v2.12.0）：麥克風來源 — 顯示「（系統預設）」對應 None
        _dev_sel = self._device_var.get()
        if _dev_sel == "（系統預設）":
            self.cfg.input_device = None
        else:
            self.cfg.input_device = _dev_sel
        self.cfg.append_results = self._append_var.get()
        self.cfg.auto_copy      = self._autocopy_var.get()
        self.cfg.auto_paste     = self._autopaste_var.get()
        # ── Ollama（進階設定對話框才有的欄位，見上方 docstring）───────────
        if hasattr(self, "_ollama_enabled_var"):
            self.cfg.ollama_enabled = self._ollama_enabled_var.get()
        # v2.18.0：polish backend 三選一（主頁一定有）+ Vertex 設定（進階才有）
        self.cfg.polish_backend = self._polish_backend_var.get()
        if hasattr(self, "_vertex_project_id_var"):
            self.cfg.vertex_project_id = self._vertex_project_id_var.get().strip()
        if hasattr(self, "_vertex_model_var"):
            self.cfg.vertex_model = self._vertex_model_var.get().strip()
        # Bug D（v2.13.0）：貼上策略（主頁一定有）
        self.cfg.ollama_paste_strategy = self._paste_strategy_var.get()
        if hasattr(self, "_ollama_model_var"):
            model = self._ollama_model_var.get().strip()
            if model:
                self.cfg.ollama_model = model
        if hasattr(self, "_ollama_url_var"):
            url = self._ollama_url_var.get().strip()
            if url:
                self.cfg.ollama_base_url = url
        # ── Phase 2 preset 路由（進階設定對話框才有的欄位）─────────────────
        if hasattr(self, "_routing_var"):
            self.cfg.preset_routing_enabled = self._routing_var.get()
        if hasattr(self, "_preset_switch_vars"):
            self.cfg.preset_overrides = {
                name: var.get() for name, var in self._preset_switch_vars.items()
            }
        # ── #2 熱重載（進階設定對話框才有的欄位）───────────────────────────
        if hasattr(self, "_hot_reload_var"):
            self.cfg.prompt_hot_reload = self._hot_reload_var.get()
        # ── #4 字典 ──────────────────────────────────────────────────────
        self.cfg.dictionary_enabled = self._dict_enabled_var.get()
        self.cfg.dictionary_path    = self._dict_path_var.get().strip()
        # ── Phase 3.2 歷史紀錄 ─────────────────────────────────────────────
        self.cfg.history_enabled = self._history_enabled_var.get()
        try:
            days = int(self._history_retention_var.get().strip() or "0")
            self.cfg.history_retention_days = max(0, days)
        except ValueError:
            # 非法輸入保留原值，避免吃掉使用者其他改動
            log_error("history_retention_parse_failed",
                      value=self._history_retention_var.get())
        # ── Phase 4.3 mini 視窗 ─────────────────────────────────────────
        self.cfg.mini_recording_window = self._mini_window_var.get()
        # ── A3（v2.7.0）動態效果偏好 ────────────────────────────────────
        self.cfg.reduce_motion_pref = self._reduce_motion_var.get()
        # ── v2.28.0 設定視窗重排新增欄位（辨識／錄音／外觀／資料四類）─────
        self.cfg.silero_vad_enabled = self._silero_vad_enabled_var.get()
        try:
            _thr = float(self._silero_vad_threshold_var.get().strip())
            self.cfg.silero_vad_threshold = min(1.0, max(0.0, _thr))
        except ValueError:
            log_error("silero_vad_threshold_parse_failed",
                      value=self._silero_vad_threshold_var.get())
        self.cfg.recording_watchdog = self._recording_watchdog_var.get()
        self.cfg.record_visual = self._record_visual_var.get()
        self.cfg.suspicious_audio_capture = self._suspicious_audio_var.get()
        try:
            _max_mb = int(self._suspicious_audio_maxsize_var.get().strip())
            self.cfg.suspicious_audio_max_size_mb = max(1, _max_mb)
        except ValueError:
            log_error("suspicious_audio_maxsize_parse_failed",
                      value=self._suspicious_audio_maxsize_var.get())
        self.cfg.save()
        self._on_save_cb(self.cfg)   # 通知主視窗（destroy 由 _save 的 finally 負責）

    # v2.13.0：Ollama 模型 dropdown ────────────────────────────────────────

    def _build_ollama_model_menu(self) -> None:
        """偵測本機 Ollama 模型、建立下拉選單（含刷新按鈕）。

        - 開啟 Settings 時自動偵測一次（synchronous，3s timeout）
        - 列表含「目前 cfg 值」即使本機沒這個模型也保留（避免清掉 user 設定）
        - 點 ↻ 重新偵測（user `ollama pull` 後不用重開 Settings）
        - Ollama 沒跑 → 列表只剩 cfg 值（避免 dropdown 變空）
        """
        # 清空現有 widget（refresh 用）
        for w in self._ollama_model_menu_wrap.winfo_children():
            w.destroy()

        # 偵測模型清單
        try:
            installed = self._parent.ollama.get_models()
        except Exception:
            log_error("settings_get_ollama_models_failed")
            installed = []

        current = self._ollama_model_var.get().strip() or self.cfg.ollama_model
        values = list(installed)
        # 確保 current 值在列表內（即使本機沒裝、也讓 user 看到他選了什麼）
        if current and current not in values:
            values.append(current)
        # 沒任何模型：顯示提示文字
        if not values:
            values = ["（Ollama 未啟動或無模型）"]
            current = values[0]
            self._ollama_model_var.set(current)

        # OptionMenu 本體
        ctk.CTkOptionMenu(
            self._ollama_model_menu_wrap,
            values=values,
            variable=self._ollama_model_var,
            width=200, height=30, corner_radius=8,
            fg_color=SURF_2, button_color=SURF_2,
            button_hover_color=SURF_3,
            dropdown_fg_color=SURF_1,
            text_color=TEXT_1,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 12),
        ).pack(side="left")
        # 刷新按鈕（重新偵測 Ollama）
        ctk.CTkButton(
            self._ollama_model_menu_wrap,
            text="↻", width=30, height=30, corner_radius=8,
            fg_color=SURF_2, hover_color=SURF_3, border_width=1, border_color=SURF_3,
            text_color=TEXT_2,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 14),
            command=self._refresh_ollama_model_list,
        ).pack(side="left", padx=(4, 0))

    def _refresh_ollama_model_list(self) -> None:
        """重新偵測 Ollama 模型清單（不關閉 Settings）。"""
        log_action("ollama_model_list_refresh")
        # 先觸發 health check 同步（避免 stale）
        try:
            self._parent.ollama.health_check_sync()
        except Exception:
            pass
        self._build_ollama_model_menu()

    def _reload_prompts_now(self) -> None:
        """立即觸發 PromptReloader.reload_now()，並以狀態標籤顯示結果。

        v2.28.0：按鈕搬進「AI 潤飾進階設定」對話框後，回饋文字改寫到跟
        按鈕同一個對話框裡的 _hot_reload_status_label（原本借用辨識頁的
        _dict_status_label，兩者現在不在同一個視窗，使用者會看不到回饋）。
        """
        reloader = getattr(self._parent, "_prompt_reloader", None)
        if reloader is None:
            return
        reloaded = reloader.reload_now()
        label = getattr(self, "_hot_reload_status_label", None)
        if label is None:
            return
        if reloaded:
            names = ", ".join(f"{n}.py" for n in reloaded)
            label.configure(text=f"已重新載入 {names}", text_color=SUCCESS)
        else:
            label.configure(text="Reload 失敗（檢查 console log）", text_color=DANGER)

    def _compute_dict_status(self) -> str:
        """讀字典檔回傳「目前 N 個 term + M 條 corrections」狀態字串。"""
        try:
            path_str = (self.cfg.dictionary_path or "").strip() or str(_dictionary.DEFAULT_PATH)
            from pathlib import Path as _P
            path = _P(path_str).expanduser()
            if not path.exists():
                return "字典檔不存在（點按鈕建立）"
            import json as _json
            data = _json.loads(path.read_text(encoding="utf-8"))
            n_terms = len(data.get("terms", []))
            n_corr = len(data.get("corrections", []))
            if n_corr:
                return f"目前 {n_terms} 個 term、{n_corr} 條校正規則"
            return f"目前 {n_terms} 個 term"
        except Exception:
            return "讀取字典失敗"

    def _open_dictionary_file(self) -> None:
        """在預設編輯器開啟字典 JSON。"""
        path_str = (self._dict_path_var.get().strip()
                    or str(_dictionary.DEFAULT_PATH))
        from pathlib import Path as _P
        path = _P(path_str).expanduser()
        _dictionary.ensure_file(path)
        try:
            _open_path_in_os_default(path)
            self._dict_status_label.configure(
                text=f"已開啟 {path.name}（編輯後存檔自動生效）", text_color=TEXT_3,
            )
        except Exception as e:
            self._dict_status_label.configure(
                text=f"開啟失敗：{e}", text_color=DANGER,
            )

    def _test_ollama(self) -> None:
        """於設定視窗中測試當前輸入的 base_url + model 是否可用。

        不寫回 cfg（使用者按「取消」就應丟棄），只用暫時的 OllamaClient probe。
        """
        from ollama_client import OllamaClient, OllamaConfig

        url   = self._ollama_url_var.get().strip() or "http://localhost:11434"
        model = self._ollama_model_var.get().strip() or "qwen2.5:3b-instruct"

        self._ollama_test_status.configure(text="測試中…", text_color=TEXT_3)

        def _run():
            probe = OllamaClient(OllamaConfig(
                base_url=url, model=model, enabled=True, timeout_seconds=5,
            ))
            ok = probe.health_check_sync()
            models = probe.get_models() if ok else []
            self.after(0, _done, ok, models)

        def _done(ok: bool, models: list[str]):
            if not ok:
                self._ollama_test_status.configure(
                    text="× 無法連線（請確認 ollama serve 已啟動）",
                    text_color=DANGER,
                )
                return
            has_model = any(m.split(":")[0] == model.split(":")[0] or m == model
                            for m in models)
            if has_model:
                self._ollama_test_status.configure(
                    text=f"✓ 連線成功，已找到 {model}",
                    text_color=SUCCESS,
                )
            else:
                preview = ", ".join(models[:3]) if models else "（無）"
                self._ollama_test_status.configure(
                    text=f"△ 連線成功但找不到 {model}。本機模型：{preview}",
                    text_color=WARN,
                )

        threading.Thread(target=_run, daemon=True).start()

    def _refresh_ollama_diagnostic(self) -> None:
        """背景執行緒跑 onboarding.diagnose_ollama；完成後在主執行緒更新 UI。

        Phase 4.5：給使用者「下一步該做什麼」的明確指示，含一鍵複製命令。
        """
        import onboarding

        def _run():
            url   = self._ollama_url_var.get().strip() or "http://localhost:11434"
            model = self._ollama_model_var.get().strip() or "qwen2.5:3b-instruct"
            try:
                diag = onboarding.diagnose_ollama(base_url=url, recommended=model)
                title, detail, cmd = onboarding.summarize(diag)
            except Exception:
                log_error("ollama_diagnostic_failed")
                title, detail, cmd = "△ 診斷失敗", "請見終端 log", None
            self.after(0, _apply, title, detail, cmd)

        def _apply(title: str, detail: str, cmd: Optional[str]):
            # 標題顏色：✓ 綠 / ⚠ 橙 / ✗ 紅 / 其他灰
            color = TEXT_1
            if   title.startswith("✓"): color = SUCCESS
            elif title.startswith("⚠"): color = WARN
            elif title.startswith("✗"): color = DANGER
            self._ollama_diag_title.configure(text=title, text_color=color)
            self._ollama_diag_detail.configure(text=detail)

            if cmd:
                self._ollama_diag_cmd.configure(text=f"$ {cmd}")
                self._ollama_diag_copy_btn.pack(side="right")
                # 重綁複製命令（lambda 捕獲當下的 cmd）
                def _do_copy(c=cmd):
                    try:
                        import pyperclip
                        pyperclip.copy(c)
                        self._ollama_diag_copy_btn.configure(text="已複製")
                        self.after(1500, lambda: self._ollama_diag_copy_btn.configure(text="複製命令"))
                    except Exception:
                        log_error("ollama_diag_copy_failed")
                self._ollama_diag_copy_btn.configure(command=_do_copy)
            else:
                self._ollama_diag_cmd.configure(text="")
                self._ollama_diag_copy_btn.pack_forget()

        threading.Thread(target=_run, daemon=True).start()

    # ── 匯入 / 匯出（Phase 4.4）─────────────────────────────────────────────

    _EXPORT_SCHEMA_VERSION = 1
    _EXPORT_INCLUDED = ("config.json", "dictionary.json")
    _EXPORT_EXCLUDED_REASON = {
        "history.db":         "privacy — 個人語音轉錄內容不應隨設定流通",
        "polish_log.jsonl":   "size — debug log，匯出無意義",
        "logs/":              "size — App 運行紀錄",
    }

    def _export_settings(self) -> None:
        """打包 config.json + dictionary.json 成單一 zip 給使用者另存。

        刻意不含 history.db（隱私）、polish_log.jsonl / logs/（debug 用）。
        """
        import datetime, json, zipfile

        default_name = f"whisper-pro-settings-{datetime.date.today():%Y%m%d}.zip"
        out = fd.asksaveasfilename(
            defaultextension=".zip",
            initialfile=default_name,
            filetypes=[("Whisper Pro 設定", "*.zip"), ("所有檔案", "*.*")],
            title="匯出 Whisper Pro 設定",
        )
        if not out:
            log_action("export_settings_cancelled")
            return

        whisper_dir = os.path.expanduser("~/.whisper_app")
        # Fix Cluster D-2 / 2026-05-23：app_version 從 _version.py SSoT 讀；
        # 之前 hard-coded "v2.2.0" 從 v2.2.0 之後 6 次發版都沒同步。
        from _version import __version__ as _app_ver
        manifest = {
            "schema_version": self._EXPORT_SCHEMA_VERSION,
            "exported_at":    datetime.datetime.now().isoformat(timespec="seconds"),
            "app_version":    f"v{_app_ver}",
            "files":          [],
            "excluded":       self._EXPORT_EXCLUDED_REASON,
        }
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for fname in self._EXPORT_INCLUDED:
                    src = os.path.join(whisper_dir, fname)
                    if os.path.exists(src):
                        zf.write(src, arcname=fname)
                        manifest["files"].append(fname)
                # manifest 固定寫最後（即便其他檔案缺失）
                zf.writestr("manifest.json",
                            json.dumps(manifest, indent=2, ensure_ascii=False))
            log_action("export_settings_succeeded",
                       path=out, files=",".join(manifest["files"]))
            self._toast_via_parent(f"設定已匯出 → {os.path.basename(out)}")
        except Exception:
            log_error("export_settings_failed", path=out)
            self._toast_via_parent("匯出失敗（請看 log）")

    def _import_settings(self) -> None:
        """從 zip 匯入設定；先驗證 manifest，再備份舊檔再覆寫，最後重新載入。"""
        import json, shutil, zipfile

        path = fd.askopenfilename(
            filetypes=[("Whisper Pro 設定", "*.zip"), ("所有檔案", "*.*")],
            title="匯入 Whisper Pro 設定",
        )
        if not path:
            log_action("import_settings_cancelled")
            return

        whisper_dir = os.path.expanduser("~/.whisper_app")
        os.makedirs(whisper_dir, exist_ok=True)

        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = zf.namelist()
                if "manifest.json" not in names:
                    raise ValueError("zip 內缺 manifest.json，這不是 Whisper Pro 設定檔")
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                # schema_version 檢查（目前只支援 v1）
                if manifest.get("schema_version") != self._EXPORT_SCHEMA_VERSION:
                    raise ValueError(
                        f"schema_version 不相容（檔案 v{manifest.get('schema_version')}, "
                        f"目前支援 v{self._EXPORT_SCHEMA_VERSION}）"
                    )
                # 備份既有檔（加 .imported_bak 後綴）+ 解出新檔
                imported: list[str] = []
                for fname in self._EXPORT_INCLUDED:
                    if fname not in names:
                        continue
                    dst = os.path.join(whisper_dir, fname)
                    if os.path.exists(dst):
                        shutil.copy2(dst, dst + ".imported_bak")
                    with zf.open(fname) as src, open(dst, "wb") as out:
                        shutil.copyfileobj(src, out)
                    imported.append(fname)
            log_action("import_settings_succeeded",
                       path=path, files=",".join(imported))
            self._toast_via_parent(
                f"已匯入 {len(imported)} 個檔案。請重新啟動 App 套用設定。"
            )
        except Exception as e:
            log_error("import_settings_failed", path=path)
            self._toast_via_parent(f"匯入失敗：{e}")

    def _toast_via_parent(self, msg: str) -> None:
        """從 SettingsWindow 觸發主視窗的 toast（避免設定視窗本身懸浮一個 toast）。"""
        try:
            self._parent._show_toast(msg)
        except Exception:
            pass

    # ── 搜尋（v2.28.0）────────────────────────────────────────────────────────

    def _run_search(self, event=None) -> None:
        """搜尋列按 Enter：找到就切分類 + 高亮該列；找不到就 toast 提示。"""
        query = self._search_var.get().strip()
        if not query:
            return
        match = self._search_index.get(query)
        if match is None:
            for kw, target in self._search_index.items():
                if query in kw or kw in query:
                    match = target
                    break
        if match is None:
            self._toast_via_parent(f"找不到「{query}」相關設定")
            return
        category, row_id = match
        self._select_category(category)
        self._highlight_row(row_id)

    def _highlight_row(self, row_id: str) -> None:
        """把某一列的底色短暫換成 MARK_BG（搜尋高亮），1.6 秒後淡回透明。"""
        frame = self._row_frames.get(row_id)
        if frame is None:
            return
        try:
            if not frame.winfo_exists():
                return
            frame.configure(fg_color=MARK_BG)
        except Exception:
            return

        def _revert():
            try:
                if frame.winfo_exists():
                    frame.configure(fg_color="transparent")
            except Exception:
                pass

        self.after(1600, _revert)

    # ── 輸入電平即時表（v2.28.0，「錄音」分類唯一 Canvas）───────────────────
    #
    # 安全鐵律（血條最高）：這支獨立、最小的 sd.InputStream 絕對不能跟真正的
    # 錄音搶麥克風裝置——每個 render tick 都會檢查主 App 是否進入錄音狀態，
    # 是的話立刻釋放串流；任何失敗（權限、裝置忙碌、sounddevice 不可用）都
    # 靜默降級成地板呼吸動畫，不影響設定視窗其他任何操作。

    def _start_level_meter(self) -> None:
        """進入「錄音」分類頁：重置波形引擎、啟動 render tick。"""
        self._level_rms = 0.0
        self._level_stream_failed = False
        self._level_state_start = time.perf_counter()
        self._level_last_tick = self._level_state_start
        if self._level_engine is not None:
            self._level_engine.reset()
        self._level_tick()

    def _stop_level_meter(self) -> None:
        """離開「錄音」分類頁 / 關閉設定視窗：停 render tick + 釋放麥克風串流。"""
        if self._level_after_id is not None:
            try:
                self.after_cancel(self._level_after_id)
            except Exception:
                pass
            self._level_after_id = None
        self._release_level_stream()

    def _level_tick(self) -> None:
        """電平表 render tick（20 FPS，跟主視窗 RENDER_TICK_MS 一致）。

        每 tick 檢查主 App 是否正在錄音——是的話立刻放掉麥克風裝置（避免
        跟真正的錄音搶裝置降質或失敗），並顯示地板呼吸動畫；主 App 錄完後
        下個 tick 會自動嘗試重新接上麥克風。
        """
        if self._active_category != "recording":
            return
        try:
            if not self.winfo_exists() or self._level_canvas is None or not self._level_canvas.winfo_exists():
                return
        except Exception:
            return

        main_recording = False
        try:
            main_recording = getattr(self._parent, "_state", None) == "recording"
        except Exception:
            pass

        if main_recording:
            if self._level_stream is not None:
                self._release_level_stream()
        elif self._level_stream is None and not self._level_stream_failed:
            self._open_level_stream()

        now = time.perf_counter()
        dt_ms = max(0.0, (now - self._level_last_tick) * 1000.0)
        self._level_last_tick = now
        rms = self._level_rms if (self._level_stream is not None and not main_recording) else 0.0
        self._level_engine.update(rms, dt_ms)

        try:
            self._level_canvas.delete("all")
            elapsed_ms = (now - self._level_state_start) * 1000.0
            _draw_spectral_bars(
                self._level_canvas, self._level_engine,
                width=self._LEVEL_METER_W, height=self._LEVEL_METER_H,
                elapsed_ms=elapsed_ms, big=True,
            )
        except tk.TclError:
            return

        self._level_after_id = self.after(RENDER_TICK_MS, self._level_tick)

    def _open_level_stream(self) -> None:
        """開一個獨立、最小的麥克風串流只算 RMS，不錄音、不存音訊、完全不碰
        AudioRecorder（不會干擾主錄音狀態機）。失敗一律靜默降級。"""
        try:
            import numpy as np
            import sounddevice as sd
            from recorder import SAMPLE_RATE as _sr, BLOCK_MS as _block_ms
        except Exception:
            self._level_stream_failed = True
            return
        if sd is None:
            self._level_stream_failed = True
            return
        try:
            device = self._resolve_preview_device_index()
            blocksize = int(_block_ms / 1000 * _sr)

            def _cb(indata, frames, time_info, status, _sw=self):
                try:
                    _sw._level_rms = float(np.sqrt(np.mean(indata ** 2)))
                except Exception:
                    pass

            stream = sd.InputStream(
                samplerate=_sr, channels=1, dtype="float32",
                blocksize=blocksize, device=device, callback=_cb,
            )
            stream.start()
            self._level_stream = stream
        except Exception:
            log_error("settings_level_meter_open_failed")
            self._level_stream = None
            self._level_stream_failed = True

    def _release_level_stream(self) -> None:
        """關掉目前的電平表麥克風串流（若有）。render tick 是否繼續跑不受影響。"""
        stream = self._level_stream
        self._level_stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log_error("settings_level_meter_release_failed")

    def _resolve_preview_device_index(self) -> Optional[int]:
        """把麥克風下拉選單目前選的裝置名稱換成 PortAudio index；系統預設回 None。"""
        sel = self._device_var.get()
        if sel == "（系統預設）":
            return None
        for d in self._available_devices:
            if d["name"] == sel:
                return d["id"]
        return None

    def _on_preview_device_changed(self, _value: str = "") -> None:
        """麥克風下拉選單變更時，若電平表正在跑就釋放舊串流，下個 tick 用新裝置重開。"""
        if self._active_category == "recording":
            self._release_level_stream()
            self._level_stream_failed = False

    def _on_window_destroyed(self, event=None) -> None:
        """視窗即將銷毀時停掉電平表 render loop + 麥克風串流，避免殘留背景資源。

        用 <Destroy> event binding（__init__ 裡 self.bind("<Destroy>", ...)）
        而不是覆寫 destroy()：常見情境是使用者一開 Settings 就直接按
        儲存／取消，從未離開過「錄音」分類頁，_select_category 的清理
        路徑不會被觸發，需要補最後一道防線，而 <Destroy> event 涵蓋所有
        銷毀路徑（顯式 .destroy()、WM 關閉、父視窗連鎖銷毀）。
        """
        try:
            self._stop_level_meter()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  HOTKEY BIND DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class HotkeyBindDialog(ctk.CTkToplevel):
    """快捷鍵重新綁定對話框（modal）。

    使用 Tk 原生的 <KeyPress> / <KeyRelease> 事件擷取按鍵組合，
    而非 pynput——原因見 _start_capture 的說明。

    流程：
      1. _start_capture() 綁定 Tk 鍵盤事件
      2. 使用者按下組合鍵並放開 → _on_captured() 鎖定結果
      3. 使用者點「確認套用」→ _apply() 呼叫 on_apply_cb
    """

    def __init__(self, parent, current_combo: str, on_apply_cb) -> None:
        """初始化對話框並立即開始擷取按鍵。current_combo 目前未使用，保留供未來顯示用。

        Fix Cluster H / 2026-05-23：dialog 開啟期間先暫停 NSEvent global monitor，
        避免使用者試按新 hotkey 時 hotkey_mgr 同時也接到 → 觸發背景錄音 / mini HUD
        跳出 / 結果區多一段空錄音。`destroy()` 時 restart 回來。
        """
        super().__init__(parent)
        self._on_apply_cb = on_apply_cb
        self._captured: Optional[str] = None
        # 取 AppWindow ref（CTkToplevel 的 parent 是 SettingsWindow、再上層才是 AppWindow）
        self._app_window = getattr(parent, "_parent", None)
        # Cluster H：開 dialog 期間暫停 NSEvent monitor、不讓試按誤觸錄音
        if self._app_window is not None:
            try:
                self._app_window.hotkey_mgr.stop()
                log.info("HOTKEY: monitor paused during HotkeyBindDialog")
            except Exception:
                log_error("hotkey_mgr_pause_for_dialog_failed")
        self.title("設定快捷鍵")
        self.geometry("340x220")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.grab_set()
        # WM_DELETE_WINDOW / 取消 / _apply / 紅× 都走同個 destroy() —
        # destroy() 內 Cluster H 補 restart logic（單一 source of truth）。
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._build()
        self._start_capture()

    def _build(self) -> None:
        """建立說明文字、偵測標籤、取消和確認套用按鈕。"""
        ctk.CTkLabel(
            self,
            text="請按下想要的快捷鍵\n可以是單一鍵（如右 Cmd）或組合鍵\n（按下後鬆開確認）",
            font=ctk.CTkFont("SF Pro Text", 14),
            text_color=TEXT_2,
            justify="center",
        ).pack(pady=(20, 10))

        self._detect_label = ctk.CTkLabel(
            self, text="等待按鍵…",
            font=ctk.CTkFont("SF Pro Display", 20, "bold"),
            fg_color=SURF_2, text_color=TEXT_1,
            corner_radius=10, padx=SPACE_XL, pady=SPACE_MD,
        )
        self._detect_label.pack(pady=SPACE_XS)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(pady=SPACE_LG)

        ctk.CTkButton(
            bar, text="取消", width=90, height=32, corner_radius=8,
            fg_color=SURF_2, text_color=TEXT_2,
            hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            font=ctk.CTkFont("SF Pro Text", 13),
            command=self.destroy,   # Cluster H：destroy() 統一 restart hotkey monitor
        ).pack(side="left", padx=6)

        self._apply_btn = ctk.CTkButton(
            bar, text="確認套用", width=90, height=32, corner_radius=8,
            state="disabled",
            fg_color=ACCENT, hover_color=ACCENT_HV,
            text_color=TEXT_1,
            font=ctk.CTkFont("SF Pro Text", 13, "bold"),
            command=self._apply,
        )
        self._apply_btn.pack(side="left", padx=6)

    # ── 按鍵擷取（使用 Tk 原生事件，不用 pynput）────────────────────────
    #
    # 為何不用 pynput capture_hotkey()：
    #   macOS 26.4+ 將 TSMGetInputSourceProperty 改為「僅限主執行緒」的硬斷言。
    #   pynput Listener 在背景執行緒透過 ctypes 呼叫 TSM 解析鍵碼 → SIGTRAP 閃退。
    #   主 HotkeyManager 啟動早、流程穩定所以沒崩；但此處新開 Listener 必然崩潰。
    #
    # 解決方案：
    #   改用 Tk 的 <KeyPress> / <KeyRelease> binding，事件在主執行緒回調，
    #   完全不觸碰 TSM API。唯一代價是對話框必須持續擁有鍵盤焦點（grab_set 已保證）。
    _MODIFIERS = frozenset({"cmd", "ctrl", "alt", "shift"})
    # 側別 modifier 名稱集合（用於 lone-modifier 偵測）
    _SIDED_MODIFIERS = frozenset({
        "right_cmd",    "left_cmd",
        "right_option", "left_option",
        "right_ctrl",   "left_ctrl",
        "right_shift",  "left_shift",
    })
    # 側別 → 通用（用於 combo 模式：right_cmd → cmd）
    _SIDED_TO_GENERIC = {
        "right_cmd":    "cmd",  "left_cmd":     "cmd",
        "right_option": "alt",  "left_option":  "alt",
        "right_ctrl":   "ctrl", "left_ctrl":    "ctrl",
        "right_shift":  "shift","left_shift":   "shift",
    }

    def _start_capture(self) -> None:
        self._current_keys: set[str] = set()
        self._max_combo:    set[str] = set()
        # 對話框必須搶到焦點，否則 KeyPress 事件不會進來
        self.focus_force()
        self.bind("<KeyPress>",   self._on_tk_key_press)
        self.bind("<KeyRelease>", self._on_tk_key_release)

    def _unbind_capture(self) -> None:
        """解除 KeyPress / KeyRelease 綁定，防止視窗銷毀後仍有回調觸發。"""
        try:
            self.unbind("<KeyPress>")
            self.unbind("<KeyRelease>")
        except tk.TclError:
            pass

    def _reset_cycle(self) -> None:
        """清掉上一輪的擷取狀態，讓使用者能按新組合。"""
        self._captured = None
        self._current_keys = set()
        self._max_combo = set()
        self._apply_btn.configure(state="disabled")
        self._detect_label.configure(text="等待按鍵…")

    def _on_tk_key_press(self, event) -> None:
        """記錄目前按住的所有鍵，並即時更新偵測標籤（預覽組合）。"""
        name = self._keysym_to_name(event.keysym)
        if not name:
            return
        # 若上一輪已完成且使用者開始新按鍵 → 自動重置，開啟新一輪
        if self._captured and not self._current_keys:
            self._reset_cycle()
        self._current_keys.add(name)
        if len(self._current_keys) > len(self._max_combo):
            self._max_combo = set(self._current_keys)
        # 即時預覽使用者目前按住的組合
        self._detect_label.configure(
            text=format_hotkey(self._combo_str(self._max_combo))
        )

    def _on_tk_key_release(self, event) -> None:
        """放開鍵時：判定 lone-modifier 或 combo 兩種情境。

        Lone-modifier：max_combo 恰好 1 個元素且為側別 modifier → 鎖定為 lone。
        Combo：max_combo 含至少 1 個非修飾鍵 → 鎖定為 combo（normalize 側別）。
        否則（只按了修飾鍵但不是 lone 條件）→ 提示需加字母鍵。
        """
        name = self._keysym_to_name(event.keysym)
        if name:
            self._current_keys.discard(name)
        if self._captured:
            return
        if not self._max_combo:
            return

        # Lone-modifier：恰好 1 個鍵且為側別 modifier
        if len(self._max_combo) == 1:
            only = next(iter(self._max_combo))
            if only in self._SIDED_MODIFIERS:
                self._captured = only
                self._on_captured(only)
                return

        # Combo：至少一個非修飾鍵（字母 / 數字 / 空白）
        has_non_mod = any(
            k not in self._MODIFIERS and k not in self._SIDED_MODIFIERS
            for k in self._max_combo
        )
        if not has_non_mod:
            # 只按了修飾鍵但是多個（例如 cmd+alt）→ 需要再加字母鍵
            self._detect_label.configure(
                text=format_hotkey(self._combo_str(self._max_combo)) + "   ← 需要加字母/數字/空白"
            )
            return
        combo = self._combo_str(self._max_combo)
        self._captured = combo
        self._on_captured(combo)

    @staticmethod
    def _keysym_to_name(keysym: str) -> Optional[str]:
        """Tk keysym → 我們的 combo 命名。

        側別 modifier 保留側別資訊（'right_cmd' / 'left_option' ...）；
        到了 _combo_str 才依模式決定 normalize 或保留。
        回 None 表示此鍵不納入 combo（避免 F-keys、方向鍵等產生奇怪 combo）。
        """
        ks = keysym.lower()
        # 側別感知映射：保留 left/right 資訊
        sided_map = {
            "meta_l":    "left_cmd",    "meta_r":    "right_cmd",
            "command_l": "left_cmd",    "command_r": "right_cmd",
            "super_l":   "left_cmd",    "super_r":   "right_cmd",  # 非 Mac 備援
            "control_l": "left_ctrl",   "control_r": "right_ctrl",
            "alt_l":     "left_option", "alt_r":     "right_option",
            "option_l":  "left_option", "option_r":  "right_option",
            "shift_l":   "left_shift",  "shift_r":   "right_shift",
        }
        if ks in sided_map:
            return sided_map[ks]
        if ks == "space":
            return "space"
        if len(ks) == 1 and ks.isalnum():
            return ks
        return None

    @classmethod
    def _combo_str(cls, keys: set[str]) -> str:
        """將 sided / 通用 modifier name 集合組成 combo 字串。

        Combo 模式：sided modifier 收斂為通用名稱（right_cmd → cmd）後排序輸出。
        Lone-modifier 模式：呼叫端在判定為 lone 時直接用 sided name，不走此函式。
        """
        order = ["cmd", "ctrl", "alt", "shift"]
        # 把 sided modifier 投影成通用名稱，其餘鍵保留
        normalized: set[str] = set()
        for k in keys:
            if k in cls._SIDED_TO_GENERIC:
                normalized.add(cls._SIDED_TO_GENERIC[k])
            else:
                normalized.add(k)
        mods    = [m for m in order if m in normalized]
        letters = sorted(k for k in normalized if k not in order)
        return "+".join(mods + letters)

    def _on_captured(self, combo: str) -> None:
        """鎖定組合後：更新標籤顯示，啟用「確認套用」按鈕。"""
        self._detect_label.configure(text=format_hotkey(combo))
        self._apply_btn.configure(state="normal")

    def _apply(self) -> None:
        """使用者確認後：呼叫 on_apply_cb 回傳組合字串，然後關閉對話框。"""
        if self._captured:
            self._on_apply_cb(self._captured)
        self.destroy()

    def destroy(self) -> None:
        """覆寫 destroy：先解除 Tk 鍵盤綁定 + Cluster H restart hotkey monitor，再 super.destroy。"""
        # 確保對話框關閉時解除綁定，避免 Tcl 錯誤訊息
        try:
            self._unbind_capture()
        except Exception:
            pass
        # Cluster H：dialog 關閉時 restart hotkey monitor 回來（無論走 _apply / 取消 / 紅×）
        # D4-S6（v2.10.0 / 2026-05-23）：用 hotkey_mgr 內部 `_combo_str`（上次 restart
        # 用的 combo）而非 `aw.cfg.hotkey`。當 user 在 SettingsWindow 內已透過
        # HotkeyBindDialog 改了 _apply_hotkey（寫 SettingsWindow.cfg.hotkey）但
        # 還沒按 Save → AppWindow.cfg.hotkey 仍是舊值；restart 用 aw.cfg.hotkey
        # 雖然能 resume 監聽，但對舊 cfg 一致；改用 `_combo_str` 也一樣（兩者
        # 在這個時點是同的），純粹更貼近「resume 當前 effective combo」語意，
        # 避免未來 cfg 被別處非同步改動時 race。
        if getattr(self, "_app_window", None) is not None:
            try:
                aw = self._app_window
                last_combo = getattr(aw.hotkey_mgr, "_combo_str", None) or aw.cfg.hotkey
                aw.hotkey_mgr.restart(last_combo)
                log.info(f"HOTKEY: monitor resumed after HotkeyBindDialog (combo={last_combo!r})")
                self._app_window = None   # 防止重複 restart
            except Exception:
                log_error("hotkey_mgr_resume_after_dialog_failed")
        super().destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  ACCESSIBILITY DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class AccessibilityDialog(ctk.CTkToplevel):
    """macOS 輔助使用權限引導對話框（modal）。

    首次執行若 pynput 偵測到未取得輔助使用權限，由 main.py 的非同步執行緒
    延遲 0.8s 後呼叫此對話框，告知使用者開啟系統設定的步驟。
    """

    def __init__(self, parent) -> None:
        """建立對話框並呈現步驟說明與「開啟系統設定」按鈕。"""
        super().__init__(parent)
        self.title("需要權限")
        self.geometry("500x300")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.grab_set()

        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.pack(pady=(32, 12))
        ctk.CTkLabel(
            header_row, text="",
            image=get_icon("lock", 20, TEXT_1),
            width=22,
        ).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(
            header_row, text="需要「輔助使用」權限",
            font=ctk.CTkFont("SF Pro Display", 17, "bold"),
            text_color=TEXT_1,
        ).pack(side="left")

        ctk.CTkLabel(
            self,
            text=(
                "全域快捷鍵與自動貼上功能需要開啟 macOS 輔助使用權限。\n\n"
                "開啟步驟：\n"
                "1. 點擊「開啟系統設定」\n"
                "2. 前往 隱私權與安全性 > 輔助使用\n"
                "3. 加入並允許 Terminal 或 python3\n"
                "4. 重新啟動此 App"
            ),
            font=ctk.CTkFont("SF Pro Text", 13),
            text_color=TEXT_2,
            justify="left",
        ).pack(padx=SPACE_2XL, pady=SPACE_XS)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(pady=20)

        ctk.CTkButton(
            bar, text="跳過", width=110, height=34, corner_radius=8,
            fg_color=SURF_2, text_color=TEXT_2,
            hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            font=ctk.CTkFont("SF Pro Text", 13),
            command=self.destroy,
        ).pack(side="left", padx=6)

        ctk.CTkButton(
            bar, text="開啟系統設定", width=130, height=34, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HV,
            text_color=TEXT_1,
            font=ctk.CTkFont("SF Pro Text", 13, "bold"),
            command=self._open_prefs,
        ).pack(side="left", padx=6)

    def _open_prefs(self) -> None:
        """以 macOS URL scheme 直接跳到「隱私權 > 輔助使用」設定頁，然後關閉對話框。

        IS_MAC 專屬：Windows 沒有對應的「輔助使用」權限系統，此對話框
        目前只在 macOS 流程被觸發（見 main.py），Windows 分支直接關閉、no-op。
        """
        if IS_MAC:
            subprocess.run([
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            ])
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  HistoryWindow（Phase 3.2 歷史紀錄檢視）
# ─────────────────────────────────────────────────────────────────────────────

def _hist_group_header_text(d) -> str:
    """把一個 date 物件轉成分組標頭文字。

    今天 → 「今天 · 8/14」；昨天 → 「昨天 · 8/13」；7 天內 → 「週三 · 8/12」；
    同年較早 → 「8/5」；跨年 → 「2025/12/25」。
    """
    import datetime as _dt
    today = _dt.date.today()
    delta = (today - d).days
    if delta == 0:
        rel = "今天"
    elif delta == 1:
        rel = "昨天"
    elif 0 < delta < 7:
        rel = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][d.weekday()]
    else:
        rel = None
    date_str = f"{d.year}/{d.month}/{d.day}" if d.year != today.year else f"{d.month}/{d.day}"
    return f"{rel} · {date_str}" if rel else date_str


def _hist_first_sentence(text: str, max_chars: int) -> str:
    """取第一句（遇中英文句尾標點或換行就切）、再依字數上限截斷。"""
    import re
    t = (text or "").strip()
    if not t:
        return "（空白）"
    m = re.search(r"[。！？!?\n]", t)
    if m and m.start() > 0:
        t = t[:m.start()].strip()
    if len(t) > max_chars:
        t = t[:max_chars].rstrip() + "…"
    return t or "（空白）"


def _hist_format_row_meta(entry) -> str:
    """清單 row 第二行 metadata：「14:32 · 0:18 · 96 字 · Claude」。"""
    import datetime as _dt
    time_str = _dt.datetime.fromtimestamp(entry.timestamp).strftime("%H:%M")
    total_s = int(round(entry.duration_s or 0.0))
    dur_str = f"{total_s // 60}:{total_s % 60:02d}"
    display_text = entry.polished_text or entry.raw_text or ""
    parts = [time_str, dur_str, f"{len(display_text)} 字"]
    if entry.preset_used and entry.preset_used != "default":
        p = _presets.PRESETS.get(entry.preset_used)
        parts.append(p.display_name if p else entry.preset_used)
    elif entry.target_app:
        parts.append(entry.target_app)
    return " · ".join(parts)


class HistoryWindow(ctk.CTkToplevel):
    """歷史紀錄視窗（modal-less）。

    左側：搜尋框 + 篩選 chip + 按天分組清單（日期標頭捲動時黏頂）
    右側：選中項目的詳細內容（raw / polished 對照）+ 動作按鈕
    底部：多選時出現「合併複製 N 段」列

    動作：
      • 複製 — 把 raw 或 polished 複製到剪貼簿
      • 合併複製 — ⌘/Ctrl 點選多列後，依時間順序合併複製
      • 重新潤飾 — 呼叫主視窗的 _repolish_from_history
      • 刪除 — 從 DB 刪除並刷新清單
    """

    WIN_W = 980
    WIN_H = 640

    # 篩選 chip：(key, 顯示文字)
    _FILTER_CHIPS = (
        ("all", "全部"),
        ("polished", "已潤飾"),
        ("dict", "有字典校正"),
        ("long", ">30 秒"),
    )

    # 左清單 canvas 版面規格（v2.28.0 效能重寫）：每列固定高度，繪製時就要算出
    # 確切 y 座標，不像舊版 CTkFrame.pack() 可以讓 Tk 自己排版。
    _HEADER_H = 26            # 日期分組標頭高度
    _ROW_H = 60               # 單筆列 band 高度（含上下間距）
    _ROW_GAP = SPACE_XS       # 卡片與 band 邊緣的間距，模擬原本 pack(pady=SPACE_XS)
    _CARD_MARGIN_X = SPACE_SM # 卡片左右離清單邊緣的距離，模擬原本 pack(padx=SPACE_SM)
    _ROW_PAD_X = SPACE_SM     # 卡片內文字／橫條的左右內距

    def __init__(self, parent, store, on_repolish) -> None:
        """Args:
            parent:      AppWindow 實例（master）
            store:       HistoryStore 實例
            on_repolish: 按「重新潤飾」時呼叫 on_repolish(entry)
        """
        super().__init__(parent)
        self._parent     = parent
        self._store      = store
        self._on_repolish = on_repolish

        self.title("歷史紀錄")
        self.geometry(f"{self.WIN_W}x{self.WIN_H}")
        self.minsize(800, 480)
        self.configure(fg_color=BG)
        self.transient(parent)
        self.after(50, self.lift)

        self._entries: list = []
        self._entry_by_id: dict = {}
        self._selected_ids: set = set()      # ⌘/Ctrl 多選集合
        self._row_items: dict = {}           # entry.id -> {"rect": canvas item id}
        self._row_ranges: list = []          # [(y0,y1,x0,x1,entry), ...]，點擊 hit-test 用
        self._group_offsets: list = []       # [{label,count_text,y}]，供 sticky header 用
        self._rendered_width: int = 0        # 上次繪製用的 canvas 寬度，見 _on_list_canvas_configure
        self._filter_key: str = "all"
        self._dict_terms_lc: set = set()     # 字典 term / correction 小寫快取，供「有字典校正」篩選

        self._build_ui()
        # 這裡刻意不做 `update_idletasks()` 去搶版面。canvas item 的 x 座標是繪製
        # 當下算死的（不像 widget 有 pack(fill="x") 會自己跟著容器變寬），而
        # __init__ 這個時間點視窗還沒被 map——實測就算先跑 update_idletasks()，
        # winfo_width() 仍然只回 1，結果是 scrollregion 變成 (0,0,1,12884)、卡片
        # 外框與長度橫條被壓在 x=8~-7、_row_ranges 的 x 區間跟著壞掉、使用者點
        # 列中央完全選不到。真正擋得住的是 `_on_list_canvas_configure`：等 Tk 送
        # 出第一個帶真實寬度的 <Configure> 再重畫。下面的首次 _refresh() 仍要跑，
        # 它同時負責載資料、切分組、設定預設選取，不能延後。
        self._load_dict_terms_cache()
        self._refresh(query="")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # 視窗三段式：搜尋/篩選列（固定）／主區（撐滿）／合併複製列（多選時才出現）
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # ── 搜尋 + 篩選列 ────────────────────────────────────────────────
        top = ctk.CTkFrame(self, fg_color=SURF_1, corner_radius=0)
        top.grid(row=0, column=0, sticky="ew")

        search_row = ctk.CTkFrame(top, fg_color="transparent")
        search_row.pack(fill="x", padx=SPACE_LG, pady=(SPACE_MD, SPACE_XS))

        ctk.CTkLabel(
            search_row, text="搜尋",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
            text_color=TEXT_3,
        ).pack(side="left", padx=(0, 8))

        self._search_var = tk.StringVar(value="")
        entry = ctk.CTkEntry(
            search_row,
            textvariable=self._search_var,
            width=380, height=32,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
            fg_color=SURF_2, border_color=SURF_3, text_color=TEXT_1,
            placeholder_text="輸入關鍵字（中文 ≥ 2 字、英文 ≥ 3 字）",
        )
        entry.pack(side="left", padx=SPACE_XS)
        self._search_var.trace_add("write", lambda *_: self._on_search_changed())

        self._count_label = ctk.CTkLabel(
            search_row, text="",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 12),
            text_color=TEXT_3,
        )
        self._count_label.pack(side="right", padx=SPACE_SM)

        chip_row = ctk.CTkFrame(top, fg_color="transparent")
        chip_row.pack(fill="x", padx=SPACE_LG, pady=(0, SPACE_MD))

        self._chip_buttons: dict = {}
        for key, label in self._FILTER_CHIPS:
            btn = ctk.CTkButton(
                chip_row, text=label, width=0, height=26, corner_radius=999,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
                fg_color=SURF_2, hover_color=SURF_3, text_color=TEXT_2,
                border_width=1, border_color=SURF_3,
                command=lambda k=key: self._set_filter(k),
            )
            btn.pack(side="left", padx=(0, SPACE_XS))
            self._chip_buttons[key] = btn
        self._update_chip_styles()

        # ── 主區：左清單 + 右詳細 ────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color=BG)
        body.grid(row=1, column=0, sticky="nsew")

        list_col = ctk.CTkFrame(body, width=340, fg_color=SURF_1, corner_radius=0)
        list_col.pack(side="left", fill="y")
        list_col.pack_propagate(False)

        # 日期黏頂列：固定在清單最上方，文字跟著捲動位置即時更新
        self._sticky_header = ctk.CTkFrame(list_col, height=30, fg_color=SURF_2, corner_radius=0)
        self._sticky_header.pack(side="top", fill="x")
        self._sticky_header.pack_propagate(False)
        self._sticky_date_label = ctk.CTkLabel(
            self._sticky_header, text="",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12, "bold"),
            text_color=TEXT_2,
        )
        self._sticky_date_label.pack(side="left", padx=SPACE_MD)
        self._sticky_count_label = ctk.CTkLabel(
            self._sticky_header, text="",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11),
            text_color=TEXT_3,
        )
        self._sticky_count_label.pack(side="right", padx=SPACE_MD)

        # 左清單：改用裸 tk.Canvas 畫「繪圖項目」，取代 CTkScrollableFrame +
        # 每列一張 CTkFrame 卡片。實測 234 列時舊做法捲動 139.6ms/幀（7 FPS，
        # 使用者回報「往下拉會 lag」）；canvas item 版本 700 列穩定在 9.3ms/幀、
        # 2000 列仍 8.9ms/幀——Tk canvas 只重繪可見範圍內的 item，embedded
        # widget 每幀都要對每個 widget 做一次完整幾何處理，成本隨列數線性增加。
        list_canvas_wrap = ctk.CTkFrame(list_col, fg_color=SURF_1, corner_radius=0)
        list_canvas_wrap.pack(side="top", fill="both", expand=True)
        list_canvas_wrap.grid_rowconfigure(0, weight=1)
        list_canvas_wrap.grid_columnconfigure(0, weight=1)

        self._list_canvas = tk.Canvas(
            list_canvas_wrap, bg=SURF_1, highlightthickness=0, bd=0,
        )
        self._list_canvas.grid(row=0, column=0, sticky="nsew")
        # xscrollincrement 不需要（清單只捲垂直），yscrollincrement 給滾輪
        # 用的 yview_scroll("units") 換算基準，數值照抄 customtkinter
        # ctk_scrollable_frame.py `_set_scroll_increments()`
        self._list_canvas.configure(yscrollincrement=(1 if IS_WINDOWS else 8))
        # CTkScrollableFrame 免費附贈的滾輪支援也一併沒了，見 `_on_list_mouse_wheel`
        self._list_canvas.bind("<MouseWheel>", self._on_list_mouse_wheel)
        # 寬度變了要重畫（首次 map、以及使用者拉動視窗）——見方法內註解
        self._list_canvas.bind("<Configure>", self._on_list_canvas_configure, add="+")
        self._list_canvas.bind("<Button-1>", self._on_list_canvas_click)
        self._list_canvas.bind("<Control-Button-1>", lambda e: self._on_list_canvas_click(e, toggle=True))
        if IS_MAC:
            self._list_canvas.bind("<Command-Button-1>", lambda e: self._on_list_canvas_click(e, toggle=True))

        # 捲動條沿用同規格（寬 6 圓角 3），command 直接接 canvas.yview
        self._list_scrollbar = ctk.CTkScrollbar(
            list_canvas_wrap, width=6, corner_radius=3,
            button_color=SURF_3, button_hover_color=SURF_4,
            command=self._list_canvas.yview,
        )
        self._list_scrollbar.grid(row=0, column=1, sticky="ns")

        # v2.28.0 行為沿用：內容裝得下時自動收起捲動條。模組層級的
        # `_autohide_scrollbar()` 吃的是 CTkScrollableFrame 的內部屬性
        # （`_scrollbar`／`_parent_canvas`），裸 canvas 沒有這些，另寫一份
        # 邏輯相同的版本（`_autohide_list_scrollbar`）；不去改
        # `_autohide_scrollbar()` 本身——它目前也給主視窗 `_blocks_container`
        # 用，動它的簽章風險不值得。
        self._autohide_list_scrollbar()
        self._install_scroll_hook()

        # 分隔線
        ctk.CTkFrame(body, width=1, fg_color=SURF_3).pack(side="left", fill="y")

        # 右詳細
        self._detail_frame = ctk.CTkFrame(body, fg_color=BG)
        self._detail_frame.pack(side="left", fill="both", expand=True)

        self._build_empty_detail()

        # ── 合併複製列（多選 ≥2 筆時才出現）──────────────────────────────
        self._merge_bar = ctk.CTkFrame(self, height=52, fg_color=SURF_2, corner_radius=0)
        self._merge_bar.grid(row=2, column=0, sticky="ew")
        self._merge_bar.grid_propagate(False)
        self._merge_bar.grid_remove()

        merge_inner = ctk.CTkFrame(self._merge_bar, fg_color="transparent")
        merge_inner.pack(fill="both", expand=True, padx=SPACE_LG, pady=SPACE_SM)

        self._merge_bar_btn = ctk.CTkButton(
            merge_inner, text="合併複製 0 段",
            image=get_icon("copy", 15, TEXT_1),
            compound="left",
            width=160, height=32, corner_radius=8,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
            fg_color=ACCENT, hover_color=ACCENT_HV, text_color=TEXT_1,
            command=self._merge_copy_selected,
        )
        self._merge_bar_btn.pack(side="right")

    def _install_scroll_hook(self) -> None:
        """接管清單 canvas 的 yscrollcommand，捲動（滾輪／拖拉／程式呼叫）時即時更新黏頂日期列。

        只裝一次（canvas／scrollbar 物件本身跨 refresh 不會重建，只有畫在
        canvas 上的 item 會）。
        """
        try:
            orig_set = self._list_scrollbar.set

            def _hooked(first, last, _orig=orig_set):
                _orig(first, last)
                self._update_sticky_header()

            self._list_canvas.configure(yscrollcommand=_hooked)
        except Exception:
            pass

    def _autohide_list_scrollbar(self) -> None:
        """canvas 版的自動收放捲動條，邏輯同模組層級 `_autohide_scrollbar()`（給
        CTkScrollableFrame 用），但這裡的清單是裸 tk.Canvas，沒有
        `_scrollbar` / `_parent_canvas` 這些 customtkinter 內部屬性可以借，
        只能自己重寫一份。內容改變（重新渲染）後不會觸發 canvas 的
        `<Configure>`，所以 `_render_grouped_rows()` 結尾會直接呼叫
        `self._sync_list_scrollbar()`（這裡存起來的 closure）。
        """
        canvas = self._list_canvas
        bar = self._list_scrollbar

        def _sync(_evt=None) -> None:
            try:
                region = canvas.bbox("all")
                if region is None:
                    bar.grid_remove()
                    return
                content_h = region[3] - region[1]
                visible_h = canvas.winfo_height()
                # +2 容差：內容剛好等高時不要因為捨入誤差閃出捲動條
                if content_h <= visible_h + 2:
                    bar.grid_remove()
                else:
                    bar.grid()
            except Exception:
                pass

        self._sync_list_scrollbar = _sync
        canvas.bind("<Configure>", lambda e: canvas.after_idle(_sync), add="+")
        canvas.after(80, _sync)   # 首次繪製後同步一次（此時 winfo_height 才有值）

    def _on_list_canvas_configure(self, event) -> None:
        """canvas 寬度變了就整份重畫。

        為什麼需要：canvas item 的 x 座標在繪製當下就算死了，不像原本的
        CTkFrame 卡片有 `pack(fill="x")` 會自己跟著容器變寬。所以兩種情況都
        會壞：① `__init__` 裡第一次繪製時視窗還沒 map，`winfo_width()` 回 1
        ② 使用者拉動視窗改變寬度。①的症狀實測是卡片外框與長度橫條全部被壓在
        最左邊（x=8~-7）、`_row_ranges` 的 x 區間跟著反向，使用者點列中央
        `_entry_at_point()` 回 None——**選取功能完全失效**，但文字仍正常顯示
        （文字的 x 是固定的 `left + _ROW_PAD_X`，不依賴寬度），所以只看畫面
        文字或只比對顏色的檢查抓不到。

        為什麼用 <Configure> 而不是重新加回舊版的 `after(0)/after(150)` 兩段式
        保險：定時重試是猜 Tk 什麼時候好了，<Configure> 是 Tk 直接告訴我們
        「寬度現在是多少」，可靠得多，而且順帶把視窗 resize 也一併處理掉。

        重畫會重建所有 item，捲動位置會歸零，所以先存 yview 再還原。
        """
        if event.width <= 1 or event.width == self._rendered_width:
            return
        if not self._entries:
            return
        try:
            first, _ = self._list_canvas.yview()
            self._render_grouped_rows()
            self._list_canvas.yview_moveto(first)
            self._update_sticky_header()
        except Exception:
            pass

    def _on_list_mouse_wheel(self, event) -> None:
        """滑鼠滾輪捲動清單 canvas。

        CTkScrollableFrame 內建的 `_mouse_wheel_all()` 是掛在 `bind_all` 上，
        再用 `widget.master` 鏈往上找是不是同一顆 canvas；裸 canvas 底下沒有
        子 widget 可以這樣找，直接綁在 canvas 自己身上即可（游標懸停在
        canvas 上才會收到事件，效果相同、程式碼更少）。分平台 delta 換算
        邏輯照抄 customtkinter `ctk_scrollable_frame.py` 的 `_mouse_wheel_all()`
        （本專案是 Mac／Windows 雙棲專案，Windows 分支不能省略）。
        """
        if self._list_canvas.yview() == (0.0, 1.0):
            return   # 內容裝得下，不用捲
        if IS_WINDOWS:
            self._list_canvas.yview_scroll(-int(event.delta / 6), "units")
        else:
            self._list_canvas.yview_scroll(-event.delta, "units")

    def _on_list_canvas_click(self, event, toggle: bool = False) -> None:
        """canvas 版清單點擊：換算成內容座標後查表找出點到哪一列。

        原本每列是獨立 widget，點擊事件由 Tk 自己按 widget 分派；改成畫在
        單一 canvas 上後沒有子 widget 可以綁事件，只能自己維護一份
        `self._row_ranges`（y0/y1/x0/x1 → entry 的區間對照表，繪製當下同步
        建立），點擊時線性掃過去找命中的那一列。
        """
        cx = self._list_canvas.canvasx(event.x)
        cy = self._list_canvas.canvasy(event.y)
        entry = self._entry_at_point(cx, cy)
        if entry is None:
            return
        if toggle:
            self._on_row_toggle_click(entry)
        else:
            self._on_row_plain_click(entry)

    def _entry_at_point(self, x: float, y: float):
        for y0, y1, x0, x1, entry in self._row_ranges:
            if y0 <= y < y1 and x0 <= x < x1:
                return entry
        return None

    def _build_empty_detail(self) -> None:
        """未選取時的空態畫面。"""
        for w in self._detail_frame.winfo_children():
            w.destroy()
        ctk.CTkLabel(
            self._detail_frame,
            text="← 從左側選取一筆紀錄",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 14),
            text_color=TEXT_3,
        ).pack(expand=True)

    # ── 篩選 ──────────────────────────────────────────────────────────────────

    def _set_filter(self, key: str) -> None:
        if key == self._filter_key:
            return
        self._filter_key = key
        self._update_chip_styles()
        self._refresh(query=self._search_var.get())

    def _update_chip_styles(self) -> None:
        for key, btn in self._chip_buttons.items():
            active = (key == self._filter_key)
            btn.configure(
                fg_color=ACCENT if active else SURF_2,
                hover_color=ACCENT_HV if active else SURF_3,
                text_color=TEXT_1 if active else TEXT_2,
                border_color=ACCENT if active else SURF_3,
            )

    def _passes_filter(self, e) -> bool:
        if self._filter_key == "polished":
            return e.has_polish()
        if self._filter_key == "dict":
            return self._has_dict_hit(e)
        if self._filter_key == "long":
            return (e.duration_s or 0.0) > 30.0
        return True

    def _load_dict_terms_cache(self) -> None:
        """載入目前字典（term + correction 目標字）小寫快取，供「有字典校正」篩選比對。

        這是啟發式判斷（比對目前字典內容是否出現在文字中），不是精確的「當時是否真的
        套用過校正」旗標——history.db schema 沒有存這個旗標，不新增欄位是本次任務的限制。
        """
        try:
            path = self._parent._dictionary_path()
            terms = _dictionary.load_terms(path)
            corrections = _dictionary.load_corrections(path)
            merged = {t.lower() for t in terms if t}
            merged.update(dst.lower() for _src, dst in corrections if dst)
            self._dict_terms_lc = merged
        except Exception:
            self._dict_terms_lc = set()

    def _has_dict_hit(self, e) -> bool:
        if not self._dict_terms_lc:
            return False
        hay = f"{e.raw_text or ''} {e.polished_text or ''}".lower()
        return any(term in hay for term in self._dict_terms_lc)

    # ── 資料刷新 ──────────────────────────────────────────────────────────────

    def _on_search_changed(self) -> None:
        self.after(180, lambda q=self._search_var.get(): self._refresh(q))

    def _refresh(self, query: str) -> None:
        """重新查詢、套篩選 chip、按天分組重建清單。"""
        # 若搜尋框的內容已變動（使用者繼續打字），放棄這次舊 query
        if query != self._search_var.get():
            return
        if query.strip():
            fetched = self._store.search(query, limit=200)
        else:
            fetched = self._store.list_recent(limit=200)
        self._entries = [e for e in fetched if self._passes_filter(e)]

        self._count_label.configure(text=f"{len(self._entries)} / {self._store.count()} 符合")

        # 清空舊清單內容
        self._list_canvas.delete("all")
        self._entry_by_id = {e.id: e for e in self._entries}
        self._group_offsets = []
        self._row_items = {}
        self._row_ranges = []

        if not self._entries:
            self._selected_ids = set()
            width = max(self._list_canvas.winfo_width(), 1)
            self._list_canvas.create_text(
                width / 2, 40, text="（沒有符合的紀錄）",
                fill=TEXT_4, font=(FONT_FAMILY_TEXT, 13),
            )
            self._list_canvas.configure(scrollregion=(0, 0, width, 80))
            self._sync_list_scrollbar()
            self._sticky_date_label.configure(text="")
            self._sticky_count_label.configure(text="")
            self._on_selection_changed()
            return

        # 預設選第一筆（最新一筆）
        self._selected_ids = {self._entries[0].id}
        self._render_grouped_rows()
        self._refresh_scroll_state()
        self._on_selection_changed()

    def _refresh_scroll_state(self) -> None:
        """捲回清單頂端 + 更新黏頂列文字。

        舊版這裡是兩段式 `after(0)`／`after(150)` 保險，因為 y 座標要靠
        `winfo_y()` 事後量測，而 macOS Tk 第一輪 idle 有時還沒 commit 完
        pack() 的版面（見 `_add_result_block` 的 `_scroll_to_top` 註解）。
        v2.28.0 canvas 重寫後 y 座標在 `_render_grouped_rows()` 繪製當下就
        已經算好、直接寫進 `self._group_offsets`，`scrollregion` 也是我們
        自己算好座標同步設定的，不用等任何 idle round，兩段式保險因此
        整段移除，只留下「捲回頂端」＋「刷新黏頂列文字」。
        """
        try:
            self._list_canvas.yview_moveto(0.0)
            self._update_sticky_header()
        except Exception:
            pass

    # ── 清單渲染（按天分組）──────────────────────────────────────────────────

    def _render_grouped_rows(self) -> None:
        """把 self._entries（timestamp DESC）依本地日期切段畫成 canvas item，
        段與段之間插入日期標頭列。

        v2.28.0 效能重寫：整個清單改成畫在同一個 tk.Canvas 上的繪圖 item
        （create_rectangle / create_text / create_polygon），取代原本每列
        一張 CTkFrame 卡片。實測：CTk widget 卡片 200 列 139.6ms/幀（7 FPS）；
        canvas item 700 列 9.3ms/幀、2000 列仍 8.9ms/幀——因為 Tk canvas
        只重繪可見範圍內的 item，embedded widget 每幀都要對每個 widget 做
        一次完整幾何處理，成本隨列數線性增加，canvas item 則幾乎打平。
        """
        import datetime as _dt

        canvas = self._list_canvas
        canvas.delete("all")
        self._row_items = {}
        self._row_ranges = []
        self._group_offsets = []

        width = max(canvas.winfo_width(), 1)
        self._rendered_width = width

        groups: list = []   # [(date, [entry, ...]), ...]
        for e in self._entries:
            d = _dt.datetime.fromtimestamp(e.timestamp).date()
            if groups and groups[-1][0] == d:
                groups[-1][1].append(e)
            else:
                groups.append((d, [e]))

        y = 0.0
        for d, group_entries in groups:
            label_text = _hist_group_header_text(d)
            count_text = f"{len(group_entries)} 段"
            self._draw_day_header_row(y, width, label_text, count_text)
            self._group_offsets.append({"y": y, "label": label_text, "count_text": count_text})
            y += self._HEADER_H

            day_max = max((ge.duration_s or 0.0) for ge in group_entries) or 1.0
            for ge in group_entries:
                self._draw_list_row(y, width, ge, day_max)
                y += self._ROW_H

        canvas.configure(scrollregion=(0, 0, width, y))
        self._sync_list_scrollbar()

    def _draw_day_header_row(self, y: float, width: float, label_text: str, count_text: str) -> None:
        """日期分組標頭（清單內的天然邊界；同樣文字也用於黏頂列）。"""
        canvas = self._list_canvas
        canvas.create_rectangle(0, y, width, y + self._HEADER_H, fill=SURF_2, outline="")
        canvas.create_text(
            SPACE_MD, y + self._HEADER_H / 2, text=label_text, anchor="w",
            fill=TEXT_2, font=(FONT_FAMILY_TEXT, 12, "bold"),
        )
        canvas.create_text(
            width - SPACE_MD, y + self._HEADER_H / 2, text=count_text, anchor="e",
            fill=TEXT_3, font=(FONT_FAMILY_MONO, 11),
        )

    def _draw_list_row(self, y: float, width: float, entry, day_max: float) -> None:
        """繪製單筆清單 row：卡片底 + 兩行文字（首句 + metadata）+ 底部時長橫條，
        band 高 `_ROW_H`（約 60pt）；卡片本身在 band 內縮進 `_ROW_GAP`，模擬
        原本 `card.pack(pady=SPACE_XS)` 的上下間距。
        """
        canvas = self._list_canvas
        top = y + self._ROW_GAP
        bottom = y + self._ROW_H - self._ROW_GAP
        left = self._CARD_MARGIN_X
        right = width - self._CARD_MARGIN_X
        selected = entry.id in self._selected_ids

        rect_id = self._create_rounded_rect(
            left, top, right, bottom, radius=8,
            fill=SURF_2 if selected else SURF_1,
            outline=ACCENT if selected else SURF_3,
            width=1,
        )
        self._row_items[entry.id] = {"rect": rect_id}
        self._row_ranges.append((top, bottom, left, right, entry))

        display_text = entry.polished_text or entry.raw_text or ""
        canvas.create_text(
            left + self._ROW_PAD_X, top + 17, anchor="w",
            text=_hist_first_sentence(display_text, 20),
            fill=TEXT_1, font=(FONT_FAMILY_TEXT, 13),
        )
        canvas.create_text(
            left + self._ROW_PAD_X, top + 35, anchor="w",
            text=_hist_format_row_meta(entry),
            fill=TEXT_3, font=(FONT_FAMILY_MONO, 11),
        )

        # 長度橫條：軌道 + 填充，寬度比例 = 該段秒數 ÷ 當天最長
        track_y = bottom - 9
        track_left = left + self._ROW_PAD_X
        track_right = right - self._ROW_PAD_X
        canvas.create_rectangle(track_left, track_y, track_right, track_y + 2, fill=SURF_3, outline="")
        frac = max(0.03, min(1.0, (entry.duration_s or 0.0) / day_max))
        fill_right = track_left + (track_right - track_left) * frac
        canvas.create_rectangle(track_left, track_y, fill_right, track_y + 2, fill=ACCENT, outline="")

    def _create_rounded_rect(self, x0: float, y0: float, x1: float, y1: float, radius: float, **kwargs):
        """用 create_polygon(smooth=True) 畫圓角矩形——tk canvas 沒有原生圓角矩形
        item。實測 200 列（真實 history.db）連續捲動 20 幀，中位數穩定落在
        8.7~9.0ms（16ms 預算內），維持圓角沒有超標，所以保留這個做法而不是
        退回 create_rectangle 直角。最差值偶爾飆到 20~40ms（系統雜訊，如背景
        程序搶 CPU／GC），但驗收標準看的是中位數，不受這類偶發尖峰影響。
        """
        r = radius
        points = [
            x0 + r, y0,
            x1 - r, y0,
            x1, y0,
            x1, y0 + r,
            x1, y1 - r,
            x1, y1,
            x1 - r, y1,
            x0 + r, y1,
            x0, y1,
            x0, y1 - r,
            x0, y0 + r,
            x0, y0,
        ]
        return self._list_canvas.create_polygon(points, smooth=True, **kwargs)

    def _update_sticky_header(self, *_args) -> None:
        """依目前捲動位置，把黏頂列文字換成「當前最上方那個分組」的日期／筆數。"""
        if not self._group_offsets:
            return
        try:
            top_y = self._list_canvas.canvasy(0)
        except Exception:
            return
        current = self._group_offsets[0]
        for off in self._group_offsets:
            if off["y"] <= top_y + 2:
                current = off
            else:
                break
        # v2.28.0 捲動流暢度：只在「日期真的變了」才 configure。
        #   CTk 的 configure() 會走完整的選項處理 + 觸發重繪，成本遠高於一次
        #   字串比較；而捲動事件每秒觸發數十次、日期卻多半沒變——原本每次都
        #   無條件 configure 兩個 label，是使用者回報「往下拉不順」的主因之一。
        if getattr(self, "_sticky_last_label", None) != current["label"]:
            self._sticky_last_label = current["label"]
            self._sticky_date_label.configure(text=current["label"])
            self._sticky_count_label.configure(text=current["count_text"])

    # ── 選取（單選 / ⌘ 多選）──────────────────────────────────────────────────

    def _on_row_plain_click(self, entry) -> None:
        self._selected_ids = {entry.id}
        self._on_selection_changed()

    def _on_row_toggle_click(self, entry) -> None:
        ids = set(self._selected_ids)
        if entry.id in ids:
            ids.discard(entry.id)
        else:
            ids.add(entry.id)
        self._selected_ids = ids
        self._on_selection_changed()

    def _clear_selection(self) -> None:
        self._selected_ids = set()
        self._on_selection_changed()

    def _apply_selection_styles(self) -> None:
        canvas = self._list_canvas
        for eid, info in self._row_items.items():
            sel = eid in self._selected_ids
            try:
                canvas.itemconfig(
                    info["rect"],
                    fill=SURF_2 if sel else SURF_1,
                    outline=ACCENT if sel else SURF_3,
                )
            except Exception:
                pass

    def _on_selection_changed(self) -> None:
        """選取集合變動後的單一進入點：重刷 row 高亮、右側詳細、合併複製列。"""
        self._apply_selection_styles()
        n = len(self._selected_ids)
        if n >= 2:
            self._merge_bar_btn.configure(text=f"合併複製 {n} 段")
            self._merge_bar.grid()
            self._build_multi_detail()
        else:
            self._merge_bar.grid_remove()
            if n == 1:
                entry = self._entry_by_id.get(next(iter(self._selected_ids)))
                if entry is not None:
                    self._build_detail(entry)
                else:
                    self._build_empty_detail()
            else:
                self._build_empty_detail()

    # ── 右側詳細 ──────────────────────────────────────────────────────────────

    def _build_detail(self, entry) -> None:
        """右側單筆詳細視圖。"""
        import datetime as _dt
        for w in self._detail_frame.winfo_children():
            w.destroy()

        pad = 20
        # Header
        header = ctk.CTkFrame(self._detail_frame, fg_color="transparent")
        header.pack(fill="x", padx=pad, pady=(pad, 8))

        time_str = _dt.datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        ctk.CTkLabel(
            header, text=time_str,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 13),
            text_color=TEXT_3,
        ).pack(side="left")

        meta_parts = [f"{entry.duration_s:.1f}s"]
        if entry.language:
            meta_parts.append(entry.language)
        meta_parts.append(entry.model_whisper)
        if entry.target_app:
            meta_parts.append(f"→ {entry.target_app}")
        if entry.preset_used != "default":
            p = _presets.PRESETS.get(entry.preset_used)
            meta_parts.append(p.display_name if p else entry.preset_used)
        ctk.CTkLabel(
            header, text="  ·  ".join(meta_parts),
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
            text_color=TEXT_3,
        ).pack(side="left", padx=SPACE_MD)

        # 原文 / 潤飾 對照（潤飾在上，原文在下；有潤飾就顯示兩段）
        query = self._search_var.get().strip()
        if entry.polished_text:
            self._build_text_block(self._detail_frame, "潤飾版", entry.polished_text, ACCENT, query)
            self._build_text_block(self._detail_frame, "原文", entry.raw_text, TEXT_3, query)
        else:
            self._build_text_block(self._detail_frame, "原文", entry.raw_text, TEXT_2, query)

        # 動作列
        bar = ctk.CTkFrame(self._detail_frame, fg_color="transparent", height=56)
        bar.pack(fill="x", side="bottom", padx=pad, pady=pad)

        def _copy_text(text: str) -> None:
            try:
                import pyperclip
                pyperclip.copy(text)
                self._toast("已複製")
            except Exception:
                log_error("history_copy_failed")

        ctk.CTkButton(
            bar, text="複製潤飾版" if entry.polished_text else "複製原文",
            image=get_icon("copy", 15, TEXT_1),
            compound="left",
            width=140, height=32, corner_radius=8,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
            fg_color=ACCENT, hover_color=ACCENT_HV, text_color=TEXT_1,
            command=lambda: _copy_text(entry.polished_text or entry.raw_text),
        ).pack(side="left", padx=SPACE_XS)

        if entry.polished_text:
            ctk.CTkButton(
                bar, text="複製原文",
                width=110, height=32, corner_radius=8,
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
                fg_color=SURF_2, hover_color=SURF_3,
                border_width=1, border_color=SURF_3,
                text_color=TEXT_2,
                command=lambda: _copy_text(entry.raw_text),
            ).pack(side="left", padx=SPACE_XS)

        ctk.CTkButton(
            bar, text="重新潤飾",
            image=get_icon("sparkles", 15, TEXT_2),
            compound="left",
            width=110, height=32, corner_radius=8,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
            fg_color=SURF_2, hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            text_color=TEXT_2,
            command=lambda: (self._on_repolish(entry), self.destroy()),
        ).pack(side="left", padx=SPACE_XS)

        ctk.CTkButton(
            bar, text="刪除",
            image=get_icon("x", 15, TEXT_3),
            compound="left",
            width=90, height=32, corner_radius=8,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
            fg_color=SURF_2, hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            text_color=TEXT_3,
            command=lambda: self._delete_selected(entry.id),
        ).pack(side="right", padx=SPACE_XS)

    def _build_multi_detail(self) -> None:
        """右側多選摘要視圖：列出已選段落 + 清除選取（合併複製動作在底部列）。"""
        import datetime as _dt
        for w in self._detail_frame.winfo_children():
            w.destroy()

        pad = 20
        chosen = sorted(
            (self._entry_by_id[i] for i in self._selected_ids if i in self._entry_by_id),
            key=lambda e: e.timestamp,
        )

        ctk.CTkLabel(
            self._detail_frame, text=f"已選取 {len(chosen)} 段",
            font=ctk.CTkFont(FONT_FAMILY_UI, 17, "bold"),
            text_color=TEXT_1, anchor="w",
        ).pack(fill="x", padx=pad, pady=(pad, SPACE_SM))

        preview = ctk.CTkScrollableFrame(self._detail_frame, fg_color=SURF_1, corner_radius=8)
        preview.pack(fill="both", expand=True, padx=pad, pady=(0, SPACE_SM))
        for e in chosen:
            t = _dt.datetime.fromtimestamp(e.timestamp).strftime("%H:%M")
            text = _hist_first_sentence(e.polished_text or e.raw_text or "", 40)
            ctk.CTkLabel(
                preview, text=f"{t} · {text}",
                font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
                text_color=TEXT_2, anchor="w", justify="left",
            ).pack(fill="x", padx=SPACE_SM, pady=(SPACE_XS, 0))

        ctk.CTkButton(
            self._detail_frame, text="清除選取",
            width=110, height=32, corner_radius=8,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
            fg_color=SURF_2, hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            text_color=TEXT_2,
            command=self._clear_selection,
        ).pack(anchor="w", padx=pad, pady=(0, pad))

    def _merge_copy_selected(self) -> None:
        """把目前多選的段落依時間先後合併，一次複製到剪貼簿。"""
        chosen = sorted(
            (self._entry_by_id[i] for i in self._selected_ids if i in self._entry_by_id),
            key=lambda e: e.timestamp,
        )
        if len(chosen) < 2:
            return
        merged_text = "\n\n".join((e.polished_text or e.raw_text or "").strip() for e in chosen)
        try:
            import pyperclip
            pyperclip.copy(merged_text)
            self._toast(f"已複製 {len(chosen)} 段")
            log_action("history_merge_copy", count=len(chosen))
        except Exception:
            log_error("history_merge_copy_failed")
            self._toast("複製失敗")

    def _build_text_block(self, parent, label: str, text: str, label_color: str, query: str = "") -> None:
        """渲染一段標籤 + 文字區塊；query 非空時用 MARK_BG 高亮所有比對到的片段。"""
        block = ctk.CTkFrame(parent, fg_color="transparent")
        block.pack(fill="both", expand=True, padx=20, pady=(8, 0))

        ctk.CTkLabel(
            block, text=label,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11, "bold"),
            text_color=label_color,
        ).pack(anchor="w", pady=(0, 4))

        box = ctk.CTkTextbox(
            block,
            fg_color=SURF_1, border_color=SURF_3, border_width=1,
            text_color=TEXT_1, font=ctk.CTkFont(FONT_FAMILY_TEXT, 14),
            wrap="word", corner_radius=8,
            scrollbar_button_color=SURF_3,
        )
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        if query:
            self._highlight_matches(box, text, query)
        box.configure(state="disabled")

    def _highlight_matches(self, box, text: str, query: str) -> None:
        """在 CTkTextbox 裡用 tag_config 高亮所有 query 命中片段（大小寫不分）。"""
        try:
            box.tag_config("hist_search_mark", background=MARK_BG)
            low_text = (text or "").lower()
            low_q = query.lower()
            if not low_q:
                return
            start = 0
            while True:
                idx = low_text.find(low_q, start)
                if idx == -1:
                    break
                box.tag_add("hist_search_mark", f"1.0+{idx}c", f"1.0+{idx + len(query)}c")
                start = idx + len(query)
        except Exception:
            pass

    def _delete_selected(self, id: int) -> None:
        """刪除當前選中的紀錄並刷新。"""
        if not self._store.delete(id):
            self._toast("刪除失敗")
            return
        log_action("history_deleted", id=id)
        self._toast("已刪除")
        self._refresh(query=self._search_var.get())

    def _toast(self, msg: str) -> None:
        """沿用 AppWindow 的 toast（在 parent 上顯示）。"""
        try:
            self._parent._show_toast(msg)
        except Exception:
            pass


def _walk_children(widget):
    """遞迴 yield 一個 widget 底下所有子元件（含深層）。"""
    for child in widget.winfo_children():
        yield child
        yield from _walk_children(child)


# ─────────────────────────────────────────────────────────────────────────────
#  UtteranceBlock（Fix 19 Path B / 2026-05-23）— Speakly-style 獨立段落區塊
# ─────────────────────────────────────────────────────────────────────────────

class UtteranceBlock(ctk.CTkFrame):
    """單一語音段的視覺容器（Speakly 風格獨立 block）。

    每個 block 顯示一段轉錄結果，含時間戳、時長、語言/模型、文字、
    per-block 動作圖示（複製 / 刪除）。最新 block 由 AppWindow 標示
    （邊框 ACCENT）；舊 block 邊框灰色。

    原文 / 潤飾 toggle 仍由 AppWindow 頂部 chip 全域控制（操作最新 block），
    但 polished_text 與 raw_text 都存在 block 自己，舊 block 切換時的狀態被
    凍結保留（PR #14 之後若需要 per-block toggle，加 hover-only 圖示即可）。
    """

    HEADER_ICON_SIZE = 14

    def __init__(
        self,
        master,
        *,
        raw_text: str,
        timestamp_iso: str,
        duration_s: float,
        language: str,
        model: str,
        on_copy,
        on_delete,
        wraplength: int = 600,
    ) -> None:
        super().__init__(
            master, corner_radius=10,
            fg_color=SURF_1,
            border_width=1, border_color=SURF_3,
        )
        # State
        self.raw_text: str = raw_text
        self.polished_text: Optional[str] = None
        self.showing_polished: bool = False
        self.timestamp_iso = timestamp_iso
        self.duration_s = duration_s
        self.language = language
        self.model = model
        self._on_copy = on_copy
        self._on_delete = on_delete
        self._wraplength = wraplength
        self._build_ui()

    def _build_ui(self) -> None:
        # Header row：時間戳 + 元資料 | 動作圖示
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=SPACE_MD, pady=(SPACE_SM, 2))

        meta_text = (
            f"{self.timestamp_iso}  ·  {self.duration_s:.1f}s  ·  "
            f"{self.language.upper() if self.language else '?'}  ·  {self.model}"
        )
        ctk.CTkLabel(
            hdr, text=meta_text,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11),
            text_color=TEXT_3,
            anchor="w",
        ).pack(side="left")

        # 動作圖示在右側
        actions = ctk.CTkFrame(hdr, fg_color="transparent")
        actions.pack(side="right")
        btn_common = dict(
            text="", width=24, height=24, corner_radius=4,
            fg_color="transparent",
            hover_color=SURF_2,
            border_width=0,
        )
        ctk.CTkButton(
            actions,
            image=get_icon("copy", self.HEADER_ICON_SIZE, TEXT_3),
            command=lambda: self._on_copy(self),
            **btn_common,
        ).pack(side="left", padx=1)
        ctk.CTkButton(
            actions,
            image=get_icon("x", self.HEADER_ICON_SIZE, TEXT_3),
            command=lambda: self._on_delete(self),
            **btn_common,
        ).pack(side="left", padx=1)

        # Body：文字顯示
        self._text_label = ctk.CTkLabel(
            self, text=self.raw_text,
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 14),
            text_color=TEXT_1,
            justify="left", anchor="w",
            wraplength=self._wraplength,
        )
        self._text_label.pack(
            fill="x", padx=SPACE_MD, pady=(0, SPACE_MD),
        )

    # ── 對外 API ──────────────────────────────────────────────────────────

    def get_current_text(self) -> str:
        """目前顯示的文字（依 showing_polished 決定 raw / polished）。"""
        if self.showing_polished and self.polished_text is not None:
            return self.polished_text
        return self.raw_text

    def set_polished(self, polished_text: str) -> None:
        """寫入潤飾版並切換顯示為潤飾版。"""
        self.polished_text = polished_text
        self.showing_polished = True
        self._text_label.configure(text=polished_text)

    def set_showing_polished(self, show_polished: bool) -> None:
        """切換顯示原文 / 潤飾版；無潤飾版時 noop。"""
        if self.polished_text is None:
            return
        if show_polished == self.showing_polished:
            return
        self.showing_polished = show_polished
        self._text_label.configure(text=self.get_current_text())

    def highlight_as_latest(self, is_latest: bool) -> None:
        """視覺標示「我是最新一段」（邊框色變化）。"""
        self.configure(
            border_color=(ACCENT if is_latest else SURF_3),
        )

    def update_wraplength(self, new_wraplength: int) -> None:
        """視窗 resize 時呼叫，讓文字 wrap 寬度跟著變。"""
        self._wraplength = new_wraplength
        self._text_label.configure(wraplength=new_wraplength)


# ─────────────────────────────────────────────────────────────────────────────
#  UtteranceBlockV2（Aperture 主視窗內容層 / v2.28.0）── 新骨架專用卡片
# ─────────────────────────────────────────────────────────────────────────────
#  只在 self._skeleton_mode（cfg.record_visual != "chamber"）時被建立；chamber
#  逃生門完全走上面的舊 UtteranceBlock，兩個類別故意不共用一行程式碼、互不
#  相依——改這裡不會波及 chamber 逃生門，反之亦然。
#
#  三個外部狀態（由 AppWindow 呼叫對應方法驅動，決定卡片整體長相）：
#      loading ──set_result()──▶ ready
#              ──set_failed()──▶ failed（終態，不會再變回 loading/ready）
#  潤飾狀態疊加在 ready 之上（不影響 loading/failed）：
#      none ──set_polish_pending()──▶ pending ──set_polished()──▶ ready
#                                              └─set_polish_failed()─▶ failed
#
#  每次狀態切換都整段清掉子元件重建（_clear + _build_*），不做局部 patch。
#  切換頻率低（一次轉錄、一次潤飾、一次 toggle），用「重建」換「不用維護
#  一堆局部更新分支」的簡單，划算（簡單優先）。
#
#  公開 API 刻意跟舊 UtteranceBlock 同名同義（get_current_text / set_polished /
#  set_showing_polished / highlight_as_latest / update_wraplength / raw_text /
#  polished_text / showing_polished），AppWindow 既有呼叫點（_on_block_copy、
#  _finish_polish 的 identity 比對等）才能兩種卡片共用同一段程式碼，只有
#  「怎麼畫」不一樣、「怎麼被呼叫」維持一致。
# ─────────────────────────────────────────────────────────────────────────────

class UtteranceBlockV2(ctk.CTkFrame):
    """Aperture 卡片：見本區塊頂部的狀態機說明。"""

    BODY_MAX_LINES = 8   # 顯示行數 > 8 才截斷（≈200pt），見 _relayout_body

    def __init__(self, master, *, on_copy, on_save, wraplength: int = 608) -> None:
        super().__init__(master, corner_radius=9, border_width=1)
        self._on_copy_cb = on_copy
        self._on_save_cb = on_save
        # tk.Text 用 pack(fill="x") 讓 Tk 自己依實際渲染寬度 reflow（見
        # _build_body），不需要像舊版 CTkLabel.wraplength 那樣手動算像素寬。
        # 保留這個欄位只是為了跟呼叫端既有的 update_wraplength(w) 簽章相容。
        self._wraplength = wraplength

        # ── 對外可見狀態（跟舊 UtteranceBlock 同名同義）──────────────────
        self.raw_text: str            = ""
        self.polished_text: Optional[str] = None
        self.showing_polished: bool   = False
        self.timestamp_iso: str       = ""
        self.timestamp_epoch: float   = 0.0
        self.duration_s: float        = 0.0
        self.language: str            = ""
        self.model: str                = ""
        self.history_id: Optional[int] = None   # 供「最近」區跟目前 session 去重用

        # ── 內部狀態 ─────────────────────────────────────────────────────
        self._card_state: str    = "loading"   # loading / ready / failed
        self._polish_state: str  = "none"      # none / pending / ready / failed
        self._corrections: list[str] = []
        self._is_latest  = True
        self._hovering   = False
        self._expanded   = False
        self._body_text: Optional[tk.Text] = None
        self._body_wrap: Optional[ctk.CTkFrame] = None
        self._expand_row: Optional[ctk.CTkFrame] = None
        self._expand_link: Optional[ctk.CTkLabel] = None
        self._fail_message = ""

        self._build_loading()

    # ══════════════════════════════════════════════════════════════════════
    #  外部驅動 API（AppWindow 呼叫）
    # ══════════════════════════════════════════════════════════════════════

    def set_result(
        self, *, raw_text: str, timestamp_iso: str, duration_s: float,
        language: str, model: str, corrections: Optional[list[str]] = None,
    ) -> None:
        """loading → ready：填入真正的轉錄內容。"""
        self.raw_text          = raw_text
        self.polished_text     = None
        self.showing_polished  = False
        self.timestamp_iso     = timestamp_iso
        self.duration_s        = duration_s
        self.language          = language
        self.model              = model
        self._corrections      = list(corrections or [])
        self._polish_state     = "none"
        self._card_state       = "ready"
        self._expanded          = False
        self._build_ready()

    def set_failed(self, message: str = "轉錄失敗") -> None:
        """→ failed（終態）。呼叫端須自行先設好 self.timestamp_iso。"""
        self._card_state   = "failed"
        self._fail_message = message
        self._build_failed()

    def set_polish_pending(self) -> None:
        """ready 狀態下、AI 潤飾已送出還沒回來——segmented「潤飾」格顯示載入態。"""
        if self._card_state != "ready":
            return
        self._polish_state = "pending"
        self._build_ready()

    def set_polished(self, polished_text: str) -> None:
        """潤飾完成：寫入潤飾版並切換顯示為潤飾版。"""
        self.polished_text    = polished_text
        self.showing_polished = True
        self._polish_state    = "ready"
        if self._card_state == "ready":
            self._build_ready()

    def set_polish_failed(self) -> None:
        """潤飾失敗：降級回原文，segmented「潤飾」格顯示失敗提示。"""
        if self._card_state != "ready":
            return
        self.polished_text    = None
        self.showing_polished = False
        self._polish_state    = "failed"
        self._build_ready()

    def get_current_text(self) -> str:
        """目前顯示的文字（failed 卡沒有正文可複製、回空字串）。"""
        if self._card_state != "ready":
            return ""
        if self.showing_polished and self.polished_text is not None:
            return self.polished_text
        return self.raw_text

    def set_showing_polished(self, show_polished: bool) -> None:
        """切換原文／潤飾版；無潤飾版時 noop。"""
        if self.polished_text is None:
            return
        if show_polished == self.showing_polished:
            return
        self.showing_polished = show_polished
        self._build_ready()

    def highlight_as_latest(self, is_latest: bool) -> None:
        """視覺標示「我是最新一段」（CARD_HI/LINE_HI vs CARD/LINE + 模型名顯示）。"""
        self._is_latest = is_latest
        self._apply_card_colors()
        # 模型名只在最新段顯示、meta 行需要整段重建才能反映
        if self._card_state == "ready":
            self._build_ready()

    def update_wraplength(self, new_width: int) -> None:
        """視窗 resize 時呼叫：tk.Text 已經靠 pack(fill="x") 自動 reflow，
        這裡只需要重新量一次顯示行數、決定要不要截斷（見 _relayout_body）。
        """
        self._wraplength = new_width
        if self._body_text is not None:
            self.after_idle(self._relayout_body)

    # ══════════════════════════════════════════════════════════════════════
    #  版面重建
    # ══════════════════════════════════════════════════════════════════════

    def _clear(self) -> None:
        for w in self.winfo_children():
            w.destroy()
        self._body_text   = None
        self._body_wrap   = None
        self._expand_row  = None
        self._expand_link = None

    def _apply_card_colors(self) -> None:
        if self._card_state == "failed":
            self.configure(fg_color=RED_BG, border_color=RED_LINE)
            return
        fg = CARD_HI if self._is_latest else CARD
        # 最新段邊框恆為 LINE_HI；舊段平常 LINE、hover 時暫時轉 LINE_HI。
        border = LINE_HI if (self._is_latest or self._hovering) else LINE
        self.configure(fg_color=fg, border_color=border)

    def _bind_hover(self) -> None:
        """整張卡（含所有子元件）hover 時邊框變化。add="+" 是必要的——子元件
        裡的圖示鈕／segmented 按鈕自己已經綁了 <Enter>/<Leave> 做 hover_color，
        不加 "+" 會直接蓋掉那些既有 binding、按鈕自己的 hover 回饋會消失。
        """
        def _enter(_e=None):
            self._hovering = True
            self._apply_card_colors()

        def _leave(_e=None):
            self._hovering = False
            self._apply_card_colors()

        for w in (self, *_walk_children(self)):
            w.bind("<Enter>", _enter, add="+")
            w.bind("<Leave>", _leave, add="+")

    # ── loading 態：卡片高度固定 88，避免下一段內容填入時跳動 ──────────────

    def _build_loading(self) -> None:
        self._clear()
        self.configure(height=88)
        self.pack_propagate(False)
        self._apply_card_colors()

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=14)

        # 3 條骨架：11pt 高、gap 9、寬 96%/88%/54%、色 SKEL/SKEL_2 交替。
        bar_specs = ((0.96, SKEL), (0.88, SKEL_2), (0.54, SKEL))
        for i, (frac, color) in enumerate(bar_specs):
            row = ctk.CTkFrame(body, fg_color="transparent", height=11)
            row.pack(fill="x", pady=(0 if i == 0 else 9, 0))
            row.pack_propagate(False)
            ctk.CTkFrame(row, fg_color=color, corner_radius=3).place(
                relx=0, rely=0, relwidth=frac, relheight=1.0
            )

        self._bind_hover()

    # ── failed 態：RED_BG/RED_LINE 卡片（終態）──────────────────────────

    def _build_failed(self) -> None:
        self._clear()
        self.pack_propagate(True)
        self._apply_card_colors()

        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=16, pady=(14, 16))

        row = ctk.CTkFrame(wrap, fg_color="transparent", height=22)
        row.pack(fill="x")
        row.pack_propagate(False)
        ctk.CTkLabel(
            row, text=self.timestamp_iso, anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11, "bold"), text_color=RED_TEXT,
        ).pack(side="left")

        ctk.CTkLabel(
            wrap, text=f"⚠ {self._fail_message}", anchor="w", justify="left",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 13), text_color=RED_TEXT,
            wraplength=560,
        ).pack(fill="x", pady=(8, 0))

        self._bind_hover()

    # ── ready 態 ─────────────────────────────────────────────────────────

    def _build_ready(self) -> None:
        self._clear()
        self.pack_propagate(True)
        self._apply_card_colors()

        # padding 14 上／16 左右／16 下（下比上多 2、補正文最後一行的行高餘白）
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=16, pady=(14, 16))

        self._build_meta_row(wrap)
        self._build_body(wrap)

        self._bind_hover()

    def _build_meta_row(self, parent) -> None:
        """meta 行：高 30、margin-bottom 10、元素 gap 9——時間｜長度·模型｜
        校正 chip｜segmented｜圖示鈕（複製／存檔），單一橫向 flow，非兩端對齊。

        v2.28.0 行高 22 → 30：這一行有 `pack_propagate(False)`，所以它的高度
        會**反過來把子元素壓扁**——圖示鈕宣告 24×24、實測只渲染成 28×22。
        使用者回報「複製跟下載按鈕太小、很多都是沒有生效的」，實測整排可點
        目標都只有 20~22pt 高（macOS 指標介面舒適下限約 28）。行高不先放大，
        單獨改按鈕尺寸不會生效。
        """
        row = ctk.CTkFrame(parent, fg_color="transparent", height=30)
        row.pack(fill="x", pady=(0, 10))
        row.pack_propagate(False)

        time_color = META_HI if self._is_latest else META
        ctk.CTkLabel(
            row, text=self.timestamp_iso, anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11, "bold"), text_color=time_color,
        ).pack(side="left", padx=(0, 9))

        # 規格「長度·模型」只列這兩項（語言刻意不放，維持 meta 行精簡）
        meta_bits = [f"{self.duration_s:.0f}s"]
        if self._is_latest and self.model:   # 模型名只在最新段顯示
            meta_bits.append(self.model)
        ctk.CTkLabel(
            row, text=" · ".join(meta_bits), anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11), text_color=TEXT_4,
        ).pack(side="left", padx=(0, 9))

        if self._corrections:
            chip_font = ctk.CTkFont(FONT_FAMILY_TEXT, 10, "bold")
            chip_text = f"校正 {len(self._corrections)}"
            ctk.CTkLabel(
                row, text=chip_text, fg_color=CHIP_BG, text_color=CYAN_TEXT,
                corner_radius=5, height=20,
                width=chip_font.measure(chip_text) + 7 * 2,
                font=chip_font,
            ).pack(side="left", padx=(0, 9))

        self._build_segmented(row)

        icons = ctk.CTkFrame(row, fg_color="transparent")
        icons.pack(side="left")
        self._icon_btn(icons, "copy", self._handle_copy).pack(side="left", padx=(0, 4))
        self._icon_btn(icons, "download", self._handle_save).pack(side="left")

    def _icon_btn(self, parent, icon_name: str, command) -> ctk.CTkButton:
        """圖示鈕 30×30 radius 7（複製 ⧉、存檔）——貼著它作用的那一段，
        取代舊版放在底部工具列的複製/存檔按鈕。

        v2.28.0 由 24×24／圖示 12px 放大到 30×30／圖示 15px：實測原本受限於
        meta 行的 height=22，實際只渲染成 28×22，使用者用觸控板點常常落空。
        30 是「舒適下限 28」再加一點餘裕，仍比一般文字鈕（32）小、不會在
        meta 行裡搶戲。圖示同步 12→15，否則放大後圖示會顯得虛浮。
        """
        color = META_HI if self._is_latest else META
        return ctk.CTkButton(
            parent, text="", image=get_icon(icon_name, 15, color),
            width=30, height=30, corner_radius=7,
            fg_color="transparent", hover_color=ROW_HI,
            command=command,
        )

    def _handle_copy(self) -> None:
        self._on_copy_cb(self)

    def _handle_save(self) -> None:
        self._on_save_cb(self)

    def _build_segmented(self, parent) -> None:
        """segmented（原文｜潤飾）：高 30 radius 8、SEG_BG+SEG_LINE 外殼、
        選中格 SEG_ON+SEG_ON_FG。潤飾中 → 「潤飾中…」disabled；潤飾失敗 →
        「潤飾失敗」RED_TEXT disabled；沒有潤飾版 → 「潤飾」disabled。
        """
        has_polished = self.polished_text is not None
        pending = self._polish_state == "pending"
        failed  = self._polish_state == "failed"

        shell = ctk.CTkFrame(
            parent, fg_color=SEG_BG, border_width=1, border_color=SEG_LINE,
            corner_radius=8, height=30,
        )
        shell.pack(side="left", padx=(0, 9))
        shell.pack_propagate(False)
        inner = ctk.CTkFrame(shell, fg_color="transparent")
        inner.pack(padx=1, pady=1)

        seg_font = ctk.CTkFont(FONT_FAMILY_TEXT, 11)

        raw_selected = not self.showing_polished
        ctk.CTkButton(
            inner, text="原文", font=seg_font,
            width=seg_font.measure("原文") + 11 * 2, height=26, corner_radius=7,
            fg_color=(SEG_ON if raw_selected else "transparent"),
            text_color=(SEG_ON_FG if raw_selected else TEXT_3),
            hover_color=(SEG_ON if raw_selected else SEG_BG),
            command=lambda: self.set_showing_polished(False),
        ).pack(side="left")

        if pending:
            pol_text, pol_enabled = "潤飾中…", False
        elif failed:
            pol_text, pol_enabled = "潤飾失敗", False
        else:
            pol_text, pol_enabled = "潤飾", has_polished

        pol_selected = self.showing_polished and has_polished
        if failed:
            pol_text_color = RED_TEXT
        elif pol_selected:
            pol_text_color = SEG_ON_FG
        elif pol_enabled:
            pol_text_color = TEXT_3
        else:
            pol_text_color = TEXT_4

        ctk.CTkButton(
            inner, text=pol_text, font=seg_font,
            width=seg_font.measure(pol_text) + 11 * 2, height=26, corner_radius=7,
            fg_color=(SEG_ON if pol_selected else "transparent"),
            text_color=pol_text_color,
            hover_color=(SEG_ON if pol_selected else SEG_BG),
            state=("normal" if pol_enabled else "disabled"),
            command=(lambda: self.set_showing_polished(True)) if pol_enabled else None,
        ).pack(side="left")

    BODY_FONT_SIZE = 15   # 規格 14.5、取整
    BODY_SPACING2  = 6    # 行距（≈1.75）

    _body_linespace: Optional[int] = None   # class-level cache，見 _get_body_linespace

    @classmethod
    def _get_body_linespace(cls) -> int:
        """正文字型的原生行高（不含 spacing2），只算一次、跨 instance 共用。

        用來把「顯示行數」換算成精確像素高度（見 _relayout_body 的
        needed_px 公式）——這步是必要的，不能直接把行數丟給 tk.Text 的
        height= 選項，見下方 _relayout_body 開頭那段長註解。
        """
        if cls._body_linespace is None:
            f = tkfont.Font(family=FONT_FAMILY_TEXT, size=cls.BODY_FONT_SIZE)
            cls._body_linespace = f.metrics("linespace")
        return cls._body_linespace

    def _build_body(self, parent) -> None:
        """正文：14.5、tk.Text spacing2=6（行距≈1.75）、字典校正詞加底線。

        用 tk.Text 而非 CTkLabel 是因為 spacing2（行距）與 tag_configure
        （校正詞底線）CTkLabel 都做不到。寬度用 pack(fill="x") 讓 Tk 依實際
        渲染寬度自動 reflow，不用像舊版那樣手動算 wraplength 像素值。

        Text 外面多包一層 _body_wrap（固定像素高、pack_propagate(False)）——
        見 _relayout_body 開頭的長註解：tk.Text 自己的 height= 選項是「N ×
        字型原生行高」，不會把 spacing2 的行距加成算進去，直接拿去截斷會把
        最後一行文字整條吃掉又不觸發展開連結（用真實視窗截圖重現過的 bug）。
        改成自己算精確像素高度、灌到外層 wrapper，繞開這個 Tk 既有限制。
        """
        fg = TEXT_BODY if self._is_latest else TEXT_BODY_2
        bg = CARD_HI if self._is_latest else CARD

        self._body_wrap = ctk.CTkFrame(parent, fg_color="transparent")
        self._body_wrap.pack_propagate(False)
        self._body_wrap.pack(fill="x")

        text = tk.Text(
            self._body_wrap, wrap="word", font=ctk.CTkFont(FONT_FAMILY_TEXT, self.BODY_FONT_SIZE),
            spacing2=self.BODY_SPACING2, bg=bg, fg=fg, insertwidth=0, bd=0, highlightthickness=0,
            relief="flat", padx=0, pady=0, cursor="arrow", width=1, height=1,
            selectbackground=bg, selectforeground=fg,
        )
        text.pack(fill="both", expand=True)
        text.tag_configure("mark", foreground=MARK, underline=1)
        text.insert("1.0", self.get_current_text() or "")

        # 字典校正詞加底線：對 self._corrections 裡每個詞在最終文字中的所有
        # 出現位置加 "mark" tag（實線，tk.Text 沒有虛線可用）。
        for term in dict.fromkeys(t for t in self._corrections if t):
            start = "1.0"
            while True:
                pos = text.search(term, start, stopindex="end")
                if not pos:
                    break
                end_idx = f"{pos}+{len(term)}c"
                text.tag_add("mark", pos, end_idx)
                start = end_idx

        text.configure(state="disabled")   # 唯讀，避免使用者不小心改到
        self._body_text = text
        self._last_body_width = -1

        # 展開列先建好但不 pack；_relayout_body 依實際顯示行數決定要不要秀。
        self._expand_row = ctk.CTkFrame(parent, fg_color="transparent", height=24)
        self._expand_row.pack_propagate(False)
        self._expand_link = ctk.CTkLabel(
            self._expand_row, text="展開", cursor="hand2",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12, "bold"), text_color=CYAN_TEXT,
        )
        self._expand_link.pack(side="left")
        self._expand_link.bind("<Button-1>", lambda e: self._toggle_expand())

        # 用 <Configure> 而非 after_idle 排一次「猜」——CTkScrollableFrame 巢狀
        # 好幾層 CTkFrame 才到這顆 tk.Text，實測 Tk 在最終寬度 commit 前會連續
        # 送好幾個中間值（例：width=18 → displaylines=173 這種明顯錯的數字），
        # after_idle 只排一次很可能剛好抓到還沒穩定的那個中間值，量出來的行數
        # 會偏小。<Configure> 每次 Tk 真的改動這顆 widget 的幾何時都會 fire，
        # 包含最後穩定下來那一次，用「跟上次量到的寬度不同才重算」擋掉多餘重工。
        text.bind("<Configure>", self._on_body_configure, add="+")

    def _on_body_configure(self, event) -> None:
        if event.width == self._last_body_width:
            return
        self._last_body_width = event.width
        self._relayout_body()

    def _toggle_expand(self) -> None:
        self._expanded = not self._expanded
        self._relayout_body()

    def _relayout_body(self) -> None:
        """量測目前顯示行數，> 8 行才截斷 + 顯示展開列（未展開時鎖 8 行高）。

        不能直接用 `text.configure(height=k)`：tk.Text 的 height= 選項只用
        「N × 字型原生 linespace」估高度，完全不管 spacing2（行距）疊加的
        額外像素——實測 6 行文字（linespace=19、spacing2=6）height=6 只給
        116px，但 6 行真正要 6×19 + 5×6 = 144px，短少的 28px 剛好等於吃掉
        最後一行還多一點，而且無聲無息（沒有捲軸、沒有省略號）。改成自己
        用 linespace + spacing2 算出精確像素高度、直接設定 _body_wrap（見
        _build_body）的固定高度，繞開這個 Tk 限制。
        """
        text = self._body_text
        if text is None:
            return
        try:
            raw = text.count("1.0", "end", "displaylines")
            n = raw[0] if isinstance(raw, tuple) else (raw or 1)
        except Exception:
            n = 1
        n = max(1, n)

        linespace = self._get_body_linespace()

        def _px_for(k: int) -> int:
            # +2 安全邊界：字型 hinting／widget 內部留白造成的次像素誤差，
            # 寧可多留 2px 空白也不要再吃字（實測過差 1 行的慘況）。
            return k * linespace + max(0, k - 1) * self.BODY_SPACING2 + 2

        if n <= self.BODY_MAX_LINES:
            self._body_wrap.configure(height=_px_for(n))
            if self._expand_row is not None:
                self._expand_row.pack_forget()
            return

        if self._expanded:
            self._body_wrap.configure(height=_px_for(n))
            self._expand_link.configure(text="收合")
        else:
            self._body_wrap.configure(height=_px_for(self.BODY_MAX_LINES))
            self._expand_link.configure(text="展開")
        self._expand_row.pack(fill="x", pady=(8, 0))


# ─────────────────────────────────────────────────────────────────────────────
#  MiniRecordingWindow（Phase 4.3 浮動小型 HUD）
# ─────────────────────────────────────────────────────────────────────────────

class MiniRecordingWindow(tk.Toplevel):
    """錄音 / 處理中的浮動 mini HUD（Speakly 風格 always-on-top）。

    140×38 無邊框視窗，跟隨 AppWindow 狀態自動顯示／隱藏。內容：狀態圓點
    （依狀態色）+ 計時器（mm:ss）。點擊 → lift 主視窗到前景。

    Fix 8（2026-05-22）：透過 PyObjC 把對應 NSWindow 升到 NSStatusWindowLevel
    並加 collectionBehavior，達成「跨 Space / 全螢幕仍可見 / 不搶 focus」。
    PyObjC import 或 NSWindow 找不到時 fallback 成普通 Toplevel（仍能用，但
    only same Space 可見）。

    AppWindow 透過 `update(state, elapsed_s, rms)` 推進狀態；不該由 mini
    自己 polling，避免狀態真相分散。

    v2.25.0：cfg.record_visual != "chamber" 時加寬到 220×44，錄音中額外顯示
    13 根 bar 的 Aperture 極簡波形（取代「錄音中」文字，dot 顏色已經在傳達
    狀態）；cfg.record_visual == "chamber" 時完全退回舊版 140×38（逃生門）。
    """

    # v2.25.0 新預設（waveform 模式）；legacy（chamber）模式在 __init__ 內
    # 覆寫回舊值，見下方 self._legacy_visual 判斷
    WIN_W = 220
    WIN_H = 44
    # 距螢幕底邊（Speakly 風格：中下方）
    BOTTOM_MARGIN = 120

    # 用獨特 title 讓 PyObjC 找到對應 NSWindow（Tk 沒有直接拿 handle 的 API）
    # Fix 9 / 2026-05-22（P2-B）：title 加 id(self) 後綴避免 toggle 快速
    # destroy/recreate 時 NSApp.windows() 短暫含舊 instance 的同名 NSWindow，
    # 迴圈第一個取錯。class const 移除，改 instance attribute。
    _NS_TITLE_PREFIX = "WhisperProMiniHUD"

    def __init__(self, master) -> None:
        super().__init__(master)
        self._master = master
        self._closed = False
        self._ns_window = None   # 升級成功才設
        # Fix 9 P2-B：每 instance 獨一無二 title，destroy 中的舊 window 不會混淆
        self._ns_title = f"{self._NS_TITLE_PREFIX}-{id(self):x}"

        # v2.25.0 Aperture 波形：cfg.record_visual == "chamber" 時完全退回
        # 舊版 140×38（逃生門，新程式碼路徑一行都不執行）。WIN_W/WIN_H 覆寫
        # 成 instance attribute，下面既有的定位／geometry 邏輯原樣沿用。
        self._legacy_visual = (
            getattr(getattr(master, "cfg", None), "record_visual", "waveform")
            == "chamber"
        )
        if self._legacy_visual:
            self.WIN_W = 140
            self.WIN_H = 38

        # 無邊框
        self.overrideredirect(True)
        try:
            self.attributes("-alpha", 0.94)
        except Exception:
            pass
        self.configure(bg=SURF_2)

        # 給獨特 title 讓 PyObjC 比對找出對應 NSWindow
        # （overrideredirect=True 後仍會建立 NSWindow，title 不會顯示在 UI 上）
        try:
            self.title(self._ns_title)
        except Exception:
            pass

        # 初始位置（之後 show() 會重定位到游標所在螢幕中下方）
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - self.WIN_W) // 2
        y = sh - self.WIN_H - self.BOTTOM_MARGIN
        self.geometry(f"{self.WIN_W}x{self.WIN_H}+{x}+{y}")

        # B5（v2.7.0）：升級前先 withdraw + 強制處理 idle event，逼 Tk 把
        # overrideredirect/alpha/title/geometry 所有 deferred commit 跑完，
        # 否則 Tk-on-Aqua 某些版本會在 setLevel_ 後反向蓋掉 styleMask/level。
        try:
            self.withdraw()
            self.update_idletasks()
        except Exception:
            pass

        # 升級成 NSPanel-level（跨 Space / 全螢幕可見 / 不搶 focus）
        self._upgrade_to_panel_level()

        # 1px SURF_4 邊框
        outer = tk.Frame(self, bg=SURF_4, highlightthickness=0)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=SURF_2, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        # 狀態圓點 + 文字 + 計時器
        self._dot = tk.Label(
            inner, text="●",
            font=(FONT_FAMILY_TEXT, 14),
            fg=DANGER, bg=SURF_2,
        )
        self._dot.pack(side="left", padx=(12, 6))

        self._label = tk.Label(
            inner, text="錄音中",
            font=(FONT_FAMILY_TEXT, 12),
            fg=TEXT_1, bg=SURF_2,
        )
        self._label.pack(side="left")

        self._timer = tk.Label(
            inner, text="00:00",
            font=(FONT_FAMILY_MONO, 12),
            fg=TEXT_3, bg=SURF_2,
        )
        self._timer.pack(side="right", padx=SPACE_MD)

        # v2.25.0 Aperture 波形：13 bar 極簡波形（不畫峰值帽／削波線，見
        # _draw_spectral_bars 的 big=False 分支）。錄音中取代「錄音中」文字
        # 顯示（dot 顏色已經在傳達狀態，見 show_recording/show_processing）；
        # 逃生門模式（chamber）完全不建立。
        self._wave_engine: Optional[WaveformEngine] = None
        self._wave_canvas: Optional[tk.Canvas] = None
        self._wave_tick_active = False
        self._wave_last_tick = 0.0
        self._wave_state_start = 0.0
        if not self._legacy_visual:
            self._wave_engine = WaveformEngine(n_bars=WAVE_N_BARS_MINI)
            self._wave_canvas = tk.Canvas(
                inner, width=MINI_WAVE_W, height=MINI_WAVE_H,
                bg=SURF_2, highlightthickness=0, bd=0,
            )
            # 不在這裡 pack：show_recording() 時取代 _label 顯示（見下方）

        # 點擊任何處 → 把主視窗拉前
        _click_targets = [self, outer, inner, self._dot, self._label, self._timer]
        if self._wave_canvas is not None:
            _click_targets.append(self._wave_canvas)
        for w in _click_targets:
            w.bind("<Button-1>", self._on_click)

        # 預設隱藏；由 AppWindow 透過 .show() 顯示
        self.withdraw()

    # ── 跟游標所在螢幕（Speakly 風格）──────────────────────────────────────
    def _position_at_cursor_screen_bottom(self) -> None:
        """把 HUD 移到游標所在螢幕的中下方，距底部 BOTTOM_MARGIN px。

        多螢幕座標換算：NSScreen.frame 用左下原點、Tk geometry 用左上原點，
        且 origin 不是 (0,0)（副螢幕可能在主螢幕左 / 右 / 上）。
        Tk 的 y 是相對「主螢幕（screens[0]）」的左上座標。

        IS_MAC 專屬邏輯（游標所在螢幕追蹤）在 Windows 分支直接跳過，走
        `_position_primary_screen_bottom` 簡化版（永遠顯示在主螢幕中下方）。
        WINDOWS_PORT_PLAN.md：多螢幕跟隨游標列為 Phase 2（需要
        `ctypes.windll.user32.MonitorFromPoint`），本輪先簡化。
        """
        if not IS_MAC:
            self._position_primary_screen_bottom()
            return
        try:
            from AppKit import NSScreen, NSEvent  # type: ignore

            mouse_loc = NSEvent.mouseLocation()
            target_screen = None
            # B6（v2.7.0）：右/上邊用 < 開區間，避免游標剛好在兩螢幕共用邊
            # 上時被前一個螢幕搶走。游標真在邊界外（右/上）就會落到下一個
            # 螢幕的左/下開區間，最後 fallback 走 mainScreen。
            for screen in NSScreen.screens():
                f = screen.frame()
                if (f.origin.x <= mouse_loc.x < f.origin.x + f.size.width and
                        f.origin.y <= mouse_loc.y < f.origin.y + f.size.height):
                    target_screen = screen
                    break
            if target_screen is None:
                target_screen = NSScreen.mainScreen()

            sf = target_screen.frame()
            primary_h = NSScreen.screens()[0].frame().size.height

            # x：目標螢幕水平居中
            ns_x = sf.origin.x + (sf.size.width - self.WIN_W) / 2
            # y：NS 左下座標 → 目標螢幕底邊 + BOTTOM_MARGIN 為 HUD 下緣
            ns_y_bottom = sf.origin.y + self.BOTTOM_MARGIN
            # Tk y（左上原點）= primary_h - HUD 上緣 NS y
            #                = primary_h - (ns_y_bottom + WIN_H)
            tk_y = int(primary_h - (ns_y_bottom + self.WIN_H))
            tk_x = int(ns_x)

            self.geometry(f"{self.WIN_W}x{self.WIN_H}+{tk_x}+{tk_y}")
        except Exception as e:
            # 取游標螢幕失敗 → fallback：主螢幕中下方（用 Tk 原生 API）
            log_error(f"mini_hud_position_failed: {e}")
            self._position_primary_screen_bottom()

    def _position_primary_screen_bottom(self) -> None:
        """把 HUD 移到主螢幕（Tk `winfo_screenwidth/height`）中下方。

        跨平台 fallback：mac 的 AppKit 游標定位失敗時、以及 Windows 分支
        （尚未實作多螢幕游標追蹤）都走這條路徑。
        """
        try:
            self.update_idletasks()
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x = (sw - self.WIN_W) // 2
            y = sh - self.WIN_H - self.BOTTOM_MARGIN
            self.geometry(f"{self.WIN_W}x{self.WIN_H}+{x}+{y}")
        except Exception:
            pass

    # ── PyObjC NSPanel-level 升級 ──────────────────────────────────────────
    # NSStatusWindowLevel = 25（高於一般視窗、低於螢幕保護程式）
    _NS_STATUS_WINDOW_LEVEL = 25
    # CollectionBehavior bitmask
    _NS_COLLECTION_BEHAVIOR = (
        (1 << 0)   # CanJoinAllSpaces — 跨 Space
        | (1 << 4) # Stationary — 不跟 Mission Control 縮
        | (1 << 8) # FullScreenAuxiliary — 別人全螢幕時可見
    )

    def _apply_panel_level(self, ns_window) -> None:
        """對指定 NSWindow 套用 NSStatusWindowLevel + collectionBehavior。

        Helper：被 _upgrade_to_panel_level（初始升級）與 _reapply_panel_level
        （show_* 時的 re-apply）共用，確保兩條路徑保持一致。
        """
        ns_window.setLevel_(self._NS_STATUS_WINDOW_LEVEL)
        ns_window.setCollectionBehavior_(self._NS_COLLECTION_BEHAVIOR)
        try:
            ns_window.setHidesOnDeactivate_(False)  # 失焦時不要自己隱藏
        except Exception:
            pass

    def _upgrade_to_panel_level(self) -> None:
        """把對應 NSWindow 升到 NSStatusWindowLevel + collectionBehavior。

        失敗時靜默 fallback；HUD 仍能用，只是缺「跨 Space / 全螢幕可見」。
        Bug A（v2.12.0）：升級失敗時加 `-topmost` 兜底，至少同 Space 在最頂。

        IS_WINDOWS 分支：Windows 沒有 Space 概念，NSWindow level 升級這套
        機制完全不適用，直接用 Tk `-topmost` 讓 HUD 置頂即可（`_fallback_topmost`
        本來就是跨平台實作）。
        """
        if IS_WINDOWS:
            self._fallback_topmost()
            return
        try:
            from AppKit import NSApp  # type: ignore

            # 找到對應 NSWindow（用 instance-unique title 比對）
            ns_window = None
            for w in NSApp.windows():
                try:
                    if w.title() == self._ns_title:
                        ns_window = w
                        break
                except Exception:
                    continue

            if ns_window is None:
                log_error("mini_hud_nswindow_not_found")
                self._fallback_topmost()  # Bug A：找不到 NSWindow → topmost 兜底
                return

            self._apply_panel_level(ns_window)
            self._ns_window = ns_window
            log_state("mini_hud_panel_level_upgraded")
        except Exception as e:
            # PyObjC import 失敗或其他例外 → fallback 成普通 Toplevel + topmost
            log_error(f"mini_hud_panel_upgrade_failed: {e}")
            self._fallback_topmost()

    def _fallback_topmost(self) -> None:
        """Bug A（v2.12.0）：NSPanel-level 升級失敗時的兜底措施。

        無法跨 Space / 全螢幕可見，但至少在「同 Space」最頂端，使用者仍能
        看到 mini HUD（總比完全看不見好）。Tk `-topmost` 跨平台、無 PyObjC 依賴。
        """
        try:
            self.attributes("-topmost", True)
            log.info("MINI_HUD: panel-level 升級失敗，啟用 Tk -topmost fallback")
        except Exception:
            log_error("mini_hud_topmost_fallback_failed")

    def _reapply_panel_level(self) -> None:
        """B4（v2.7.0）：每次 show_* 前 re-apply NSWindow level。

        Tk-on-Aqua 某些版本 deiconify 後 NSWindow styleMask/level 會被靜默
        重設，跨 Space 能力悄悄丟失。re-apply 是 cheap（兩個 setter call），
        值得每次 show 都跑一次保險。`self._ns_window` 為 None（升級失敗）
        則直接 no-op，不嘗試重新搜尋 NSApp.windows()（避免 toggle 拿錯）。
        """
        if self._ns_window is None:
            return
        try:
            self._apply_panel_level(self._ns_window)
        except Exception as e:
            log_error(f"mini_hud_panel_level_reapply_failed: {e}")

    def show_recording(self) -> None:
        """進入錄音狀態 → 顯示紅點 + 「錄音中」+ 0:00 計時。

        每次顯示前重新定位到游標所在螢幕中下方（Speakly 風格）。
        B4（v2.7.0）：deiconify 後 re-apply panel level（cheap 保險）。
        Bug A（v2.12.0）：加診斷 log（geometry + 視窗 state）+ topmost 重新確認。
        """
        if self._closed:
            log.warning("MINI_HUD: show_recording on closed window, skip")
            return
        self._position_at_cursor_screen_bottom()
        self._dot.configure(fg=DANGER)
        self._label.configure(text="錄音中")
        self._timer.configure(text="00:00")
        # v2.25.0 Aperture 波形：canvas 取代文字 label，啟動 20 FPS render tick
        if self._wave_canvas is not None:
            self._label.pack_forget()
            self._wave_canvas.pack(side="left", padx=(4, 4))
            self._wave_engine.reset()
            self._wave_state_start = time.perf_counter()
            self._wave_last_tick = self._wave_state_start
            self._wave_tick_active = True
            self._wave_tick()
        self.deiconify()
        self._reapply_panel_level()
        # Bug A：若 NSPanel 升級失敗（_ns_window=None），重新跑 topmost 保證可見
        if self._ns_window is None:
            self._fallback_topmost()
        try:
            log.info(
                f"MINI_HUD: show_recording → geom={self.winfo_geometry()} "
                f"state={self.state()} ns_panel={'on' if self._ns_window else 'fallback'}"
            )
        except Exception:
            pass

    def show_processing(self) -> None:
        """進入處理中狀態 → 琥珀色 + 「轉錄中」。

        D3-S6（v2.10.0 / 2026-05-23）：原本「不重定位以避免跳動」的設計
        在多螢幕場景下會把 HUD 留在錄音時的螢幕上，user 若把焦點 / 游標
        移到副螢幕就看不到「轉錄中」狀態。改成 always 重定位到游標所在
        螢幕；單螢幕用戶感知不到（位置不變）。
        B4（v2.7.0）：deiconify 後 re-apply panel level。
        """
        if self._closed:
            return
        self._position_at_cursor_screen_bottom()   # D3-S6：multi-monitor 跟手
        self._dot.configure(fg=WARN)
        self._label.configure(text="轉錄中")
        # v2.25.0 Aperture 波形：處理中不畫波形（沒有錄音訊號），停 render
        # tick、canvas 收起來換回文字 label
        if self._wave_canvas is not None:
            self._wave_tick_active = False
            self._wave_canvas.pack_forget()
            self._wave_canvas.delete("all")
            self._label.pack(side="left")
        self.deiconify()
        self._reapply_panel_level()

    def _wave_tick(self) -> None:
        """v2.25.0：mini HUD 波形 render tick（50ms／20 FPS，跟主視窗一致）。

        只在錄音中 tick；show_processing() / hide() 會把 _wave_tick_active
        關掉，迴圈下一次執行時發現旗標為 False 就直接 return、不再重新排程
        （不留 dangling after() callback）。
        """
        if not self._wave_tick_active or self._closed:
            return
        try:
            now = time.perf_counter()
            rms = 0.0
            recorder = getattr(self._master, "recorder", None)
            if recorder is not None and recorder.is_recording():
                rms = recorder.get_rms_level()
            dt_ms = max(0.0, (now - self._wave_last_tick) * 1000.0)
            self._wave_last_tick = now
            self._wave_engine.update(rms, dt_ms)

            self._wave_canvas.delete("all")
            elapsed_ms = (now - self._wave_state_start) * 1000.0
            _draw_spectral_bars(
                self._wave_canvas, self._wave_engine,
                width=MINI_WAVE_W, height=MINI_WAVE_H,
                elapsed_ms=elapsed_ms, big=False,
            )
        except tk.TclError:
            return

        self.after(RENDER_TICK_MS, self._wave_tick)

    def update_timer(self, seconds: float) -> None:
        """錄音中每秒呼叫一次更新計時器。"""
        if self._closed:
            return
        s = int(seconds)
        self._timer.configure(text=f"{s // 60:02d}:{s % 60:02d}")

    def hide(self) -> None:
        if self._closed:
            return
        self._wave_tick_active = False
        self.withdraw()

    def _on_click(self, _event) -> None:
        """點擊 → 把主視窗拉到前景（不關閉 mini，等 AppWindow 自己 hide）。"""
        try:
            self._master.deiconify()
            self._master.lift()
            self._master.focus_force()
        except Exception:
            log_error("mini_window_lift_main_failed")

    def close(self) -> None:
        """主視窗關閉時呼叫，徹底銷毀避 X server 殘留 widget。"""
        self._closed = True
        try:
            self.destroy()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def cfg_val(v):
    """原樣回傳值（輔助函式，讓 topbar 初始化迴圈可讀性更高）。"""
    return v
