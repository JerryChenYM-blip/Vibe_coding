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
from ollama_client import OllamaClient
from recorder import AudioRecorder
from transcriber import Transcriber, TranscriptionResult
from icons import get_icon
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
    ORANGE,                         # legacy alias
    INDIGO, INDIGO_DIM,
    # Waveform
    WAVE_IDLE_COL, WAVE_LIVE_COL,
    # Misc
    SPINNER,
)

WAVE_BARS = 44

# Breathing glow — record button
GLOW_SIZE       = 240   # Canvas size (must exceed button+glow radius)
GLOW_CENTER     = GLOW_SIZE // 2
BTN_RADIUS      = 82    # Button visual radius
GLOW_FPS        = 30
GLOW_TICK_MS    = 1000 // GLOW_FPS


# ─────────────────────────────────────────────────────────────────────────────
#  Colour blending helpers (simulate alpha on tk.Canvas)
# ─────────────────────────────────────────────────────────────────────────────

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _blend(fg_hex: str, bg_hex: str, alpha: float) -> str:
    """Alpha-composite fg over bg. alpha=1.0 → pure fg; alpha=0 → pure bg."""
    alpha = max(0.0, min(1.0, alpha))
    r1, g1, b1 = _hex_to_rgb(fg_hex)
    r2, g2, b2 = _hex_to_rgb(bg_hex)
    r = int(r2 + (r1 - r2) * alpha)
    g = int(g2 + (g1 - g2) * alpha)
    b = int(b2 + (b1 - b2) * alpha)
    return f"#{r:02X}{g:02X}{b:02X}"


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
        self.hotkey_mgr  = HotkeyManager(
            on_press_cb=self._hotkey_press,
            on_release_cb=self._hotkey_release,
        )

        # State
        self._state:       str   = "idle"
        self._hotkey_held: bool  = False
        self._rec_start:   float = 0.0
        self._spin_idx:    int   = 0
        self._wave_phase:  float = 0.0

        # Auto-paste
        self._paste_target: Optional[str] = None

        # Streaming
        self._stream_samples: int       = 0
        self._stream_chunks:  list[str] = []
        self._stream_tick_id            = None

        self._build_ui()
        self._start_hotkey_listener()
        self.after(1500, self._warmup_model)

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
            fg_color=SURF1,
        )
        card.pack(fill="x", padx=16, pady=(14, 6))

        # Waveform
        self._wave_canvas = tk.Canvas(
            card, height=72, bg=SURF1, highlightthickness=0,
        )
        self._wave_canvas.pack(fill="x", padx=24, pady=(20, 8))
        self._draw_idle_wave()

        # Button zone — Canvas hosts the breathing glow; button is overlaid on top
        zone = ctk.CTkFrame(card, fg_color="transparent")
        zone.pack(pady=(4, 6))

        self._glow_canvas = tk.Canvas(
            zone,
            width=GLOW_SIZE, height=GLOW_SIZE,
            bg=SURF1, highlightthickness=0,
        )
        self._glow_canvas.pack()

        self._record_btn = ctk.CTkButton(
            self._glow_canvas,
            text="🎤\n點擊錄音",
            width=BTN_RADIUS * 2, height=BTN_RADIUS * 2,
            corner_radius=BTN_RADIUS,
            fg_color=GREEN,
            hover_color="#28B84A",
            text_color="#FFFFFF",
            font=ctk.CTkFont("SF Pro Text", 14, "bold"),
            border_width=0,
            command=self._on_record_btn,
        )
        self._record_btn.place(x=GLOW_CENTER, y=GLOW_CENTER, anchor="center")

        # Glow animation state
        self._glow_phase: float = 0.0
        self._glow_running: bool = False
        self._start_glow()

        # Auto-paste target label
        self._target_label = ctk.CTkLabel(
            card, text="",
            font=ctk.CTkFont("SF Pro Text", 12),
            text_color=BLUE_HV,
        )
        self._target_label.pack(pady=(4, 2))

        # Hotkey hint
        self._hotkey_hint = ctk.CTkLabel(
            card,
            text=f"按住 {self.cfg.format_hotkey_display()} 即時錄音",
            font=ctk.CTkFont("SF Pro Text", 12),
            text_color=TEXT3,
        )
        self._hotkey_hint.pack(pady=(0, 18))

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
            hover_color="#4745C8" if ap_on else SURF_2,
            command=self._toggle_auto_paste,
        )
        self._ap_btn.pack(side="left", padx=4)

        # Ollama polish
        ollama_ok = self.ollama.is_available()
        self._ollama_btn = ctk.CTkButton(
            row, text="潤飾", width=96,
            image=get_icon("sparkles", icon_size,
                           TEXT_1 if ollama_ok else TEXT_3),
            compound="left",
            state="normal" if ollama_ok else "disabled",
            fg_color=ACCENT if ollama_ok else SURF_1,
            hover_color=ACCENT_HV if ollama_ok else SURF_2,
            border_width=1,
            border_color=ACCENT if ollama_ok else SURF_3,
            text_color=TEXT_1 if ollama_ok else TEXT_3,
            height=32, corner_radius=8,
            font=ctk.CTkFont("SF Pro Text", 13),
            command=self._on_ollama,
        )
        self._ollama_btn.pack(side="left", padx=4)

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

        self._timer_label = ctk.CTkLabel(
            inner, text="",
            font=ctk.CTkFont("SF Pro Text", 12, "bold"),
            text_color=RED,
        )
        self._timer_label.pack(side="right")

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
        self._state       = "recording"
        self._rec_start   = time.perf_counter()
        self._hotkey_held = True
        self._stream_samples = 0
        self._stream_chunks  = []

        # Capture frontmost app for auto-paste
        if self.cfg.auto_paste:
            self._paste_target = _ap.get_frontmost_app()
            if self._paste_target and self._paste_target not in ("Python", "python3"):
                self._target_label.configure(
                    text=f"⌨  → {self._paste_target}"
                )
            else:
                self._paste_target = None
                self._target_label.configure(text="")
        else:
            self._paste_target = None
            self._target_label.configure(text="")

        self.recorder.start()

        self._record_btn.configure(
            text="●\n錄音中…",
            fg_color=RED,
            hover_color="#CC3630",
        )
        self._model_menu.configure(state="disabled")
        self._lang_menu.configure(state="disabled")
        self._status_dot.configure(text_color=RED)
        self._status_label.configure(text="  錄音中")

        self._update_wave()
        self._update_timer()
        self._stream_tick_id = self.after(5000, self._stream_tick)

    def _transition_to_processing(self) -> None:
        if self._state != "recording":
            return
        self._state       = "processing"
        self._hotkey_held = False

        if self._stream_tick_id is not None:
            self.after_cancel(self._stream_tick_id)
            self._stream_tick_id = None

        full_audio = self.recorder.stop()

        self._record_btn.configure(
            text=f"{SPINNER[0]}\n轉錄中…",
            fg_color=ORANGE,
            hover_color=ORANGE,
            state="disabled",
        )
        self._target_label.configure(text="")
        self._status_dot.configure(text_color=ORANGE)
        self._status_label.configure(text="  轉錄中，請稍候…")
        self._timer_label.configure(text="")

        self._draw_idle_wave()
        self._animate_spinner()

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
        self._state = "idle"

        self._record_btn.configure(
            text="🎤\n點擊錄音",
            fg_color=GREEN,
            hover_color="#28B84A",
            state="normal",
        )
        self._model_menu.configure(state="normal")
        self._lang_menu.configure(state="normal")
        self._target_label.configure(text="")

        model = self._model_var.get()
        self._status_dot.configure(text_color=GREEN)
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

    _FAST_THRESHOLD = 8 * 16_000

    def _run_transcription(self, audio, model: str, lang) -> None:
        if len(audio) <= self._FAST_THRESHOLD and not self._stream_chunks:
            result = self.transcriber.transcribe_fast(audio, language=lang)
        else:
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
        valid = text and text not in (
            "（未偵測到語音內容）",
            "（沒有偵測到音訊，請確認麥克風是否正常運作）",
        )

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
        self._textbox.insert("end", result.text)
        self._textbox.see("end")
        self._textbox.configure(state="disabled")

    # ═══════════════════════════════════════════════════════════════════════
    #  ANIMATIONS
    # ═══════════════════════════════════════════════════════════════════════

    # ── Breathing glow (always-on, 30fps) ────────────────────────────────────
    def _start_glow(self) -> None:
        if self._glow_running:
            return
        self._glow_running = True
        self._draw_glow()

    def _draw_glow(self) -> None:
        """Single glow tick. Renders state-aware concentric halo or spinning arc."""
        if not self._glow_running:
            return

        c  = self._glow_canvas
        cx = cy = GLOW_CENTER
        c.delete("glow")

        phase = self._glow_phase

        if self._state == "processing":
            # Rotating conic-arc ring — 12 segments, each fading tail-to-head
            r_outer = BTN_RADIUS + 16        # 98
            rotation = (phase * 4.5) % 360    # ~13.5°/frame → fast spin
            segments  = 12
            seg_ext   = 26
            seg_gap   = 30 - seg_ext
            for i in range(segments):
                alpha = 1.0 - (i / segments) * 0.85   # 1.0 → 0.15 across tail
                col   = _blend(ORANGE, SURF1, alpha * 0.9)
                start = rotation + i * (seg_ext + seg_gap)
                c.create_arc(
                    cx - r_outer, cy - r_outer,
                    cx + r_outer, cy + r_outer,
                    start=start, extent=seg_ext,
                    style="arc", outline=col, width=4,
                    tags="glow",
                )
        else:
            # Breathing halo — 6 concentric layers, outer = dimmer
            if self._state == "recording":
                base         = RED
                speed        = 0.105          # ~2.5s breath cycle
                halo_layers  = 7
                base_radius  = BTN_RADIUS + 6  # start right outside button
                spread       = 26             # max expansion at peak
                strength     = 0.78
            else:  # idle
                base         = GREEN
                speed        = 0.045          # ~6s slow breath
                halo_layers  = 6
                base_radius  = BTN_RADIUS + 6
                spread       = 14
                strength     = 0.42

            # breath = 0.0 (compressed) → 1.0 (expanded)
            breath = 0.5 + 0.5 * math.sin(phase * speed)

            for i in range(halo_layers):
                t = i / (halo_layers - 1)               # 0 (inner) → 1 (outer)
                r = base_radius + t * spread + breath * (spread * 0.35)
                # Alpha: inner ring brightest, outer fades out; breath modulates overall
                layer_alpha = (1.0 - t) ** 1.8          # exponential falloff
                alpha = strength * layer_alpha * (0.55 + 0.45 * breath)
                col   = _blend(base, SURF1, alpha)
                c.create_oval(
                    cx - r, cy - r, cx + r, cy + r,
                    outline=col, width=2,
                    tags="glow",
                )

        self._glow_phase += 1
        self.after(GLOW_TICK_MS, self._draw_glow)

    def _update_wave(self) -> None:
        if self._state != "recording":
            self._draw_idle_wave()
            return
        self._draw_live_wave(self.recorder.get_rms_level())
        self.after(50, self._update_wave)

    def _draw_idle_wave(self) -> None:
        c  = self._wave_canvas
        c.delete("all")
        w  = c.winfo_width() or WIN_W - 80
        h  = 72
        bw = max(2, w // WAVE_BARS - 3)
        gp = w // WAVE_BARS
        for i in range(WAVE_BARS):
            x  = i * gp + gp // 2
            bh = 3 + int(5 * abs(math.sin(i * 0.25)))
            y0 = (h - bh) // 2
            c.create_rectangle(x, y0, x + bw, y0 + bh,
                                fill=WAVE_IDLE_COL, outline="")

    def _draw_live_wave(self, rms: float) -> None:
        import random
        c  = self._wave_canvas
        c.delete("all")
        w  = c.winfo_width() or WIN_W - 80
        h  = 72
        bw = max(2, w // WAVE_BARS - 3)
        gp = w // WAVE_BARS
        self._wave_phase = (self._wave_phase + 0.20) % (2 * math.pi)
        for i in range(WAVE_BARS):
            x    = i * gp + gp // 2
            wave = 0.5 + 0.5 * math.sin(self._wave_phase + i * 0.32)
            jit  = random.uniform(0.88, 1.12)
            bh   = max(3, int((h - 12) * rms * 5 * wave * jit))
            bh   = min(bh, h - 12)
            y0   = (h - bh) // 2
            # White → red-tinted by amplitude
            t   = min(1.0, rms * 7)
            r_c = int(0xF5 + t * (0xFF - 0xF5))
            g_c = int(0xF5 + t * (0x45 - 0xF5))
            b_c = int(0xF7 + t * (0x3A - 0xF7))
            c.create_rectangle(x, y0, x + bw, y0 + bh,
                                fill=f"#{r_c:02X}{g_c:02X}{b_c:02X}", outline="")

    def _animate_spinner(self) -> None:
        if self._state != "processing":
            return
        self._record_btn.configure(
            text=f"{SPINNER[self._spin_idx % len(SPINNER)]}\n轉錄中…"
        )
        self._spin_idx += 1
        self.after(120, self._animate_spinner)

    def _update_timer(self) -> None:
        if self._state != "recording":
            self._timer_label.configure(text="")
            return
        elapsed = int(time.perf_counter() - self._rec_start)
        mm, ss  = divmod(elapsed, 60)
        self._timer_label.configure(text=f"{mm:02d}:{ss:02d}")
        self._record_btn.configure(text=f"●\n錄音中…\n{mm:02d}:{ss:02d}")
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
        self._show_placeholder()

    def _toggle_auto_paste(self) -> None:
        self.cfg.auto_paste = not self.cfg.auto_paste
        self.cfg.save()
        on = self.cfg.auto_paste
        self._ap_btn.configure(
            fg_color=INDIGO if on else SURF_1,
            border_color=INDIGO if on else SURF_3,
            text_color=TEXT_1 if on else TEXT_3,
            hover_color="#4745C8" if on else SURF_2,
            image=get_icon("keyboard", 15, TEXT_1 if on else TEXT_3),
        )
        self._show_toast("自動貼上已開啟" if on else "自動貼上已關閉")

    def _on_ollama(self) -> None:
        text = self._get_result_text()
        if not text:
            return
        self._ollama_btn.configure(state="disabled", text="處理中…")

        def _run():
            result = self.ollama.process(text)
            self.after(0, _done, result)

        def _done(result):
            self._ollama_btn.configure(state="normal", text="潤飾")
            if result.error:
                self._show_toast(f"Ollama 錯誤: {result.error}")
                return
            self._textbox.configure(state="normal")
            self._textbox.delete("1.0", "end")
            self._textbox.insert("end", result.text)
            self._textbox.configure(state="disabled")
            self._show_toast("潤飾完成")

        threading.Thread(target=_run, daemon=True).start()

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
        self.cfg = cfg
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
            hover_color="#4745C8" if on else SURF_2,
            image=get_icon("keyboard", 15, TEXT_1 if on else TEXT_3),
        )

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
                self.transcriber.warmup("small")
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
        self.cfg.save()
        self._on_save_cb(self.cfg)
        self.destroy()


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

    def _start_capture(self) -> None:
        def _cap():
            combo = capture_hotkey(timeout=15)
            if combo:
                self._captured = combo
                self.after(0, self._on_captured, combo)
        threading.Thread(target=_cap, daemon=True).start()

    def _on_captured(self, combo: str) -> None:
        self._detect_label.configure(text=format_hotkey(combo))
        self._apply_btn.configure(state="normal")

    def _apply(self) -> None:
        if self._captured:
            self._on_apply_cb(self._captured)
        self.destroy()


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
