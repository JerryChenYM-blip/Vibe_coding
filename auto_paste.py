"""
自動貼上模組：偵測前景 App 並將文字注入游標位置。

流程：
  1. 錄音開始前呼叫 get_frontmost_app() 記錄目前前景應用程式名稱
  2. 轉錄（與潤飾）完成後呼叫 paste_to_app()：
       a. 將文字寫入剪貼簿（pyperclip）
       b. 用 osascript 把目標 App 拉回前景
       c. 用 pynput 模擬 ⌘V 貼上

限制：
  • 僅支援 macOS（osascript 與 ⌘V 為 macOS 專屬）
  • 需要「輔助使用」權限（pynput 鍵盤模擬）
"""

from __future__ import annotations

import subprocess
import time
from typing import Optional

from logger import get_logger, log_error

log = get_logger("auto_paste")


def get_frontmost_app() -> Optional[str]:
    """取得目前最前景的 macOS 應用程式名稱。

    Bug B（v2.12.0 / 2026-05-23）：優先用 NSWorkspace.frontmostApplication()
    （Cocoa 原生 API，比 osascript 可靠 + 快 ~100x）。NSWorkspace 跨 Space /
    全螢幕都能正確回應，不會被 osascript timeout 卡住。失敗時 fallback osascript。

    D5-S10（v2.10.0）：osascript 路徑保留 timeout / 多行容錯。

    Returns:
        前景 App 的 localized name（例如 "Claude"、"Notes"），失敗回 None。
    """
    # 優先嘗試 NSWorkspace（Cocoa native，無 timeout 風險、不會回 'Python'）
    try:
        from AppKit import NSWorkspace  # type: ignore
        ws = NSWorkspace.sharedWorkspace()
        app = ws.frontmostApplication()
        if app is not None:
            name = app.localizedName()
            if name:
                log.debug(f"AUTO-PASTE: frontmost (NSWorkspace) = '{name}'")
                return str(name)
    except Exception:
        log_error("get_frontmost_app_nsworkspace_failed")

    # Fallback：osascript（保留 D5-S10 容錯處理）
    try:
        result = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" '
                'to get name of first process whose frontmost is true',
            ],
            capture_output=True,
            text=True,
            timeout=1.2,
        )
        if result.returncode != 0:
            log_error(
                "get_frontmost_app_nonzero",
                rc=result.returncode,
                stderr=(result.stderr or "")[:200],
            )
            return None
        raw = (result.stdout or "").strip()
        if not raw:
            return None
        first_line = raw.splitlines()[0].strip()
        log.debug(f"AUTO-PASTE: frontmost (osascript) = '{first_line}'")
        return first_line or None
    except subprocess.TimeoutExpired:
        log_error("get_frontmost_app_timeout", timeout_s=1.2)
        return None
    except Exception:
        log_error("get_frontmost_app_failed")
        return None


def paste_to_app(
    text: str,
    app_name: Optional[str],
    activate_delay: float = 0.18,            # 一般情況的緩衝（秒）
    fullscreen_activate_delay: float = 0.55, # 全螢幕 Space 切換的緩衝（秒）
    max_wait_for_frontmost: float = 1.5,     # frontmost polling 最久等多久
) -> bool:
    """將文字貼到指定 App 的當前游標位置。

    三步驟：
      1. 把 text 寫入系統剪貼簿
      2. 用 osascript 把 app_name 拉到前景
      3. **Poll frontmost 確認真的切過去**（Bug 2 / 2026-05-23：全螢幕 Space 切換動畫 ≈ 0.5s）
      4. 用 pynput 模擬 ⌘V

    Args:
        text:                       要貼上的文字。
        app_name:                   目標 App 名稱（由 get_frontmost_app() 取得），None 則跳過拉前景。
        activate_delay:             一般情況下 activate 後的初始等待時間。
        fullscreen_activate_delay:  目標 App 在全螢幕獨立 Space 時的初始等待（更長，
                                    給 macOS Space 切換動畫時間）。
        max_wait_for_frontmost:     polling frontmost 切換的最大等待秒數。

    Returns:
        True 代表 ⌘V 成功送出，False 代表任何步驟失敗。
    """

    # ── 步驟 1：寫入剪貼簿 ───────────────────────────────────────────────────
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception:
        log_error("auto_paste_clipboard_failed", text_len=len(text))
        return False

    # ── 步驟 2：把目標 App 拉到前景 + 等 Space 切換 ─────────────────────────
    if app_name:
        # Bug 2（v2.8.0 / 2026-05-23）：先偵測目標 App 是否在全螢幕 Space。
        # 全螢幕 App 的 NSWindow 屬於獨立 Space，activate 觸發 OS 跨 Space 動畫
        # （~0.5s）；0.18s 太短，⌘V 還在原 Space 觸發 → 落到錯誤的 App。
        is_fullscreen = _is_app_fullscreen(app_name)
        delay = fullscreen_activate_delay if is_fullscreen else activate_delay
        # v2.13.0 / 2026-05-24：activate 改 NSWorkspace 優先（osascript fallback）。
        # NSWorkspace.runningApplications + activateWithOptions 比 osascript 快
        # ~50 倍（<1ms vs 50ms），且跨 Space 行為更可靠。
        activated_via_native = False
        try:
            from AppKit import (  # type: ignore
                NSWorkspace,
                NSApplicationActivateIgnoringOtherApps,
            )
            ws = NSWorkspace.sharedWorkspace()
            for running in ws.runningApplications():
                if running.localizedName() == app_name:
                    running.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
                    activated_via_native = True
                    break
        except Exception:
            log_error("auto_paste_activate_nsworkspace_failed", app=app_name)

        if not activated_via_native:
            # Fallback：osascript（既有 D5-S10 容錯）
            try:
                safe_name = app_name.replace("\\", "\\\\").replace('"', '\\"')
                try:
                    result = subprocess.run(
                        ["osascript", "-e", f'tell application "{safe_name}" to activate'],
                        capture_output=True, text=True, timeout=1.5,
                    )
                    if result.returncode != 0:
                        log_error(
                            "auto_paste_activate_nonzero",
                            app=app_name,
                            rc=result.returncode,
                            stderr=(result.stderr or "")[:200],
                        )
                except subprocess.TimeoutExpired:
                    log_error("auto_paste_activate_timeout", app=app_name, timeout_s=1.5)
            except Exception:
                log_error("auto_paste_activate_failed", app=app_name)
        try:
            time.sleep(delay)

            # Bug 2：activate 後 poll frontmost 確認真的切過去（最多再等 max_wait）。
            # 若 max_wait 過了還沒切，依然繼續送 ⌘V（避免使用者被永久卡住）。
            deadline = time.monotonic() + max_wait_for_frontmost
            poll_count = 0
            while time.monotonic() < deadline:
                front = get_frontmost_app()
                poll_count += 1
                if front and front == app_name:
                    break
                time.sleep(0.08)
            else:
                # 迴圈正常結束（沒 break）= 超時但 frontmost 仍不對
                log.warning(
                    f"AUTO-PASTE: frontmost still != '{app_name}' after "
                    f"{max_wait_for_frontmost}s (fullscreen={is_fullscreen}, "
                    f"polls={poll_count}). 仍送 ⌘V。"
                )
        except Exception:
            log_error("auto_paste_activate_failed", app=app_name)

    # ── 步驟 3：模擬 ⌘V ─────────────────────────────────────────────────────
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        with kb.pressed(Key.cmd):
            kb.tap("v")
        log.info(
            f"AUTO-PASTE: ⌘V sent → '{app_name}' "
            f"(text_len={len(text)}, fullscreen={_is_app_fullscreen(app_name) if app_name else 'n/a'})"
        )
        # v2.19.0：pipeline event「paste_done」、觀測性失敗不影響主流程
        try:
            from pipeline_id import event as pipeline_event  # type: ignore
            pipeline_event(
                "paste_done", target_app=app_name or "",
                text_len=len(text), success=True,
            )
        except Exception:
            pass
        return True
    except Exception:
        log_error("auto_paste_keyboard_failed", app=app_name)
        try:
            from pipeline_id import event as pipeline_event  # type: ignore
            pipeline_event(
                "paste_done", target_app=app_name or "",
                text_len=len(text), success=False,
            )
        except Exception:
            pass
        return False


def _is_app_fullscreen(app_name: str) -> bool:
    """Bug 2（v2.8.0）：偵測指定 App 的前景視窗是否處於全螢幕（獨立 Space）狀態。

    用 AppleScript 問 System Events 該 process 第一個 window 的 `value of attribute "AXFullScreen"`。
    回 True 表示綠按鈕全螢幕、視窗在獨立 Space。

    任何錯誤（App 不存在、權限不足、無 AXFullScreen 屬性）都回 False（保守處理：
    走一般 activate_delay 即可，不會更慘）。
    """
    if not app_name:
        return False
    try:
        safe_name = app_name.replace("\\", "\\\\").replace('"', '\\"')
        result = subprocess.run(
            [
                "osascript", "-e",
                f'tell application "System Events" to tell process "{safe_name}" '
                f'to get value of attribute "AXFullScreen" of front window',
            ],
            capture_output=True, text=True, timeout=1.0,
        )
        out = result.stdout.strip().lower()
        return out == "true"
    except Exception:
        return False
