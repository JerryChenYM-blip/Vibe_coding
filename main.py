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

import atexit
import datetime
import faulthandler
import os
import signal
import sys
import threading
import time
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
from _version import __version__
from hotkey_manager import check_accessibility, is_pynput_available

# .app bundle 路徑（給 _relaunch_app 用）
import pathlib as _pathlib
_APP_BUNDLE = _pathlib.Path.home() / "Applications" / "WhisperPro.app"


def check_dependencies() -> list[str]:
    """嘗試 import 所有關鍵套件，回傳缺少的套件名稱清單。

    v2.11.0：移除 webrtcvad（前置 VAD 已改用 faster-whisper 內建 silero
    + numpy RMS，vad.py 模組已刪）。
    """
    missing = []
    for pkg in ("sounddevice", "faster_whisper", "customtkinter", "pyperclip"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    return missing


# ── App Nap 抑制（Fix 6 Step 2 / 2026-05-22）─────────────────────────────────
# macOS App Nap 會在 App 閒置 + 不在前景時提高 callback latency，導致
# pynput CGEventTap 觸發 kCGEventTapDisabledByTimeout（pynput 1.7.7 沒處理
# 這個事件）→ 熱鍵永久死亡，listener thread 不死、watchdog 旗標看不出來。
# 解法：用 NSProcessInfo.beginActivityWithOptions_reason_ 抑制 App Nap。
#
# 關鍵：token 必須 module-level 強參照——一旦被 GC，App Nap 立刻復活。
_APP_NAP_TOKEN = None


def _disable_app_nap() -> None:
    """抑制 macOS App Nap，避免閒置後 pynput CGEventTap callback latency
    升高觸發 timeout disable（Fix 6 / 2026-05-22）。

    用 NSActivityBackground | NSActivityLatencyCritical（後者單獨只在
    foreground 有效，必須 OR background flag 才能對 background app 生效）。
    詳見 plan：docs/superpowers/plans/2026-05-22-hotkey-idle-resilience.md §3 Step 2。
    """
    global _APP_NAP_TOKEN
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSProcessInfo
        NSActivityBackground = 0x000000FF
        NSActivityLatencyCritical = 0xFF00000000
        opts = NSActivityBackground | NSActivityLatencyCritical
        _APP_NAP_TOKEN = NSProcessInfo.processInfo() \
            .beginActivityWithOptions_reason_(
                opts,
                "Whisper Pro global hotkey listener must stay responsive",
            )
        log.info("APP_NAP: suppression activated (token kept module-level)")
    except Exception as e:
        log.warning(f"APP_NAP: suppression failed - {e}")


# ── Single-instance lockfile（v2.18.1 / 2026-05-25）──────────────────────────
# v2.18.0 silent crash 根因：老 process 持有 NSEvent global hotkey monitor，
# 新 process 啟動時 monitor 註冊衝突→ hang。
# 解法：lockfile（pidfile）機制。啟動時若偵測舊 PID 還活著就 SIGTERM 它，
# 3 秒後沒死再 SIGKILL，然後覆寫 pidfile 為自己 PID。atexit 清理。
_PID_FILE = os.path.expanduser("~/.whisper_app/whisper_pro.pid")


def _is_pid_alive(pid: int) -> bool:
    """用 signal 0 探測 PID 是否仍活著（不真的送訊號）。"""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill_stale_process(pid: int, timeout: float = 3.0) -> None:
    """SIGTERM 舊 process，最多等 timeout 秒；沒死就 SIGKILL。"""
    try:
        log.warning(f"SINGLE_INSTANCE: stale PID {pid} alive — sending SIGTERM")
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        log.warning(f"SINGLE_INSTANCE: SIGTERM PID {pid} failed: {e}")
        return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            log.info(f"SINGLE_INSTANCE: stale PID {pid} exited gracefully")
            return
        time.sleep(0.1)

    # 還沒死 → SIGKILL
    try:
        log.warning(f"SINGLE_INSTANCE: PID {pid} did not exit in {timeout}s — sending SIGKILL")
        os.kill(pid, signal.SIGKILL)
    except OSError as e:
        log.warning(f"SINGLE_INSTANCE: SIGKILL PID {pid} failed: {e}")


def _remove_pid_file() -> None:
    """atexit handler：清掉自己的 pidfile（只在 pidfile 內 PID 是自己時才刪）。"""
    try:
        if not os.path.exists(_PID_FILE):
            return
        with open(_PID_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content.isdigit() and int(content) == os.getpid():
            os.remove(_PID_FILE)
            log.info(f"SINGLE_INSTANCE: pidfile removed ({_PID_FILE})")
    except Exception as e:
        # atexit 不能拋例外
        try:
            log.warning(f"SINGLE_INSTANCE: pidfile cleanup failed: {e}")
        except Exception:
            pass


def _acquire_single_instance_lock() -> None:
    """確保只有一個 Whisper Pro instance 在跑。

    - 讀 ~/.whisper_app/whisper_pro.pid
    - 若 PID 存在且還活著 → SIGTERM（3s）→ SIGKILL
    - 寫入自己的 PID
    - 註冊 atexit 清理

    任何 IO 失敗都只記 log 不拋例外（不能因為 lockfile 機制壞掉就讓 App 起不來）。
    """
    try:
        os.makedirs(os.path.dirname(_PID_FILE), exist_ok=True)

        # 讀舊 PID
        if os.path.exists(_PID_FILE):
            try:
                with open(_PID_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content.isdigit():
                    old_pid = int(content)
                    if old_pid != os.getpid() and _is_pid_alive(old_pid):
                        _kill_stale_process(old_pid)
                    elif old_pid == os.getpid():
                        log.debug(f"SINGLE_INSTANCE: pidfile already owned by us ({old_pid})")
                    else:
                        log.info(f"SINGLE_INSTANCE: stale pidfile (PID {old_pid} dead) — overwriting")
                else:
                    log.warning(f"SINGLE_INSTANCE: pidfile content invalid ({content!r}) — overwriting")
            except Exception as e:
                log.warning(f"SINGLE_INSTANCE: read pidfile failed: {e} — overwriting")

        # 寫入自己的 PID（用 .tmp + replace 確保原子性）
        tmp_path = _PID_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        os.replace(tmp_path, _PID_FILE)
        log.info(f"SINGLE_INSTANCE: lock acquired (PID={os.getpid()}, file={_PID_FILE})")

        # 註冊 atexit 清理
        atexit.register(_remove_pid_file)
    except Exception as e:
        log_error("single_instance_lock_failed", error=str(e))


def _relaunch_app() -> bool:
    """重啟自己（v2.6.0 主題切換用）。

    呼叫者必須**先**完成所有 cleanup（hotkey_mgr.stop / recorder.stop / mini HUD destroy /
    history.db close / Cocoa observer removeObserver_）再呼叫本函式，避免新舊 process
    共存的 race window（Eng Review Issue 1）。

    路徑：
      • 從 `.app` bundle 啟動：`subprocess.Popen(["open", "-n", "...WhisperPro.app"])`
        然後**呼叫者**需要 `sys.exit(0)` 收尾。新 instance 經 LaunchServices 啟動、
        完全獨立於本 process。
      • 從 CLI / dev 模式：`os.execv(sys.executable, [sys.executable, "main.py"])`
        本 process image 被 replace、呼叫者不需要 exit（function 不會 return）。

    Returns:
        True  — spawn 成功（呼叫者該繼續 exit）
        False — 全部失敗（呼叫者應 fallback 顯示 toast 提示手動重啟）

    錯誤處理：所有 exception 都包到 try、寫進 log_error；不 raise。
    """
    import subprocess

    # Bug 1（v2.8.0 / 2026-05-23）：優先試 .app bundle 路徑（不論 sys.executable）。
    # 舊版（v2.6.0/v2.7.0）以 `"WhisperPro.app" in sys.executable` 偵測 bundled，
    # 結果 user 用 `venv/bin/python3 main.py` 跑 dev 模式時 sys.executable 是
    # venv python（不含 WhisperPro.app）→ 直接走 execv → GUI App 已啟動
    # NSApplication mainloop 時 execv 在 macOS 上極易失敗（Cocoa 進程 image 被
    # 替換但 NSApp / Tk 仍 hold 各種 native 資源、TSM 主執行緒斷言等）。
    # 新版改成「.app 存在就用」，dev 模式只要先跑過 build_app.sh 就 OK。
    if _APP_BUNDLE.exists():
        try:
            subprocess.Popen(
                ["open", "-n", str(_APP_BUNDLE)],
                start_new_session=True,
            )
            log.info(
                f"RELAUNCH: spawned new instance via 'open -n {_APP_BUNDLE}' "
                f"(sys.executable={sys.executable})"
            )
            return True
        except Exception as e:
            log_error("relaunch_app_bundle_failed", error=str(e))
            # 不直接 return False、繼續試 execv 兜底
    else:
        log.warning(
            f"RELAUNCH: {_APP_BUNDLE} 不存在；fallback execv（GUI 模式下幾乎一定會失敗、"
            f"建議跑 bash build_app.sh 重建 .app bundle）"
        )

    # Fallback：re-exec 同個 python + main.py
    # 警告：GUI App 已啟動 NSApplication mainloop 時，execv 在 macOS 上極易失敗。
    try:
        main_py = str(_pathlib.Path(__file__).resolve())
        log.info(f"RELAUNCH: os.execv({sys.executable}, [..., {main_py}])")
        os.execv(sys.executable, [sys.executable, main_py])
        # execv 不會 return；走到這行表示真的失敗
        return False
    except Exception as e:
        log_error("relaunch_execv_failed", error=str(e))
        return False


def main() -> None:
    """應用程式主流程：依賴檢查 → 載入設定 → 建立視窗 → 主迴圈。"""

    # ── 0. 抑制 App Nap（Fix 6 Step 2 / 2026-05-22）────────────────────────
    # 必須在 mainloop 前呼叫；token 由 module-level _APP_NAP_TOKEN 強參照保留。
    _disable_app_nap()

    # ── 0a. Single-instance lockfile（v2.18.1 / 2026-05-25）────────────────
    # 防 user 雙擊 .app 時老 process 還在跑 → NSEvent monitor 衝突 silent hang。
    # 若偵測到舊 PID 還活著就先把它幹掉，再寫自己的 PID。
    _acquire_single_instance_lock()

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
        f"hotkey={cfg.hotkey} auto_paste={cfg.auto_paste} auto_copy={cfg.auto_copy} "
        f"theme={cfg.theme}"
    )

    # ── 2a. CustomTkinter appearance mode（v2.6.0）────────────────────────
    # 讓 CTk 內建 widget（scrollbar、dropdown 預設樣式等）跟隨 cfg.theme。
    # 我們自訂 widget 用 tokens.py 的 single-value 常數已自動跟著 theme；
    # 但 CTk 內建預設值只認識 set_appearance_mode 的 Light / Dark。
    ctk.set_appearance_mode("Light" if cfg.theme == "light" else "Dark")

    # ── 3. 建立 tkinter 根視窗 ────────────────────────────────────────────────
    root = ctk.CTk()
    root.title("🎙 Whisper Pro")
    root.geometry(f"{WIN_W}x{WIN_H}")   # 與 gui.py 的 WIN_W / WIN_H 保持一致
    # minsize 高度 860：AppWindow 自然 reqheight ≈ 858（含 ActionBar 58 +
    # StatusBar 32 + 分隔線），舊值 750 會把底部兩列擠掉。寬度 640 維持不變。
    root.minsize(640, 860)
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

        # D4-S4（v2.9.0）：splash 顯示期間（1.5s + 200ms fade）user 若 ⌘Q，
        # root 會被銷毀；splash callback 對已銷毀 root 跑 deiconify 會拋
        # TclError。winfo_exists 檢查避免 log_error 噪音與潛在 race。
        def _splash_done():
            try:
                if root.winfo_exists():
                    root.deiconify()
                    root.lift()
            except Exception:
                log_error("splash_on_done_root_destroyed")

        SplashScreen(
            root,
            on_done=_splash_done,
            version=f"v{__version__}",
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
