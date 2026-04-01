"""
Entry point for Whisper 語音轉文字小幫手.

Checks runtime dependencies, shows accessibility guidance if needed,
then launches the main GUI window.
"""

from __future__ import annotations

import sys
import threading

import customtkinter as ctk

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
        print("❌ 缺少以下套件，請先執行安裝：")
        print("   pip install " + " ".join(missing))
        sys.exit(1)

    # ── Load config ───────────────────────────────────────────────────────
    cfg = Config.load()

    # ── Root window ───────────────────────────────────────────────────────
    root = ctk.CTk()
    root.title("🎙 Whisper Pro")
    # Use constants from gui.py for consistency
    root.geometry(f"{WIN_W}x{WIN_H}")
    root.minsize(640, 750) 
    root.resizable(True, True)

    # macOS: set dock icon title
    try:
        root.tk.call("wm", "iconphoto", root._w)
    except Exception:
        pass

    # ── Main app ──────────────────────────────────────────────────────────
    app = AppWindow(root, cfg)

    # ── Accessibility check (non-blocking) ────────────────────────────────
    def _check_access():
        if is_pynput_available() and not check_accessibility():
            if root.winfo_exists():
                root.after(800, lambda: AccessibilityDialog(root) if root.winfo_exists() else None)

    threading.Thread(target=_check_access, daemon=True).start()

    # ── Cleanup on close ─────────────────────────────────────────────────
    def _on_close():
        app.on_close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    root.mainloop()


if __name__ == "__main__":
    main()
