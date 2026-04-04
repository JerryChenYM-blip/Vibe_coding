"""
Main application window — Apple design language.

Colour system:  authentic macOS/iOS system palette
Glass effect:   simulated with layered surfaces (#F2F2F7 bg → #FFFFFF card)
Auto-paste:     captures frontmost app before recording, injects ⌘V after STT

Layout (740 × 740 px):
  TopBar      — logo · model / language pills
  RecordCard  — waveform · circular button · ring indicator
  ResultCard  — transcription textbox (expands)
  ActionBar   — pill action buttons
  StatusBar   — status · auto-paste badge · hotkey · timer
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
import auto_paste as _ap

# ── Appearance ────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

WIN_W, WIN_H = 740, 740

# ── Apple system colour palette ───────────────────────────────────────────────
# Tuples = (light, dark)  |  strings = mode-independent

# Backgrounds
SYS_BG   = ("#F2F2F7", "#000000")   # iOS groupedBackground
CARD     = ("#FFFFFF",  "#1C1C1E")   # iOS secondaryGroupedBackground
CARD2    = ("#F2F2F7",  "#2C2C2E")   # inset / secondary card
SEP      = ("#C6C6C8",  "#38383A")   # separator

# Text
LABEL    = ("#000000",  "#FFFFFF")    # primary label
LABEL2   = ("#3C3C43",  "#EBEBF5")   # secondary label (60% opacity approx)
LABEL3   = ("#8E8E93",  "#8E8E93")   # tertiary / hint

# System tints (iOS)
BLUE     = ("#007AFF",  "#0A84FF")
BLUE_BG  = ("#E5F1FF",  "#0A2540")
GREEN    = ("#34C759",  "#30D158")
GREEN_BG = ("#E3F9EC",  "#012B17")
RED      = ("#FF3B30",  "#FF453A")
RED_BG   = ("#FFE5E4",  "#3A0000")
ORANGE   = ("#FF9500",  "#FF9F0A")
INDIGO   = ("#5856D6",  "#5E5CE6")

# Waveform idle bars
WAVE_IDLE = ("#D1D1D6", "#38383A")

SPINNER  = ["⠋", "⠙", "⠸", "⠴", "⠦", "⠇"]
WAVE_BARS = 42


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dark() -> bool:
    return ctk.get_appearance_mode() == "Dark"


def _card_hex() -> str:
    return CARD[1] if _dark() else CARD[0]


# ─────────────────────────────────────────────────────────────────────────────
#  AppWindow
# ─────────────────────────────────────────────────────────────────────────────

class AppWindow(ctk.CTkFrame):
    """Root frame — holds the full UI."""

    def __init__(self, master: ctk.CTk, cfg: Config) -> None:
        super().__init__(master, fg_color=SYS_BG, corner_radius=0)
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
        self._pulse_hi:    bool  = True
        self._wave_phase:  float = 0.0

        # Auto-paste
        self._paste_target: Optional[str] = None

        # Streaming
        self._stream_samples: int        = 0
        self._stream_chunks:  list[str]  = []
        self._stream_tick_id             = None

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
        bar = ctk.CTkFrame(self, height=64, corner_radius=0, fg_color="transparent")
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # Left: app title
        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side="left", padx=22, fill="y")
        ctk.CTkLabel(
            left,
            text="🎙",
            font=ctk.CTkFont(size=24),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            left,
            text="Whisper Pro",
            font=ctk.CTkFont("Helvetica Neue", 18, "bold"),
            text_color=LABEL,
        ).pack(side="left")

        # Right: language + model pills
        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.pack(side="right", padx=20, fill="y")

        for label_txt, var_attr, menu_attr, values, cb in [
            ("語言", "_lang_var", "_lang_menu", list(LANGUAGE_OPTIONS.keys()), self._on_language_change),
            ("模型", "_model_var", "_model_menu", list(MODEL_INFO.keys()), self._on_model_change),
        ]:
            pill = ctk.CTkFrame(right, fg_color=CARD2, corner_radius=10, border_width=1, border_color=SEP)
            pill.pack(side="left", padx=(0, 8), pady=16)

            ctk.CTkLabel(
                pill, text=label_txt,
                font=ctk.CTkFont("Helvetica Neue", 11),
                text_color=LABEL3, padx=8,
            ).pack(side="left")

            var = ctk.StringVar(
                value=self.cfg.language if var_attr == "_lang_var" else self.cfg.model
            )
            setattr(self, var_attr, var)

            menu = ctk.CTkOptionMenu(
                pill,
                values=values,
                variable=var,
                width=96 if var_attr == "_lang_var" else 80,
                height=28,
                corner_radius=8,
                fg_color="transparent",
                button_color="transparent",
                button_hover_color=CARD2,
                dropdown_fg_color=CARD,
                text_color=LABEL,
                font=ctk.CTkFont("Helvetica Neue", 12),
                command=cb,
            )
            menu.pack(side="left", padx=(0, 4))
            setattr(self, menu_attr, menu)

    # ── Record card ──────────────────────────────────────────────────────────

    def _build_record_card(self) -> None:
        card = ctk.CTkFrame(
            self, corner_radius=20,
            fg_color=CARD, border_color=SEP, border_width=1,
        )
        card.pack(fill="x", padx=16, pady=(2, 6))

        # Waveform
        self._wave_canvas = tk.Canvas(
            card, height=68, bg=_card_hex(), highlightthickness=0,
        )
        self._wave_canvas.pack(fill="x", padx=20, pady=(16, 4))
        self._draw_idle_wave()

        # Button zone
        zone = ctk.CTkFrame(card, fg_color="transparent")
        zone.pack(pady=(6, 4))

        # Outer ring (glow)
        self._btn_ring = ctk.CTkFrame(
            zone, width=182, height=182, corner_radius=91,
            fg_color="transparent", border_color=GREEN_BG, border_width=3,
        )
        self._btn_ring.pack()
        self._btn_ring.pack_propagate(False)

        self._record_btn = ctk.CTkButton(
            self._btn_ring,
            text="🎤\n點擊錄音",
            width=154, height=154, corner_radius=77,
            fg_color=GREEN, hover_color=("#2AAF50", "#28B84A"),
            text_color="#FFFFFF",
            font=ctk.CTkFont("Helvetica Neue", 13, "bold"),
            border_width=0,
            command=self._on_record_btn,
        )
        self._record_btn.place(relx=0.5, rely=0.5, anchor="center")

        # Auto-paste target label (shows during recording)
        self._target_label = ctk.CTkLabel(
            card,
            text="",
            font=ctk.CTkFont("Helvetica Neue", 11),
            text_color=LABEL3,
        )
        self._target_label.pack(pady=(2, 2))

        # Hotkey hint
        self._hotkey_hint = ctk.CTkLabel(
            card,
            text=f"按住 {self.cfg.format_hotkey_display()} 即時錄音",
            font=ctk.CTkFont("Helvetica Neue", 11),
            text_color=LABEL3,
        )
        self._hotkey_hint.pack(pady=(0, 14))

    # ── Result card ──────────────────────────────────────────────────────────

    def _build_result_card(self) -> None:
        card = ctk.CTkFrame(
            self, corner_radius=20,
            fg_color=CARD, border_color=SEP, border_width=1,
        )
        card.pack(fill="both", expand=True, padx=16, pady=(0, 6))

        hdr = ctk.CTkFrame(card, fg_color="transparent", height=44)
        hdr.pack(fill="x", padx=18, pady=(12, 0))
        hdr.pack_propagate(False)

        self._result_title = ctk.CTkLabel(
            hdr, text="📝  轉錄結果",
            font=ctk.CTkFont("Helvetica Neue", 13, "bold"),
            text_color=LABEL, anchor="w",
        )
        self._result_title.pack(side="left")

        ctk.CTkButton(
            hdr, text="清除", width=48, height=24,
            fg_color="transparent", border_width=1, border_color=SEP,
            text_color=LABEL3, hover_color=CARD2,
            font=ctk.CTkFont(size=11), corner_radius=8,
            command=self._on_clear,
        ).pack(side="right")

        ctk.CTkFrame(card, height=1, fg_color=SEP).pack(fill="x", padx=18, pady=(8, 0))

        self._textbox = ctk.CTkTextbox(
            card,
            font=ctk.CTkFont("Helvetica Neue", 13),
            wrap="word", corner_radius=0, border_width=0,
            fg_color="transparent", text_color=LABEL,
            state="disabled",
        )
        self._textbox.pack(fill="both", expand=True, padx=6, pady=(4, 8))
        self._show_placeholder()

    # ── Action bar ───────────────────────────────────────────────────────────

    def _build_action_bar(self) -> None:
        bar = ctk.CTkFrame(self, height=52, corner_radius=0, fg_color="transparent")
        bar.pack(fill="x")
        bar.pack_propagate(False)

        row = ctk.CTkFrame(bar, fg_color="transparent")
        row.pack(expand=True, pady=10)

        ghost = dict(
            height=32, corner_radius=16,
            font=ctk.CTkFont("Helvetica Neue", 12),
            fg_color=CARD, border_width=1, border_color=SEP,
            text_color=LABEL, hover_color=CARD2,
        )

        ctk.CTkButton(row, text="📋  複製", width=88, command=self._on_copy, **ghost).pack(side="left", padx=4)
        ctk.CTkButton(row, text="💾  存檔", width=88, command=self._on_save, **ghost).pack(side="left", padx=4)

        # Auto-paste toggle chip
        self._ap_btn = ctk.CTkButton(
            row,
            text="⌨  自動貼上",
            width=100, height=32, corner_radius=16,
            font=ctk.CTkFont("Helvetica Neue", 12),
            fg_color=INDIGO if self.cfg.auto_paste else CARD,
            border_width=1,
            border_color=INDIGO if self.cfg.auto_paste else SEP,
            text_color="#FFFFFF" if self.cfg.auto_paste else LABEL3,
            hover_color=("#4745AC", "#504EC4"),
            command=self._toggle_auto_paste,
        )
        self._ap_btn.pack(side="left", padx=4)

        ollama_ok = self.ollama.is_available()
        self._ollama_btn = ctk.CTkButton(
            row, text="✨  潤飾", width=88,
            state="normal" if ollama_ok else "disabled",
            fg_color=BLUE if ollama_ok else CARD,
            hover_color=("#0062CC", "#0070E0") if ollama_ok else None,
            border_width=1,
            border_color=BLUE if ollama_ok else SEP,
            text_color="#FFFFFF" if ollama_ok else LABEL3,
            height=32, corner_radius=16,
            font=ctk.CTkFont("Helvetica Neue", 12),
            command=self._on_ollama,
        )
        self._ollama_btn.pack(side="left", padx=4)

        ctk.CTkButton(row, text="⚙  設定", width=88, command=self._open_settings, **ghost).pack(side="left", padx=4)

    # ── Status bar ───────────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        bar = ctk.CTkFrame(
            self, height=30, corner_radius=0,
            fg_color=CARD, border_color=SEP,
        )
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=5)

        self._status_dot = ctk.CTkLabel(
            inner, text="●", text_color=GREEN[0], font=ctk.CTkFont(size=9),
        )
        self._status_dot.pack(side="left")

        self._status_label = ctk.CTkLabel(
            inner, text=" 模型載入中…",
            font=ctk.CTkFont("Helvetica Neue", 11), text_color=LABEL3,
        )
        self._status_label.pack(side="left")

        self._timer_label = ctk.CTkLabel(
            inner, text="",
            font=ctk.CTkFont("Helvetica Neue", 11, "bold"), text_color=RED[0],
        )
        self._timer_label.pack(side="right")

        self._hotkey_status = ctk.CTkLabel(
            inner, text=self.cfg.format_hotkey_display(),
            font=ctk.CTkFont("Helvetica Neue", 11), text_color=LABEL3,
        )
        self._hotkey_status.pack(side="right", padx=14)

    # ═══════════════════════════════════════════════════════════════════════
    #  STATE MACHINE
    # ═══════════════════════════════════════════════════════════════════════

    def _transition_to_recording(self) -> None:
        if self._state != "idle":
            return
        self._state      = "recording"
        self._rec_start  = time.perf_counter()
        self._hotkey_held = True
        self._stream_samples = 0
        self._stream_chunks  = []

        # ── Capture frontmost app for auto-paste ──────────────────────────
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
            fg_color=RED, hover_color=("#CC2E24", "#CC3630"),
        )
        self._btn_ring.configure(border_color=RED_BG)
        self._model_menu.configure(state="disabled")
        self._lang_menu.configure(state="disabled")
        self._status_dot.configure(text_color=RED[0])
        self._status_label.configure(text=" 錄音中")

        self._pulse_btn()
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
            fg_color=ORANGE, hover_color=ORANGE,
            state="disabled",
        )
        self._btn_ring.configure(border_color=("transparent", "transparent"))
        self._target_label.configure(text="")
        self._status_dot.configure(text_color=ORANGE[0])
        self._status_label.configure(text=" 轉錄中，請稍候…")
        self._timer_label.configure(text="")

        self._draw_idle_wave()
        self._animate_spinner()

        tail   = full_audio[self._stream_samples:]
        model  = self._model_var.get()
        lang   = self.cfg.get_whisper_language()
        audio  = tail if len(tail) > 800 else full_audio

        threading.Thread(
            target=self._run_transcription,
            args=(audio, model, lang),
            daemon=True,
        ).start()

    def _transition_to_idle(self, result: Optional[TranscriptionResult] = None) -> None:
        self._state = "idle"

        self._record_btn.configure(
            text="🎤\n點擊錄音",
            fg_color=GREEN, hover_color=("#2AAF50", "#28B84A"),
            state="normal",
        )
        self._btn_ring.configure(border_color=GREEN_BG)
        self._model_menu.configure(state="normal")
        self._lang_menu.configure(state="normal")
        self._target_label.configure(text="")

        model = self._model_var.get()
        self._status_dot.configure(text_color=GREEN[0])
        self._status_label.configure(text=f" 就緒 ({model})")

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
        snap       = self.recorder.get_buffer_snapshot()
        new        = len(snap) - self._stream_samples
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
        self._show_toast(f"✅  轉錄完成（{result.elapsed_seconds:.1f}s）")

        text = result.text
        valid = text and text not in ("（未偵測到語音內容）", "（沒有偵測到音訊，請確認麥克風是否正常運作）")

        # Auto-copy (always, if configured)
        if self.cfg.auto_copy and valid:
            try:
                import pyperclip
                pyperclip.copy(text)
            except Exception:
                pass

        # Auto-paste (inject into previously focused app)
        if self.cfg.auto_paste and valid and self._paste_target:
            target = self._paste_target
            self._paste_target = None
            threading.Thread(
                target=self._do_auto_paste,
                args=(text, target),
                daemon=True,
            ).start()

    def _do_auto_paste(self, text: str, target: str) -> None:
        """Background: copy to clipboard, activate target, ⌘V."""
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
            text=f"📝  轉錄結果  ({dur}s · {lang} · {model})"
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

    def _pulse_btn(self) -> None:
        if self._state != "recording":
            return
        color = RED if self._pulse_hi else RED_BG
        self._pulse_hi = not self._pulse_hi
        self._record_btn.configure(fg_color=color)
        self.after(550, self._pulse_btn)

    def _update_wave(self) -> None:
        if self._state != "recording":
            self._draw_idle_wave()
            return
        self._draw_live_wave(self.recorder.get_rms_level())
        self.after(50, self._update_wave)

    def _draw_idle_wave(self) -> None:
        c = self._wave_canvas
        c.delete("all")
        w  = c.winfo_width() or WIN_W - 80
        h  = 68
        bw = max(3, w // WAVE_BARS - 3)
        gp = w // WAVE_BARS
        col = WAVE_IDLE[1] if _dark() else WAVE_IDLE[0]
        for i in range(WAVE_BARS):
            x  = i * gp + gp // 2
            bh = 4 + int(6 * abs(math.sin(i * 0.22)))
            y0 = (h - bh) // 2
            c.create_rectangle(x, y0, x + bw, y0 + bh, fill=col, outline="")

    def _draw_live_wave(self, rms: float) -> None:
        import random
        c = self._wave_canvas
        c.delete("all")
        w  = c.winfo_width() or WIN_W - 80
        h  = 68
        bw = max(3, w // WAVE_BARS - 3)
        gp = w // WAVE_BARS
        self._wave_phase = (self._wave_phase + 0.22) % (2 * math.pi)
        for i in range(WAVE_BARS):
            x    = i * gp + gp // 2
            wave = 0.5 + 0.5 * math.sin(self._wave_phase + i * 0.35)
            jit  = random.uniform(0.85, 1.15)
            bh   = max(4, int((h - 14) * rms * 5 * wave * jit))
            bh   = min(bh, h - 14)
            y0   = (h - bh) // 2
            # Green → Red gradient by amplitude
            t = min(1.0, rms * 7)
            r = int(0x34 + t * (0xFF - 0x34))
            g = int(0xC7 + t * (0x3B - 0xC7))
            b = int(0x59 + t * (0x30 - 0x59))
            c.create_rectangle(x, y0, x + bw, y0 + bh,
                                fill=f"#{r:02X}{g:02X}{b:02X}", outline="")

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
            self._show_toast("📋  已複製到剪貼簿")
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
                self._show_toast("💾  已儲存")
            except Exception as e:
                self._show_toast(f"儲存失敗: {e}")

    def _on_clear(self) -> None:
        self._textbox.configure(state="normal")
        self._textbox.delete("1.0", "end")
        self._textbox.configure(state="disabled")
        self._result_title.configure(text="📝  轉錄結果")
        self._show_placeholder()

    def _toggle_auto_paste(self) -> None:
        self.cfg.auto_paste = not self.cfg.auto_paste
        self.cfg.save()
        on = self.cfg.auto_paste
        self._ap_btn.configure(
            fg_color=INDIGO if on else CARD,
            border_color=INDIGO if on else SEP,
            text_color="#FFFFFF" if on else LABEL3,
        )
        self._show_toast("⌨  自動貼上已開啟" if on else "⌨  自動貼上已關閉")

    def _on_ollama(self) -> None:
        text = self._get_result_text()
        if not text:
            return
        self._ollama_btn.configure(state="disabled", text="✨  處理中…")

        def _run():
            result = self.ollama.process(text)
            self.after(0, _done, result)

        def _done(result):
            self._ollama_btn.configure(state="normal", text="✨  潤飾")
            if result.error:
                self._show_toast(f"Ollama 錯誤: {result.error}")
                return
            self._textbox.configure(state="normal")
            self._textbox.delete("1.0", "end")
            self._textbox.insert("end", result.text)
            self._textbox.configure(state="disabled")
            self._show_toast("✨  潤飾完成")

        threading.Thread(target=_run, daemon=True).start()

    def _on_model_change(self, value: str) -> None:
        self.cfg.model = value
        self.cfg.save()
        self._status_label.configure(text=f" 就緒 ({value})")

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
        # Sync auto-paste button
        on = cfg.auto_paste
        self._ap_btn.configure(
            fg_color=INDIGO if on else CARD,
            border_color=INDIGO if on else SEP,
            text_color="#FFFFFF" if on else LABEL3,
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
                    text=f" 就緒 ({model} · {label})"
                ))
                self.after(0, lambda: self._status_dot.configure(
                    text_color=GREEN[0]
                ))
            except Exception:
                self.after(0, lambda: self._status_label.configure(text=" 模型載入失敗"))
                self.after(0, lambda: self._status_dot.configure(text_color=RED[0]))

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
            self, corner_radius=14,
            fg_color=CARD, border_width=1, border_color=SEP,
        )
        ctk.CTkLabel(
            toast, text=message,
            font=ctk.CTkFont("Helvetica Neue", 12),
            text_color=LABEL, padx=16, pady=9,
        ).pack()
        toast.place(relx=1.0, rely=1.0, x=-18, y=-50, anchor="se")
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
        self.geometry("420x560")
        self.resizable(False, False)
        self.grab_set()
        self._build()

    def _build(self) -> None:
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        def section(title: str) -> ctk.CTkFrame:
            ctk.CTkLabel(
                scroll, text=title,
                font=ctk.CTkFont("Helvetica Neue", 11, "bold"),
                text_color=LABEL3, anchor="w",
            ).pack(fill="x", padx=16, pady=(20, 6))
            f = ctk.CTkFrame(
                scroll, corner_radius=16,
                fg_color=CARD, border_color=SEP, border_width=1,
            )
            f.pack(fill="x", padx=12, pady=(0, 4))
            return f

        def row(parent, label: str, widget_fn) -> None:
            r = ctk.CTkFrame(parent, fg_color="transparent", height=50)
            r.pack(fill="x", padx=16, pady=2)
            r.pack_propagate(False)
            ctk.CTkLabel(
                r, text=label, anchor="w",
                font=ctk.CTkFont("Helvetica Neue", 13),
                text_color=LABEL,
            ).pack(side="left")
            widget_fn(r)

        def sep_line(parent) -> None:
            ctk.CTkFrame(parent, height=1, fg_color=SEP).pack(
                fill="x", padx=16, pady=0
            )

        # ── 語音辨識 ──────────────────────────────────────────────────────
        stt = section("語音辨識")
        self._model_var = ctk.StringVar(value=self.cfg.model)

        def model_row(r):
            ctk.CTkOptionMenu(
                r, values=list(MODEL_INFO.keys()),
                variable=self._model_var,
                width=100, height=28, corner_radius=8,
                command=self._on_model_preview,
            ).pack(side="right")

        row(stt, "模型大小", model_row)
        self._model_desc = ctk.CTkLabel(
            stt, text=MODEL_INFO.get(self.cfg.model, ""),
            font=ctk.CTkFont(size=11), text_color=LABEL3,
            wraplength=360, anchor="w",
        )
        self._model_desc.pack(fill="x", padx=16, pady=(0, 10))
        sep_line(stt)

        self._lang_var = ctk.StringVar(value=self.cfg.language)

        def lang_row(r):
            ctk.CTkOptionMenu(
                r, values=list(LANGUAGE_OPTIONS.keys()),
                variable=self._lang_var,
                width=110, height=28, corner_radius=8,
            ).pack(side="right")

        row(stt, "辨識語言", lang_row)

        # ── 快捷鍵 ────────────────────────────────────────────────────────
        hk = section("快捷鍵")
        hk_row = ctk.CTkFrame(hk, fg_color="transparent", height=52)
        hk_row.pack(fill="x", padx=16, pady=4)
        hk_row.pack_propagate(False)

        ctk.CTkLabel(
            hk_row, text="全域快捷鍵", anchor="w",
            font=ctk.CTkFont("Helvetica Neue", 13), text_color=LABEL,
        ).pack(side="left")

        hk_r = ctk.CTkFrame(hk_row, fg_color="transparent")
        hk_r.pack(side="right")

        self._hk_label = ctk.CTkLabel(
            hk_r, text=format_hotkey(self.cfg.hotkey),
            font=ctk.CTkFont("Helvetica Neue", 12, "bold"),
            fg_color=CARD2, text_color=LABEL,
            corner_radius=8, padx=10, pady=4,
        )
        self._hk_label.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            hk_r, text="重新綁定", width=80, height=28, corner_radius=8,
            fg_color=BLUE_BG, hover_color=BLUE,
            text_color=BLUE, command=self._rebind_hotkey,
        ).pack(side="left")

        # ── 輸出偏好 ──────────────────────────────────────────────────────
        out = section("輸出偏好")

        self._append_var   = ctk.BooleanVar(value=self.cfg.append_results)
        self._autocopy_var = ctk.BooleanVar(value=self.cfg.auto_copy)
        self._autopaste_var = ctk.BooleanVar(value=self.cfg.auto_paste)

        def sw(var, accent=None):
            def fn(r):
                ctk.CTkSwitch(
                    r, text="", variable=var,
                    onvalue=True, offvalue=False,
                    progress_color=accent or BLUE[0],
                ).pack(side="right")
            return fn

        row(out, "追加錄音結果", sw(self._append_var))
        sep_line(out)
        row(out, "轉錄後自動複製", sw(self._autocopy_var))
        sep_line(out)
        row(out, "語音轉文字後自動貼上 ⌨", sw(self._autopaste_var, INDIGO[0]))

        # ── 關於 ──────────────────────────────────────────────────────────
        about = section("關於")
        for label, path in [
            ("Whisper 快取", "~/.cache/huggingface"),
            ("設定檔", "~/.whisper_app/config.json"),
        ]:
            pr = ctk.CTkFrame(about, fg_color="transparent", height=36)
            pr.pack(fill="x", padx=16, pady=2)
            pr.pack_propagate(False)
            ctk.CTkLabel(pr, text=label, anchor="w",
                         font=ctk.CTkFont(size=12), text_color=LABEL).pack(side="left")
            ctk.CTkLabel(pr, text=path, anchor="e",
                         font=ctk.CTkFont(size=11), text_color=LABEL3).pack(side="right")

        ctk.CTkButton(
            about, text="開啟設定資料夾", width=140, height=28,
            fg_color="transparent", border_width=1, border_color=SEP,
            text_color=BLUE, hover_color=BLUE_BG,
            font=ctk.CTkFont(size=12), corner_radius=8,
            command=lambda: subprocess.run(
                ["open", os.path.expanduser("~/.whisper_app")]
            ),
        ).pack(anchor="w", padx=16, pady=(0, 12))

        # ── Buttons ───────────────────────────────────────────────────────
        btn_bar = ctk.CTkFrame(self, fg_color="transparent", height=56)
        btn_bar.pack(fill="x", side="bottom", padx=16, pady=8)
        btn_bar.pack_propagate(False)

        ctk.CTkButton(
            btn_bar, text="取消", width=100, height=36, corner_radius=10,
            fg_color=CARD2, text_color=LABEL, hover_color=CARD,
            command=self.destroy,
        ).pack(side="right", padx=(6, 0))

        ctk.CTkButton(
            btn_bar, text="儲存", width=100, height=36, corner_radius=10,
            fg_color=BLUE, hover_color=("#0062CC", "#0070E0"),
            command=self._save,
        ).pack(side="right")

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
        self.geometry("320x210")
        self.resizable(False, False)
        self.grab_set()
        self._build()
        self._start_capture()

    def _build(self) -> None:
        ctk.CTkLabel(
            self, text="請按下想要的快捷鍵組合\n（按下後鬆開確認）",
            font=ctk.CTkFont("Helvetica Neue", 13), justify="center",
        ).pack(pady=(24, 10))

        self._detect_label = ctk.CTkLabel(
            self, text="等待按鍵…",
            font=ctk.CTkFont("Helvetica Neue", 16, "bold"),
            fg_color=CARD2, text_color=LABEL,
            corner_radius=10, padx=20, pady=10,
        )
        self._detect_label.pack(pady=8)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(pady=12)

        ctk.CTkButton(
            bar, text="取消", width=90, height=32, corner_radius=10,
            fg_color=CARD2, text_color=LABEL, command=self.destroy,
        ).pack(side="left", padx=6)

        self._apply_btn = ctk.CTkButton(
            bar, text="確認套用", width=90, height=32, corner_radius=10,
            state="disabled", fg_color=BLUE, hover_color=("#0062CC", "#0070E0"),
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
        self.geometry("480x290")
        self.resizable(False, False)
        self.grab_set()

        ctk.CTkLabel(
            self, text="🔐  需要「輔助使用」權限",
            font=ctk.CTkFont("Helvetica Neue", 16, "bold"), text_color=LABEL,
        ).pack(pady=(28, 10))

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
            font=ctk.CTkFont("Helvetica Neue", 13),
            text_color=LABEL, justify="left",
        ).pack(padx=28, pady=4)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(pady=16)

        ctk.CTkButton(
            bar, text="跳過", width=120, height=32, corner_radius=10,
            fg_color=CARD2, text_color=LABEL, command=self.destroy,
        ).pack(side="left", padx=6)

        ctk.CTkButton(
            bar, text="開啟系統設定", width=130, height=32, corner_radius=10,
            fg_color=BLUE, hover_color=("#0062CC", "#0070E0"),
            command=self._open_prefs,
        ).pack(side="left", padx=6)

    def _open_prefs(self) -> None:
        subprocess.run([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])
        self.destroy()
