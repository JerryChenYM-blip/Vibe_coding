"""
Whisper Pro 啟動畫面（Phase 4.1）。

設計（規格見 docs/superpowers/specs/2026-04-22-app-icon-splash-design.md）：
  • 480 × 280，無標題列，視窗正中央
  • 內容：56px Cyan 麥克風 → 「Whisper Pro」標題 → 副標 → 版本號
  • 1.5s 後 200ms 淡出，淡出完成後執行 on_done callback

使用方式：
    splash = SplashScreen(root, on_done=lambda: root.deiconify())
    # SplashScreen 自己排程：1500ms 後開始淡出，淡出完 destroy 並呼叫 on_done

邊界：
  • 即使使用者強制關閉視窗，on_done 仍會被觸發（避免主視窗被卡在 withdraw）
  • 找不到 icon.png 時靜默退到「無 icon」版本
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk

from logger import get_logger, log_error

log = get_logger("splash")


# ── 常數 ──────────────────────────────────────────────────────────────────────

WIN_W = 480
WIN_H = 280

ICON_SIZE = 56                    # 標題上方的麥克風 icon 尺寸
ICON_PNG = Path(__file__).parent / "assets" / "icon.png"

DISPLAY_MS  = 1500                # 全亮顯示時間
FADE_STEP_MS = 16                 # ~60fps
FADE_DUR_MS  = 200                # 淡出總時長

# 從 tokens.py 取顏色（避免重複定義；splash.py 啟動時 tokens 一定已 import）
from tokens import (
    BG, SURF_1, SURF_4,
    TEXT_1, TEXT_3, TEXT_4,
    ACCENT,
    FONT_FAMILY_UI, FONT_FAMILY_TEXT,
)


# ── SplashScreen ──────────────────────────────────────────────────────────────

class SplashScreen(tk.Toplevel):
    """無邊框啟動畫面。

    用 `tk.Toplevel`（非 CTkToplevel）以便：
      • `overrideredirect(True)` 隱藏標題列
      • `attributes("-alpha", x)` 控制整體透明度做淡出動畫

    Args:
        master:   tk root（通常是 main.py 的 ctk.CTk()）
        on_done:  淡出完成後呼叫；通常用來 deiconify 主視窗
        version:  顯示在右下角的版本字串（如 "v2.2.0"）
    """

    def __init__(
        self,
        master,
        on_done: Optional[Callable[[], None]] = None,
        version: str = "",
    ) -> None:
        super().__init__(master)
        self._on_done   = on_done
        self._fade_step = 0
        self._closed    = False
        self._photo: Optional[tk.PhotoImage] = None  # 保留參考避 GC

        self._configure_window()
        self._build_ui(version)
        self.after(DISPLAY_MS, self._start_fade)

    # ── 視窗設定 ──────────────────────────────────────────────────────────────

    def _configure_window(self) -> None:
        """無邊框 + 螢幕置中 + 始終在前。"""
        self.overrideredirect(True)
        try:
            # 始終在最前（macOS 行為較不一致，盡力即可）
            self.attributes("-topmost", True)
        except Exception:
            pass
        # 起始全不透明
        try:
            self.attributes("-alpha", 1.0)
        except Exception:
            pass
        self.configure(bg=SURF_1)

        # 置中
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - WIN_W) // 2
        y = (sh - WIN_H) // 2
        self.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

    def _build_ui(self, version: str) -> None:
        """繪製 splash 內容（icon + title + subtitle + version）。"""
        # 外框：1px SURF_4 邊框
        outer = tk.Frame(
            self,
            bg=SURF_4,           # 邊框色
            highlightthickness=0,
        )
        outer.pack(fill="both", expand=True)
        # 內層往內縮 1px = 1px 邊框效果
        inner = tk.Frame(outer, bg=SURF_1, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        # Icon（從 assets/icon.png 載入；找不到時靜默略過）
        icon_label: Optional[tk.Label] = None
        if ICON_PNG.exists():
            try:
                from PIL import Image, ImageTk
                img = Image.open(ICON_PNG).convert("RGBA")
                img = img.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
                # 把背景色設為 SURF_1，避免 PNG alpha 在 Tk 上呈現黑底
                bg = Image.new("RGBA", img.size, SURF_1)
                bg.paste(img, mask=img.split()[3])
                self._photo = ImageTk.PhotoImage(bg)
                icon_label = tk.Label(inner, image=self._photo, bg=SURF_1)
            except Exception:
                log_error("splash_icon_load_failed", path=str(ICON_PNG))
                icon_label = None

        if icon_label is not None:
            icon_label.pack(pady=(50, 12))
        else:
            # 無 icon：留適當的上方空白
            tk.Frame(inner, bg=SURF_1, height=70).pack()

        # 標題
        tk.Label(
            inner, text="Whisper Pro",
            font=(FONT_FAMILY_UI, 28, "bold"),
            fg=TEXT_1, bg=SURF_1,
        ).pack(pady=(0, 6))

        # 副標
        tk.Label(
            inner, text="本地語音轉文字，完全離線",
            font=(FONT_FAMILY_TEXT, 13),
            fg=TEXT_3, bg=SURF_1,
        ).pack()

        # 版本（右下）
        if version:
            tk.Label(
                inner, text=version,
                font=(FONT_FAMILY_TEXT, 11),
                fg=TEXT_4, bg=SURF_1,
            ).place(relx=1.0, rely=1.0, x=-16, y=-12, anchor="se")

    # ── 淡出動畫 ─────────────────────────────────────────────────────────────

    def _start_fade(self) -> None:
        """啟動淡出（每 FADE_STEP_MS 一格，總時長 FADE_DUR_MS）。"""
        self._fade_step = 0
        self._fade_tick()

    def _fade_tick(self) -> None:
        if self._closed:
            return
        steps = max(1, FADE_DUR_MS // FADE_STEP_MS)
        self._fade_step += 1
        alpha = max(0.0, 1.0 - self._fade_step / steps)
        try:
            self.attributes("-alpha", alpha)
        except Exception:
            # macOS 某些情境 alpha 不可用 → 直接收尾
            self._finish()
            return
        if self._fade_step >= steps:
            self._finish()
        else:
            self.after(FADE_STEP_MS, self._fade_tick)

    def _finish(self) -> None:
        """銷毀視窗並觸發 on_done callback（兩段都包 try）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.destroy()
        except Exception:
            log_error("splash_destroy_failed")
        if self._on_done:
            try:
                self._on_done()
            except Exception:
                log_error("splash_on_done_failed")
