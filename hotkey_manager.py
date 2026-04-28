"""
macOS 全域快捷鍵管理器。

功能：
  • HotkeyManager：監聽可設定的組合鍵，觸發時呼叫 callback
  • capture_hotkey()：讓使用者互動式輸入新快捷鍵（給 HotkeyBindDialog 用）
  • parse_hotkey() / format_hotkey()：字串解析與格式化工具函式

執行緒安全修正（相較舊版）：
  1. callback 在鎖外呼叫，消除 callback 內持鎖造成的死鎖風險
  2. stop() 先在鎖內取出 listener 參照、清空狀態，再在鎖外 stop()
  3. 自我修復機制：combo_active 卡住超過 _STALE_COMBO_SEC 秒視為漏收事件，下次 press 自動重置
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional, Set

# pynput 在 macOS 上需要「輔助使用」權限才能監聽全域按鍵
# 如果沒安裝或沒權限，所有功能靜默降級（App 不崩潰）
try:
    from pynput import keyboard as _kb
    from pynput.keyboard import Key, KeyCode
    _PYNPUT_AVAILABLE = True
except ImportError:
    _PYNPUT_AVAILABLE = False

from logger import get_logger, log_action, log_error

log = get_logger("hotkey")

# combo_active=True 持續超過此秒數仍未見 release，視為 pynput 漏收事件。
# 選 2 秒：足夠涵蓋正常「按住說話」場景，不會誤觸發重置。
_STALE_COMBO_SEC = 2.0


# ── 系統查詢 ──────────────────────────────────────────────────────────────────

def is_pynput_available() -> bool:
    """回傳 pynput 是否已安裝且可 import。"""
    return _PYNPUT_AVAILABLE


def check_accessibility() -> bool:
    """檢查目前 process 是否已取得 macOS 輔助使用權限。

    非 macOS 平台永遠回傳 True（不需要此權限）。
    """
    import platform
    if platform.system() != "Darwin":
        return True   # 非 macOS 無需檢查
    try:
        from ApplicationServices import AXIsProcessTrusted
        return AXIsProcessTrusted()
    except Exception:
        return False   # 無法查詢時保守回傳 False


# ── 符號對照表 ────────────────────────────────────────────────────────────────

# 組合鍵字串中的修飾鍵名稱 → macOS 鍵盤符號
# 同時支援 macOS 慣稱：option=alt（⌥）、command=cmd（⌘）、return=enter
_SYMBOL_MAP: dict[str, str] = {
    "cmd":     "⌘",
    "command": "⌘",
    "ctrl":    "⌃",
    "control": "⌃",
    "alt":     "⌥",
    "option":  "⌥",   # macOS 慣稱
    "opt":     "⌥",
    "shift":   "⇧",
    "space":   "Space",
    "return":  "↩",
    "enter":   "↩",
    "tab":     "⇥",
    "esc":     "⎋",
    "escape":  "⎋",
}


# ── 字串解析與格式化 ──────────────────────────────────────────────────────────

def parse_hotkey(combo: str) -> set:
    """將 'cmd+alt+r' 格式的字串解析為 pynput Key / char 物件的集合。

    Args:
        combo: 加號分隔的按鍵組合字串，例如 "cmd+alt+r"。

    Returns:
        pynput 鍵值的集合；pynput 不可用時回傳空集合。
    """
    if not _PYNPUT_AVAILABLE:
        return set()
    res: set = set()
    for p in combo.lower().split("+"):
        p = p.strip()
        # 接受 macOS 慣稱：option=alt、command=cmd、control=ctrl、opt=alt
        if p in ("cmd", "command"):
            res.add(Key.cmd)
        elif p == "shift":
            res.add(Key.shift)
        elif p in ("alt", "option", "opt"):
            res.add(Key.alt)
        elif p in ("ctrl", "control"):
            res.add(Key.ctrl)
        elif p == "space":
            res.add(Key.space)
        elif p in ("return", "enter"):
            res.add(Key.enter)
        elif p == "tab":
            res.add(Key.tab)
        elif p in ("esc", "escape"):
            res.add(Key.esc)
        elif len(p) == 1:
            res.add(p)   # 單字元直接加入（例如 "r"）
        else:
            try:
                res.add(getattr(Key, p, p))   # 嘗試當作 Key 屬性名稱
            except Exception:
                pass
    return res


def format_hotkey(combo: str) -> str:
    """將組合鍵字串格式化為 macOS 符號顯示，例如 'cmd+alt+r' → '⌘⌥R'。"""
    parts = combo.lower().split("+")
    return "".join(_SYMBOL_MAP.get(p.strip(), p.strip().upper()) for p in parts)


def _normalize_key(key) -> object:
    """將左右修飾鍵（cmd_l / cmd_r 等）統一映射到正規名稱（cmd）。

    pynput 會分別回報左右側修飾鍵；我們的組合比對不需要區分左右。
    """
    if not _PYNPUT_AVAILABLE:
        return key
    # 將左右 Command 統一為 Key.cmd
    if key in (Key.cmd,   Key.cmd_l,   Key.cmd_r):   return Key.cmd
    if key in (Key.shift, Key.shift_l, Key.shift_r): return Key.shift
    if key in (Key.alt,   Key.alt_l,   Key.alt_r):   return Key.alt
    if key in (Key.ctrl,  Key.ctrl_l,  Key.ctrl_r):  return Key.ctrl
    # 有 char 屬性的鍵（字母、數字）取小寫字元
    if hasattr(key, "char") and key.char:
        return key.char.lower()
    return key


def _keys_to_combo(keys: set) -> str:
    """將正規化後的 pynput 鍵值集合轉換回 'cmd+alt+r' 格式字串。

    修飾鍵依固定順序排列（cmd > ctrl > alt > shift），再接字母鍵。
    """
    # 修飾鍵的標準順序
    order = [
        (Key.cmd,   "cmd"),
        (Key.ctrl,  "ctrl"),
        (Key.alt,   "alt"),
        (Key.shift, "shift"),
    ]
    parts: list[str] = []
    remaining = set(keys)

    # 先處理修飾鍵（按固定順序）
    for k, name in order:
        if k in remaining:
            parts.append(name)
            remaining.discard(k)

    # 再處理非修飾鍵（字母、空白等）
    for k in sorted(remaining, key=str):
        if isinstance(k, str):
            parts.append(k)
        elif k is Key.space:
            parts.append("space")
        elif hasattr(k, "name"):
            parts.append(k.name)
        else:
            parts.append(str(k).replace("Key.", ""))

    return "+".join(parts) if parts else ""


# ── 互動式按鍵擷取 ────────────────────────────────────────────────────────────

def capture_hotkey(timeout: float = 15.0) -> Optional[str]:
    """阻塞直到使用者按下（並開始放開）一個組合鍵，回傳組合字串。

    用於 HotkeyBindDialog 讓使用者重新設定快捷鍵。
    注意：macOS 26.4+ 上此函式在主執行緒可能崩潰（TSM 斷言），
    請改用 HotkeyBindDialog 的 Tk 原生事件擷取方案。

    Args:
        timeout: 等待超時秒數，超過後回傳 None。

    Returns:
        組合字串（例如 "cmd+alt+r"），超時或 pynput 不可用時回傳 None。
    """
    if not _PYNPUT_AVAILABLE:
        return None

    done        = threading.Event()
    current:    set = set()
    max_combo:  list[set] = [set()]
    result:     list[Optional[str]] = [None]

    def _on_press(key):
        """pynput press callback：追蹤目前按住的鍵組合。"""
        nk = _normalize_key(key)
        current.add(nk)
        if len(current) > len(max_combo[0]):
            max_combo[0] = set(current)   # 記錄歷史最大組合

    def _on_release(key):
        """pynput release callback：第一個鍵放開時鎖定結果。"""
        if max_combo[0] and not done.is_set():
            result[0] = _keys_to_combo(max_combo[0])
            done.set()
            return False   # 回傳 False 讓 pynput 停止監聽
        nk = _normalize_key(key)
        current.discard(nk)

    listener = _kb.Listener(on_press=_on_press, on_release=_on_release)
    listener.daemon = True
    listener.start()
    done.wait(timeout=timeout)
    if listener.running:
        listener.stop()

    return result[0]


# ── HotkeyManager ─────────────────────────────────────────────────────────────

class HotkeyManager:
    """監聽可設定組合鍵的全域快捷鍵管理器。

    執行緒安全：所有共享狀態受 _lock 保護，但 callback 在鎖外呼叫，
    避免 callback 內持鎖造成死鎖。

    使用方式：
        mgr = HotkeyManager(on_press_cb=start_recording, on_release_cb=stop_recording)
        mgr.restart("cmd+alt+r")   # 開始監聽
        mgr.stop()                 # 停止監聽（例如 App 關閉時）
    """

    def __init__(self, on_press_cb: Callable, on_release_cb: Callable) -> None:
        """初始化管理器（不立即開始監聽；需呼叫 restart()）。

        Args:
            on_press_cb:   組合鍵按下時的回呼（在主執行緒 marshal 後呼叫）。
            on_release_cb: 組合鍵放開時的回呼。
        """
        self._on_press_cb    = on_press_cb
        self._on_release_cb  = on_release_cb
        self._hotkeys:       set  = set()            # 目前監聽的鍵值集合
        self._pressed:       Set  = set()            # 目前按住的鍵值集合
        self._combo_active:  bool = False            # 組合鍵是否已觸發
        self._combo_active_at: float = 0.0           # 最後一次觸發的時間戳
        self._last_event_at:   float = 0.0           # 最後一次事件的時間戳
        self._listener: Optional[_kb.Listener] = None
        self._lock = threading.Lock()

    def restart(self, combo: str) -> None:
        """停止舊的監聽器並以新組合鍵啟動新監聽器。

        Args:
            combo: 組合鍵字串，例如 "cmd+alt+r"。
        """
        self.stop()
        self._hotkeys = parse_hotkey(combo)
        self._combo_str = combo
        log.info(f"HOTKEY: Starting listener for combo='{combo}' keys={self._hotkeys}")
        self._listener = _kb.Listener(
            on_press=self._on_p,
            on_release=self._on_r,
            suppress=False,   # 不阻止按鍵事件傳到其他 App
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        """停止監聽器並清空所有狀態。

        設計重點：先在鎖內取出 listener 參照、清空狀態，
        再在鎖外呼叫 stop()，避免 pynput 執行緒持鎖時造成死鎖。
        """
        listener_to_stop = None
        with self._lock:
            listener_to_stop = self._listener
            self._listener   = None
            self._pressed.clear()
            self._combo_active = False
        # 在鎖外停止，避免 pynput 執行緒中持鎖等待造成死鎖
        if listener_to_stop is not None:
            log.info("HOTKEY: Stopping listener...")
            try:
                listener_to_stop.stop()
            except Exception:
                log_error("hotkey_stop_failed")

    def _normalize(self, key) -> object:
        """正規化左右修飾鍵（委派給模組層函式）。"""
        return _normalize_key(key)

    def _on_p(self, key) -> None:
        """pynput 按鍵事件：追蹤按下的鍵，判斷是否觸發組合鍵。"""
        nk  = self._normalize(key)
        now = time.monotonic()
        fire   = False
        healed = False

        with self._lock:
            # 自我修復：combo_active 卡太久 = pynput 漏收 release 事件，強制重置
            if (
                self._combo_active
                and (now - self._combo_active_at) > _STALE_COMBO_SEC
            ):
                healed = True
                self._combo_active = False
                self._pressed.clear()

            self._pressed.add(nk)
            self._last_event_at = now

            # 所有必要鍵都已按下 → 首次觸發（不重複觸發）
            if not self._combo_active and self._hotkeys.issubset(self._pressed):
                self._combo_active    = True
                self._combo_active_at = now
                fire = True

        if healed:
            log.warning("HOTKEY: self-heal — stale combo_active cleared")
        if nk in self._hotkeys:
            log.debug(f"HOTKEY: press   {nk!r:>10}  pressed={sorted(self._pressed, key=str)}")
        if fire:
            combo = getattr(self, "_combo_str", "?")
            log_action("hotkey_pressed", combo=combo)
            self._on_press_cb()   # 在鎖外呼叫，避免 callback 內持鎖死鎖

    def _on_r(self, key) -> None:
        """pynput 放開事件：若組合鍵放開則觸發 release callback。"""
        nk  = self._normalize(key)
        now = time.monotonic()
        fire = False

        with self._lock:
            # 只有組合鍵中的某個鍵放開時才觸發 release
            if self._combo_active and nk in self._hotkeys:
                self._combo_active = False
                fire = True
            self._pressed.discard(nk)
            self._last_event_at = now

        if nk in self._hotkeys:
            log.debug(f"HOTKEY: release {nk!r:>10}  pressed={sorted(self._pressed, key=str)}")
        if fire:
            combo = getattr(self, "_combo_str", "?")
            log_action("hotkey_released", combo=combo)
            self._on_release_cb()   # 在鎖外呼叫
