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


def get_frontmost_app() -> Optional[str]:
    """取得目前最前景的 macOS 應用程式名稱。

    透過 osascript 執行一小段 AppleScript，查詢 System Events
    目前 frontmost=true 的 process 名稱。

    Returns:
        前景 App 的顯示名稱字串（例如 "Notion"、"Slack"），
        若 osascript 失敗或回傳空字串則回傳 None。
    """
    try:
        result = subprocess.run(
            [
                "osascript", "-e",
                # AppleScript：透過 System Events 查詢前景 process 名稱
                'tell application "System Events" '
                'to get name of first process whose frontmost is true',
            ],
            capture_output=True,
            text=True,
            timeout=2,   # 防止 osascript 卡住阻塞 UI 執行緒
        )
        name = result.stdout.strip()
        return name or None   # 空字串轉 None，讓呼叫端統一判斷
    except Exception as e:
        print(f"AUTO-PASTE: get_frontmost_app failed: {e}")
        return None


def paste_to_app(
    text: str,
    app_name: Optional[str],
    activate_delay: float = 0.18,   # 等待 App 拉到前景的緩衝時間（秒）
) -> bool:
    """將文字貼到指定 App 的當前游標位置。

    三步驟：
      1. 把 text 寫入系統剪貼簿
      2. 用 osascript 把 app_name 拉到前景
      3. 用 pynput 模擬 ⌘V

    Args:
        text:           要貼上的文字。
        app_name:       目標 App 名稱（由 get_frontmost_app() 取得），None 則跳過拉前景。
        activate_delay: 拉前景後等待的秒數，給 App 時間完成視窗切換。

    Returns:
        True 代表 ⌘V 成功送出，False 代表任何步驟失敗。
    """

    # ── 步驟 1：寫入剪貼簿 ───────────────────────────────────────────────────
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception as e:
        print(f"AUTO-PASTE: clipboard write failed: {e}")
        return False

    # ── 步驟 2：把目標 App 拉到前景 ─────────────────────────────────────────
    if app_name:
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "{app_name}" to activate'],
                timeout=3,   # 若 App 無回應，最多等 3 秒
            )
            time.sleep(activate_delay)   # 給 App 時間完成視窗切換，再送 ⌘V
        except Exception as e:
            print(f"AUTO-PASTE: activate '{app_name}' failed: {e}")
            # 繼續往下執行——貼到目前焦點視窗，雖不完美但比什麼都不做好

    # ── 步驟 3：模擬 ⌘V ─────────────────────────────────────────────────────
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        with kb.pressed(Key.cmd):   # 按住 Command 鍵
            kb.tap("v")             # 按 V，等同 ⌘V
        print(f"AUTO-PASTE: ⌘V sent → '{app_name}'")
        return True
    except Exception as e:
        print(f"AUTO-PASTE: keyboard simulation failed: {e}")
        return False
