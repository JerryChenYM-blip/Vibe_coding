"""
Main application window — Apple MacBook Pro aesthetic.

Design language:  apple.com/tw/macbook-pro/
Colour system:    forced dark, void-black base + layered surfaces
Typography:       SF Pro Display / Text (macOS native)
Auto-paste:       captures frontmost app before recording, injects ⌘V after STT

Layout (760 × 800 px):
  TopBar      — logo · model / language selectors
  RecordCard  — waveform · circular button · ring indicator
  ResultCard  — transcription textbox
  ActionBar   — action buttons
  StatusBar   — status · hotkey · timer
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
from hotkey_manager import (
    HotkeyManager, capture_hotkey, format_hotkey,
    is_pynput_available, check_accessibility,
)
from ollama_client import OllamaClient, OllamaConfig
from recorder import AudioRecorder
from transcriber import Transcriber, TranscriptionResult
from icons import get_icon, get_canvas_icon
from animation import blend, breathe, ease_in_out_cubic, Ripple
import auto_paste as _ap

# ── Appearance — force Apple dark aesthetic ───────────────────────────────────
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

WIN_W, WIN_H = 760, 800

# ── Design tokens (imported — do not redefine locally) ────────────────────────
# Canonical names + legacy aliases, all sourced from tokens.py
from tokens import (  # noqa: F401  (legacy aliases used across this module)
    # Surfaces
    BG, SURF_1, SURF_2, SURF_3, SURF_4,
    SURF1, SURF2, SURF3,            # legacy aliases
    # Text
    TEXT_1, TEXT_2, TEXT_3, TEXT_4,
    TEXT1, TEXT2, TEXT3,            # legacy aliases
    # Accents
    ACCENT, ACCENT_HV, ACCENT_BG,
    BLUE, BLUE_HV, BLUE_DIM,        # legacy aliases (→ ACCENT)
    SUCCESS, SUCCESS_DIM,
    GREEN, GREEN_DIM,               # legacy aliases
    DANGER, DANGER_DIM,
    RED, RED_DIM,                   # legacy aliases
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
#  Ambient Chamber — geometry & animation constants
# ─────────────────────────────────────────────────────────────────────────────

CHAMBER_SIZE   = 280
CHAMBER_CENTER = CHAMBER_SIZE // 2          # (140, 140)
DISC_RADIUS    = 60                         # central clickable disc

RING_RADII_5   = (80, 96, 112, 128, 140)    # idle / recording
RING_RADII_4   = (80, 100, 120, 140)        # processing (one less = "收斂")
RING_STROKE    = 2

RING_ALPHA_IDLE       = (0.25, 0.18, 0.12, 0.07, 0.03)
RING_ALPHA_RECORDING  = (0.35, 0.24, 0.15, 0.08, 0.04)
RING_ALPHA_PROCESSING = (0.30, 0.18, 0.10, 0.05)

# RMS-driven expansion + ripple emission
RMS_EXPAND_GAIN   = 0.18     # max +18 % radius at full RMS
RMS_RIPPLE_THR    = 0.15     # trigger when rms > 0.15 AND > prev × 1.5
RIPPLE_R0         = 140
RIPPLE_R1         = 180
RIPPLE_DURATION   = 1.2      # seconds
RIPPLE_ALPHA0     = 0.4
RIPPLE_MAX        = 3

# Processing state — rotating particle belt
PROC_PARTICLES        = 12
PROC_PARTICLE_RADIUS  = 4


# ─────────────────────────────────────────────────────────────────────────────
#  Reduced-motion detection (macOS system preference)
# ─────────────────────────────────────────────────────────────────────────────

def system_reduce_motion() -> bool:
    """Read macOS Reduce Motion preference once at app start.

    Changes to the system pref require an app restart per macOS convention.
    Falls back to False on any error (key absent, timeout, non-Darwin).
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
    """Root frame — Apple MacBook Pro aesthetic."""

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
            on_press_cb=self._hotkey_press,
            on_release_cb=self._hotkey_release,
        )

        # State
        self._state:       str   = "idle"
        self._hotkey_held: bool  = False
        self._rec_start:   float = 0.0

        # Auto-paste
        self._paste_target: Optional[str] = None

        # Streaming
        self._stream_samples: int       = 0
        self._stream_chunks:  list[str] = []
        self._stream_tick_id            = None

        # Polish（AI 潤飾）狀態追蹤
        # _polish_generation：每次新轉錄 +1，讓遲到的潤飾結果可以被識別丟棄
        # _last_raw / _last_polished：保留最新一段的兩版本，供未來「切換顯示」
        # _polish_busy：避免「潤飾中」時手動按 潤飾 鈕重複觸發
        self._polish_generation: int              = 0
        self._last_raw:          str              = ""
        self._last_polished:     Optional[str]    = None
        self._polish_busy:       bool             = False
        # toggle chip 狀態：True=顯示潤飾版、False=顯示原文
        # 新一段轉錄進來會重置為 True；沒有潤飾版時兩顆都灰態
        self._showing_polished:  bool             = True

        self._build_ui()
        self._start_hotkey_listener()
        self.after(1500, self._warmup_model)
        # 開啟著的話再去探 Ollama；即使 Ollama 離線也不會卡 UI 建構。
        self.after(2000, self._refresh_ollama_health)

    # ═══════════════════════════════════════════════════════════════════════
    #  BUILD UI
    # ═══════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        self._build_topbar()
        self._build_record_card()
        self._build_result_card()
        self._build_action_bar()
        self._build_status_bar()

    # ── Top bar ──────────────────────────────────────────────────────────────

    def _build_topbar(self) -> None:
        bar = ctk.CTkFrame(self, height=60, corner_radius=0, fg_color=SURF1)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # Thin bottom separator line
        ctk.CTkFrame(self, height=1, fg_color=SURF3, corner_radius=0).pack(fill="x")

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
            text_color=TEXT1,
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
                text_color=TEXT3,
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
                fg_color=SURF2,
                button_color=SURF2,
                button_hover_color=SURF3,
                dropdown_fg_color=SURF1,
                text_color=TEXT1,
                font=ctk.CTkFont("SF Pro Text", 13),
                command=cb,
            )
            menu.pack(side="left")
            setattr(self, menu_attr, menu)

    # ── Record card ──────────────────────────────────────────────────────────

    def _build_record_card(self) -> None:
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

        # Canvas event bindings — 注意：tkinter 的 <Button-1> == <ButtonPress-1>，
        # 不能同時綁兩者，後者會覆蓋前者。一律走 press/release 處理。
        self._chamber.bind("<Enter>",           self._on_chamber_enter)
        self._chamber.bind("<Leave>",           self._on_chamber_leave)
        self._chamber.bind("<Motion>",          self._on_chamber_motion)
        self._chamber.bind("<ButtonPress-1>",   self._on_chamber_press)
        self._chamber.bind("<ButtonRelease-1>", self._on_chamber_release)

        # Timer — visible only during recording, SF Mono for tabular digits
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

        # Hotkey hint (text changes per state)
        self._hotkey_hint = ctk.CTkLabel(
            card,
            text=f"按下 {self.cfg.format_hotkey_display()} 即時錄音",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
            text_color=TEXT_3,
        )
        self._hotkey_hint.pack(pady=(0, SPACE_MD + 2))

        # Pre-render icons for each state (tk.Canvas needs PhotoImage, not CTkImage)
        self._icon_mic_idle = get_canvas_icon("mic",    36, TEXT_2)
        self._icon_square   = get_canvas_icon("square", 28, TEXT_1)
        self._icon_mic_proc = get_canvas_icon("mic",    36, blend(WARN, SURF_2, 0.6))

        # Chamber animation state
        self._state_start_time = time.perf_counter()
        self._ripples: list[Ripple] = []
        self._prev_rms         = 0.0
        self._pressed          = False
        self._hovering         = False
        self._reduce_motion    = system_reduce_motion()

        # Kick off the render loop (runs for the life of the window)
        self._render_tick()

    # ── Result card ──────────────────────────────────────────────────────────

    def _build_result_card(self) -> None:
        card = ctk.CTkFrame(
            self, corner_radius=16,
            fg_color=SURF1,
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
        ctk.CTkFrame(card, height=1, fg_color=SURF3, corner_radius=0).pack(
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
            text_color=TEXT1,
            state="disabled",
        )
        self._textbox.pack(fill="both", expand=True, padx=8, pady=(4, 10))
        self._show_placeholder()

    # ── Action bar ───────────────────────────────────────────────────────────

    def _build_action_bar(self) -> None:
        # Top separator
        ctk.CTkFrame(self, height=1, fg_color=SURF3, corner_radius=0).pack(fill="x")

        bar = ctk.CTkFrame(self, height=58, corner_radius=0, fg_color="transparent")
        bar.pack(fill="x")
        bar.pack_propagate(False)

        row = ctk.CTkFrame(bar, fg_color="transparent")
        row.pack(expand=True, pady=13)

        # Shared style for ghost buttons
        ghost = dict(
            height=32, corner_radius=8,
            font=ctk.CTkFont("SF Pro Text", 13),
            fg_color=SURF1,
            border_width=1, border_color=SURF3,
            text_color=TEXT2,
            hover_color=SURF2,
        )

        # Ghost buttons use TEXT_2-tinted icons
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

        # Ollama polish —— 初始以 cfg 為準，不在此做網路探測（會阻塞 UI）。
        # 實際連線狀態由 _refresh_ollama_health() 於啟動 2s 後非同步更新。
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
            row, text="設定", width=96,
            image=get_icon("settings", icon_size, TEXT_2),
            compound="left",
            command=self._open_settings, **ghost,
        ).pack(side="left", padx=4)

    # ── Status bar ───────────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        ctk.CTkFrame(self, height=1, fg_color=SURF3, corner_radius=0).pack(fill="x")
        bar = ctk.CTkFrame(self, height=32, corner_radius=0, fg_color=SURF1)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=6)

        self._status_dot = ctk.CTkLabel(
            inner, text="●", text_color=GREEN,
            font=ctk.CTkFont(size=8),
        )
        self._status_dot.pack(side="left")

        self._status_label = ctk.CTkLabel(
            inner, text="  模型載入中…",
            font=ctk.CTkFont("SF Pro Text", 12),
            text_color=TEXT3,
        )
        self._status_label.pack(side="left")

        # Timer is rendered under the chamber (see _build_record_card); status
        # bar no longer carries a redundant mm:ss label.

        self._hotkey_status = ctk.CTkLabel(
            inner, text=self.cfg.format_hotkey_display(),
            font=ctk.CTkFont("SF Pro Text", 12),
            text_color=TEXT3,
        )
        self._hotkey_status.pack(side="right", padx=16)

    # ═══════════════════════════════════════════════════════════════════════
    #  STATE MACHINE
    # ═══════════════════════════════════════════════════════════════════════

    def _transition_to_recording(self) -> None:
        if self._state != "idle":
            return
        self._state            = "recording"
        self._state_start_time = time.perf_counter()
        self._rec_start        = self._state_start_time
        self._hotkey_held      = True
        self._stream_samples   = 0
        self._stream_chunks    = []
        self._ripples.clear()
        self._prev_rms         = 0.0

        # Capture frontmost app for auto-paste —— 背景執行緒執行，避免 2 秒
        # osascript timeout 卡住 recorder.start()。短錄音也夠時間在轉錄前完成。
        self._paste_target = None
        self._target_label.configure(text="")
        if self.cfg.auto_paste:
            def _capture_frontmost():
                app = _ap.get_frontmost_app()
                if not app or app in ("Python", "python3"):
                    return
                def _apply():
                    if self._state == "recording":   # 仍在錄，才更新
                        self._paste_target = app
                        self._target_label.configure(text=f"→ {app}")
                self.after(0, _apply)
            threading.Thread(target=_capture_frontmost, daemon=True).start()

        self.recorder.start()

        # UI: chamber render loop picks up new state automatically
        self._timer_label.configure(text="00:00")
        self._hotkey_hint.configure(
            text=f"放開 {self.cfg.format_hotkey_display()} 停止錄音"
        )
        self._model_menu.configure(state="disabled")
        self._lang_menu.configure(state="disabled")
        self._status_dot.configure(text_color=DANGER)
        self._status_label.configure(text="  錄音中")

        self._update_timer()
        # Streaming 中段轉錄目前暫停：small 模型品質拖累主模型最終輸出，等 Ollama
        # 接上後再評估是否重啟。_stream_tick 方法保留備用。
        self._stream_tick_id = None

    def _transition_to_processing(self) -> None:
        if self._state != "recording":
            return
        self._state            = "processing"
        self._state_start_time = time.perf_counter()
        self._hotkey_held      = False

        if self._stream_tick_id is not None:
            self.after_cancel(self._stream_tick_id)
            self._stream_tick_id = None

        full_audio = self.recorder.stop()

        # UI: chamber render loop will switch to WARN palette + particle belt
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

    def _transition_to_idle(self, result: Optional[TranscriptionResult] = None) -> None:
        self._state            = "idle"
        self._state_start_time = time.perf_counter()
        self._ripples.clear()

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

    def _on_transcription_done(self, result: TranscriptionResult) -> None:
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
        self._last_raw       = text if valid else ""
        self._last_polished  = None
        self._showing_polished = True          # 預設顯示潤飾版（有的話）
        self._apply_toggle_style()             # 新結果暫無潤飾版 → 兩顆灰

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
            threading.Thread(
                target=self._do_auto_paste,
                args=(text, target),
                daemon=True,
            ).start()

    # ── 潤飾管線 ────────────────────────────────────────────────────────────

    def _start_polish(self, gen: int, raw_text: str, target: Optional[str]) -> None:
        """啟動背景潤飾；完成時將於主執行緒回呼 _finish_polish。"""
        self._polish_busy = True
        self._append_result_title_suffix("  · 潤飾中…")

        def _run():
            resp = self.ollama.process(raw_text)
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
            # 降級：提示錯誤，title 標示「原文」，auto-paste 貼原文
            self._replace_result_title_suffix(f"  · 原文（潤飾失敗）")
            self._show_toast(f"AI 潤飾失敗：{resp.error}")
            paste_text = raw_text
        else:
            # 成功：textbox 的最新一段用潤飾版替換
            polished = resp.text
            self._last_polished = polished
            self._replace_latest_with(polished, expect_current=raw_text)
            self._showing_polished = True
            self._apply_toggle_style()          # 現在有潤飾版了 → 兩顆點亮
            self._replace_result_title_suffix(
                f"  · 已潤飾 · {resp.elapsed_seconds:.1f}s"
            )
            self._show_toast(f"AI 潤飾完成 · {resp.elapsed_seconds:.1f}s")

            # 剪貼簿覆蓋成潤飾版（如果開啟 auto_copy）
            if self.cfg.auto_copy:
                try:
                    import pyperclip
                    pyperclip.copy(polished)
                except Exception:
                    pass
            paste_text = polished

        # 自動貼上（策略 B：等潤飾完才貼）
        if target:
            threading.Thread(
                target=self._do_auto_paste,
                args=(paste_text, target),
                daemon=True,
            ).start()

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

    def _append_result_title_suffix(self, suffix: str) -> None:
        """在結果標題尾端加上狀態說明（不重複累加）。"""
        cur = self._result_title.cget("text")
        # 若已存在「·」開頭的尾綴，先移除再接
        base = cur.split("  ·", 1)[0] if "  ·" in cur else cur
        # 保留原本的括號資訊，只在最後加 suffix
        self._result_title.configure(text=base + suffix if "  ·" in cur else cur + suffix)

    def _replace_result_title_suffix(self, suffix: str) -> None:
        """用新的尾綴覆蓋『潤飾中…』之類的臨時提示。"""
        cur = self._result_title.cget("text")
        # 砍掉所有「  ·」之後的部分（即第一段之後）
        base = cur.split("  ·", 1)[0]
        # 但原本 _display_result 寫的 "轉錄結果 (Xs · lang · model)" 本身就有 "·"
        # 那個分隔寫在括號內，不會被這個 split 吃到（split 的是 "  ·"，有兩空格）
        self._result_title.configure(text=base + suffix)

    def _do_auto_paste(self, text: str, target: str) -> None:
        success = _ap.paste_to_app(text, target)
        if success:
            self.after(0, lambda: self._show_toast(f"⌨  已貼入 {target}"))
        else:
            self.after(0, lambda: self._show_toast("⌨  自動貼上失敗（請確認輔助使用權限）"))

    def _display_result(self, result: TranscriptionResult) -> None:
        dur   = int(result.duration_seconds)
        lang  = result.language.upper() if result.language else "?"
        model = self._model_var.get()
        self._result_title.configure(
            text=f"轉錄結果  ({dur}s · {lang} · {model})"
        )
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
        """Main render loop — 50 ms cadence, owns the chamber canvas."""
        try:
            self._draw_chamber()
        except tk.TclError:
            # Canvas destroyed during app shutdown — stop the loop silently.
            return
        self.after(RENDER_TICK_MS, self._render_tick)

    def _draw_chamber(self) -> None:
        """Render ambient rings + central disc + icon for the current state."""
        now = time.perf_counter()
        c   = self._chamber
        c.delete("all")

        state = self._state
        rm    = self._reduce_motion

        # ─── State-dependent palette and geometry ────────────────────────
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

        # ─── Breathing scale ─────────────────────────────────────────────
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

        # ─── Ripples (recording, non-reduced-motion) ─────────────────────
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

        # ─── Ambient concentric rings (outside-in so inner draws on top) ─
        for radius, a in zip(reversed(radii), reversed(alphas)):
            r = radius * scale
            col = blend(color, SURF_1, a)
            c.create_oval(
                cx - r, cy - r, cx + r, cy + r,
                outline=col, width=RING_STROKE,
            )

        # ─── Processing rotating particle belt ───────────────────────────
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
                # Brightness gradient: head particle brightest → fades around belt
                idx_from_head = (i % PROC_PARTICLES) / PROC_PARTICLES
                a_p = 1.0 - idx_from_head * 0.85              # 1.0 → 0.15
                if rm:
                    a_p = 0.6
                col_p = blend(WARN, SURF_1, a_p)
                c.create_oval(
                    px - pr, py - pr, px + pr, py + pr,
                    fill=col_p, outline="",
                )

        # ─── Central disc ────────────────────────────────────────────────
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

        # ─── Central icon ────────────────────────────────────────────────
        if state == "idle":
            icon = self._icon_mic_idle
        elif state == "recording":
            icon = self._icon_square
        else:
            icon = self._icon_mic_proc

        c.create_image(cx, cy, image=icon)

    # ── Canvas event handlers ────────────────────────────────────────────

    def _in_disc(self, x: int, y: int) -> bool:
        dx = x - CHAMBER_CENTER
        dy = y - CHAMBER_CENTER
        return dx * dx + dy * dy <= DISC_RADIUS * DISC_RADIUS

    def _on_chamber_enter(self, event) -> None:
        self._hovering = self._in_disc(event.x, event.y) and self._state == "idle"

    def _on_chamber_leave(self, event) -> None:
        self._hovering = False
        self._pressed  = False
        try:
            self._chamber.configure(cursor="")
        except tk.TclError:
            pass

    def _on_chamber_motion(self, event) -> None:
        inside = self._in_disc(event.x, event.y)
        self._hovering = inside and self._state == "idle"
        try:
            self._chamber.configure(cursor="hand2" if self._hovering else "")
        except tk.TclError:
            pass

    def _on_chamber_press(self, event) -> None:
        # idle → 即將開始錄音；recording → 即將停止（轉 processing）
        if self._in_disc(event.x, event.y) and self._state in ("idle", "recording"):
            self._pressed = True
        print(f"CHAMBER: press  at ({event.x},{event.y}) state={self._state} pressed={self._pressed}")

    def _on_chamber_release(self, event) -> None:
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
        if self._state != "recording":
            self._timer_label.configure(text="")
            return
        elapsed = int(time.perf_counter() - self._rec_start)
        mm, ss  = divmod(elapsed, 60)
        self._timer_label.configure(text=f"{mm:02d}:{ss:02d}")
        self.after(1000, self._update_timer)

    # ═══════════════════════════════════════════════════════════════════════
    #  EVENT HANDLERS
    # ═══════════════════════════════════════════════════════════════════════

    def _on_record_btn(self) -> None:
        if   self._state == "idle":      self._transition_to_recording()
        elif self._state == "recording": self._transition_to_processing()

    def _hotkey_press(self) -> None:
        self.after(0, self._on_hotkey_press)

    def _hotkey_release(self) -> None:
        self.after(0, self._on_hotkey_release)

    def _on_hotkey_press(self) -> None:
        if self._state == "idle" and not self._hotkey_held:
            self._transition_to_recording()

    def _on_hotkey_release(self) -> None:
        if self._state == "recording":
            self._transition_to_processing()

    def _on_copy(self) -> None:
        text = self._get_result_text()
        if not text:
            return
        try:
            import pyperclip
            pyperclip.copy(text)
            self._show_toast("已複製到剪貼簿")
        except Exception as e:
            self._show_toast(f"複製失敗: {e}")

    def _on_save(self) -> None:
        text = self._get_result_text()
        if not text:
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
                self._show_toast("已儲存")
            except Exception as e:
                self._show_toast(f"儲存失敗: {e}")

    def _on_clear(self) -> None:
        self._textbox.configure(state="normal")
        self._textbox.delete("1.0", "end")
        self._textbox.configure(state="disabled")
        self._result_title.configure(text="轉錄結果")
        # 重置 toggle 狀態：清空後無最新段可切
        self._last_raw = ""
        self._last_polished = None
        self._showing_polished = True
        self._apply_toggle_style()
        self._show_placeholder()

    def _toggle_auto_paste(self) -> None:
        self.cfg.auto_paste = not self.cfg.auto_paste
        self.cfg.save()
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
            self._show_toast("AI 潤飾未啟用，請到「設定」開啟")
            return
        if self.ollama.health_ok is False:
            self._show_toast("無法連線 Ollama 服務，請確認 ollama serve 已啟動")
            # 重新探一次，下次點擊就能反映最新狀態
            self._refresh_ollama_health()
            return
        if self._polish_busy:
            self._show_toast("目前已在潤飾中，請稍候…")
            return

        text = self._get_result_text()
        if not text:
            return

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
        self.cfg.model = value
        self.cfg.save()
        self._status_label.configure(text=f"  就緒 ({value})")

    def _on_language_change(self, value: str) -> None:
        self.cfg.language = value
        self.cfg.save()

    # ═══════════════════════════════════════════════════════════════════════
    #  SETTINGS
    # ═══════════════════════════════════════════════════════════════════════

    def _open_settings(self) -> None:
        SettingsWindow(self, self.cfg, self._on_settings_saved)

    def _on_settings_saved(self, cfg: Config) -> None:
        old_hotkey = self.cfg.hotkey
        self.cfg = cfg
        # 只有 hotkey 真正變更才重啟 pynput listener——反覆 stop/start 在 macOS
        # 上曾與 MLX/Metal 並存時觸發 native 層不穩定，能避免就避免。
        if cfg.hotkey != old_hotkey:
            self._start_hotkey_listener()
        self._hotkey_hint.configure(text=f"按住 {cfg.format_hotkey_display()} 即時錄音")
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

    # ═══════════════════════════════════════════════════════════════════════
    #  HELPERS
    # ═══════════════════════════════════════════════════════════════════════

    def _start_hotkey_listener(self) -> None:
        if not is_pynput_available():
            return
        self.hotkey_mgr.restart(self.cfg.hotkey)

    def _warmup_model(self) -> None:
        model = self._model_var.get()

        def _load():
            try:
                self.transcriber.warmup(model)
                backend = self.transcriber.active_backend()
                label   = "⚡ Metal" if backend == "mlx" else "CPU"
                self.after(0, lambda: self._status_label.configure(
                    text=f"  就緒 ({model} · {label})"
                ))
                self.after(0, lambda: self._status_dot.configure(text_color=GREEN))
            except Exception:
                self.after(0, lambda: self._status_label.configure(text="  模型載入失敗"))
                self.after(0, lambda: self._status_dot.configure(text_color=RED))

        threading.Thread(target=_load, daemon=True).start()

    def _show_placeholder(self) -> None:
        self._textbox.configure(state="normal")
        if not self._textbox.get("1.0", "end").strip():
            self._textbox.insert("1.0", "（等待第一次錄音...）")
        self._textbox.configure(state="disabled")

    def _get_result_text(self) -> str:
        t = self._textbox.get("1.0", "end").strip()
        return "" if t == "（等待第一次錄音...）" else t

    def _show_toast(self, message: str) -> None:
        toast = ctk.CTkFrame(
            self, corner_radius=10,
            fg_color=SURF2,
            border_width=1, border_color=SURF3,
        )
        ctk.CTkLabel(
            toast, text=message,
            font=ctk.CTkFont("SF Pro Text", 13),
            text_color=TEXT1, padx=18, pady=10,
        ).pack()
        toast.place(relx=1.0, rely=1.0, x=-20, y=-52, anchor="se")
        self.after(2800, toast.destroy)

    def on_close(self) -> None:
        self.hotkey_mgr.stop()
        if self.recorder.is_recording():
            self.recorder.stop()


# ─────────────────────────────────────────────────────────────────────────────
#  SETTINGS WINDOW
# ─────────────────────────────────────────────────────────────────────────────

class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, parent, cfg: Config, on_save_cb) -> None:
        super().__init__(parent)
        self.cfg         = Config(**cfg.__dict__)
        self._on_save_cb = on_save_cb
        self.title("設定")
        self.geometry("440x580")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.grab_set()
        self._build()

    def _build(self) -> None:
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        def section(title: str) -> ctk.CTkFrame:
            ctk.CTkLabel(
                scroll, text=title.upper(),
                font=ctk.CTkFont("SF Pro Text", 11),
                text_color=TEXT3, anchor="w",
            ).pack(fill="x", padx=20, pady=(22, 6))
            f = ctk.CTkFrame(
                scroll, corner_radius=12,
                fg_color=SURF1,
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
                text_color=TEXT1,
            ).pack(side="left")
            widget_fn(r)

        def sep_line(parent) -> None:
            ctk.CTkFrame(parent, height=1, fg_color=SURF3).pack(
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
                fg_color=SURF2, button_color=SURF2,
                button_hover_color=SURF3,
                dropdown_fg_color=SURF1,
                text_color=TEXT1,
                font=ctk.CTkFont("SF Pro Text", 13),
                command=self._on_model_preview,
            ).pack(side="right")

        row(stt, "模型大小", model_row)
        self._model_desc = ctk.CTkLabel(
            stt, text=MODEL_INFO.get(self.cfg.model, ""),
            font=ctk.CTkFont("SF Pro Text", 11),
            text_color=TEXT3,
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
                fg_color=SURF2, button_color=SURF2,
                button_hover_color=SURF3,
                dropdown_fg_color=SURF1,
                text_color=TEXT1,
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
            font=ctk.CTkFont("SF Pro Text", 14), text_color=TEXT1,
        ).pack(side="left")

        hk_r = ctk.CTkFrame(hk_row, fg_color="transparent")
        hk_r.pack(side="right")

        self._hk_label = ctk.CTkLabel(
            hk_r, text=format_hotkey(self.cfg.hotkey),
            font=ctk.CTkFont("SF Pro Text", 13, "bold"),
            fg_color=SURF2, text_color=TEXT1,
            corner_radius=8, padx=12, pady=4,
        )
        self._hk_label.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            hk_r, text="重新綁定", width=80, height=28, corner_radius=8,
            fg_color=BLUE_DIM,
            hover_color=BLUE,
            border_width=1, border_color=BLUE,
            text_color=BLUE_HV,
            font=ctk.CTkFont("SF Pro Text", 12),
            command=self._rebind_hotkey,
        ).pack(side="left")

        # ── 輸出偏好 ──────────────────────────────────────────────────────
        out = section("輸出偏好")
        self._append_var    = ctk.BooleanVar(value=self.cfg.append_results)
        self._autocopy_var  = ctk.BooleanVar(value=self.cfg.auto_copy)
        self._autopaste_var = ctk.BooleanVar(value=self.cfg.auto_paste)

        sw_style = dict(
            progress_color=BLUE,
            button_color=TEXT1,
            button_hover_color=TEXT2,
            fg_color=SURF3,
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
            font=ctk.CTkFont("SF Pro Text", 14), text_color=TEXT1,
        ).pack(side="left")
        self._ollama_model_var = ctk.StringVar(value=self.cfg.ollama_model)
        ctk.CTkEntry(
            model_row, textvariable=self._ollama_model_var,
            width=200, height=30, corner_radius=8,
            fg_color=SURF2, border_color=SURF3,
            text_color=TEXT1,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 12),
        ).pack(side="right")
        sep_line(ai)

        # Base URL（進階；一般使用者不需要改）
        url_row = ctk.CTkFrame(ai, fg_color="transparent", height=52)
        url_row.pack(fill="x", padx=16, pady=4)
        url_row.pack_propagate(False)
        ctk.CTkLabel(
            url_row, text="服務位址", anchor="w",
            font=ctk.CTkFont("SF Pro Text", 14), text_color=TEXT1,
        ).pack(side="left")
        self._ollama_url_var = ctk.StringVar(value=self.cfg.ollama_base_url)
        ctk.CTkEntry(
            url_row, textvariable=self._ollama_url_var,
            width=200, height=30, corner_radius=8,
            fg_color=SURF2, border_color=SURF3,
            text_color=TEXT3,
            font=ctk.CTkFont(FONT_FAMILY_MONO, 11),
        ).pack(side="right")
        sep_line(ai)

        # 測試連線 + 狀態標籤
        test_row = ctk.CTkFrame(ai, fg_color="transparent", height=52)
        test_row.pack(fill="x", padx=16, pady=(4, 8))
        test_row.pack_propagate(False)
        self._ollama_test_status = ctk.CTkLabel(
            test_row, text="（尚未測試）",
            anchor="w",
            font=ctk.CTkFont("SF Pro Text", 12), text_color=TEXT3,
        )
        self._ollama_test_status.pack(side="left")
        ctk.CTkButton(
            test_row, text="測試連線", width=100, height=30, corner_radius=8,
            fg_color=SURF2, text_color=TEXT1,
            hover_color=SURF3,
            border_width=1, border_color=SURF3,
            font=ctk.CTkFont("SF Pro Text", 12),
            command=self._test_ollama,
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
                         text_color=TEXT1).pack(side="left")
            ctk.CTkLabel(pr, text=path, anchor="e",
                         font=ctk.CTkFont("SF Pro Text", 11),
                         text_color=TEXT3).pack(side="right")

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
        ).pack(anchor="w", padx=16, pady=(0, 14))

        # ── Buttons ───────────────────────────────────────────────────────
        ctk.CTkFrame(self, height=1, fg_color=SURF3, corner_radius=0).pack(fill="x")
        btn_bar = ctk.CTkFrame(self, height=60, fg_color=SURF1, corner_radius=0)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)

        inner = ctk.CTkFrame(btn_bar, fg_color="transparent")
        inner.pack(side="right", padx=20, pady=12)

        ctk.CTkButton(
            inner, text="取消", width=88, height=36, corner_radius=8,
            fg_color=SURF2, text_color=TEXT2,
            hover_color=SURF3,
            border_width=1, border_color=SURF3,
            font=ctk.CTkFont("SF Pro Text", 14),
            command=self.destroy,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            inner, text="儲存", width=88, height=36, corner_radius=8,
            fg_color=BLUE, hover_color=BLUE_HV,
            text_color=TEXT1,
            font=ctk.CTkFont("SF Pro Text", 14, "bold"),
            command=self._save,
        ).pack(side="left")

    def _on_model_preview(self, value: str) -> None:
        self._model_desc.configure(text=MODEL_INFO.get(value, ""))

    def _rebind_hotkey(self) -> None:
        HotkeyBindDialog(self, self.cfg.hotkey, self._apply_hotkey).focus()

    def _apply_hotkey(self, combo: str) -> None:
        self.cfg.hotkey = combo
        self._hk_label.configure(text=format_hotkey(combo))

    def _save(self) -> None:
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
        self.cfg.save()
        self._on_save_cb(self.cfg)
        self.destroy()

    def _test_ollama(self) -> None:
        """於設定視窗中測試當前輸入的 base_url + model 是否可用。

        不寫回 cfg（使用者按「取消」就應丟棄），只用暫時的 OllamaClient probe。
        """
        from ollama_client import OllamaClient, OllamaConfig

        url   = self._ollama_url_var.get().strip() or "http://localhost:11434"
        model = self._ollama_model_var.get().strip() or "qwen2.5:3b-instruct"

        self._ollama_test_status.configure(text="測試中…", text_color=TEXT3)

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


# ─────────────────────────────────────────────────────────────────────────────
#  HOTKEY BIND DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class HotkeyBindDialog(ctk.CTkToplevel):
    def __init__(self, parent, current_combo: str, on_apply_cb) -> None:
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
        ctk.CTkLabel(
            self,
            text="請按下想要的快捷鍵組合\n（按下後鬆開確認）",
            font=ctk.CTkFont("SF Pro Text", 14),
            text_color=TEXT2,
            justify="center",
        ).pack(pady=(28, 12))

        self._detect_label = ctk.CTkLabel(
            self, text="等待按鍵…",
            font=ctk.CTkFont("SF Pro Display", 20, "bold"),
            fg_color=SURF2, text_color=TEXT1,
            corner_radius=10, padx=24, pady=12,
        )
        self._detect_label.pack(pady=4)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(pady=16)

        ctk.CTkButton(
            bar, text="取消", width=90, height=32, corner_radius=8,
            fg_color=SURF2, text_color=TEXT2,
            hover_color=SURF3,
            border_width=1, border_color=SURF3,
            font=ctk.CTkFont("SF Pro Text", 13),
            command=self.destroy,
        ).pack(side="left", padx=6)

        self._apply_btn = ctk.CTkButton(
            bar, text="確認套用", width=90, height=32, corner_radius=8,
            state="disabled",
            fg_color=BLUE, hover_color=BLUE_HV,
            text_color=TEXT1,
            font=ctk.CTkFont("SF Pro Text", 13, "bold"),
            command=self._apply,
        )
        self._apply_btn.pack(side="left", padx=6)

    # ── 按鍵擷取（Tk 原生，不用 pynput） ────────────────────────────────
    # 為什麼不用 pynput：macOS 26.4+ 把 TSMGetInputSourceProperty 改成「僅
    # 限主執行緒」的硬斷言。pynput Listener 在背景執行緒透過 ctypes 呼叫
    # TSM 解析鍵碼 → SIGTRAP 閃退。主 HotkeyManager 啟動早、流程穩沒炸；
    # 但此處 capture_hotkey() 新開 Listener 必炸。
    # 解法：改用 Tk 的 <KeyPress>/<KeyRelease> binding，事件在主執行緒回
    # 調，完全不碰 TSM。唯一代價是對話框必須持續擁有鍵盤焦點（grab_set
    # 已保證）。
    def _start_capture(self) -> None:
        self._current_keys: set[str] = set()
        self._max_combo:    set[str] = set()
        # 對話框必須搶到焦點，否則 KeyPress 事件不會進來
        self.focus_force()
        self.bind("<KeyPress>",   self._on_tk_key_press)
        self.bind("<KeyRelease>", self._on_tk_key_release)
        self._capture_timeout_id = self.after(15_000, self._on_capture_timeout)

    def _unbind_capture(self) -> None:
        try:
            self.unbind("<KeyPress>")
            self.unbind("<KeyRelease>")
        except tk.TclError:
            pass
        if getattr(self, "_capture_timeout_id", None):
            try:
                self.after_cancel(self._capture_timeout_id)
            except Exception:
                pass
            self._capture_timeout_id = None

    def _on_tk_key_press(self, event) -> None:
        if self._captured:
            return
        name = self._keysym_to_name(event.keysym)
        if not name:
            return
        self._current_keys.add(name)
        if len(self._current_keys) > len(self._max_combo):
            self._max_combo = set(self._current_keys)
        # 即時預覽使用者目前按住的組合
        self._detect_label.configure(text=format_hotkey(self._combo_str(self._max_combo)))

    def _on_tk_key_release(self, event) -> None:
        if self._captured:
            return
        if self._max_combo:
            combo = self._combo_str(self._max_combo)
            # 過濾掉純修飾鍵的組合（例如只按 ⌘⌥ 沒接字母）
            if any(k not in ("cmd", "ctrl", "alt", "shift") for k in self._max_combo):
                self._captured = combo
                self._unbind_capture()
                self._on_captured(combo)
                return
        name = self._keysym_to_name(event.keysym)
        if name:
            self._current_keys.discard(name)

    def _on_capture_timeout(self) -> None:
        self._capture_timeout_id = None
        if not self._captured:
            self._detect_label.configure(text="逾時，請重新開啟")
            self._unbind_capture()

    @staticmethod
    def _keysym_to_name(keysym: str) -> Optional[str]:
        """Tk keysym → 我們的 combo 命名（'cmd' / 'alt' / 'r' …）。

        回 None 表示此鍵不納入 combo（避免 F-keys、方向鍵等產生奇怪 combo）。
        """
        ks = keysym.lower()
        mod_map = {
            "meta_l": "cmd",   "meta_r": "cmd",
            "command_l": "cmd","command_r": "cmd",
            "super_l": "cmd",  "super_r": "cmd",  # 非 Mac 備援
            "control_l": "ctrl","control_r": "ctrl",
            "alt_l": "alt",    "alt_r": "alt",
            "option_l": "alt", "option_r": "alt",
            "shift_l": "shift","shift_r": "shift",
            "space": "space",
        }
        if ks in mod_map:
            return mod_map[ks]
        if len(ks) == 1 and ks.isalnum():
            return ks
        return None

    @staticmethod
    def _combo_str(keys: set[str]) -> str:
        order = ["cmd", "ctrl", "alt", "shift"]
        mods    = [m for m in order if m in keys]
        letters = sorted(k for k in keys if k not in order)
        return "+".join(mods + letters)

    def _on_captured(self, combo: str) -> None:
        self._detect_label.configure(text=format_hotkey(combo))
        self._apply_btn.configure(state="normal")

    def _apply(self) -> None:
        if self._captured:
            self._on_apply_cb(self._captured)
        self.destroy()

    def destroy(self) -> None:
        # 確保對話框關閉時解除綁定與 timer，避免 Tcl 錯誤訊息
        self._unbind_capture()
        super().destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  ACCESSIBILITY DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class AccessibilityDialog(ctk.CTkToplevel):
    def __init__(self, parent) -> None:
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
            text_color=TEXT2,
            justify="left",
        ).pack(padx=32, pady=4)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(pady=20)

        ctk.CTkButton(
            bar, text="跳過", width=110, height=34, corner_radius=8,
            fg_color=SURF2, text_color=TEXT2,
            hover_color=SURF3,
            border_width=1, border_color=SURF3,
            font=ctk.CTkFont("SF Pro Text", 13),
            command=self.destroy,
        ).pack(side="left", padx=6)

        ctk.CTkButton(
            bar, text="開啟系統設定", width=130, height=34, corner_radius=8,
            fg_color=BLUE, hover_color=BLUE_HV,
            text_color=TEXT1,
            font=ctk.CTkFont("SF Pro Text", 13, "bold"),
            command=self._open_prefs,
        ).pack(side="left", padx=6)

    def _open_prefs(self) -> None:
        subprocess.run([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def cfg_val(v):
    """Return value as-is (helper to keep topbar loop readable)."""
    return v
