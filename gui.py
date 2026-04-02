"""
Whisper 語音轉文字 — macOS Pro Version
A refined, modern transcription tool designed with macOS aesthetic principles.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import tkinter as tk
import tkinter.messagebox
import tkinter.filedialog as fd
from typing import Optional, Callable
from PIL import Image, ImageTk, ImageDraw, ImageFilter

import customtkinter as ctk

from config import (
    MODEL_INFO,
    LANGUAGE_OPTIONS,
    Config,
    CONFIG_PATH,
)
from hotkey_manager import (
    HotkeyManager,
    capture_hotkey,
    format_hotkey,
    is_pynput_available,
)

try:
    from pynput.keyboard import Controller, Key
    _keyboard = Controller()
except ImportError:
    _keyboard = None

from ollama_client import OllamaClient
from recorder import AudioRecorder
from transcriber import Transcriber, TranscriptionResult
from vad import VAD

# ── Appearance ────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("Dark") # MacBook Pro Style is Dark by default
ctk.set_default_color_theme("blue")

WIN_W, WIN_H = 820, 920 # 大器排版

# MacBook Pro Design Palette
COLORS = {
    "accent":       "#0071E3",   # Apple Electric Blue
    "accent_deep":  "#A052FF",   # Apple Purple (M4 Pro style)
    "accent_hover": "#0077ED",
    
    # Backgrounds
    "bg_window":    "#000000",   # Pure Black Background
    "bg_card":      "#1D1D1F",   # Apple Deep Gray Card
    "bg_card_inner":"#2D2D2F",   
    
    # Text
    "text_main":    "#F5F5F7",   # High Contrast Off-White
    "text_sec":     "#86868B",   # Secondary Apple Gray
    "text_dim":     "#424245",
    
    # UI Details
    "border":       "#424245",
    "wave_idle":    "#2D2D2F",
    
    # Semantic Colors (Restored)
    "red":          "#FF3B30",
    "green":        "#34C759",
    "orange":       "#FF9500",
    "blue":         "#0071E3",
}

# 🛠️ Liquid Glass Image Generator (MacBook Pro Edition)
def make_liquid_glass_image(size: int, color1: str, color2: str) -> Image.Image:
    """Generate a sophisticated sphere with dual gradients and high-gloss effects."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    
    def hex_to_rgb(h): return tuple(int(h.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
    rgb1 = hex_to_rgb(color1)
    rgb2 = hex_to_rgb(color2)
    
    for y in range(size):
        for x in range(size):
            # Calculate distance from center (0.0 to 1.0)
            dx, dy = (x - size/2) / (size/2), (y - size/2) / (size/2)
            dist = (dx**2 + dy**2)**0.5
            
            if dist <= 1.0:
                # 1. Base Gradient (Apple Style Vertical/Diagonal)
                ratio = (dx + dy + 2) / 4 # Diagonal gradient
                r = int(rgb1[0] * (1-ratio) + rgb2[0] * ratio)
                g = int(rgb1[1] * (1-ratio) + rgb2[1] * ratio)
                b = int(rgb1[2] * (1-ratio) + rgb2[2] * ratio)
                
                # 2. Fresnel Effect (Darker edges)
                fresnel = 0.7 + 0.3 * (1.0 - dist)
                r, g, b = int(r * fresnel), int(g * fresnel), int(b * fresnel)
                
                # 3. Specular Highlight (Top-Left)
                hx, hy = dx + 0.4, dy + 0.4
                h_dist = (hx**2 + hy**2)**0.5
                specular = int(255 * max(0, 1.0 - h_dist)**3)
                r, g, b = min(255, r + specular), min(255, g + specular), min(255, b + specular)
                
                # 4. Secondary Glow (Bottom-Right)
                gx, gy = dx - 0.5, dy - 0.5
                g_dist = (gx**2 + gy**2)**0.5
                glow = int(80 * max(0, 1.0 - g_dist)**2)
                r, g, b = min(255, r + glow), min(255, g + glow), min(255, b + glow)
                
                # Antialiased Alpha
                alpha = 255 if dist < 0.98 else int(255 * (1.0 - dist) / 0.02)
                img.putpixel((x, y), (r, g, b, max(0, int(alpha))))
                
    return img

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
WAVE_BARS = 40

def _is_dark() -> bool:
    return ctk.get_appearance_mode() == "Dark"

# ─────────────────────────────────────────────────────────────────────────────

class AppWindow(ctk.CTkFrame):
    """Root frame with macOS-style glassmorphism layout."""

    def __init__(self, master: ctk.CTk, cfg: Config) -> None:
        super().__init__(master, fg_color=COLORS["bg_window"])
        self.pack(fill="both", expand=True)

        self.cfg = cfg
        self.recorder = AudioRecorder()
        if self.cfg.input_device:
            self.recorder.set_device_by_name(self.cfg.input_device)
            
        self.transcriber = Transcriber()
        self.vad = VAD()
        self.ollama = OllamaClient()
        self.ollama.set_enabled(self.cfg.ollama_enabled)
        
        self._ui_lock = threading.Lock() # 狀態轉換鎖
        self._last_action_time = 0.0 # 節流計時
        
        self.hotkey_mgr = HotkeyManager(
            on_press_cb=self._hotkey_press,
            on_release_cb=self._hotkey_release,
        )

        self._state: str = "idle"         # idle | recording | processing
        self._hotkey_held: bool = False
        self._recording_start: float = 0.0
        self._spinner_idx: int = 0
        self._pulse_toggle: bool = True
        self._wave_phase: float = 0.0

        # Pre-generate Liquid Glass PIL Images (Pro Gradients)
        self._pil_idle = make_liquid_glass_image(120, COLORS["accent"], COLORS["accent_deep"]) # Blue to Purple
        self._pil_rec  = make_liquid_glass_image(120, "#FF3B30", "#FF2D55") # Deep Red
        self._pil_proc = make_liquid_glass_image(120, "#A052FF", "#5AC8FA") # Purple to Cyan

        # Convert to Canvas-compatible PhotoImage
        self._tk_img_idle = ImageTk.PhotoImage(self._pil_idle)
        self._tk_img_rec  = ImageTk.PhotoImage(self._pil_rec)
        self._tk_img_proc = ImageTk.PhotoImage(self._pil_proc)

        # Streaming state (thread-safe ordered chunks)
        self._stream_processed_samples: int = 0
        self._stream_chunks: dict[int, str] = {}
        self._stream_chunk_counter: int = 0
        self._stream_tick_id = None

        self._build_ui()
        self._start_hotkey_listener()
        
        # Warm up model in background
        self.safe_after(1000, self._warmup_model)

    def safe_after(self, ms: int, func: Callable, *args) -> Optional[str]:
        """Safely schedule a task on the UI thread only if the widget exists."""
        if self.winfo_exists():
            return self.after(ms, func, *args)
        return None

    # ═══════════════════════════════════════════════════════════════════════
    #  BUILD UI
    # ═══════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        self._build_topbar()
        self._build_main_card()
        # 先渲染底部元件
        self._build_status_bar()
        self._build_action_bar()
        # 最後渲染結果卡片，讓它佔據中間剩餘空間
        self._build_result_card()

    def _build_topbar(self) -> None:
        """MacBook Pro Style Thin Topbar."""
        bar = ctk.CTkFrame(self, height=80, corner_radius=0, fg_color="transparent")
        bar.pack(fill="x", side="top", padx=40, pady=(20, 0))
        bar.pack_propagate(False)

        # App Identity (Bold SF Pro)
        ctk.CTkLabel(
            bar,
            text="🎙  Whisper Pro",
            font=ctk.CTkFont("SF Pro Display", 24, "bold"),
            text_color=COLORS["text_main"],
        ).pack(side="left")

        # Right Controls
        ctrls = ctk.CTkFrame(bar, fg_color="transparent")
        ctrls.pack(side="right")

        menu_cfg = dict(
            width=130, height=36, corner_radius=10, 
            font=ctk.CTkFont("SF Pro Text", 13),
            fg_color=COLORS["bg_card"],
            button_color=COLORS["bg_card"],
            button_hover_color=COLORS["bg_card_inner"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["bg_card_inner"]
        )

        self._lang_var = ctk.StringVar(value=self.cfg.language)
        self._lang_menu = ctk.CTkOptionMenu(
            ctrls, values=list(LANGUAGE_OPTIONS.keys()),
            variable=self._lang_var, command=self._on_language_change,
            **menu_cfg
        )
        self._lang_menu.pack(side="right", padx=(12, 0))

        self._model_var = ctk.StringVar(value=self.cfg.model)
        self._model_menu = ctk.CTkOptionMenu(
            ctrls, values=list(MODEL_INFO.keys()),
            variable=self._model_var, command=self._on_model_change,
            **menu_cfg
        )
        self._model_menu.pack(side="right")

    def _build_main_card(self) -> None:
        """The 'Performance Center' card."""
        card = ctk.CTkFrame(self, corner_radius=24, fg_color=COLORS["bg_card"],
                            border_width=1, border_color=COLORS["border"])
        card.pack(fill="x", padx=40, pady=30)

        # Minimalist Waveform
        self._wave_canvas = tk.Canvas(
            card, height=100, bg=COLORS["bg_card"], highlightthickness=0
        )
        self._wave_canvas.pack(fill="x", padx=50, pady=(40, 10))
        self._wave_canvas.bind("<Configure>", lambda e: self._draw_idle_wave())
        
        # Pro Recording Button
        btn_container = ctk.CTkFrame(card, fg_color="transparent")
        btn_container.pack(pady=(20, 40))

        self._record_canvas = tk.Canvas(
            btn_container, width=180, height=180, 
            bg=COLORS["bg_card"], highlightthickness=0
        )
        self._record_canvas.pack()
        self._record_canvas.bind("<Button-1>", lambda e: self._on_record_btn())
        self._record_canvas.bind("<Enter>", lambda e: self._record_canvas.config(cursor="hand2"))
        
        self._draw_glass_button("idle")

        self._btn_label = ctk.CTkLabel(
            card, text="按住 ⌘⇧Space 開始轉錄",
            font=ctk.CTkFont("SF Pro Display", 16, "bold"),
            text_color=COLORS["text_sec"]
        )
        self._btn_label.pack(pady=(0, 40))

    def _draw_glass_button(self, state: str) -> None:
        """Hand-draw the liquid glass button on canvas with Pro aesthetics."""
        c = self._record_canvas
        if not c.winfo_exists(): return
        c.delete("all")
        
        cx, cy = 90, 90 # Center of 180x180
        
        # 取得顏色配置
        if state == "idle":
            icon = "🎤"
        elif state == "recording":
            icon = "■"
        else: # processing
            icon = SPINNER[self._spinner_idx % len(SPINNER)]

        # 1. Draw Subtle Metal Ring (Pro Style)
        ring_color = "#424245" if not _is_dark() else "#333336"
        c.create_oval(cx-85, cy-85, cx+85, cy+85, outline=ring_color, width=1)
        
        # 2. Draw Main Liquid Sphere
        img = self._tk_img_idle if state == "idle" else (self._tk_img_rec if state == "recording" else self._tk_img_proc)
        c.create_image(cx, cy, image=img)
        
        # 3. Draw Icon (Pure White, Bold SF Pro)
        font_size = 52 if icon == "🎤" else 36
        c.create_text(
            cx, cy, text=icon, fill="#FFFFFF", 
            font=("SF Pro Display", font_size, "bold")
        )

    def _build_result_card(self) -> None:
        """MacBook Pro Style Result Card."""
        card = ctk.CTkFrame(self, corner_radius=24, fg_color=COLORS["bg_card"],
                            border_width=1, border_color=COLORS["border"])
        card.pack(fill="both", expand=True, padx=40, pady=(0, 20))

        header = ctk.CTkFrame(card, fg_color="transparent", height=50)
        header.pack(fill="x", padx=25, pady=(15, 0))
        header.pack_propagate(False)

        self._result_title = ctk.CTkLabel(
            header, text="📝  轉錄內容",
            font=ctk.CTkFont("SF Pro Display", 15, "bold"),
            text_color=COLORS["text_main"],
        )
        self._result_title.pack(side="left")

        ctk.CTkButton(
            header, text="清空 ✕", width=70, height=30,
            fg_color="transparent", text_color=COLORS["text_sec"],
            hover_color=COLORS["bg_card_inner"],
            font=ctk.CTkFont("SF Pro Text", 12),
            command=self._on_clear,
        ).pack(side="right")

        # The Text Area (Deep Integrated)
        inner = ctk.CTkFrame(card, fg_color=COLORS["bg_card_inner"], corner_radius=16)
        inner.pack(fill="both", expand=True, padx=20, pady=20)

        self._textbox = ctk.CTkTextbox(
            inner, fg_color="transparent",
            font=ctk.CTkFont("SF Pro Text", 15),
            wrap="word", border_width=0, state="disabled",
            text_color=COLORS["text_main"],
        )
        self._textbox.pack(fill="both", expand=True, padx=12, pady=12)
        self._show_placeholder()

    def _build_action_bar(self) -> None:
        """Floating integrated action bar."""
        bar = ctk.CTkFrame(self, height=80, fg_color="transparent")
        bar.pack(fill="x", side="bottom", padx=40, pady=(0, 10))
        
        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(expand=True)

        btn_style = dict(
            height=40, corner_radius=12, 
            font=ctk.CTkFont("SF Pro Text", 14, "bold"),
            fg_color=COLORS["bg_card"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_main"],
            hover_color=COLORS["bg_card_inner"]
        )

        ctk.CTkButton(
            inner, text="📋 複製文字", width=120,
            command=self._on_copy, **btn_style
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            inner, text="💾 儲存檔案", width=120,
            command=self._on_save, **btn_style
        ).pack(side="left", padx=8)

        self._ollama_btn = ctk.CTkButton(
            inner, text="✨ AI 潤飾", width=140,
            fg_color=COLORS["accent"], text_color="#FFFFFF",
            hover_color=COLORS["accent_hover"],
            command=self._on_ollama, state="disabled",
            height=40, corner_radius=12, font=ctk.CTkFont("SF Pro Text", 14, "bold")
        )
        self._ollama_btn.pack(side="left", padx=8)

        def _check_ollama():
            try:
                is_avail = self.ollama.is_available()
                def _update_ui():
                    if is_avail:
                        self._ollama_btn.configure(state="normal", text="✨ AI 潤飾")
                    else:
                        self._ollama_btn.configure(state="disabled", fg_color=COLORS["text_dim"], text="✨ AI 潤飾 (未啟動)")
                self.safe_after(0, _update_ui)
            except Exception as e:
                print(f"Ollama check failed: {e}")
        
        threading.Thread(target=_check_ollama, daemon=True).start()

        ctk.CTkButton(
            inner, text="⚙️", width=48,
            command=self._open_settings, **btn_style
        ).pack(side="left", padx=8)

    def _build_status_bar(self) -> None:
        """Minimal Pro status line."""
        bar = ctk.CTkFrame(self, height=40, fg_color="transparent")
        bar.pack(fill="x", side="bottom", padx=45, pady=(0, 15))

        self._status_dot = ctk.CTkLabel(bar, text="●", text_color="#34C759", font=ctk.CTkFont(size=10))
        self._status_dot.pack(side="left")

        self._status_label = ctk.CTkLabel(
            bar, text=" 系統就緒", font=ctk.CTkFont("SF Pro Text", 12),
            text_color=COLORS["text_sec"]
        )
        self._status_label.pack(side="left")

        self._timer_label = ctk.CTkLabel(
            bar, text="", font=ctk.CTkFont("SF Pro Text", 12, "bold"),
            text_color=COLORS["accent"]
        )
        self._timer_label.pack(side="right")

    # ═══════════════════════════════════════════════════════════════════════
    #  STATE MACHINE & LOGIC
    # ═══════════════════════════════════════════════════════════════════════

    def _transition_to_recording(self) -> None:
        with self._ui_lock:
            if self._state != "idle": return

            # Stability: Ensure stream state is clean
            self._stream_processed_samples = 0
            self._stream_chunks = {}
            self._stream_chunk_counter = 0

            if not self.recorder.start():
                self._show_toast("❌ 無法啟動錄音")
                # Show a more explicit dialog for microphone permissions
                self.safe_after(100, lambda: tk.messagebox.showerror(
                    "麥克風權限錯誤", 
                    "無法啟動錄音設備。\n\n請確保已在系統「設定 > 隱私權與安全性 > 麥克風」中允許此應用程式存取麥克風。"
                ))
                return

            self._state = "recording"
            self._recording_start = time.perf_counter()
            self._hotkey_held = True

        # UI Updates (Canvas Redraw) - 放在鎖外避免卡住 UI
        self._draw_glass_button("recording")
        self._btn_label.configure(text="正在錄音... (放開停止)", text_color=COLORS["red"])
        self._status_label.configure(text=" 正在收音")
        self._status_dot.configure(text_color=COLORS["red"])
        
        self._pulse_btn()
        self._update_wave()
        self._update_timer()
        
        # Start background streaming
        self._stream_tick_id = self.safe_after(5000, self._stream_tick)

    def _transition_to_processing(self) -> None:
        with self._ui_lock:
            if self._state != "recording": return
            self._state = "processing"
            self._hotkey_held = False

            if self._stream_tick_id:
                self.after_cancel(self._stream_tick_id)
                self._stream_tick_id = None

            full_audio = self.recorder.stop()

        # UI Updates
        self._draw_glass_button("processing")
        self._btn_label.configure(text="正在處理中...", text_color=COLORS["orange"])
        self._status_label.configure(text=" 正在轉錄語音")
        self._status_dot.configure(text_color=COLORS["orange"])

        self._draw_idle_wave()
        self._animate_spinner()

        # Handle leftovers not caught by streaming
        tail_audio = full_audio[self._stream_processed_samples:]
        audio_to_process = tail_audio if len(tail_audio) > 1000 else full_audio

        threading.Thread(
            target=self._run_transcription,
            args=(audio_to_process, self._model_var.get(), self.cfg.get_whisper_language()),
            daemon=True
        ).start()

    def _transition_to_idle(self, result: Optional[TranscriptionResult] = None) -> None:
        with self._ui_lock:
            self._state = "idle"
            self._hotkey_held = False # 確保重設熱鍵狀態，防止鎖死
        
        self._draw_glass_button("idle")
        self._btn_label.configure(text="按住 ⌘⇧Space 開始轉錄", text_color=COLORS["text_sec"])
        
        model = self._model_var.get()
        self._status_label.configure(text=f" 系統就緒 ({model})")
        self._status_dot.configure(text_color=COLORS["green"])

        if result: self._display_result(result)

    # ═══════════════════════════════════════════════════════════════════════
    #  CORE TASKS
    # ═══════════════════════════════════════════════════════════════════════

    def _stream_tick(self) -> None:
        if self._state != "recording": return

        chunk = self.recorder.get_recent_buffer(self._stream_processed_samples)
        new_samples = len(chunk)

        # Evaluate at least every 0.3s (4800 samples at 16kHz)
        if new_samples < 4800:
            self._stream_tick_id = self.safe_after(300, self._stream_tick)
            return

        # Check last 1 second (or whatever is available) for speech
        recent_chunk = chunk[-16000:] if len(chunk) > 16000 else chunk
        is_speech = self.vad.is_speech(recent_chunk)

        # Triggers: 
        # 1. Silence detected and we have at least 1.5 seconds of unprocessed audio
        # 2. Max buffer reached (e.g. 10 seconds)
        silence_trigger = (not is_speech) and (new_samples > 1.5 * 16000)
        length_trigger = (new_samples > 10 * 16000)

        if not (silence_trigger or length_trigger):
            self._stream_tick_id = self.safe_after(300, self._stream_tick)
            return

        self._stream_processed_samples += new_samples

        idx = self._stream_chunk_counter
        self._stream_chunk_counter += 1
        lang = self.cfg.get_whisper_language()

        def _proc(i=idx, aud=chunk):
            try:
                res = self.transcriber.transcribe_fast(aud, language=lang)
                if res.text and "偵測到" not in res.text:
                    self._stream_chunks[i] = res.text
            except Exception as e:
                print(f"Streaming chunk {i} failed: {e}")

        threading.Thread(target=_proc, daemon=True).start()
        self._stream_tick_id = self.safe_after(300, self._stream_tick)

    def _run_transcription(self, audio, model_size, language) -> None:
        try:
            # Use fast model for short clips if no chunks exist
            if len(audio) < 8 * 16000 and not self._stream_chunks:
                result = self.transcriber.transcribe_fast(audio, language=language)
            else:
                result = self.transcriber.transcribe(audio, model_size=model_size, language=language)

            # Build final text in correct order
            sorted_keys = sorted(self._stream_chunks.keys())
            prior_text = "".join(self._stream_chunks[k] for k in sorted_keys)

            if prior_text:
                tail = result.text if "未偵測" not in result.text else ""
                result.text = (prior_text + tail).strip() or "（未偵測到內容）"

            self.safe_after(0, self._on_transcription_done, result)
        except Exception as e:
            print(f"TRANSCRIPTION ERROR: {e}")
            # Ensure UI recovers
            err_res = TranscriptionResult(text=f"❌ 發生錯誤: {str(e)}", language="", duration_seconds=0, elapsed_seconds=0)
            self.safe_after(0, self._on_transcription_done, err_res)

    def _on_transcription_done(self, result: TranscriptionResult) -> None:
        self._transition_to_idle(result)
        self._show_toast(f"✅ 完成 ({result.elapsed_seconds:.1f}s)")
        
        # 1. 複製到剪貼簿 (如果開啟自動複製或自動貼上)
        if self.cfg.auto_copy or self.cfg.auto_paste:
            try:
                import pyperclip
                pyperclip.copy(result.text)
            except: pass
            
        # 2. 自動貼上 (模擬 Command+V)
        if self.cfg.auto_paste and _keyboard:
            def _paste():
                try:
                    # 給剪貼簿一點點同步時間，並讓使用者有時間切換回原視窗
                    time.sleep(0.2)
                    # macOS 下模擬 Cmd+V
                    with _keyboard.pressed(Key.cmd):
                        _keyboard.press('v')
                        _keyboard.release('v')
                except Exception as e:
                    print(f"AUTO-PASTE ERROR: {e}")
                    self.safe_after(0, lambda: self._show_toast("❌ 自動貼上失敗"))
            
            # 使用獨立執行緒執行模擬按鍵，避免卡住 UI
            threading.Thread(target=_paste, daemon=True).start()

    def safe_after(self, ms: int, func: Callable, *args) -> Optional[str]:
        """Wrapper for after() that checks if the widget still exists and passes args."""
        try:
            if self.winfo_exists():
                return self.after(ms, func, *args)
        except: pass
        return None

    def _display_result(self, result: TranscriptionResult) -> None:
        self._textbox.configure(state="normal")
        existing = self._textbox.get("1.0", "end").strip()
        
        if existing and "等待第一次" not in existing:
            self._textbox.insert("end", "\n\n" + "—"*20 + "\n\n")
        else:
            self._textbox.delete("1.0", "end")

        self._textbox.insert("end", result.text)
        self._textbox.see("end")
        self._textbox.configure(state="disabled")

    # ═══════════════════════════════════════════════════════════════════════
    #  UI HELPERS & ANIMATIONS
    # ── HELPERS ──

    def _draw_live_wave(self, rms: float) -> None:
        """MacBook Pro Style Siri Wave."""
        import math
        c = self._wave_canvas
        if not c.winfo_exists(): return
        c.delete("all")
        w, h = c.winfo_width(), 100
        if w < 100: w = WIN_W - 100
        
        gap = w // WAVE_BARS
        self._wave_phase += 0.3 # Faster for Pro feel
        
        for i in range(WAVE_BARS):
            x = i * gap + gap//2
            sin_val = math.sin(self._wave_phase + i*0.3)
            # Thinner, more precise bars
            bar_h = max(2, int(h * rms * 6 * (0.4 + 0.6*sin_val)))
            bar_h = min(bar_h, h - 20)
            
            # Electric Blue to Purple gradient based on amplitude
            color = COLORS["accent"] if sin_val > 0 else COLORS["accent_deep"]
            
            c.create_line(x, (h-bar_h)//2, x, (h+bar_h)//2, 
                          fill=color, width=3, capstyle="round")

    def _draw_idle_wave(self) -> None:
        """Minimalist idle state."""
        c = self._wave_canvas
        if not c.winfo_exists(): return
        c.delete("all")
        try:
            self.update_idletasks()
        except: return
        w = c.winfo_width()
        h = 100
        if w < 50: w = WIN_W - 100
        
        gap = w // WAVE_BARS
        color = COLORS["text_dim"]
        for i in range(WAVE_BARS):
            x = i * gap + gap//2
            c.create_oval(x-1, h//2-1, x+1, h//2+1, fill=color, outline="")

    def _animate_spinner(self) -> None:
        if self._state != "processing": return
        self._draw_glass_button("processing")
        self._spinner_idx += 1
        self.safe_after(100, self._animate_spinner)

    def _pulse_btn(self) -> None:
        pass

    def _update_wave(self) -> None:
        if self._state == "recording":
            self._draw_live_wave(self.recorder.get_rms_level())
            self.safe_after(50, self._update_wave)

    def _update_timer(self) -> None:
        if self._state != "recording": return
        if not self.winfo_exists(): return # 安全檢查
        elapsed = int(time.perf_counter() - self._recording_start)
        self._timer_label.configure(text=f"{elapsed//60:02d}:{elapsed%60:02d}")
        self.after(1000, self._update_timer)

    def _show_toast(self, msg: str) -> None:
        if not self.winfo_exists(): return
        t = ctk.CTkLabel(self, text=f" {msg} ", fg_color=COLORS["bg_card"], 
                         text_color=COLORS["text_main"], corner_radius=10, 
                         font=ctk.CTkFont("SF Pro Text", 12), border_width=1, border_color=COLORS["border"])
        t.place(relx=0.5, rely=0.9, anchor="center")
        self.safe_after(2500, t.destroy)

    def _show_placeholder(self) -> None:
        self._textbox.configure(state="normal")
        self._textbox.insert("1.0", "（等待第一次錄音...）")
        self._textbox.configure(state="disabled")

    def _on_record_btn(self) -> None:
        now = time.perf_counter()
        if now - self._last_action_time < 0.3: return
        self._last_action_time = now

        if self._state == "idle": 
            self._transition_to_recording()
        elif self._state == "recording": 
            # 滑鼠點擊停止也加入時長保護
            elapsed = time.perf_counter() - self._recording_start
            if elapsed < 0.5:
                self.recorder.stop()
                self._transition_to_idle()
                self._show_toast("⚠ 錄音過短已取消")
                return
            self._transition_to_processing()

    def _hotkey_press(self) -> None: self.safe_after(0, self._on_hotkey_press)
    def _hotkey_release(self) -> None: self.safe_after(0, self._on_hotkey_release)
    def _on_hotkey_press(self) -> None:
        if self._state == "idle" and not self._hotkey_held:
            self._transition_to_recording()

    def _on_hotkey_release(self) -> None:
        if self._state == "recording":
            # 確保是在按住快捷鍵的狀態下放開，而不是因為其他原因的干擾
            if not self._hotkey_held: return

            # 防呆：檢查錄音時長，避免誤觸 (少於 0.5 秒則取消)
            elapsed = time.perf_counter() - self._recording_start
            if elapsed < 0.5:
                self.recorder.stop()
                self._transition_to_idle()
                self._show_toast("⚠ 錄音過短已取消")
                return
            self._transition_to_processing()

    def _on_copy(self) -> None:
        t = self._textbox.get("1.0", "end").strip()
        if "等待第一次" in t or not t: return
        import pyperclip
        pyperclip.copy(t)
        self._show_toast("📋 已複製到剪貼簿")

    def _on_save(self) -> None:
        t = self._textbox.get("1.0", "end").strip()
        if "等待第一次" in t or not t: return
        p = fd.asksaveasfilename(defaultextension=".txt", filetypes=[("文字檔", "*.txt")])
        if p:
            with open(p, "w") as f: f.write(t)
            self._show_toast("💾 存檔成功")

    def _on_clear(self) -> None:
        self._textbox.configure(state="normal")
        self._textbox.delete("1.0", "end")
        self._show_placeholder()
        self._textbox.configure(state="disabled")

    def _on_ollama(self) -> None:
        t = self._textbox.get("1.0", "end").strip()
        if "等待第一次" in t or not t: return
        self._ollama_btn.configure(state="disabled", text="處理中...")
        def _run():
            try:
                res = self.ollama.process(t)
                self.safe_after(0, _done, res)
            except Exception as e:
                print(f"Ollama processing failed: {e}")
                self.safe_after(0, lambda: [self._ollama_btn.configure(state="normal", text="✨ AI 潤飾"), self._show_toast("✨ AI 潤飾失敗")])
        def _done(res):
            if not self.winfo_exists(): return
            self._ollama_btn.configure(state="normal", text="✨ AI 潤飾")
            if res.error: self._show_toast(f"AI 錯誤: {res.error}")
            else:
                self._textbox.configure(state="normal")
                self._textbox.delete("1.0", "end")
                self._textbox.insert("end", res.text)
                self._textbox.configure(state="disabled")
                self._show_toast("✨ 潤飾完成")
        threading.Thread(target=_run, daemon=True).start()

    def _on_model_change(self, v) -> None:
        self.cfg.model = v
        self.cfg.save()
        self._status_label.configure(text=f" 模型就緒 ({v})")
        # Explicitly unload old model
        threading.Thread(target=self.transcriber.unload, daemon=True).start()

    def _on_language_change(self, v) -> None:
        self.cfg.language = v
        self.cfg.save()

    def _open_settings(self) -> None:
        SettingsWindow(self, self.cfg, self._on_settings_saved)

    def _on_settings_saved(self, new_cfg: Config) -> None:
        """Apply new settings safely using a delay to avoid UI thread conflicts."""
        def _apply():
            try:
                # 1. Check if we need to restart hotkey listener
                if self.cfg.hotkey != new_cfg.hotkey:
                    self.hotkey_mgr.restart(new_cfg.hotkey)
                
                # 2. Update recorder device only if changed
                if self.cfg.input_device != new_cfg.input_device:
                    self.recorder.set_device_by_name(new_cfg.input_device)
                
                # 3. Update model only if changed
                if self.cfg.model != new_cfg.model:
                    # Async unload to prevent UI freeze
                    threading.Thread(target=self.transcriber.unload, daemon=True).start()
                
                # 4. Update Ollama state
                self.ollama.set_enabled(new_cfg.ollama_enabled)
                
                # Update our reference
                self.cfg = new_cfg
                
                # Async Ollama status check to keep UI responsive
                def _check():
                    is_avail = self.ollama.is_available()
                    self.safe_after(0, lambda: self._ollama_btn.configure(
                        state="normal" if is_avail else "disabled",
                        text="✨ AI 潤飾" if is_avail else "✨ AI 潤飾 (未啟動)"
                    ))
                threading.Thread(target=_check, daemon=True).start()

                self._status_label.configure(text=f" 系統就緒 ({self.cfg.model})")
                self._show_toast("✅ 設定已更新")
                
            except Exception as e:
                print(f"FATAL ERROR applying settings: {e}")
                self._show_toast("❌ 套用設定時發生錯誤")

        # 關鍵：給予 SettingsWindow 0.1 秒的時間徹底銷毀，避免執行緒衝突
        self.after(100, _apply)

    def _start_hotkey_listener(self) -> None:
        if is_pynput_available(): self.hotkey_mgr.restart(self.cfg.hotkey)

    def _warmup_model(self) -> None:
        m = self._model_var.get()
        def _load():
            try:
                self.transcriber.warmup(m)
                self.transcriber.warmup("small")
                self.safe_after(0, lambda: self._status_label.configure(text=f" 模型已就緒 ({m})"))
            except Exception as e:
                print(f"Warmup failed: {e}")
                self.safe_after(0, lambda: self._status_label.configure(text=" 模型載入失敗"))
        threading.Thread(target=_load, daemon=True).start()

    def on_close(self) -> None:
        """Cleanup before exit."""
        with self._ui_lock:
            self._state = "idle" # 禁止後續回調更新 UI
        try:
            if self._stream_tick_id:
                self.after_cancel(self._stream_tick_id)
                self._stream_tick_id = None
            self.hotkey_mgr.stop()
            if self.recorder.is_recording():
                self.recorder.stop()
            # 異步卸載模型，避免卡住退出過程
            threading.Thread(target=self.transcriber.unload, daemon=True).start()
        except Exception as e:
            print(f"Cleanup error: {e}")

# ─────────────────────────────────────────────────────────────────────────────

class SettingsWindow(ctk.CTkToplevel):
    """MacBook Pro Style System Settings."""
    def __init__(self, parent, cfg: Config, on_save) -> None:
        super().__init__(parent)
        self.parent = parent
        self.cfg = Config(**cfg.__dict__)
        self._on_save = on_save
        
        self.title("⚙️ 設定")
        self.geometry("520x680") 
        self.resizable(False, False)
        
        self.configure(fg_color=COLORS["bg_window"])
        self.attributes("-topmost", True)
        self.grab_set()
        
        self._build()

    def safe_after(self, ms: int, func: Callable, *args) -> Optional[str]:
        if self.winfo_exists():
            return self.after(ms, func, *args)
        return None

    def _build(self) -> None:
        main_container = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        main_container.pack(fill="both", expand=True, padx=10, pady=10)

        f = ctk.CTkFrame(main_container, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=30, pady=20)

        ctk.CTkLabel(
            f, text="系統設定", 
            font=ctk.CTkFont("SF Pro Display", 24, "bold"),
            text_color=COLORS["text_main"]
        ).pack(anchor="w", pady=(0, 30))

        # Rows Helper
        def row(label, widget_fn, height=56):
            r = ctk.CTkFrame(f, fg_color=COLORS["bg_card"], corner_radius=12, height=height, border_width=1, border_color=COLORS["border"])
            r.pack(fill="x", pady=6)
            r.pack_propagate(False)
            ctk.CTkLabel(r, text=label, font=ctk.CTkFont("SF Pro Text", 14), padx=20).pack(side="left")
            widget_fn(r)

        # Settings Rows...
        self._m_var = ctk.StringVar(value=self.cfg.model)
        row("Whisper 模型", lambda r: ctk.CTkOptionMenu(r, values=list(MODEL_INFO.keys()), variable=self._m_var, width=160, fg_color=COLORS["bg_card_inner"], button_color=COLORS["bg_card_inner"]).pack(side="right", padx=15))

        self._l_var = ctk.StringVar(value=self.cfg.language)
        row("辨識語言", lambda r: ctk.CTkOptionMenu(r, values=list(LANGUAGE_OPTIONS.keys()), variable=self._l_var, width=160, fg_color=COLORS["bg_card_inner"], button_color=COLORS["bg_card_inner"]).pack(side="right", padx=15))

        try:
            devices = self.parent.recorder.list_devices()
            device_names = [d["name"] for d in devices]
        except Exception:
            device_names = []
        if not device_names: device_names = ["(系統預設)"]
        
        current_dev = self.cfg.input_device
        if not current_dev or current_dev not in device_names:
            current_dev = device_names[0]
            
        self._d_var = ctk.StringVar(value=current_dev)
        row("音訊輸入設備", lambda r: ctk.CTkOptionMenu(r, values=device_names, variable=self._d_var, width=220, fg_color=COLORS["bg_card_inner"], button_color=COLORS["bg_card_inner"]).pack(side="right", padx=15))

        # Hotkey
        h_row = ctk.CTkFrame(f, fg_color=COLORS["bg_card"], corner_radius=12, height=56, border_width=1, border_color=COLORS["border"])
        h_row.pack(fill="x", pady=6)
        h_row.pack_propagate(False)
        ctk.CTkLabel(h_row, text="全域快捷鍵", font=ctk.CTkFont("SF Pro Text", 14), padx=20).pack(side="left")
        self._hk_label = ctk.CTkLabel(h_row, text=format_hotkey(self.cfg.hotkey), font=ctk.CTkFont("SF Pro Text", 13, "bold"), text_color=COLORS["accent"])
        self._hk_label.pack(side="right", padx=20)
        ctk.CTkButton(h_row, text="修改", width=70, height=32, corner_radius=8, fg_color=COLORS["bg_card_inner"], hover_color=COLORS["border"], command=self._rebind).pack(side="right")

        # Switches
        self._copy_var = ctk.BooleanVar(value=self.cfg.auto_copy)
        row("自動複製到剪貼簿", lambda r: ctk.CTkSwitch(r, text="", variable=self._copy_var, progress_color=COLORS["accent"]).pack(side="right", padx=10))

        self._paste_var = ctk.BooleanVar(value=self.cfg.auto_paste)
        row("自動貼入目前視窗", lambda r: ctk.CTkSwitch(r, text="", variable=self._paste_var, progress_color=COLORS["accent"]).pack(side="right", padx=10))

        self._ollama_var = ctk.BooleanVar(value=self.cfg.ollama_enabled)
        row("啟用 Ollama AI 潤飾", lambda r: ctk.CTkSwitch(r, text="", variable=self._ollama_var, progress_color=COLORS["accent"]).pack(side="right", padx=10))

        # Bottom
        ctk.CTkLabel(f, text=f"設定檔: {CONFIG_PATH}", font=ctk.CTkFont(size=11), text_color=COLORS["text_sec"]).pack(pady=(30, 15))
        
        btn_bar = ctk.CTkFrame(f, fg_color="transparent")
        btn_bar.pack(fill="x", pady=10)
        
        ctk.CTkButton(btn_bar, text="儲存設定", fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self._save, height=44, corner_radius=12, font=ctk.CTkFont("SF Pro Text", 14, "bold")).pack(side="right", padx=5, expand=True, fill="x")
        ctk.CTkButton(btn_bar, text="取消", fg_color="transparent", text_color=COLORS["text_main"], border_width=1, border_color=COLORS["border"], command=self.destroy, height=44, corner_radius=12).pack(side="right", padx=5, expand=True, fill="x")

    def _rebind(self) -> None:
        def _done(c):
            self.cfg.hotkey = c
            self._hk_label.configure(text=format_hotkey(c))
        # 使用 SettingsWindow 的 parent (即 AppWindow) 作為 master
        # 以避免過深的多層 CTkToplevel 嵌套在 macOS 上產生抓取 (grab) 問題
        HotkeyBindDialog(self.parent, _done)

    def _save(self) -> None:
        try:
            self.cfg.model = self._m_var.get()
            self.cfg.language = self._l_var.get()
            self.cfg.input_device = self._d_var.get()
            self.cfg.auto_copy = self._copy_var.get()
            self.cfg.auto_paste = self._paste_var.get()
            self.cfg.ollama_enabled = self._ollama_var.get()
            self.cfg.save()
            self._on_save(self.cfg)
            self.destroy()
        except Exception as e:
            print(f"ERROR Saving Settings: {e}")
            tk.messagebox.showerror("儲存失敗", f"無法儲存設定：{e}")

class HotkeyBindDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_apply) -> None:
        super().__init__(parent)
        self.title("綁定按鍵")
        self.geometry("300x180")
        self.attributes("-topmost", True)
        self.grab_set()
        self._on_apply = on_apply
        ctk.CTkLabel(self, text="請按下按鍵組合\n(放開後完成)", pady=20).pack()
        self._l = ctk.CTkLabel(self, text="...", font=ctk.CTkFont(size=18, weight="bold"))
        self._l.pack(pady=10)
        threading.Thread(target=self._cap, daemon=True).start()

    def _cap(self):
        try:
            c = capture_hotkey(timeout=10)
            if c:
                if self.winfo_exists():
                    self.after(0, lambda: self._l.configure(text=format_hotkey(c)))
                    self.after(800, lambda: [self._on_apply(c), self.destroy()])
            else:
                if self.winfo_exists():
                    self.destroy()
        except Exception as e:
            print(f"Hotkey capture failed: {e}")
            if self.winfo_exists():
                self.destroy()
class AccessibilityDialog(ctk.CTkToplevel):
    """macOS Permission Guidance."""
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.title("需要權限")
        self.geometry("480x300")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()

        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=30, pady=30)

        ctk.CTkLabel(f, text="🔐  需要「輔助使用」權限", font=ctk.CTkFont("SF Pro Display", 18, "bold")).pack(pady=(0, 15))

        msg = (
            "全域快捷鍵功能需要開啟 macOS 輔助使用權限。\n\n"
            "1. 點擊「開啟系統設定」\n"
            "2. 前往 隱私權與安全性 > 輔助使用\n"
            "3. 加入並允許 Terminal 或 python3\n"
            "4. 重新啟動此 App"
        )
        ctk.CTkLabel(f, text=msg, font=ctk.CTkFont("SF Pro Text", 13), justify="left").pack(pady=5)

        btn_bar = ctk.CTkFrame(f, fg_color="transparent")
        btn_bar.pack(side="bottom", fill="x", pady=(20, 0))

        ctk.CTkButton(btn_bar, text="開啟系統設定", fg_color=COLORS["blue"], command=self._open).pack(side="right", padx=5)
        ctk.CTkButton(btn_bar, text="稍後再說", fg_color="transparent", text_color=COLORS["text_main"], border_width=1, border_color=COLORS["border"], command=self.destroy).pack(side="right", padx=5)

    def _open(self) -> None:
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])
        self.destroy()
