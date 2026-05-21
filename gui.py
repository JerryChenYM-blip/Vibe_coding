"""
主應用程式視窗——Apple MacBook Pro 美學設計。

設計語言：  apple.com/tw/macbook-pro/（深色、簡潔、金屬感）
色彩系統：  強制深色，虛空黑底色 + 四層表面深度
字型：      SF Pro Display / Text（macOS 原生字型）
自動貼上：  錄音開始前記錄前景 App，轉錄完成後模擬 ⌘V 注入文字

視窗佈局（760 × 800 px，由上到下）：
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
from typing import Optional

import customtkinter as ctk

from config import MODEL_INFO, LANGUAGE_OPTIONS, Config
from logger import get_logger, log_action, log_error, log_settings, log_state
from hotkey_manager import (
    HotkeyManager, capture_hotkey, format_hotkey,
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
import auto_paste as _ap

log = get_logger("gui")

# ── 強制套用 Apple 深色美學 ───────────────────────────────────────────────────
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

WIN_W, WIN_H = 760, 880   # 視窗預設寬度 × 高度（像素）
# WIN_H 推導：實測 AppWindow.winfo_reqheight() ≈ 858（TopBar 60 + RecordCard 398 +
# ResultCard 281 + ActionBar 58 + StatusBar 32 + 分隔線×3 + 內邊距），舊值 800
# 會把 ActionBar（5 顆動作按鈕）與 StatusBar 擠出視窗下緣。預留 22px 緩衝。

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
    # Typography + spacing
    FONT_FAMILY_TEXT, FONT_FAMILY_MONO,
    SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG,
    # Motion
    BREATHE_IDLE_MS, BREATHE_RECORDING_MS, BREATHE_PROCESSING_MS,
    ROTATE_PROCESSING_MS, RENDER_TICK_MS,
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
#  Reduced-motion detection (macOS system preference)
# ─────────────────────────────────────────────────────────────────────────────

def system_reduce_motion() -> bool:
    """讀取 macOS「減少動態效果」系統偏好設定（啟動時讀取一次）。

    依 macOS 慣例，偏好設定變更需要重新啟動 App 才生效。
    任何錯誤（鍵不存在、超時、非 Darwin 平台）都回傳 False（不限制動畫）。
    """
    try:
        r = subprocess.run(
            ["defaults", "read", "com.apple.universalaccess", "reduceMotion"],
            capture_output=True, text=True, timeout=0.5,
        )
        return r.stdout.strip() == "1"
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  AppWindow
# ─────────────────────────────────────────────────────────────────────────────

class AppWindow(ctk.CTkFrame):
    """應用程式根框架——Apple MacBook Pro 美學風格。

    包含完整的 UI 狀態機（idle → recording → processing → idle）、
    錄音管線（recorder → transcriber → ollama_client）、
    快捷鍵管理（pynput）、自動貼上（auto_paste）等所有核心邏輯。
    """

    # 穩定性（Fix 1 / 2026-05-21）：pynput Listener 心跳檢查週期。
    # macOS CGEventTap 在 focus 切換、TCC state change、sleep/wake 後偶爾會失效；
    # pynput 內部不會自動恢復，listener thread 安靜結束、零 log 線索。
    # 5 秒是權衡：> 10s 使用者感知太慢，< 1s 過密無意義（running 檢查 ~µs）。
    HOTKEY_WATCHDOG_INTERVAL_MS = 5000

    # 穩定性（Fix 4 / 2026-05-21）：processing 狀態超時自癒上限。
    # MLX 對 large-v3-turbo：RTF ≈ 0.1，60s 音訊 → 6s 推論；cold start +5-10s；
    # 60s 有 ~40s 緩衝，避免誤殺正常推論。若使用者誤殺再放大或設計可設定。
    PROCESSING_TIMEOUT_MS = 60_000

    def __init__(self, master: ctk.CTk, cfg: Config) -> None:
        super().__init__(master, fg_color=BG, corner_radius=0)
        self.pack(fill="both", expand=True)

        self.cfg = cfg
        self.recorder    = AudioRecorder()
        self.transcriber = Transcriber()
        self.ollama      = OllamaClient()
        # 用設定檔同步 Ollama 參數（base_url / model / enabled / timeout）
        self.ollama.apply_app_config(cfg)
        self.hotkey_mgr  = HotkeyManager(
            on_tap_cb=self._hotkey_tap,
        )

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
        self._stream_chunks:  list[str] = []
        self._stream_tick_id            = None

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

        self._build_ui()
        self._start_hotkey_listener()
        # 診斷（Task B）：mainloop 第一個 idle tick 抵達時間戳，
        # 配合 hotkey_manager.py 的 listener_fully_started / first_press_received
        # 可以判斷「首次按鍵丟失」是否落在 listener 啟動 vs. tk 進 mainloop 的 gap
        self.after(0, lambda: log.info(
            f"GUI: first idle tick reached at t={time.monotonic():.3f}"
        ))
        # 穩定性（Fix 1 / 2026-05-21）：每 5 秒檢查 pynput Listener 是否還活著，
        # 死掉就自動 restart（macOS CGEventTap 偶爾會被系統內部停掉，零 log）
        self.after(self.HOTKEY_WATCHDOG_INTERVAL_MS, self._hotkey_watchdog)
        self.after(1500, self._warmup_model)
        # 開啟著的話再去探 Ollama；即使 Ollama 離線也不會卡 UI 建構。
        self.after(2000, self._refresh_ollama_health)

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
        """依序建立五大 UI 區塊（由上到下）。"""
        self._build_topbar()
        self._build_record_card()
        self._build_result_card()
        self._build_action_bar()
        self._build_status_bar()

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
        left.pack(side="left", padx=24, fill="y")

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

        # Ambient chamber — single canvas replacing waveform + outer ring + button
        self._chamber = tk.Canvas(
            card,
            width=CHAMBER_SIZE, height=CHAMBER_SIZE,
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
        self._reduce_motion    = system_reduce_motion()

        # 啟動渲染迴圈（在視窗存活期間持續執行，每 50ms 更新一次 Canvas）
        self._render_tick()

    # ── Result card ──────────────────────────────────────────────────────────

    def _build_result_card(self) -> None:
        """建立轉錄結果卡片：標題列（含原文/潤飾切換）、文字區、清除按鈕。"""
        card = ctk.CTkFrame(
            self, corner_radius=16,
            fg_color=SURF_1,
        )
        card.pack(fill="both", expand=True, padx=16, pady=(0, 6))

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

        # Text area
        self._textbox = ctk.CTkTextbox(
            card,
            font=ctk.CTkFont("SF Pro Text", 14),
            wrap="word",
            corner_radius=0,
            border_width=0,
            fg_color="transparent",
            text_color=TEXT_1,
            state="disabled",
        )
        self._textbox.pack(fill="both", expand=True, padx=8, pady=(4, 10))
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
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            row, text="存檔", width=96,
            image=get_icon("download", icon_size, TEXT_2),
            compound="left",
            command=self._on_save, **ghost,
        ).pack(side="left", padx=4)

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
        self._ap_btn.pack(side="left", padx=4)

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
        self._ollama_btn.pack(side="left", padx=4)
        # 依目前 cfg 立即套一次外觀（啟動時僅是視覺狀態）
        self._apply_polish_button_style(enabled=self.cfg.ollama_enabled, healthy=False)

        ctk.CTkButton(
            row, text="歷史", width=96,
            image=get_icon("history", icon_size, TEXT_2),
            compound="left",
            command=self._open_history, **ghost,
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            row, text="設定", width=96,
            image=get_icon("settings", icon_size, TEXT_2),
            compound="left",
            command=self._open_settings, **ghost,
        ).pack(side="left", padx=4)

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
        self._hotkey_status.pack(side="right", padx=16)

    # ═══════════════════════════════════════════════════════════════════════
    #  STATE MACHINE
    # ═══════════════════════════════════════════════════════════════════════

    def _transition_to_recording(self) -> None:
        """狀態機：idle → recording。啟動麥克風錄音並更新全部 UI。"""
        if self._state != "idle":
            log.debug(f"_transition_to_recording ignored (state={self._state})")
            return
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

        # Capture frontmost app —— 背景執行緒執行，避免 2 秒 osascript timeout
        # 卡住 recorder.start()。此資訊同時被 preset 路由（A1）與 auto-paste 用；
        # 所以**永遠抓**，不再綁 auto_paste 設定。
        self._paste_target  = None
        self._frontmost_app = None
        self._target_label.configure(text="")

        def _capture_frontmost():
            app = _ap.get_frontmost_app()
            if not app or app in ("Python", "python3"):
                return
            def _apply():
                if self._state != "recording":
                    return
                self._frontmost_app = app
                # 只有 auto_paste 開啟時才作為 ⌘V 目標 + 顯示提示
                if self.cfg.auto_paste:
                    self._paste_target = app
                    self._target_label.configure(text=f"→ {app}")
            self.after(0, _apply)
        threading.Thread(target=_capture_frontmost, daemon=True).start()

        self.recorder.start()

        # UI：Chamber 渲染迴圈會在下次 tick 時自動依新狀態重繪，不需手動觸發
        self._timer_label.configure(text="00:00")
        self._hotkey_hint.configure(
            text=f"再按 {self.cfg.format_hotkey_display()} 停止錄音"
        )
        self._model_menu.configure(state="disabled")
        self._lang_menu.configure(state="disabled")
        self._status_dot.configure(text_color=DANGER)
        self._status_label.configure(text="  錄音中")

        self._update_timer()
        # 中段串流轉錄目前暫停使用：實測 small 模型的中段結果品質拖累
        # 最終主模型輸出，等 Ollama 整合穩定後再評估是否重啟。
        # _stream_tick 方法已保留，未來啟用時直接排程即可。
        self._stream_tick_id = None

        # Phase 4.3 mini 視窗：開啟錄音 HUD
        if self._mini_window is not None:
            self._mini_window.show_recording()

    def _transition_to_processing(self) -> None:
        """狀態機：recording → processing。停止麥克風並在背景執行緒跑 Whisper。"""
        if self._state != "recording":
            log.debug(f"_transition_to_processing ignored (state={self._state})")
            return
        duration = time.perf_counter() - self._rec_start
        log_state("recording->processing", duration_s=f"{duration:.2f}")
        self._state            = "processing"
        self._state_start_time = time.perf_counter()
        # 穩定性（Fix 4 / 2026-05-21）：記錄 processing 進入時間並排程 60s 自癒檢查
        self._processing_started_at = time.monotonic()
        self.after(self.PROCESSING_TIMEOUT_MS, self._processing_timeout_check)

        if self._stream_tick_id is not None:
            self.after_cancel(self._stream_tick_id)
            self._stream_tick_id = None

        full_audio = self.recorder.stop()

        # UI：Chamber 渲染迴圈下一 tick 會自動切換到 WARN 配色 + 粒子旋轉環
        self._timer_label.configure(text="")
        self._hotkey_hint.configure(text="轉錄中…")
        self._target_label.configure(text="")
        self._status_dot.configure(text_color=WARN)
        self._status_label.configure(text="  轉錄中，請稍候…")

        tail  = full_audio[self._stream_samples:]
        model = self._model_var.get()
        lang  = self.cfg.get_whisper_language()
        audio = tail if len(tail) > 800 else full_audio

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

        # Phase 4.3 mini 視窗：閒置時隱藏（不 destroy，下次錄音再 show）
        if self._mini_window is not None:
            self._mini_window.hide()

        self._timer_label.configure(text="")
        self._hotkey_hint.configure(
            text=f"按下 {self.cfg.format_hotkey_display()} 即時錄音"
        )
        self._model_menu.configure(state="normal")
        self._lang_menu.configure(state="normal")
        self._target_label.configure(text="")

        model = self._model_var.get()
        self._status_dot.configure(text_color=SUCCESS)
        self._status_label.configure(text=f"  就緒 ({model})")

        if result is not None:
            self._display_result(result)

    # ═══════════════════════════════════════════════════════════════════════
    #  STREAMING
    # ═══════════════════════════════════════════════════════════════════════

    _STREAM_CHUNK_SAMPLES = 5 * 16_000

    def _stream_tick(self) -> None:
        """中段串流轉錄（目前暫停使用）：每秒取最新 chunk 送 transcribe_fast。

        保留此方法以備 Phase 3 啟用；主迴圈不再排程此函式。
        """
        self._stream_tick_id = None
        if self._state != "recording":
            return
        snap = self.recorder.get_buffer_snapshot()
        new  = len(snap) - self._stream_samples
        if new < 4 * 16_000:
            self._stream_tick_id = self.after(1000, self._stream_tick)
            return
        chunk = snap[self._stream_samples:]
        self._stream_samples = len(snap)
        lang = self.cfg.get_whisper_language()

        def _process(audio=chunk):
            r = self.transcriber.transcribe_fast(audio, language=lang)
            if r.text and r.text not in (
                "（未偵測到語音內容）", "（沒有偵測到音訊，請確認麥克風是否正常運作）"
            ):
                self._stream_chunks.append(r.text)

        threading.Thread(target=_process, daemon=True).start()
        if self._state == "recording":
            self._stream_tick_id = self.after(1000, self._stream_tick)

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
            # 一律用使用者選的模型做最終轉錄，不再因短音檔退化到 transcribe_fast
            # （那條路徑寫死 small，會嚴重拖垮品質）。
            result = self.transcriber.transcribe(audio, model_size=model, language=lang)

            prior = list(self._stream_chunks)
            if prior:
                tail = result.text if result.text != "（未偵測到語音內容）" else ""
                combined = "".join(prior) + tail
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
        """
        if self._state == "processing":
            empty = TranscriptionResult(
                text="（轉錄失敗，請查看 log）",
                language="",
                duration_seconds=0.0,
                elapsed_seconds=0.0,
                segments=[],
            )
            self._transition_to_idle(empty)
        self._show_toast("⚠ 轉錄失敗")

    def _processing_timeout_check(self) -> None:
        """processing 狀態超過 60s 沒結束 → 強制切回 idle（Fix 4 / 2026-05-21）。

        對應 _transition_to_processing 排程的 after。若狀態早已正常切回 idle 則
        no-op；若超時則 log_warning + log_action + 強制 idle + toast。
        """
        if self._state != "processing":
            return   # 已正常結束，無事
        elapsed = time.monotonic() - getattr(self, "_processing_started_at", 0)
        if elapsed >= self.PROCESSING_TIMEOUT_MS / 1000:
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
            self._show_toast("⏱ 轉錄超時 — 已自動恢復")

    def _on_transcription_done(self, result: TranscriptionResult) -> None:
        """主執行緒：轉錄完成後決定是否走潤飾流程，並觸發剪貼簿 / 自動貼上。"""
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

        # 決定路徑：能潤飾就走潤飾流程，失敗自動降級回原文。
        # 規劃書 6.4「策略 B」：等潤飾完再貼，因此 auto-paste 也延到潤飾後。
        take_polish_path = (
            valid
            and self.cfg.ollama_enabled
            and self.ollama.health_ok is True
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

    # ── 潤飾管線 ────────────────────────────────────────────────────────────

    def _start_polish(self, gen: int, raw_text: str, target: Optional[str]) -> None:
        """啟動背景潤飾；完成時將於主執行緒回呼 _finish_polish。"""
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

        # C2：三段式標題狀態（preset 名稱不會被後續 status 吃掉）
        self._title_preset = preset.display_name if preset_name != "default" else None
        self._title_status = "潤飾中…"
        self._rebuild_result_title()

        dict_terms = self._dictionary_terms if self.cfg.dictionary_enabled else None

        def _run():
            resp = self.ollama.process(
                llm_input,
                prompt_template=preset.resolve_prompt(),
                dictionary_terms=dict_terms,
                preset_name=preset_name,
            )
            # 把 raw_text（= Whisper 原文）交給 _finish_polish 做 expect_current
            # 比對，textbox 從頭到尾維持 Whisper 原文直到替換為潤飾版
            self.after(0, self._finish_polish, gen, raw_text, target, resp)

        threading.Thread(target=_run, daemon=True).start()

    def _finish_polish(
        self,
        gen: int,
        raw_text: str,
        target: Optional[str],
        resp,
    ) -> None:
        """主執行緒：套用潤飾結果（或降級回原文）+ 觸發自動貼上。"""
        # 若期間又開始新轉錄，這次結果直接丟棄，避免蓋到新內容
        if gen != self._polish_generation:
            self._polish_busy = False
            return

        self._polish_busy = False

        if resp.error:
            # 降級：提示錯誤，title 狀態轉為「原文（潤飾失敗）」，auto-paste 貼原文
            # 注意：preset 名稱（若有）刻意保留，讓使用者知道是哪條 preset 炸
            self._title_status = "原文（潤飾失敗）"
            self._rebuild_result_title()
            self._show_toast(f"AI 潤飾失敗：{resp.error}")
            paste_text = raw_text
        else:
            # 成功：textbox 的最新一段用潤飾版替換（textbox 此時是 Whisper 原文）
            polished = resp.text
            self._last_polished = polished
            self._replace_latest_with(polished, expect_current=raw_text)
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
        """切換 textbox 最新一段的顯示內容（原文 / 潤飾）。

        若最新段的內容與 _last_raw / _last_polished 不符（使用者手動編輯過），
        沉默放棄切換，避免踩壞編輯。
        """
        if self._last_polished is None:
            return  # 沒有潤飾版可切
        if show_polished == self._showing_polished:
            return  # 已經是這個狀態

        target_text = self._last_polished if show_polished else self._last_raw
        current_expected = self._last_raw if show_polished else self._last_polished
        self._replace_latest_with(target_text, expect_current=current_expected)

        self._showing_polished = show_polished
        self._apply_toggle_style()

    def _replace_latest_with(self, new_text: str, expect_current: str) -> None:
        """以 new_text 替換 textbox 最新一段的內容。

        若 `expect_current` 與目前 mark 範圍內容不符，視為使用者已手動編輯或
        又有新轉錄插入，放棄替換避免踩壞使用者操作。
        """
        try:
            start = self._textbox.index("latest_start")
            end   = self._textbox.index("latest_end")
        except tk.TclError:
            return
        self._textbox.configure(state="normal")
        current = self._textbox.get(start, end)
        if current.strip() != expect_current.strip():
            self._textbox.configure(state="disabled")
            return
        self._textbox.delete(start, end)
        # 插入後 marks 會因 gravity 自動更新
        self._textbox.insert(start, new_text)
        self._textbox.see("latest_end")
        self._textbox.configure(state="disabled")

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
        success = _ap.paste_to_app(text, target)
        if success:
            self._show_toast(f"⌨  已貼入 {target}")
        else:
            self._show_toast("⌨  自動貼上失敗（請確認輔助使用權限）")

    def _display_result(self, result: TranscriptionResult) -> None:
        """將轉錄結果插入 textbox，並以 tk mark 圈住最新一段供後續精準替換。"""
        dur   = int(result.duration_seconds)
        lang  = result.language.upper() if result.language else "?"
        model = self._model_var.get()
        # C2：重設三段式標題狀態
        self._title_base   = f"轉錄結果  ({dur}s · {lang} · {model})"
        self._title_preset = None
        self._title_status = None
        self._rebuild_result_title()
        self._textbox.configure(state="normal")
        existing = self._textbox.get("1.0", "end").strip()
        if existing and existing != "（等待第一次錄音...）":
            self._textbox.insert("end", "\n\n─────────────\n\n")
        if existing == "（等待第一次錄音...）":
            self._textbox.delete("1.0", "end")

        # 以 tk mark 圈住「最新一段」的起訖，之後潤飾完成時精準替換；
        # left/right gravity 讓 mark 在插入／刪除時自動漂移到合理位置。
        self._textbox.mark_set("latest_start", "end-1c")
        self._textbox.mark_gravity("latest_start", "left")
        self._textbox.insert("end", result.text)
        self._textbox.mark_set("latest_end", "end-1c")
        self._textbox.mark_gravity("latest_end", "right")

        self._textbox.see("end")
        self._textbox.configure(state="disabled")

    # ═══════════════════════════════════════════════════════════════════════
    #  AMBIENT CHAMBER — render loop + draw + events
    # ═══════════════════════════════════════════════════════════════════════

    def _render_tick(self) -> None:
        """主渲染迴圈——每 50ms 執行一次，負責 Chamber Canvas 的全部繪製。"""
        try:
            self._draw_chamber()
        except tk.TclError:
            # App 關閉時 Canvas 已被銷毀，靜默結束迴圈
            return
        self.after(RENDER_TICK_MS, self._render_tick)

    def _draw_chamber(self) -> None:
        """Render ambient rings + central disc + icon for the current state."""
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
        """以畢氏定理判斷座標是否落在中央圓盤內（半徑 DISC_RADIUS）。"""
        dx = x - CHAMBER_CENTER
        dy = y - CHAMBER_CENTER
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
        """pynput 執行緒：tap 觸發（完整按下→放開算 1 次）→ marshal 到主執行緒。

        Tap toggle 語意：每次 tap 等同於點一下螢幕上的錄音鈕，
        idle → 開始錄音；recording → 停止；processing → 忽略。
        實際分支邏輯沿用 `_on_record_btn`，不在這裡重複實作。
        """
        self.after(0, self._on_hotkey_tap)

    def _on_hotkey_tap(self) -> None:
        """主執行緒：快捷鍵 tap → 直接重用 chamber 按鈕的 toggle handler。"""
        log_action("hotkey_triggered_toggle", combo=self.cfg.hotkey, state=self._state)
        self._on_record_btn()

    def _try_stop(self) -> None:
        """延遲後真的停錄音。只在仍為 recording 狀態才執行（防重入）。"""
        if self._state == "recording":
            self._transition_to_processing()

    def _on_copy(self) -> None:
        """複製按鈕：將目前 textbox 內容複製到剪貼簿。"""
        text = self._get_result_text()
        if not text:
            log_action("copy_clicked_empty")
            return
        try:
            import pyperclip
            pyperclip.copy(text)
            log_action("copy_succeeded", text_len=len(text))
            self._show_toast("已複製到剪貼簿")
        except Exception as e:
            log_error("copy_failed", text_len=len(text))
            self._show_toast(f"複製失敗: {e}")

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
        """清除按鈕：清空 textbox，重置三段式標題與 toggle 狀態，顯示佔位符。"""
        log_action("clear_clicked")
        self._textbox.configure(state="normal")
        self._textbox.delete("1.0", "end")
        self._textbox.configure(state="disabled")
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

    def _toggle_auto_paste(self) -> None:
        """自動貼上按鈕：切換 auto_paste 開關並即時更新按鈕樣式。"""
        old = self.cfg.auto_paste
        self.cfg.auto_paste = not self.cfg.auto_paste
        self.cfg.save()
        log_settings("changed", field="auto_paste", old=old, new=self.cfg.auto_paste)
        on = self.cfg.auto_paste
        self._ap_btn.configure(
            fg_color=INDIGO if on else SURF_1,
            border_color=INDIGO if on else SURF_3,
            text_color=TEXT_1 if on else TEXT_3,
            hover_color=INDIGO_HV if on else SURF_2,
            image=get_icon("keyboard", 15, TEXT_1 if on else TEXT_3),
        )
        self._show_toast("自動貼上已開啟" if on else "自動貼上已關閉")

    def _on_ollama(self) -> None:
        """手動按「潤飾」鈕：對目前 textbox 內容執行一次潤飾。"""
        if not self.cfg.ollama_enabled:
            log_action("ollama_clicked_disabled")
            self._show_toast("AI 潤飾未啟用，請到「設定」開啟")
            return
        if self.ollama.health_ok is False:
            log_action("ollama_clicked_offline")
            self._show_toast("無法連線 Ollama 服務，請確認 ollama serve 已啟動")
            # 重新探一次，下次點擊就能反映最新狀態
            self._refresh_ollama_health()
            return
        if self._polish_busy:
            log_action("ollama_clicked_busy")
            self._show_toast("目前已在潤飾中，請稍候…")
            return

        text = self._get_result_text()
        if not text:
            log_action("ollama_clicked_empty")
            return
        log_action("ollama_clicked", text_len=len(text))

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
            if result.error:
                self._show_toast(f"AI 潤飾失敗：{result.error}")
                return
            # 整段覆蓋（手動潤飾情境下，使用者就是要整坨結果被替換）
            self._textbox.configure(state="normal")
            self._textbox.delete("1.0", "end")
            self._textbox.insert("end", result.text)
            self._textbox.configure(state="disabled")
            self._show_toast(f"AI 潤飾完成 · {result.elapsed_seconds:.1f}s")

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
        """把潤飾按鈕依「啟用 × 連線」四種組合畫出正確樣式。"""
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
        """頂部模型選單變更時：更新 cfg 並刷新狀態列文字。"""
        old = self.cfg.model
        self.cfg.model = value
        self.cfg.save()
        log_settings("changed", field="model", old=old, new=value, source="dropdown")
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
        # textbox 換成舊的原文（讓 _finish_polish 的 expect_current 比對過）
        self._textbox.configure(state="normal")
        self._textbox.delete("1.0", "end")
        self._textbox.insert("1.0", entry.raw_text)
        self._textbox.configure(state="disabled")
        self._apply_toggle_style()
        self._frontmost_app = entry.target_app   # 讓 preset 路由走原本的 app

        log_action("history_repolish", id=entry.id)
        self._start_polish(gen, entry.raw_text, target=None)

    # ── Phase 4.3 mini 錄音窗 ──────────────────────────────────────────────

    def _ensure_mini_window(self) -> None:
        """Lazy 建立 MiniRecordingWindow；toggle on 時呼叫。"""
        if self._mini_window is not None:
            return
        try:
            self._mini_window = MiniRecordingWindow(self)
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
        # 逐欄位紀錄變動
        for field_name in ("model", "language", "hotkey", "auto_copy",
                           "auto_paste", "append_results", "ollama_enabled"):
            ov, nv = getattr(self.cfg, field_name), getattr(cfg, field_name)
            if ov != nv:
                log_settings("changed", field=field_name, old=ov, new=nv,
                             source="settings_window")
        self.cfg = cfg
        # 只有 hotkey 真正變更才重啟 pynput listener——反覆 stop/start 在 macOS
        # 上曾與 MLX/Metal 並存時觸發 native 層不穩定，能避免就避免。
        # PR #8 修正：延遲 100ms 再重啟，讓 SettingsWindow.destroy() 與舊 Listener
        # 的 CFRunLoop teardown 完成，避免 macOS Cocoa native race 造成閃退。
        if cfg.hotkey != old_hotkey:
            self.after(100, self._start_hotkey_listener)
        self._hotkey_hint.configure(text=f"按下 {cfg.format_hotkey_display()} 即時錄音")
        self._hotkey_status.configure(text=cfg.format_hotkey_display())
        self._model_var.set(cfg.model)
        self._lang_var.set(cfg.language)
        on = cfg.auto_paste
        self._ap_btn.configure(
            fg_color=INDIGO if on else SURF_1,
            border_color=INDIGO if on else SURF_3,
            text_color=TEXT_1 if on else TEXT_3,
            hover_color=INDIGO_HV if on else SURF_2,
            image=get_icon("keyboard", 15, TEXT_1 if on else TEXT_3),
        )
        # Ollama 設定同步：把新 cfg 推給 client，然後重新非同步探測一次
        self.ollama.apply_app_config(cfg)
        self._refresh_ollama_health()

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

    # ═══════════════════════════════════════════════════════════════════════
    #  HELPERS
    # ═══════════════════════════════════════════════════════════════════════

    def _start_hotkey_listener(self) -> None:
        """（重）啟動 pynput 全域快捷鍵監聽器；pynput 不可用時靜默略過。"""
        if not is_pynput_available():
            return
        self.hotkey_mgr.restart(self.cfg.hotkey)

    def _hotkey_watchdog(self) -> None:
        """週期性檢查 pynput Listener 還活著嗎；死了就 restart。

        Fix 1 / 2026-05-21：macOS CGEventTap 在 focus 切換、TCC state change、
        sleep/wake 後偶爾會失效。pynput 內部不會自動恢復、listener thread
        安靜結束，沒有任何 log（這是最坑的地方）。watchdog 是補救網。
        """
        try:
            mgr = self.hotkey_mgr
            if is_pynput_available() and mgr._listener is not None:
                if not mgr._listener.running:
                    log.warning("HOTKEY: listener died unexpectedly — restarting")
                    log_action("hotkey_listener_auto_restarted")
                    mgr.restart(self.cfg.hotkey)
        except Exception:
            log_error("hotkey_watchdog_failed")
        finally:
            self.after(self.HOTKEY_WATCHDOG_INTERVAL_MS, self._hotkey_watchdog)

    def _warmup_model(self) -> None:
        """背景預熱 Whisper 模型（延遲 1.5s 後執行，避免阻礙 UI 初始化）。"""
        model = self._model_var.get()
        log.info(f"WARMUP: starting for model={model}")

        def _load():
            try:
                self.transcriber.warmup(model)
                backend = self.transcriber.active_backend()
                label   = "⚡ Metal" if backend == "mlx" else "CPU"
                log.info(f"WARMUP: complete (model={model} backend={backend})")
                self.after(0, lambda: self._status_label.configure(
                    text=f"  就緒 ({model} · {label})"
                ))
                self.after(0, lambda: self._status_dot.configure(text_color=SUCCESS))
            except Exception:
                log_error("warmup_failed", model=model)
                self.after(0, lambda: self._status_label.configure(text="  模型載入失敗"))
                self.after(0, lambda: self._status_dot.configure(text_color=DANGER))

        threading.Thread(target=_load, daemon=True).start()

    def _show_placeholder(self) -> None:
        """若 textbox 為空，插入佔位符文字。"""
        self._textbox.configure(state="normal")
        if not self._textbox.get("1.0", "end").strip():
            self._textbox.insert("1.0", "（等待第一次錄音...）")
        self._textbox.configure(state="disabled")

    def _get_result_text(self) -> str:
        """取得 textbox 的有效文字；佔位符或空白回傳空字串。"""
        t = self._textbox.get("1.0", "end").strip()
        return "" if t == "（等待第一次錄音...）" else t

    def _show_toast(self, message: str) -> None:
        """在視窗右下角顯示一個浮動 toast，2.8 秒後自動消失。"""
        toast = ctk.CTkFrame(
            self, corner_radius=10,
            fg_color=SURF_2,
            border_width=1, border_color=SURF_3,
        )
        ctk.CTkLabel(
            toast, text=message,
            font=ctk.CTkFont("SF Pro Text", 13),
            text_color=TEXT_1, padx=18, pady=10,
        ).pack()
        toast.place(relx=1.0, rely=1.0, x=-20, y=-52, anchor="se")
        self.after(2800, toast.destroy)

    def on_close(self) -> None:
        """視窗關閉時：停止 pynput 監聽器與進行中的錄音，避免資源洩漏。"""
        log.info("GUI: on_close")
        try:
            self.hotkey_mgr.stop()
        except Exception:
            log_error("hotkey_mgr_stop_on_close_failed")
        if self.recorder.is_recording():
            log.info("GUI: recorder still active on close — stopping")
            try:
                self.recorder.stop()
            except Exception:
                log_error("recorder_stop_on_close_failed")
        # Phase 4.3 mini 視窗也要 destroy，避免主視窗關了 HUD 還浮著
        self._destroy_mini_window()


# ─────────────────────────────────────────────────────────────────────────────
#  SETTINGS WINDOW
# ─────────────────────────────────────────────────────────────────────────────

class SettingsWindow(ctk.CTkToplevel):
    """設定視窗（modal）。

    涵蓋語音辨識（模型 / 語言）、快捷鍵重新綁定、輸出偏好
    （追加 / 自動複製 / 自動貼上）、AI 潤飾（Ollama）、
    情境路由（Phase 2 preset）、個人字典（#4）等所有設定項目。

    使用者按「儲存」時呼叫 on_save_cb(new_cfg)；按「取消」不修改原 cfg。
    """

    def __init__(self, parent, cfg: Config, on_save_cb) -> None:
        """初始化設定視窗，深拷貝 cfg 以避免使用者取消時汙染原始設定。"""
        super().__init__(parent)
        self._parent     = parent                 # AppWindow（訪問 _prompt_reloader 用）
        self.cfg         = Config(**cfg.__dict__)  # 深拷貝：取消時不修改原 cfg
        self._on_save_cb = on_save_cb
        self.title("設定")
        self.geometry("440x580")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.grab_set()
        self._build()

    def _build(self) -> None:
        """建立所有設定 section 的 UI 元件與底部儲存 / 取消按鈕。"""
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        def section(title: str) -> ctk.CTkFrame:
            ctk.CTkLabel(
                scroll, text=title.upper(),
                font=ctk.CTkFont("SF Pro Text", 11),
                text_color=TEXT_3, anchor="w",
            ).pack(fill="x", padx=20, pady=(22, 6))
            f = ctk.CTkFrame(
                scroll, corner_radius=12,
                fg_color=SURF_1,
            )
            f.pack(fill="x", padx=16, pady=(0, 4))
            return f

        def row(parent, label: str, widget_fn) -> None:
            r = ctk.CTkFrame(parent, fg_color="transparent", height=50)
            r.pack(fill="x", padx=16, pady=2)
            r.pack_propagate(False)
            ctk.CTkLabel(
                r, text=label, anchor="w",
                font=ctk.CTkFont("SF Pro Text", 14),
                text_color=TEXT_1,
            ).pack(side="left")
            widget_fn(r)

        def sep_line(parent) -> None:
            ctk.CTkFrame(parent, height=1, fg_color=SURF_3).pack(
                fill="x", padx=16, pady=0
            )

        # ── 語音辨識 ──────────────────────────────────────────────────────
        stt = section("語音辨識")
        self._model_var = ctk.StringVar(value=self.cfg.model)

        def model_row(r):
            ctk.CTkOptionMenu(
                r, values=list(MODEL_INFO.keys()),
                variable=self._model_var,
                width=148, height=30, corner_radius=8,
                fg_color=SURF_2, button_color=SURF_2,
                button_hover_color=SURF_3,
                dropdown_fg_color=SURF_1,
                text_color=TEXT_1,
                font=ctk.CTkFont("SF Pro Text", 13),
                command=self._on_model_preview,
            ).pack(side="right")

        row(stt, "模型大小", model_row)
        self._model_desc = ctk.CTkLabel(
            stt, text=MODEL_INFO.get(self.cfg.model, ""),
            font=ctk.CTkFont("SF Pro Text", 11),
            text_color=TEXT_3,
            wraplength=380, anchor="w",
        )
        self._model_desc.pack(fill="x", padx=16, pady=(0, 10))
        sep_line(stt)

        self._lang_var = ctk.StringVar(value=self.cfg.language)

        def lang_row(r):
            ctk.CTkOptionMenu(
                r, values=list(LANGUAGE_OPTIONS.keys()),
                variable=self._lang_var,
                width=112, height=30, corner_radius=8,
                fg_color=SURF_2, button_color=SURF_2,
                button_hover_color=SURF_3,
                dropdown_fg_color=SURF_1,
                text_color=TEXT_1,
                font=ctk.CTkFont("SF Pro Text", 13),
            ).pack(side="right")

        row(stt, "辨識語言", lang_row)

        # ── 快捷鍵 ────────────────────────────────────────────────────────
        hk = section("快捷鍵")
        hk_row = ctk.CTkFrame(hk, fg_color="transparent", height=52)
        hk_row.pack(fill="x", padx=16, pady=4)
        hk_row.pack_propagate(False)

        ctk.CTkLabel(
            hk_row, text="全域快捷鍵", anchor="w",
            font=ctk.CTkFont("SF Pro Text", 14), text_color=TEXT_1,
        ).pack(side="left")

        hk_r = ctk.CTkFrame(hk_row, fg_color="transparent")
        hk_r.pack(side="right")

        self._hk_label = ctk.CTkLabel(
            hk_r, text=format_hotkey(self.cfg.hotkey),
            font=ctk.CTkFont("SF Pro Text", 13, "bold"),
            fg_color=SURF_2, text_color=TEXT_1,
            corner_radius=8, padx=12, pady=4,
        )
        self._hk_label.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            hk_r, text="重新綁定", width=80, height=28, corner_radius=8,
            fg_color=ACCENT_BG,
            hover_color=ACCENT,
            border_width=1, border_color=ACCENT,
            text_color=ACCENT_HV,
            font=ctk.CTkFont("SF Pro Text", 12),
            command=self._rebind_hotkey,
        ).pack(side="left")

        # ── 輸出偏好 ──────────────────────────────────────────────────────
        out = section("輸出偏好")
        self._append_var    = ctk.BooleanVar(value=self.cfg.append_results)
        self._autocopy_var  = ctk.BooleanVar(value=self.cfg.auto_copy)
        self._autopaste_var = ctk.BooleanVar(value=self.cfg.auto_paste)

        sw_style = dict(
            progress_color=ACCENT,
            button_color=TEXT_1,
            button_hover_color=TEXT_2,
            fg_color=SURF_3,
        )

        def make_sw(var, accent=None):
            s = dict(sw_style)
            if accent:
                s["progress_color"] = accent
            def fn(r):
                ctk.CTkSwitch(r, text="", variable=var,
                              onvalue=True, offvalue=False, **s).pack(side="right")
            return fn

        row(out, "追加錄音結果", make_sw(self._append_var))
        sep_line(out)
        row(out, "轉錄後自動複製", make_sw(self._autocopy_var))
        sep_line(out)
        row(out, "語音轉文字後自動貼上 ⌨", make_sw(self._autopaste_var, INDIGO))

        # ── AI 潤飾 (Ollama) ──────────────────────────────────────────────
        ai = section("AI 潤飾 (Ollama)")

        self._ollama_enabled_var = ctk.BooleanVar(value=self.cfg.ollama_enabled)
        row(ai, "啟用 AI 潤飾", make_sw(self._ollama_enabled_var, ACCENT))
        sep_line(ai)

        # 模型名稱（文字輸入；未來可改為動態 dropdown）
        model_row = ctk.CTkFrame(ai, fg_color="transparent", height=52)
        model_row.pack(fill="x", padx=16, pady=4)
        model_row.pack_propagate(False)
        ctk.CTkLabel(
            model_row, text="模型名稱", anchor="w",
            font=ctk.CTkFont("SF Pro Text", 14), text_color=TEXT_1,
        ).pack(side="left")
        self._ollama_model_var = ctk.StringVar(value=self.cfg.ollama_model)
        ctk.CTkEntry(
            model_row, textvariable=self._ollama_model_var,
            width=200, height=30, corner_radius=8,
            fg_color=SURF_2, border_color=SURF_3,
            text_color=TEXT_1,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 12),
        ).pack(side="right")
        sep_line(ai)

        # Base URL（進階；一般使用者不需要改）
        url_row = ctk.CTkFrame(ai, fg_color="transparent", height=52)
        url_row.pack(fill="x", padx=16, pady=4)
        url_row.pack_propagate(False)
        ctk.CTkLabel(
            url_row, text="服務位址", anchor="w",
            font=ctk.CTkFont("SF Pro Text", 14), text_color=TEXT_1,
        ).pack(side="left")
        self._ollama_url_var = ctk.StringVar(value=self.cfg.ollama_base_url)
        ctk.CTkEntry(
            url_row, textvariable=self._ollama_url_var,
            width=200, height=30, corner_radius=8,
            fg_color=SURF_2, border_color=SURF_3,
            text_color=TEXT_3,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11),
        ).pack(side="right")
        sep_line(ai)

        # ── 環境診斷（Phase 4.5）─────────────────────────────────────────
        # 給首次安裝、Ollama 缺東少西的使用者一行明確指引（含建議命令）。
        diag_row = ctk.CTkFrame(ai, fg_color=SURF_2, corner_radius=8)
        diag_row.pack(fill="x", padx=16, pady=(8, 4))
        self._ollama_diag_title = ctk.CTkLabel(
            diag_row, text="正在診斷…", anchor="w",
            font=ctk.CTkFont("SF Pro Text", 13, "bold"), text_color=TEXT_1,
        )
        self._ollama_diag_title.pack(fill="x", padx=12, pady=(10, 2))
        self._ollama_diag_detail = ctk.CTkLabel(
            diag_row, text="", anchor="w", justify="left",
            font=ctk.CTkFont("SF Pro Text", 11), text_color=TEXT_3,
            wraplength=520,
        )
        self._ollama_diag_detail.pack(fill="x", padx=12, pady=(0, 4))
        # 建議命令以等寬字 + 一鍵複製
        self._ollama_diag_cmd_frame = ctk.CTkFrame(diag_row, fg_color="transparent")
        self._ollama_diag_cmd_frame.pack(fill="x", padx=12, pady=(0, 10))
        self._ollama_diag_cmd = ctk.CTkLabel(
            self._ollama_diag_cmd_frame, text="", anchor="w",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11), text_color=ACCENT,
            wraplength=400,
        )
        self._ollama_diag_cmd.pack(side="left", padx=(0, 8))
        self._ollama_diag_copy_btn = ctk.CTkButton(
            self._ollama_diag_cmd_frame, text="複製命令",
            width=84, height=24, corner_radius=6,
            font=ctk.CTkFont("SF Pro Text", 11),
            fg_color=SURF_3, hover_color=SURF_4, text_color=TEXT_2,
        )
        self._ollama_diag_copy_btn.pack(side="right")
        self._ollama_diag_copy_btn.pack_forget()   # 預設隱藏，有命令才顯示

        # 啟動後立即跑一次背景診斷
        self.after(100, self._refresh_ollama_diagnostic)

        # 測試連線 + 狀態標籤
        test_row = ctk.CTkFrame(ai, fg_color="transparent", height=52)
        test_row.pack(fill="x", padx=16, pady=(4, 8))
        test_row.pack_propagate(False)
        self._ollama_test_status = ctk.CTkLabel(
            test_row, text="（尚未測試）",
            anchor="w",
            font=ctk.CTkFont("SF Pro Text", 12), text_color=TEXT_3,
        )
        self._ollama_test_status.pack(side="left")
        ctk.CTkButton(
            test_row, text="測試連線", width=100, height=30, corner_radius=8,
            fg_color=SURF_2, text_color=TEXT_1,
            hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            font=ctk.CTkFont("SF Pro Text", 12),
            command=self._test_ollama,
        ).pack(side="right")

        # ── 情境路由 (Phase 2) ────────────────────────────────────────────
        rout = section("情境路由 (Phase 2)")

        self._routing_var = ctk.BooleanVar(value=self.cfg.preset_routing_enabled)
        row(rout, "啟用情境自動切換", make_sw(self._routing_var, ACCENT))
        sep_line(rout)

        # 每個非 default preset 一個 switch；預設全部啟用
        self._preset_switch_vars: dict[str, ctk.BooleanVar] = {}
        import presets as _pr
        for pname, preset in _pr.PRESETS.items():
            if pname == "default":
                continue
            default_on = self.cfg.preset_overrides.get(pname, True)
            var = ctk.BooleanVar(value=default_on)
            self._preset_switch_vars[pname] = var
            row(rout, f"  ↳ {preset.display_name}", make_sw(var, ACCENT))
            sep_line(rout)

        # 手動 reload prompt 按鈕 + 熱重載 switch
        hot_row = ctk.CTkFrame(rout, fg_color="transparent", height=52)
        hot_row.pack(fill="x", padx=16, pady=4)
        hot_row.pack_propagate(False)
        ctk.CTkLabel(
            hot_row, text="Prompt 熱重載", anchor="w",
            font=ctk.CTkFont("SF Pro Text", 14), text_color=TEXT_1,
        ).pack(side="left")
        self._hot_reload_var = ctk.BooleanVar(value=self.cfg.prompt_hot_reload)
        ctk.CTkSwitch(
            hot_row, text="", variable=self._hot_reload_var,
            onvalue=True, offvalue=False, **sw_style,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            hot_row, text="立即重新載入", width=110, height=28, corner_radius=8,
            fg_color=SURF_2, text_color=TEXT_1,
            hover_color=SURF_3, border_width=1, border_color=SURF_3,
            font=ctk.CTkFont("SF Pro Text", 12),
            command=self._reload_prompts_now,
        ).pack(side="right", padx=(8, 0))

        # ── 個人字典 (#4) ─────────────────────────────────────────────────
        dsec = section("個人字典")
        self._dict_enabled_var = ctk.BooleanVar(value=self.cfg.dictionary_enabled)
        row(dsec, "注入到轉錄與潤飾", make_sw(self._dict_enabled_var, ACCENT))
        sep_line(dsec)

        dict_path_row = ctk.CTkFrame(dsec, fg_color="transparent", height=52)
        dict_path_row.pack(fill="x", padx=16, pady=4)
        dict_path_row.pack_propagate(False)
        ctk.CTkLabel(
            dict_path_row, text="字典檔路徑", anchor="w",
            font=ctk.CTkFont("SF Pro Text", 14), text_color=TEXT_1,
        ).pack(side="left")
        self._dict_path_var = ctk.StringVar(value=self.cfg.dictionary_path)
        ctk.CTkEntry(
            dict_path_row, textvariable=self._dict_path_var,
            placeholder_text="(預設 ~/.whisper_app/dictionary.json)",
            width=220, height=30, corner_radius=8,
            fg_color=SURF_2, border_color=SURF_3,
            text_color=TEXT_3,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11),
        ).pack(side="right")
        sep_line(dsec)

        dict_btn_row = ctk.CTkFrame(dsec, fg_color="transparent", height=52)
        dict_btn_row.pack(fill="x", padx=16, pady=(4, 8))
        dict_btn_row.pack_propagate(False)
        self._dict_status_label = ctk.CTkLabel(
            dict_btn_row, text="", anchor="w",
            font=ctk.CTkFont("SF Pro Text", 12), text_color=TEXT_3,
        )
        self._dict_status_label.pack(side="left")
        ctk.CTkButton(
            dict_btn_row, text="用預設編輯器開啟", width=150, height=30, corner_radius=8,
            fg_color=SURF_2, text_color=TEXT_1,
            hover_color=SURF_3, border_width=1, border_color=SURF_3,
            font=ctk.CTkFont("SF Pro Text", 12),
            command=self._open_dictionary_file,
        ).pack(side="right")

        # ── 介面 (Phase 4.3) ────────────────────────────────────────────
        ui_sec = section("介面")
        self._mini_window_var = ctk.BooleanVar(value=self.cfg.mini_recording_window)
        row(ui_sec, "錄音時顯示浮動 mini 視窗（右下角）",
            make_sw(self._mini_window_var, ACCENT))

        # ── 歷史紀錄 (Phase 3.2) ─────────────────────────────────────────
        hist = section("歷史紀錄 (Phase 3.2)")
        self._history_enabled_var = ctk.BooleanVar(value=self.cfg.history_enabled)
        row(hist, "寫入 ~/.whisper_app/history.db",
            make_sw(self._history_enabled_var, ACCENT))
        sep_line(hist)

        retention_row = ctk.CTkFrame(hist, fg_color="transparent", height=52)
        retention_row.pack(fill="x", padx=16, pady=4)
        retention_row.pack_propagate(False)
        ctk.CTkLabel(
            retention_row, text="保留天數（0 = 永久）", anchor="w",
            font=ctk.CTkFont("SF Pro Text", 14), text_color=TEXT_1,
        ).pack(side="left")
        self._history_retention_var = tk.StringVar(
            value=str(self.cfg.history_retention_days)
        )
        ctk.CTkEntry(
            retention_row, textvariable=self._history_retention_var,
            width=90, height=30, corner_radius=8,
            fg_color=SURF_2, border_color=SURF_3, text_color=TEXT_1,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 13),
            justify="right",
        ).pack(side="right")

        # ── 關於 ──────────────────────────────────────────────────────────
        about = section("關於")
        for label, path in [
            ("Whisper 快取", "~/.cache/huggingface"),
            ("設定檔", "~/.whisper_app/config.json"),
        ]:
            pr = ctk.CTkFrame(about, fg_color="transparent", height=38)
            pr.pack(fill="x", padx=16, pady=2)
            pr.pack_propagate(False)
            ctk.CTkLabel(pr, text=label, anchor="w",
                         font=ctk.CTkFont("SF Pro Text", 13),
                         text_color=TEXT_1).pack(side="left")
            ctk.CTkLabel(pr, text=path, anchor="e",
                         font=ctk.CTkFont("SF Pro Text", 11),
                         text_color=TEXT_3).pack(side="right")

        ctk.CTkButton(
            about, text="開啟設定資料夾", width=156, height=28,
            image=get_icon("folder", 14, ACCENT_HV),
            compound="left",
            fg_color="transparent",
            border_width=1, border_color=SURF_3,
            text_color=ACCENT_HV,
            hover_color=SURF_2,
            font=ctk.CTkFont("SF Pro Text", 12),
            corner_radius=8,
            command=lambda: subprocess.run(
                ["open", os.path.expanduser("~/.whisper_app")]
            ),
        ).pack(anchor="w", padx=16, pady=(0, 8))

        # 匯入 / 匯出（Phase 4.4）—— 設定 + 字典 + preset 覆寫 → zip
        # 不含 history.db（隱私）與 polish_log.jsonl（debug 用、可能很大）
        ie_row = ctk.CTkFrame(about, fg_color="transparent")
        ie_row.pack(anchor="w", padx=16, pady=(0, 14))
        ctk.CTkButton(
            ie_row, text="匯出設定…", width=120, height=28,
            image=get_icon("download", 14, ACCENT_HV),
            compound="left",
            fg_color="transparent",
            border_width=1, border_color=SURF_3,
            text_color=ACCENT_HV, hover_color=SURF_2,
            font=ctk.CTkFont("SF Pro Text", 12),
            corner_radius=8,
            command=self._export_settings,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            ie_row, text="匯入設定…", width=120, height=28,
            image=get_icon("file-text", 14, ACCENT_HV),
            compound="left",
            fg_color="transparent",
            border_width=1, border_color=SURF_3,
            text_color=ACCENT_HV, hover_color=SURF_2,
            font=ctk.CTkFont("SF Pro Text", 12),
            corner_radius=8,
            command=self._import_settings,
        ).pack(side="left")

        # ── Buttons ───────────────────────────────────────────────────────
        ctk.CTkFrame(self, height=1, fg_color=SURF_3, corner_radius=0).pack(fill="x")
        btn_bar = ctk.CTkFrame(self, height=60, fg_color=SURF_1, corner_radius=0)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)

        inner = ctk.CTkFrame(btn_bar, fg_color="transparent")
        inner.pack(side="right", padx=20, pady=12)

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
        """從表單欄位蒐集所有值、呼叫 cfg.save() 並通知主視窗。"""
        self.cfg.model          = self._model_var.get()
        self.cfg.language       = self._lang_var.get()
        self.cfg.append_results = self._append_var.get()
        self.cfg.auto_copy      = self._autocopy_var.get()
        self.cfg.auto_paste     = self._autopaste_var.get()
        # ── Ollama ────────────────────────────────────────────────────────
        self.cfg.ollama_enabled  = self._ollama_enabled_var.get()
        model = self._ollama_model_var.get().strip()
        if model:
            self.cfg.ollama_model = model
        url = self._ollama_url_var.get().strip()
        if url:
            self.cfg.ollama_base_url = url
        # ── Phase 2 preset 路由 ──────────────────────────────────────────
        self.cfg.preset_routing_enabled = self._routing_var.get()
        self.cfg.preset_overrides = {
            name: var.get() for name, var in self._preset_switch_vars.items()
        }
        # ── #2 熱重載 ─────────────────────────────────────────────────────
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
        self.cfg.save()
        self._on_save_cb(self.cfg)   # 通知主視窗（destroy 由 _save 的 finally 負責）

    def _reload_prompts_now(self) -> None:
        """立即觸發 PromptReloader.reload_now()，並以狀態標籤顯示結果。"""
        reloader = getattr(self._parent, "_prompt_reloader", None)
        if reloader is None:
            return
        reloaded = reloader.reload_now()
        if reloaded:
            names = ", ".join(f"{n}.py" for n in reloaded)
            self._dict_status_label.configure(
                text=f"已重新載入 {names}", text_color=SUCCESS,
            )
        else:
            self._dict_status_label.configure(
                text="Reload 失敗（檢查 console log）",
                text_color=DANGER,
            )

    def _open_dictionary_file(self) -> None:
        """在預設編輯器開啟字典 JSON。"""
        path_str = (self._dict_path_var.get().strip()
                    or str(_dictionary.DEFAULT_PATH))
        from pathlib import Path as _P
        path = _P(path_str).expanduser()
        _dictionary.ensure_file(path)
        try:
            subprocess.run(["open", str(path)])
            self._dict_status_label.configure(
                text=f"已開啟 {path.name}", text_color=TEXT_3,
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
        manifest = {
            "schema_version": self._EXPORT_SCHEMA_VERSION,
            "exported_at":    datetime.datetime.now().isoformat(timespec="seconds"),
            "app_version":    "v2.2.0",
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
        """初始化對話框並立即開始擷取按鍵。current_combo 目前未使用，保留供未來顯示用。"""
        super().__init__(parent)
        self._on_apply_cb = on_apply_cb
        self._captured: Optional[str] = None
        self.title("設定快捷鍵")
        self.geometry("340x220")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.grab_set()
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
            corner_radius=10, padx=24, pady=12,
        )
        self._detect_label.pack(pady=4)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(pady=16)

        ctk.CTkButton(
            bar, text="取消", width=90, height=32, corner_radius=8,
            fg_color=SURF_2, text_color=TEXT_2,
            hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            font=ctk.CTkFont("SF Pro Text", 13),
            command=self.destroy,
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
        """覆寫 destroy：先解除 Tk 鍵盤綁定，再呼叫 super().destroy()。"""
        # 確保對話框關閉時解除綁定，避免 Tcl 錯誤訊息
        self._unbind_capture()
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
        ).pack(padx=32, pady=4)

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
        """以 macOS URL scheme 直接跳到「隱私權 > 輔助使用」設定頁，然後關閉對話框。"""
        subprocess.run([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  HistoryWindow（Phase 3.2 歷史紀錄檢視）
# ─────────────────────────────────────────────────────────────────────────────

class HistoryWindow(ctk.CTkToplevel):
    """歷史紀錄視窗（modal-less）。

    左側：搜尋框 + 清單（時間、preset 標籤、摘要）
    右側：選中項目的詳細內容（raw / polished 並排）+ 動作按鈕

    動作：
      • 複製 — 把 raw 或 polished 複製到剪貼簿
      • 重新潤飾 — 呼叫主視窗的 _repolish_from_history
      • 刪除 — 從 DB 刪除並刷新清單
    """

    WIN_W = 980
    WIN_H = 640

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
        self._selected_id: Optional[int] = None

        self._build_ui()
        self._refresh(query="")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # 搜尋列
        top = ctk.CTkFrame(self, height=56, fg_color=SURF_1, corner_radius=0)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)

        row = ctk.CTkFrame(top, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=12)

        ctk.CTkLabel(
            row, text="搜尋",
            font=ctk.CTkFont("SF Pro Text", 13),
            text_color=TEXT_3,
        ).pack(side="left", padx=(0, 8))

        self._search_var = tk.StringVar(value="")
        entry = ctk.CTkEntry(
            row,
            textvariable=self._search_var,
            width=380, height=32,
            font=ctk.CTkFont("SF Pro Text", 13),
            fg_color=SURF_2, border_color=SURF_3, text_color=TEXT_1,
            placeholder_text="輸入關鍵字（中文 ≥ 2 字、英文 ≥ 3 字）",
        )
        entry.pack(side="left", padx=4)
        self._search_var.trace_add("write", lambda *_: self._on_search_changed())

        self._count_label = ctk.CTkLabel(
            row, text="",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 12),
            text_color=TEXT_3,
        )
        self._count_label.pack(side="right", padx=8)

        # 主區：左清單 + 右詳細
        body = ctk.CTkFrame(self, fg_color=BG)
        body.pack(fill="both", expand=True, padx=0, pady=0)

        # 左清單（CTkScrollableFrame）
        self._list_frame = ctk.CTkScrollableFrame(
            body,
            width=340,
            fg_color=SURF_1,
            corner_radius=0,
            scrollbar_button_color=SURF_3,
            scrollbar_button_hover_color=SURF_4,
        )
        self._list_frame.pack(side="left", fill="y")

        # 分隔線
        ctk.CTkFrame(body, width=1, fg_color=SURF_3).pack(side="left", fill="y")

        # 右詳細
        self._detail_frame = ctk.CTkFrame(body, fg_color=BG)
        self._detail_frame.pack(side="left", fill="both", expand=True)

        self._build_empty_detail()

    def _build_empty_detail(self) -> None:
        """未選取時的空態畫面。"""
        for w in self._detail_frame.winfo_children():
            w.destroy()
        ctk.CTkLabel(
            self._detail_frame,
            text="← 從左側選取一筆紀錄",
            font=ctk.CTkFont("SF Pro Text", 14),
            text_color=TEXT_3,
        ).pack(expand=True)

    # ── 資料刷新 ──────────────────────────────────────────────────────────────

    def _on_search_changed(self) -> None:
        self.after(180, lambda q=self._search_var.get(): self._refresh(q))

    def _refresh(self, query: str) -> None:
        """重新查詢並重建清單。"""
        # 若搜尋框的內容已變動（使用者繼續打字），放棄這次舊 query
        if query != self._search_var.get():
            return
        if query.strip():
            self._entries = self._store.search(query, limit=200)
        else:
            self._entries = self._store.list_recent(limit=200)

        self._count_label.configure(text=f"{len(self._entries)} / {self._store.count()}")

        # 清空舊 list widgets
        for w in self._list_frame.winfo_children():
            w.destroy()

        if not self._entries:
            ctk.CTkLabel(
                self._list_frame, text="（沒有符合的紀錄）",
                font=ctk.CTkFont("SF Pro Text", 13),
                text_color=TEXT_4,
            ).pack(pady=20)
            self._build_empty_detail()
            return

        # 渲染每筆
        for entry in self._entries:
            self._render_list_row(entry)

        # 自動選第一筆
        if self._entries:
            self._select(self._entries[0])

    def _render_list_row(self, entry) -> None:
        """渲染單筆清單 row（可點擊卡片）。"""
        import datetime as _dt
        selected = (entry.id == self._selected_id)
        card = ctk.CTkFrame(
            self._list_frame,
            fg_color=SURF_2 if selected else SURF_1,
            corner_radius=8,
            border_width=1,
            border_color=ACCENT if selected else SURF_3,
        )
        card.pack(fill="x", padx=8, pady=4)

        time_str = _dt.datetime.fromtimestamp(entry.timestamp).strftime("%m/%d %H:%M")
        preset = entry.preset_used
        preset_badge = ""
        if preset != "default":
            display = _presets.PRESETS.get(preset)
            preset_badge = display.display_name if display else preset

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(
            header, text=time_str,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11),
            text_color=TEXT_3,
        ).pack(side="left")
        if preset_badge:
            ctk.CTkLabel(
                header, text=f"· {preset_badge}",
                font=ctk.CTkFont("SF Pro Text", 11),
                text_color=ACCENT,
            ).pack(side="left", padx=(6, 0))
        if entry.has_polish():
            ctk.CTkLabel(
                header, text="✨",
                font=ctk.CTkFont("SF Pro Text", 11),
                text_color=INDIGO,
            ).pack(side="right")

        ctk.CTkLabel(
            card, text=entry.summary(46),
            font=ctk.CTkFont("SF Pro Text", 13),
            text_color=TEXT_1 if selected else TEXT_2,
            wraplength=310, justify="left", anchor="w",
        ).pack(fill="x", padx=10, pady=(0, 8))

        # 整張卡片可點擊
        for w in (card, *_walk_children(card)):
            w.bind("<Button-1>", lambda e, en=entry: self._select(en))

    def _select(self, entry) -> None:
        """選中某筆後刷新右側詳細。"""
        self._selected_id = entry.id
        # 重新渲染清單以更新 selected 視覺
        # （不用重新查 DB，直接用快取的 self._entries）
        for w in self._list_frame.winfo_children():
            w.destroy()
        for e in self._entries:
            self._render_list_row(e)

        self._build_detail(entry)

    def _build_detail(self, entry) -> None:
        """右側詳細視圖。"""
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
            font=ctk.CTkFont("SF Pro Text", 12),
            text_color=TEXT_3,
        ).pack(side="left", padx=12)

        # 原文 / 潤飾 並排（潤飾在上，原文在下；有潤飾就顯示兩段）
        if entry.polished_text:
            self._build_text_block(self._detail_frame, "潤飾版", entry.polished_text, ACCENT)
            self._build_text_block(self._detail_frame, "原文", entry.raw_text, TEXT_3)
        else:
            self._build_text_block(self._detail_frame, "原文", entry.raw_text, TEXT_2)

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
            font=ctk.CTkFont("SF Pro Text", 13),
            fg_color=ACCENT, hover_color=ACCENT_HV, text_color=TEXT_1,
            command=lambda: _copy_text(entry.polished_text or entry.raw_text),
        ).pack(side="left", padx=4)

        if entry.polished_text:
            ctk.CTkButton(
                bar, text="複製原文",
                width=110, height=32, corner_radius=8,
                font=ctk.CTkFont("SF Pro Text", 13),
                fg_color=SURF_2, hover_color=SURF_3,
                border_width=1, border_color=SURF_3,
                text_color=TEXT_2,
                command=lambda: _copy_text(entry.raw_text),
            ).pack(side="left", padx=4)

        ctk.CTkButton(
            bar, text="重新潤飾",
            image=get_icon("sparkles", 15, TEXT_2),
            compound="left",
            width=110, height=32, corner_radius=8,
            font=ctk.CTkFont("SF Pro Text", 13),
            fg_color=SURF_2, hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            text_color=TEXT_2,
            command=lambda: (self._on_repolish(entry), self.destroy()),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            bar, text="刪除",
            image=get_icon("x", 15, TEXT_3),
            compound="left",
            width=90, height=32, corner_radius=8,
            font=ctk.CTkFont("SF Pro Text", 13),
            fg_color=SURF_2, hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            text_color=TEXT_3,
            command=lambda: self._delete_selected(entry.id),
        ).pack(side="right", padx=4)

    def _build_text_block(self, parent, label: str, text: str, label_color: str) -> None:
        """渲染一段標籤 + 文字區塊。"""
        block = ctk.CTkFrame(parent, fg_color="transparent")
        block.pack(fill="both", expand=True, padx=20, pady=(8, 0))

        ctk.CTkLabel(
            block, text=label,
            font=ctk.CTkFont("SF Pro Text", 11, "bold"),
            text_color=label_color,
        ).pack(anchor="w", pady=(0, 4))

        box = ctk.CTkTextbox(
            block,
            fg_color=SURF_1, border_color=SURF_3, border_width=1,
            text_color=TEXT_1, font=ctk.CTkFont("SF Pro Text", 14),
            wrap="word", corner_radius=8,
            scrollbar_button_color=SURF_3,
        )
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _delete_selected(self, id: int) -> None:
        """刪除當前選中的紀錄並刷新。"""
        if not self._store.delete(id):
            self._toast("刪除失敗")
            return
        log_action("history_deleted", id=id)
        self._toast("已刪除")
        self._selected_id = None
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
#  MiniRecordingWindow（Phase 4.3 浮動小型 HUD）
# ─────────────────────────────────────────────────────────────────────────────

class MiniRecordingWindow(tk.Toplevel):
    """錄音 / 處理中的浮動 mini HUD。

    螢幕右下角固定 140×38 無邊框視窗，always-on-top，跟隨 AppWindow 狀態
    自動顯示／隱藏。內容：狀態圓點（依狀態色）+ 計時器（mm:ss）。
    點擊 → lift 主視窗到前景。

    AppWindow 透過 `update(state, elapsed_s, rms)` 推進狀態；不該由 mini
    自己 polling，避免狀態真相分散。
    """

    WIN_W = 140
    WIN_H = 38
    OFFSET_X = 24    # 距螢幕右邊緣
    OFFSET_Y = 60    # 距螢幕底邊緣（避開 Dock）

    def __init__(self, master) -> None:
        super().__init__(master)
        self._master = master
        self._closed = False

        # 無邊框 + always-on-top
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        try:
            self.attributes("-alpha", 0.94)
        except Exception:
            pass
        self.configure(bg=SURF_2)

        # 置於螢幕右下
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = sw - self.WIN_W - self.OFFSET_X
        y = sh - self.WIN_H - self.OFFSET_Y
        self.geometry(f"{self.WIN_W}x{self.WIN_H}+{x}+{y}")

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
        self._timer.pack(side="right", padx=12)

        # 點擊任何處 → 把主視窗拉前
        for w in (self, outer, inner, self._dot, self._label, self._timer):
            w.bind("<Button-1>", self._on_click)

        # 預設隱藏；由 AppWindow 透過 .show() 顯示
        self.withdraw()

    def show_recording(self) -> None:
        """進入錄音狀態 → 顯示紅點 + 「錄音中」+ 0:00 計時。"""
        if self._closed:
            return
        self._dot.configure(fg=DANGER)
        self._label.configure(text="錄音中")
        self._timer.configure(text="00:00")
        self.deiconify()

    def show_processing(self) -> None:
        """進入處理中狀態 → 琥珀色 + 「轉錄中」。"""
        if self._closed:
            return
        self._dot.configure(fg=WARN)
        self._label.configure(text="轉錄中")
        self.deiconify()

    def update_timer(self, seconds: float) -> None:
        """錄音中每秒呼叫一次更新計時器。"""
        if self._closed:
            return
        s = int(seconds)
        self._timer.configure(text=f"{s // 60:02d}:{s % 60:02d}")

    def hide(self) -> None:
        if self._closed:
            return
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
