"""
Whisper Pro 應用程式進入點。

啟動順序：
  1. 開啟 fault.log 捕捉 C-level 崩潰（SIGSEGV / SIGABRT / SIGBUS）
  2. 重導 stdout / stderr 到帶時間戳的 Logger（同時寫入 terminal 與 app.log）
  3. 檢查 Python 套件依賴，缺少則提示安裝指令並退出
  4. 讀取 ~/.whisper_app/config.json 使用者設定
  5. 建立 tkinter 根視窗並啟動 AppWindow
  6. 非同步確認 macOS 輔助使用權限，不足時顯示引導對話框
  7. 進入 tkinter 主迴圈（mainloop）直到視窗關閉
"""

from __future__ import annotations

import sys
import threading
import os
import datetime
import faulthandler
import traceback

# ── C-level 崩潰記錄器 ───────────────────────────────────────────────────────
# 捕捉 SIGSEGV / SIGABRT / SIGBUS 等 C 層面崩潰（例如 MLX/Metal mutex 失敗）。
# 這類崩潰 Python 例外機制抓不到，faulthandler 會在 kill 前把 traceback 寫入
# fault.log，方便事後分析。用獨立檔案（而非 app.log）避免時間戳被 Logger 切碎。
_fault_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fault.log")
_fault_log_fh = open(_fault_log_path, "a", encoding="utf-8")
faulthandler.enable(file=_fault_log_fh, all_threads=True)  # 所有執行緒的 traceback 都記下來


# ── 帶時間戳的 stdout/stderr 替代器 ─────────────────────────────────────────

class Logger:
    """同時寫入 terminal 與 app.log 的 stdout/stderr 替代器。

    每一行訊息加上時間戳前綴；純換行符號不加時間戳，避免 log 裡
    產生大量空行。啟動後取代 sys.stdout / sys.stderr，讓所有
    print() 與 traceback 輸出都落地。
    """

    def __init__(self, filename: str = "app.log") -> None:
        """開啟 log 檔（追加模式）並保留對原始 terminal 的參照。"""
        self.terminal = sys.stdout                             # 保留原始 terminal 輸出
        self.log = open(filename, "a", encoding="utf-8")      # 追加模式，不覆蓋舊 log

    def write(self, message: str) -> None:
        """寫入一段訊息；非空行加上 [YYYY-MM-DD HH:MM:SS] 前綴。"""
        if message.strip():
            # 有實際內容才加時間戳，避免換行符號也被標記
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            formatted_message = f"[{timestamp}] {message}"
            self.terminal.write(formatted_message)
            self.log.write(formatted_message)
        else:
            # 純換行或空白：直接寫入，不加時間戳
            self.terminal.write(message)
            self.log.write(message)
        self.log.flush()   # 立即刷新，確保崩潰前已落地

    def flush(self) -> None:
        """確保 terminal 與 log 檔的緩衝區都被清空。"""
        self.terminal.flush()
        self.log.flush()


# 啟動 log 重導；之後所有 print() 都會經過 Logger
sys.stdout = Logger("app.log")
sys.stderr = sys.stdout   # stderr 也導到同一個 Logger，統一格式

# 每次啟動印一條分隔線，方便在 app.log 裡辨識 session 邊界
print("\n" + "=" * 60)
print(f"NEW SESSION: Whisper Pro started at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)

import customtkinter as ctk
import gui
import os

# 診斷資訊：確認執行環境是否符合預期
print(f"DIAG: Current Working Directory: {os.getcwd()}")
print(f"DIAG: gui.py location: {gui.__file__}")
print(f"DIAG: Python executable: {sys.executable}")

from config import Config
from gui import WIN_W, WIN_H, AppWindow, AccessibilityDialog
from hotkey_manager import check_accessibility, is_pynput_available


def check_dependencies() -> list[str]:
    """嘗試 import 所有關鍵套件，回傳缺少的套件名稱清單。"""
    missing = []
    for pkg in ("sounddevice", "faster_whisper", "customtkinter", "pyperclip", "webrtcvad"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    return missing


def main() -> None:
    """應用程式主流程：依賴檢查 → 載入設定 → 建立視窗 → 主迴圈。"""

    # ── 1. 套件依賴檢查 ──────────────────────────────────────────────────────
    missing = check_dependencies()
    if missing:
        # 缺套件就提示並退出，不進入 GUI 以免出現難以理解的 ImportError
        print("❌ 缺少以下套件，請先執行安裝：")
        print("   pip install " + " ".join(missing))
        sys.exit(1)

    # ── 2. 載入使用者設定 ─────────────────────────────────────────────────────
    cfg = Config.load()

    # ── 3. 建立 tkinter 根視窗 ────────────────────────────────────────────────
    root = ctk.CTk()
    root.title("🎙 Whisper Pro")
    root.geometry(f"{WIN_W}x{WIN_H}")   # 與 gui.py 的 WIN_W / WIN_H 保持一致
    root.minsize(640, 750)               # 允許縮放但設最小值，防止 UI 擠爆
    root.resizable(True, True)

    # macOS：嘗試設定視窗圖示（目前傳空值，待 app_icon.py 完成後填入）
    try:
        root.tk.call("wm", "iconphoto", root._w)
    except Exception:
        pass   # 非致命，靜默忽略

    # ── 4. 建立主應用程式視窗 ─────────────────────────────────────────────────
    app = AppWindow(root, cfg)

    # ── 5. 輔助使用權限確認（非阻塞）────────────────────────────────────────
    def _check_access():
        """背景執行緒：檢查 pynput 是否有輔助使用權限，沒有就彈引導對話框。"""
        if is_pynput_available() and not check_accessibility():
            if root.winfo_exists():
                # 延遲 0.8s 讓主視窗先渲染完，再彈對話框
                root.after(800, lambda: AccessibilityDialog(root) if root.winfo_exists() else None)

    threading.Thread(target=_check_access, daemon=True).start()

    # ── 6. 視窗關閉處理 ──────────────────────────────────────────────────────
    def _on_close():
        """使用者按視窗關閉按鈕時：先通知 AppWindow 清理資源，再銷毀 tkinter。"""
        app.on_close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    # ── 7. 進入主迴圈 ─────────────────────────────────────────────────────────
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise   # 讓 sys.exit() 正常傳遞，不被下面的 except 攔截
    except BaseException:
        # 任何未捕獲的 Python 例外都落地到 app.log，方便下次 debug
        print("FATAL: Uncaught exception in main()")
        traceback.print_exc()
        raise
