"""
Whisper Pro 應用程式進入點。

啟動順序：
  1. 開啟 fault.log 捕捉 C-level 崩潰（SIGSEGV / SIGABRT / SIGBUS）
  2. 匯入 logger（自動 setup_logging：~/.whisper_app/logs/whisper_app.log）
  3. 檢查 Python 套件依賴，缺少則提示安裝指令並退出
  4. 讀取 ~/.whisper_app/config.json 使用者設定
  5. 建立 tkinter 根視窗並啟動 AppWindow
  6. 非同步確認 macOS 輔助使用權限，不足時顯示引導對話框
  7. 進入 tkinter 主迴圈（mainloop）直到視窗關閉
"""

from __future__ import annotations

import datetime
import faulthandler
import os
import sys
import threading
import traceback

# ── C-level 崩潰記錄器 ───────────────────────────────────────────────────────
# 捕捉 SIGSEGV / SIGABRT / SIGBUS 等 C 層面崩潰（例如 MLX/Metal mutex 失敗）。
# 這類崩潰 Python 例外機制抓不到，faulthandler 會在 kill 前把 traceback 寫入
# fault.log，方便事後分析。用獨立檔案（而非 whisper_app.log）避免被 rotation
# 截斷。路徑仍固定在專案根目錄，方便 debug 時立即可見。
_fault_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fault.log")
_fault_log_fh = open(_fault_log_path, "a", encoding="utf-8")
faulthandler.enable(file=_fault_log_fh, all_threads=True)  # 所有執行緒的 traceback 都記下來


# ── 匯入統一日誌系統 ─────────────────────────────────────────────────────────
# import logger 時會自動 setup_logging()，log 會寫到
# ~/.whisper_app/logs/whisper_app.log 並同時輸出到終端。
from logger import get_logger, log_error

log = get_logger("main")

# 每次啟動印一條分隔線，方便在 log 裡辨識 session 邊界
log.info("=" * 60)
log.info(f"NEW SESSION: Whisper Pro started at {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
log.info("=" * 60)

import customtkinter as ctk
import gui

# 診斷資訊：確認執行環境是否符合預期
log.debug(f"DIAG: Current Working Directory: {os.getcwd()}")
log.debug(f"DIAG: gui.py location: {gui.__file__}")
log.debug(f"DIAG: Python executable: {sys.executable}")

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
        log.error("MISSING_DEPENDENCIES: " + " ".join(missing))
        log.error("Install via: pip install " + " ".join(missing))
        sys.exit(1)

    # ── 2. 載入使用者設定 ─────────────────────────────────────────────────────
    cfg = Config.load()
    log.info(
        f"CONFIG: loaded model={cfg.model} language={cfg.language} "
        f"hotkey={cfg.hotkey} auto_paste={cfg.auto_paste} auto_copy={cfg.auto_copy}"
    )

    # ── 3. 建立 tkinter 根視窗 ────────────────────────────────────────────────
    root = ctk.CTk()
    root.title("🎙 Whisper Pro")
    root.geometry(f"{WIN_W}x{WIN_H}")   # 與 gui.py 的 WIN_W / WIN_H 保持一致
    root.minsize(640, 750)               # 允許縮放但設最小值，防止 UI 擠爆
    root.resizable(True, True)

    # ── 3a. App Icon（Phase 4.1）──────────────────────────────────────────
    # 載入 assets/icon.png 設定到 Dock / ⌘+Tab / 標題列
    # 找不到 / PIL 載入失敗一律靜默退化為「無 icon」狀態
    _icon_photo = None
    try:
        from PIL import Image, ImageTk
        icon_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets", "icon.png",
        )
        if os.path.exists(icon_path):
            _icon_img = Image.open(icon_path).resize(
                (128, 128), Image.Resampling.LANCZOS,
            )
            _icon_photo = ImageTk.PhotoImage(_icon_img)
            root.iconphoto(True, _icon_photo)
            # 保留 reference 防止 PhotoImage 被 GC
            root._whisper_icon = _icon_photo
            log.debug(f"ICON: loaded from {icon_path}")
    except Exception:
        log_error("set_dock_icon_failed")   # 非致命，只記不拋

    # ── 3b. 啟動畫面（Phase 4.1）─────────────────────────────────────────
    # 主視窗先 withdraw，splash 顯示 1.5s 後淡出 200ms，淡出完才 deiconify。
    # 即使 splash 模組載入失敗也不能讓主視窗永久隱藏 → 用 try/finally 兜住。
    root.withdraw()
    try:
        from splash import SplashScreen
        SplashScreen(
            root,
            on_done=lambda: (root.deiconify(), root.lift()),
            version="v2.2.0",
        )
    except Exception:
        log_error("splash_init_failed")
        # 無 splash 直接顯示主視窗
        root.deiconify()

    # ── 4. 建立主應用程式視窗 ─────────────────────────────────────────────────
    app = AppWindow(root, cfg)
    log.info("GUI: AppWindow initialized")

    # ── 5. 輔助使用權限確認（非阻塞）────────────────────────────────────────
    def _check_access():
        """背景執行緒：檢查 pynput 是否有輔助使用權限，沒有就彈引導對話框。"""
        if is_pynput_available() and not check_accessibility():
            log.warning("ACCESSIBILITY: permission NOT granted — global hotkey disabled")
            if root.winfo_exists():
                # 延遲 0.8s 讓主視窗先渲染完，再彈對話框
                root.after(800, lambda: AccessibilityDialog(root) if root.winfo_exists() else None)
        else:
            log.info("ACCESSIBILITY: permission granted or pynput unavailable")

    threading.Thread(target=_check_access, daemon=True).start()

    # ── 6. 視窗關閉處理 ──────────────────────────────────────────────────────
    def _on_close():
        """使用者按視窗關閉按鈕時：先通知 AppWindow 清理資源，再銷毀 tkinter。"""
        log.info("SESSION: user requested close")
        try:
            app.on_close()
        except Exception:
            log_error("app_on_close_failed")
        root.destroy()
        log.info("SESSION: ended")

    root.protocol("WM_DELETE_WINDOW", _on_close)

    # ── 7. 進入主迴圈 ─────────────────────────────────────────────────────────
    log.info("GUI: entering mainloop")
    try:
        root.mainloop()
    except Exception:
        log_error("mainloop_crashed")
        raise


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise   # 讓 sys.exit() 正常傳遞，不被下面的 except 攔截
    except BaseException:
        # 任何未捕獲的 Python 例外都落地到 log，方便下次 debug
        log.critical("FATAL: Uncaught exception in main()")
        traceback.print_exc()
        raise
