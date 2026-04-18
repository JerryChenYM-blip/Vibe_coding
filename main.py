"""
Entry point for Whisper 語音轉文字小幫手.

Checks runtime dependencies, shows accessibility guidance if needed,
then launches the main GUI window.
"""

from __future__ import annotations

import datetime
import os
import sys
import threading

# 匯入 logger（import 時會自動 setup_logging，log 會寫到
# ~/.whisper_app/logs/whisper_app.log 並同時輸出到終端）
from logger import get_logger, log_error

log = get_logger("main")

log.info("=" * 60)
log.info(f"NEW SESSION: Whisper Pro started at {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
log.info("=" * 60)

import customtkinter as ctk
import gui

log.debug(f"DIAG: Current Working Directory: {os.getcwd()}")
log.debug(f"DIAG: gui.py location: {gui.__file__}")
log.debug(f"DIAG: Python executable: {sys.executable}")

from config import Config
from gui import WIN_W, WIN_H, AppWindow, AccessibilityDialog
from hotkey_manager import check_accessibility, is_pynput_available


def check_dependencies() -> list[str]:
    """Return a list of missing critical Python packages."""
    missing = []
    for pkg in ("sounddevice", "faster_whisper", "customtkinter", "pyperclip", "webrtcvad"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    return missing


def main() -> None:
    # ── Dependency check ──────────────────────────────────────────────────
    missing = check_dependencies()
    if missing:
        log.error("MISSING_DEPENDENCIES: " + " ".join(missing))
        log.error("Install via: pip install " + " ".join(missing))
        sys.exit(1)

    # ── Load config ───────────────────────────────────────────────────────
    cfg = Config.load()
    log.info(
        f"CONFIG: loaded model={cfg.model} language={cfg.language} "
        f"hotkey={cfg.hotkey} auto_paste={cfg.auto_paste} auto_copy={cfg.auto_copy}"
    )

    # ── Root window ───────────────────────────────────────────────────────
    root = ctk.CTk()
    root.title("🎙 Whisper Pro")
    root.geometry(f"{WIN_W}x{WIN_H}")
    root.minsize(640, 750)
    root.resizable(True, True)

    # macOS: set dock icon title
    try:
        root.tk.call("wm", "iconphoto", root._w)
    except Exception:
        log_error("set_dock_icon_failed")

    # ── Main app ──────────────────────────────────────────────────────────
    app = AppWindow(root, cfg)
    log.info("GUI: AppWindow initialized")

    # ── Accessibility check (non-blocking) ────────────────────────────────
    def _check_access():
        if is_pynput_available() and not check_accessibility():
            log.warning("ACCESSIBILITY: permission NOT granted — global hotkey disabled")
            if root.winfo_exists():
                root.after(800, lambda: AccessibilityDialog(root) if root.winfo_exists() else None)
        else:
            log.info("ACCESSIBILITY: permission granted or pynput unavailable")

    threading.Thread(target=_check_access, daemon=True).start()

    # ── Cleanup on close ─────────────────────────────────────────────────
    def _on_close():
        log.info("SESSION: user requested close")
        try:
            app.on_close()
        except Exception:
            log_error("app_on_close_failed")
        root.destroy()
        log.info("SESSION: ended")

    root.protocol("WM_DELETE_WINDOW", _on_close)

    log.info("GUI: entering mainloop")
    try:
        root.mainloop()
    except Exception:
        log_error("mainloop_crashed")
        raise


if __name__ == "__main__":
    main()
