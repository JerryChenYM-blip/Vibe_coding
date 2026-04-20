"""
Entry point for Whisper 語音轉文字小幫手.

Checks runtime dependencies, shows accessibility guidance if needed,
then launches the main GUI window.
"""

from __future__ import annotations

import sys
import threading
import os
import datetime
import faulthandler
import traceback

# ── Native crash dump（獨立檔，避免被 Logger 時間戳切碎） ──────────────
# 捕捉 SIGSEGV / SIGABRT / SIGBUS 等 C-level 崩潰（例如 MLX/Metal mutex 失敗），
# 這些情況下 Python 例外機制抓不到，只會在 stderr 被 kill 前留一段文字。
_fault_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fault.log")
_fault_log_fh = open(_fault_log_path, "a", encoding="utf-8")
faulthandler.enable(file=_fault_log_fh, all_threads=True)

# ── Logging Setup ─────────────────────────────────────────────────────
class Logger:
    def __init__(self, filename="app.log"):
        self.terminal = sys.stdout
        self.log = open(filename, "a", encoding="utf-8")

    def write(self, message):
        # 避免為單純的換行符號加上時間戳
        if message.strip():
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            formatted_message = f"[{timestamp}] {message}"
            self.terminal.write(formatted_message)
            self.log.write(formatted_message)
        else:
            self.terminal.write(message)
            self.log.write(message)
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()

# 啟動日誌重新導向
sys.stdout = Logger("app.log")
sys.stderr = sys.stdout

print("\n" + "="*60)
print(f"NEW SESSION: Whisper Pro started at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("="*60)

import customtkinter as ctk
import gui
import os

print(f"DIAG: Current Working Directory: {os.getcwd()}")
print(f"DIAG: gui.py location: {gui.__file__}")
print(f"DIAG: Python executable: {sys.executable}")

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
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # 任何 Python 例外都落地到 app.log，避免下次又只留一段靜默
        print("FATAL: Uncaught exception in main()")
        traceback.print_exc()
        raise
