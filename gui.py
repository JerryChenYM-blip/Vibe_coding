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
    FONT_FAMILY_UI, FONT_FAMILY_TEXT, FONT_FAMILY_MONO,
    SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL, SPACE_2XL,
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

    def __init__(self, master: ctk.CTk, cfg: Config) -> None:
        super().__init__(master, fg_color=BG, corner_radius=0)
        self.pack(fill="both", expand=True)

        self.cfg = cfg
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
        self.transcriber = Transcriber()
        self.ollama      = OllamaClient()
        # 用設定檔同步 Ollama 參數（base_url / model / enabled / timeout）
        self.ollama.apply_app_config(cfg)
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
        self._stream_chunks:  list[str] = []
        self._stream_tick_id            = None

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
        # A3（v2.7.0）：reduce_motion 解析改成 config-aware（"auto" / "always" /
        # "never"）。SettingsWindow 改 pref 後呼叫 _refresh_reduce_motion() 立即
        # 套用、不需重啟。getattr fallback 為了向前相容沒升級的舊 config。
        self._reduce_motion    = resolve_reduce_motion(
            getattr(self.cfg, "reduce_motion_pref", "auto")
        )

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
            # v2.14.0：傳 chinese_variant 給 Qwen3-ASR 用（Whisper backend ignore）
            result = self.transcriber.transcribe(
                audio,
                model_size=model,
                language=lang,
                chinese_variant=getattr(self.cfg, "chinese_variant", "off"),
            )

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
        self._show_toast("⚠ 轉錄失敗")

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
        # v2.13.0 / 2026-05-24：修「Ollama 啟用但 health_ok=None 跳過 polish」。
        # 根因：D5-S7 TTL 30s 過期會回 None；剛 enable Ollama health check 還沒
        # 回來也是 None；舊邏輯 `is True` 排除 None → user 雖然開了 Ollama
        # 但每次 cache 過期或剛啟用都不會 polish、直接貼 Whisper 原文（含「措置率」
        # 這種同音錯字）。改用 `is not False` 放寬：None / True 都試一下；
        # 真的 False（確定沒跑）才略過。process() 內部 ConnectionError fallback
        # 自然會降級回原文，使用者不會卡住。
        take_polish_path = (
            valid
            and self.cfg.ollama_enabled
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

        # Cluster B：抓「當下 latest」block ref 一起傳進 _finish_polish。
        # polish 跑回來時這個 ref 可能已被 delete（不在 list 內）或不再是 latest，
        # _finish_polish 用 identity 比對來決定是否安全 set_polished。
        target_block = self._utterance_blocks[0] if self._utterance_blocks else None

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

        if resp.error:
            # 降級：提示錯誤，title 狀態轉為「原文（潤飾失敗）」，auto-paste 貼原文
            # 注意：preset 名稱（若有）刻意保留，讓使用者知道是哪條 preset 炸
            self._title_status = "原文（潤飾失敗）"
            self._rebuild_result_title()
            self._show_toast(f"AI 潤飾失敗：{resp.error}")
            paste_text = raw_text
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
        success = _ap.paste_to_app(text, target)
        if success:
            self._show_toast(f"⌨  已貼入 {target}")
        else:
            self._show_toast("⌨  自動貼上失敗（請確認輔助使用權限）")

    def _display_result(self, result: TranscriptionResult) -> None:
        """新增一個 UtteranceBlock，取代之前在 textbox 累積插入文字的做法。

        Fix 19 Path B / 2026-05-23：
          • 每段獨立 block（時間戳 / 時長 / 文字 / 動作圖示）
          • 自動把前一段的「最新」邊框取消
          • 新 block pack 在底部 + 自動 scroll-to-bottom 讓使用者看到
          • 標題仍顯示「最近一段的元資料」（時長 / 語言 / 模型）
        """
        dur   = float(result.duration_seconds)
        lang  = (result.language.upper() if result.language else "?")
        model = self._model_var.get()
        # C2：重設三段式標題狀態（以最新段為準）
        self._title_base   = f"轉錄結果  ({int(dur)}s · {lang} · {model})"
        self._title_preset = None
        self._title_status = None
        self._rebuild_result_title()

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
        """主執行緒：快捷鍵 tap → 直接重用 chamber 按鈕的 toggle handler。"""
        log_action("hotkey_triggered_toggle", combo=self.cfg.hotkey, state=self._state)
        self._on_record_btn()

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
        if self._utterance_blocks:
            self._utterance_blocks[0].highlight_as_latest(False)

        import datetime as _dt
        try:
            ts = _dt.datetime.fromtimestamp(entry.timestamp).strftime("%H:%M")
        except Exception:
            ts = _dt.datetime.now().strftime("%H:%M")
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
        """週期性檢查 NSEvent monitor 還活著嗎；死了就 restart。

        Fix 7 / 2026-05-22：pynput Listener 已換成 NSEvent
        addGlobal/LocalMonitorForEventsMatchingMask_handler_。NSEvent 不會自己死、
        不會被 idle timeout disable、不會被 TCC race 殺掉，watchdog 幾乎不會 fire；
        但仍保留作為極端情境兜底（系統權限被使用者中途撤銷、PyObjC 內部異常等）。

        Layer 2（CGEventTap re-enable）+ Layer 3（定時 force restart）已隨
        pynput 一併移除——NSEvent 不需要這兩層補救。
        """
        try:
            mgr = self.hotkey_mgr
            # NSEvent backend：monitor 物件不為 None 視為活著
            monitor_alive = (
                getattr(mgr, "_monitor_global", None) is not None
                or getattr(mgr, "_monitor_local", None) is not None
            )
            if not monitor_alive:
                log.warning("HOTKEY: watchdog restarting NSEvent monitor (reason=monitor_missing)")
                log_action("hotkey_listener_auto_restarted", reason="monitor_missing")
                mgr.restart(self.cfg.hotkey)
                self._last_hotkey_force_restart = time.monotonic()
            else:
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
        """
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
        """沒有任何 UtteranceBlock 時，顯示置中的佔位符 label。"""
        if self._placeholder_label is not None or self._utterance_blocks:
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

    def _get_result_text(self) -> str:
        """所有 block 串接（換行分隔）；給「存檔」用。"""
        if not self._utterance_blocks:
            return ""
        return "\n\n".join(b.get_current_text() for b in self._utterance_blocks)

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
            f.pack(fill="x", padx=SPACE_LG, pady=(0, 4))
            return f

        def row(parent, label: str, widget_fn) -> None:
            r = ctk.CTkFrame(parent, fg_color="transparent", height=50)
            r.pack(fill="x", padx=SPACE_LG, pady=2)
            r.pack_propagate(False)
            ctk.CTkLabel(
                r, text=label, anchor="w",
                font=ctk.CTkFont("SF Pro Text", 14),
                text_color=TEXT_1,
            ).pack(side="left")
            widget_fn(r)

        def sep_line(parent) -> None:
            ctk.CTkFrame(parent, height=1, fg_color=SURF_3).pack(
                fill="x", padx=SPACE_LG, pady=0
            )

        # ── 外觀（v2.6.0）── 放第一個 section 最顯眼位置 ─────────────────
        # 點 chip → 跟現在不同就彈 confirm dialog → 確認 → 立刻 save + 重啟。
        # 不跟其他設定一起等 Save 按鈕，因為重啟需要立即動作（plan §「Restart UX 流程」）。
        ap = section("外觀")
        self._theme_var = ctk.StringVar(value=self.cfg.theme)

        def theme_row(r):
            wrap = ctk.CTkFrame(r, fg_color="transparent")
            wrap.pack(side="right")
            # 兩顆 segmented chip：深色 / 淺色
            self._theme_btns: dict[str, ctk.CTkButton] = {}
            for value, label in (("dark", "深色"), ("light", "淺色")):
                btn = ctk.CTkButton(
                    wrap, text=label,
                    width=72, height=30, corner_radius=8,
                    font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
                    border_width=1,
                    command=lambda v=value: self._on_theme_clicked(v),
                )
                btn.pack(side="left", padx=(0, 4))
                self._theme_btns[value] = btn
            self._apply_theme_chip_style()

        row(ap, "主題", theme_row)
        ctk.CTkLabel(
            ap,
            text=(
                "深色：zinc + cyan（目前預設）\n"
                "淺色：暖白 + Claude 珊瑚（v2.6.0 新增）\n"
                "切換需重新啟動 App（~2 秒、自動完成）"
            ),
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
            text_color=TEXT_3,
            justify="left", anchor="w",
        ).pack(anchor="w", padx=SPACE_LG, pady=(0, 10))

        # A3（v2.7.0）：動態效果 3-way segmented（auto / always / never）
        self._reduce_motion_var = ctk.StringVar(
            value=getattr(self.cfg, "reduce_motion_pref", "auto")
        )

        def reduce_motion_row(r):
            wrap = ctk.CTkFrame(r, fg_color="transparent")
            wrap.pack(side="right")
            self._reduce_motion_btns: dict[str, ctk.CTkButton] = {}
            for value, label in (
                ("auto", "跟系統"),
                ("always", "減少動態"),
                ("never", "完整動畫"),
            ):
                btn = ctk.CTkButton(
                    wrap, text=label,
                    width=78, height=30, corner_radius=8,
                    font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
                    border_width=1,
                    command=lambda v=value: self._on_reduce_motion_clicked(v),
                )
                btn.pack(side="left", padx=(0, 4))
                self._reduce_motion_btns[value] = btn
            self._apply_reduce_motion_chip_style()

        row(ap, "動態效果", reduce_motion_row)
        ctk.CTkLabel(
            ap,
            text=(
                "跟系統：依 macOS「減少動態效果」偏好（推薦）\n"
                "減少動態：強制關閉呼吸光圈／粒子環旋轉／漣漪\n"
                "完整動畫：永遠跑完整動畫（即使系統開了 reduce motion）"
            ),
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
            text_color=TEXT_3,
            justify="left", anchor="w",
        ).pack(anchor="w", padx=SPACE_LG, pady=(0, 10))

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
        self._model_desc.pack(fill="x", padx=SPACE_LG, pady=(0, 10))
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
        sep_line(stt)

        # ── 麥克風來源（F1 / v2.12.0）────────────────────────────────────
        # 列出本機所有有輸入聲道的音訊裝置（含實體 + 虛擬如 BlackHole / Loopback 等）
        # 第一筆固定為「（系統預設）」對應 input_device=None。
        from recorder import AudioRecorder as _AR
        try:
            self._available_devices = _AR.list_devices()
        except Exception:
            self._available_devices = []
            log_error("settings_list_devices_failed")
        # dropdown 顯示清單：先放「系統預設」，後面是各裝置名
        self._device_values = ["（系統預設）"] + [d["name"] for d in self._available_devices]
        # 初始選擇：cfg.input_device 對應的 device 名（找不到就回預設）
        _current_dev = self.cfg.input_device
        if _current_dev and _current_dev in (d["name"] for d in self._available_devices):
            initial = _current_dev
        else:
            initial = "（系統預設）"
        self._device_var = ctk.StringVar(value=initial)

        def device_row(r):
            ctk.CTkOptionMenu(
                r, values=self._device_values,
                variable=self._device_var,
                width=240, height=30, corner_radius=8,
                fg_color=SURF_2, button_color=SURF_2,
                button_hover_color=SURF_3,
                dropdown_fg_color=SURF_1,
                text_color=TEXT_1,
                font=ctk.CTkFont("SF Pro Text", 13),
            ).pack(side="right")

        row(stt, "麥克風來源", device_row)
        ctk.CTkLabel(
            stt,
            text=f"偵測到 {len(self._available_devices)} 個輸入裝置；「系統預設」會跟隨 macOS 設定。",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
            text_color=TEXT_3,
            justify="left", anchor="w",
        ).pack(anchor="w", padx=SPACE_LG, pady=(0, 10))

        # ── 快捷鍵 ────────────────────────────────────────────────────────
        hk = section("快捷鍵")
        hk_row = ctk.CTkFrame(hk, fg_color="transparent", height=52)
        hk_row.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
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
            corner_radius=8, padx=SPACE_MD, pady=SPACE_XS,
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

        # Bug D（v2.13.0）：貼上策略 chip — wait（品質優先）vs raw（速度優先）
        # 預設 wait：等潤飾完再貼。對 12B 模型可能 3-20s，user 體感「貼上慢」。
        # raw：先貼 Whisper 原文（即時），潤飾結果不再覆蓋（避免貼到不同視窗）。
        # 適合：user 已經要快、品質可後續手動編輯。
        self._paste_strategy_var = ctk.StringVar(
            value=getattr(self.cfg, "ollama_paste_strategy", "wait")
        )

        def paste_strategy_row(r):
            wrap = ctk.CTkFrame(r, fg_color="transparent")
            wrap.pack(side="right")
            self._paste_strategy_btns: dict[str, ctk.CTkButton] = {}
            for value, label in (
                ("wait", "等潤飾"),
                ("raw", "先貼原文"),
            ):
                btn = ctk.CTkButton(
                    wrap, text=label,
                    width=80, height=30, corner_radius=8,
                    font=ctk.CTkFont(FONT_FAMILY_TEXT, 13),
                    border_width=1,
                    command=lambda v=value: self._on_paste_strategy_clicked(v),
                )
                btn.pack(side="left", padx=(0, 4))
                self._paste_strategy_btns[value] = btn
            self._apply_paste_strategy_chip_style()

        row(ai, "貼上策略", paste_strategy_row)
        ctk.CTkLabel(
            ai,
            text=(
                "等潤飾：等 AI 校正完成才貼上（品質優先、3-20s 視模型大小）\n"
                "先貼原文：立即貼 Whisper 原文，潤飾結果只更新 UI 不再覆蓋（速度優先）"
            ),
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
            text_color=TEXT_3,
            justify="left", anchor="w",
        ).pack(anchor="w", padx=SPACE_LG, pady=(0, 10))
        sep_line(ai)

        # v2.13.0：模型下拉選單（自動偵測本機 Ollama 已安裝模型，取代手動輸入）
        # get_models() 有 3s timeout，Ollama 沒跑也不會卡太久。
        model_row = ctk.CTkFrame(ai, fg_color="transparent", height=52)
        model_row.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
        model_row.pack_propagate(False)
        ctk.CTkLabel(
            model_row, text="模型名稱", anchor="w",
            font=ctk.CTkFont("SF Pro Text", 14), text_color=TEXT_1,
        ).pack(side="left")
        # 偵測 + 選預設
        self._ollama_model_var = ctk.StringVar(value=self.cfg.ollama_model)
        self._ollama_model_menu_wrap = ctk.CTkFrame(model_row, fg_color="transparent")
        self._ollama_model_menu_wrap.pack(side="right")
        self._build_ollama_model_menu()  # 建 OptionMenu + 刷新按鈕（首次同步偵測）
        # v2.13.0：速度／品質提示（user 反映 12B 慢；3-4B 模型對純錯字校正夠用）
        ctk.CTkLabel(
            ai,
            text=(
                "速度建議（M 系列 Apple Silicon）：\n"
                "• qwen2.5:3b-instruct — 1-2 秒（推薦，中文校正夠用）\n"
                "• gemma3:4b — 1-3 秒（平衡）\n"
                "• gemma3:12b — 3-6 秒（品質優、但體感較慢）\n"
                "找不到模型？終端機跑 `ollama pull <名稱>` 後按 ↻ 重新偵測"
            ),
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
            text_color=TEXT_3,
            justify="left", anchor="w",
        ).pack(anchor="w", padx=SPACE_LG, pady=(0, 10))
        sep_line(ai)

        # Base URL（進階；一般使用者不需要改）
        url_row = ctk.CTkFrame(ai, fg_color="transparent", height=52)
        url_row.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
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
        diag_row.pack(fill="x", padx=SPACE_LG, pady=(8, 4))
        self._ollama_diag_title = ctk.CTkLabel(
            diag_row, text="正在診斷…", anchor="w",
            font=ctk.CTkFont("SF Pro Text", 13, "bold"), text_color=TEXT_1,
        )
        self._ollama_diag_title.pack(fill="x", padx=SPACE_MD, pady=(10, 2))
        self._ollama_diag_detail = ctk.CTkLabel(
            diag_row, text="", anchor="w", justify="left",
            font=ctk.CTkFont("SF Pro Text", 11), text_color=TEXT_3,
            wraplength=520,
        )
        self._ollama_diag_detail.pack(fill="x", padx=SPACE_MD, pady=(0, 4))
        # 建議命令以等寬字 + 一鍵複製
        self._ollama_diag_cmd_frame = ctk.CTkFrame(diag_row, fg_color="transparent")
        self._ollama_diag_cmd_frame.pack(fill="x", padx=SPACE_MD, pady=(0, 10))
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
        test_row.pack(fill="x", padx=SPACE_LG, pady=(4, 8))
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
        hot_row.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
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
        dict_path_row.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
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
        dict_btn_row.pack(fill="x", padx=SPACE_LG, pady=(4, 8))
        dict_btn_row.pack_propagate(False)
        # v2.13.0：動態顯示「目前 N 個 term」讓 user 知道字典規模
        self._dict_status_label = ctk.CTkLabel(
            dict_btn_row, text=self._compute_dict_status(), anchor="w",
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
        # v2.13.0：說明常見同音字消歧使用方式
        ctk.CTkLabel(
            dsec,
            text=(
                "字典術語會注入 Whisper 與 Ollama prompt，提升專有名詞辨識率。\n"
                "例：把「Claude」「Cloud」「Cursor」加進去，可避免同音字誤判（/klɔːd/ vs /klaʊd/）。"
            ),
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
            text_color=TEXT_3,
            justify="left", anchor="w",
        ).pack(anchor="w", padx=SPACE_LG, pady=(0, 10))

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
        retention_row.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
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
            pr.pack(fill="x", padx=SPACE_LG, pady=2)
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
        ).pack(anchor="w", padx=SPACE_LG, pady=(0, 8))

        # 匯入 / 匯出（Phase 4.4）—— 設定 + 字典 + preset 覆寫 → zip
        # 不含 history.db（隱私）與 polish_log.jsonl（debug 用、可能很大）
        ie_row = ctk.CTkFrame(about, fg_color="transparent")
        ie_row.pack(anchor="w", padx=SPACE_LG, pady=(0, 14))
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
        """從表單欄位蒐集所有值、呼叫 cfg.save() 並通知主視窗。"""
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
        # ── Ollama ────────────────────────────────────────────────────────
        self.cfg.ollama_enabled  = self._ollama_enabled_var.get()
        # Bug D（v2.13.0）：貼上策略
        self.cfg.ollama_paste_strategy = self._paste_strategy_var.get()
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
        # ── A3（v2.7.0）動態效果偏好 ────────────────────────────────────
        self.cfg.reduce_motion_pref = self._reduce_motion_var.get()
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
            subprocess.run(["open", str(path)])
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
        row.pack(fill="x", padx=SPACE_LG, pady=SPACE_MD)

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
        entry.pack(side="left", padx=SPACE_XS)
        self._search_var.trace_add("write", lambda *_: self._on_search_changed())

        self._count_label = ctk.CTkLabel(
            row, text="",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 12),
            text_color=TEXT_3,
        )
        self._count_label.pack(side="right", padx=SPACE_SM)

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
        card.pack(fill="x", padx=SPACE_SM, pady=SPACE_XS)

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
        ).pack(side="left", padx=SPACE_MD)

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
        ).pack(side="left", padx=SPACE_XS)

        if entry.polished_text:
            ctk.CTkButton(
                bar, text="複製原文",
                width=110, height=32, corner_radius=8,
                font=ctk.CTkFont("SF Pro Text", 13),
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
            font=ctk.CTkFont("SF Pro Text", 13),
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
            font=ctk.CTkFont("SF Pro Text", 13),
            fg_color=SURF_2, hover_color=SURF_3,
            border_width=1, border_color=SURF_3,
            text_color=TEXT_3,
            command=lambda: self._delete_selected(entry.id),
        ).pack(side="right", padx=SPACE_XS)

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
    """

    WIN_W = 140
    WIN_H = 38
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

        # 點擊任何處 → 把主視窗拉前
        for w in (self, outer, inner, self._dot, self._label, self._timer):
            w.bind("<Button-1>", self._on_click)

        # 預設隱藏；由 AppWindow 透過 .show() 顯示
        self.withdraw()

    # ── 跟游標所在螢幕（Speakly 風格）──────────────────────────────────────
    def _position_at_cursor_screen_bottom(self) -> None:
        """把 HUD 移到游標所在螢幕的中下方，距底部 BOTTOM_MARGIN px。

        多螢幕座標換算：NSScreen.frame 用左下原點、Tk geometry 用左上原點，
        且 origin 不是 (0,0)（副螢幕可能在主螢幕左 / 右 / 上）。
        Tk 的 y 是相對「主螢幕（screens[0]）」的左上座標。
        """
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
        """
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
        self.deiconify()
        self._reapply_panel_level()

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
