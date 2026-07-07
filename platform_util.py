"""平台偵測 —— Mac/Windows 雙棲的單一真相來源。

Whisper Pro 從 macOS 版（v2.21.6）演進成 mac/windows 雙棲：同一份程式碼在兩個
作業系統上跑，各自用對的後端。所有平台相關分流都從這裡讀布林值，不要在各檔案
散寫 `sys.platform == ...`。

設計鐵律：
- **macOS 路徑必須跟移植前 byte-identical**——雙棲是純加法、不改動 Mac 既有行為
  （移植資料夾在 mac 上跑 pytest 必須跟原版一樣全綠）。
- Windows 專屬 import（ctypes.windll / winsound 等）一律延後到「確定在 Windows」
  的分支內或包 try/except，讓模組在 mac 上仍可 import + py_compile。
"""
from __future__ import annotations

import sys

IS_MAC: bool = sys.platform == "darwin"
IS_WINDOWS: bool = sys.platform == "win32"
IS_LINUX: bool = sys.platform.startswith("linux")

# 貼上快捷鍵的 modifier：mac 用 Cmd（⌘V）、Windows/Linux 用 Ctrl（Ctrl+V）
PASTE_MODIFIER_IS_CMD: bool = IS_MAC

# 預設全域錄音熱鍵：mac 用單按右 Cmd；Windows 沒有右 Cmd、用 ctrl+alt+r 組合
DEFAULT_HOTKEY: str = "right_cmd" if IS_MAC else "ctrl+alt+r"


def platform_name() -> str:
    if IS_MAC:
        return "macOS"
    if IS_WINDOWS:
        return "Windows"
    if IS_LINUX:
        return "Linux"
    return sys.platform
