"""
macOS 全域快捷鍵管理器。

功能：
  • HotkeyManager：監聽可設定的組合鍵，觸發時呼叫 callback
  • capture_hotkey()：DEPRECATED（HotkeyBindDialog 改用 Tk 原生 binding）
  • parse_hotkey() / format_hotkey()：字串解析與格式化工具函式

行為：tap toggle（點按切換）
  • 一次「完整按下→放開」算一個 tap，fire 在 release 上
  • 按住 5 秒再放開 → 仍然只算 1 個 tap（不是 start+stop）
  • 只按部分組合（例如 cmd+alt 沒按 r）→ 不 fire
  • 由呼叫端（GUI）依目前狀態決定 tap 對應的動作（開始或停止錄音）

後端（2026-05-22 改）：
  根治 pynput 1.7.7 在 macOS 26.4+ 的所有 crash（TSM 主執行緒斷言、
  SLEventTapIsEnabled PAC violation、idle timeout disable）。
  改用 macOS 原生 NSEvent.addGlobalMonitorForEventsMatchingMask_handler_
  + addLocalMonitorForEventsMatchingMask_handler_。
    • handler 永遠在主執行緒（Cocoa main runloop）執行；無 thread、無 ctypes、
      無 TSM 同步問題
    • global monitor：其他 App 焦點時收事件
    • local monitor：本 App 焦點時收事件（global 不會發給自己）
    • NSEvent 不會被 idle 自動 disable（不是 CGEventTap），無需 re-enable
  pynput 仍 import 用於：
    • parse_hotkey() 公開介面的 Key 物件回傳值
    • auto_paste.py 的 keyboard.Controller（送 ⌘V，主執行緒呼叫安全）
  若 PyObjC 不可用，退化用 pynput Listener 作為 fallback（舊行為）。

執行緒安全要點：
  1. NSEvent handler 在主執行緒呼叫，可直接動 Tk widget（不需 self.after marshal）
  2. handler 內仍包 try/except，意外 exception 不可外洩到 Cocoa runtime
  3. stop() 先在鎖內取出 monitor 參照、清空狀態，再在鎖外 removeMonitor:
  4. 自我修復機制：combo_active 卡住超過 _STALE_COMBO_SEC 秒視為漏收事件，下次 press 自動重置
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

# NSEvent 後端（PyObjC）。需要：pip install pyobjc-framework-Cocoa（已隨 PyObjC 一併安裝）
# 載入失敗時自動退化到 pynput fallback（保留舊行為）。
try:
    from AppKit import (
        NSEvent,
        NSEventMaskFlagsChanged,
        NSEventMaskKeyDown,
        NSEventMaskKeyUp,
        NSEventModifierFlagCommand,
        NSEventModifierFlagOption,
        NSEventModifierFlagControl,
        NSEventModifierFlagShift,
    )
    _NSEVENT_AVAILABLE = True
except ImportError:
    _NSEVENT_AVAILABLE = False

from logger import get_logger, log_action, log_error

log = get_logger("hotkey")

# combo_active=True 持續超過此秒數仍未見 release，視為漏收事件。
# 選 10 秒：toggle 模式下使用者可能按住很久才放（雖無此必要），給足容忍度。
_STALE_COMBO_SEC = 10.0


# ── macOS 虛擬鍵碼 → 側別感知 modifier ─────────────────────────────────────────
# 來源：HIToolbox/Events.h（kVK_*）。所有 macOS 鍵盤都遵守此編碼。
# 用於 NSEvent.keyCode() 對 FlagsChanged 事件的轉換。
_KEYCODE_TO_SIDED_MOD: dict[int, str] = {
    54:  "cmd_r",
    55:  "cmd_l",
    58:  "alt_l",     # left option
    61:  "alt_r",     # right option
    59:  "ctrl_l",
    62:  "ctrl_r",
    56:  "shift_l",
    60:  "shift_r",
}

# 側別 modifier 名稱 → 該 modifier 對應的 NSEvent 修飾旗標 bit。
# 用來判斷某顆 modifier 在 FlagsChanged 事件當下是「按下」還是「放開」。
def _modifier_bit_for(sided_name: str) -> int:
    if _NSEVENT_AVAILABLE:
        if sided_name.startswith("cmd"):   return NSEventModifierFlagCommand
        if sided_name.startswith("alt"):   return NSEventModifierFlagOption
        if sided_name.startswith("ctrl"):  return NSEventModifierFlagControl
        if sided_name.startswith("shift"): return NSEventModifierFlagShift
    return 0


# macOS keycode → US keyboard 字元（用於 KeyDown/KeyUp 事件比對 combo 中的字母鍵）。
# 只需要覆蓋 combo 字串會出現的字母 / 數字 / space。沒列到的鍵照 NSEvent
# charactersIgnoringModifiers() 回傳，保險用。
_KEYCODE_TO_CHAR: dict[int, str] = {
    0:  "a",  11: "b",  8:  "c",  2:  "d",  14: "e",  3:  "f",  5:  "g",  4:  "h",
    34: "i",  38: "j",  40: "k",  37: "l",  46: "m",  45: "n",  31: "o",  35: "p",
    12: "q",  15: "r",  1:  "s",  17: "t",  32: "u",  9:  "v",  13: "w",  7:  "x",
    16: "y",  6:  "z",
    29: "0",  18: "1",  19: "2",  20: "3",  21: "4",  23: "5",  22: "6",  26: "7",
    28: "8",  25: "9",
    49: " ",   # space
}


def _sided_name_to_pynput_key(sided_name: str):
    """側別 modifier 名稱（cmd_r 等）→ pynput Key 物件（與 parse_hotkey 一致）。

    用於把 NSEvent 收到的 modifier 事件對齊到 _pressed 集合的型別，
    讓 `_hotkeys.issubset(_pressed)` 等既有邏輯不必改。
    """
    if not _PYNPUT_AVAILABLE:
        return sided_name   # 退化：拿名稱當識別符（不會有匹配，但不會崩）
    return {
        "cmd_r":   Key.cmd_r,
        "cmd_l":   Key.cmd_l,
        "alt_r":   Key.alt_r,
        "alt_l":   Key.alt_l,
        "ctrl_r":  Key.ctrl_r,
        "ctrl_l":  Key.ctrl_l,
        "shift_r": Key.shift_r,
        "shift_l": Key.shift_l,
    }.get(sided_name, sided_name)


# ── 系統查詢 ──────────────────────────────────────────────────────────────────

def is_pynput_available() -> bool:
    """回傳 pynput 是否已安裝且可 import。"""
    return _PYNPUT_AVAILABLE


def is_nsevent_available() -> bool:
    """回傳 PyObjC（AppKit.NSEvent）是否可用。

    NSEvent 後端是 macOS 26.4+ 的首選；不可用時退化到 pynput Listener。
    """
    return _NSEVENT_AVAILABLE


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

    支援兩種格式：
      (1) 組合鍵：'cmd+alt+r' / 'ctrl+shift+space'
      (2) Lone modifier（單一鍵）：'right_cmd' / 'left_option' / 'right_ctrl' / ...
          側別感知，pynput 對應 Key.cmd_r / Key.alt_l / Key.ctrl_r ...

    Args:
        combo: 加號分隔的按鍵組合字串，或單一鍵字串。

    Returns:
        pynput 鍵值的集合；pynput 不可用時回傳空集合。
    """
    if not _PYNPUT_AVAILABLE:
        return set()

    # Lone-modifier 字串對照：左右側 modifier 直接 map 到對應的側別 Key
    _LONE_MOD_MAP = {
        "right_cmd":    Key.cmd_r,
        "left_cmd":     Key.cmd_l,
        "right_option": Key.alt_r,
        "left_option":  Key.alt_l,
        "right_alt":    Key.alt_r,
        "left_alt":     Key.alt_l,
        "right_ctrl":   Key.ctrl_r,
        "left_ctrl":    Key.ctrl_l,
        "right_shift":  Key.shift_r,
        "left_shift":   Key.shift_l,
    }
    c = combo.lower().strip()
    if c in _LONE_MOD_MAP:
        # 單鍵 lone modifier — 不展開為左右合一，保持側別資訊
        return {_LONE_MOD_MAP[c]}

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


# Lone-modifier 模式的目標鍵集合（側別感知）
def _is_lone_modifier_key(key) -> bool:
    """檢查一個 pynput key 是否為左右側 modifier（用於 lone-modifier 判定）。"""
    if not _PYNPUT_AVAILABLE:
        return False
    return key in (
        Key.cmd_l,  Key.cmd_r,
        Key.alt_l,  Key.alt_r,
        Key.ctrl_l, Key.ctrl_r,
        Key.shift_l, Key.shift_r,
    )


def format_hotkey(combo: str) -> str:
    """將組合鍵字串格式化為 macOS 符號顯示。

      'cmd+alt+r'   → '⌘⌥R'
      'right_cmd'   → 'R⌘'   （側別 + 符號）
      'left_option' → 'L⌥'
    """
    _LONE_MOD_DISPLAY = {
        "right_cmd":    "R⌘",
        "left_cmd":     "L⌘",
        "right_option": "R⌥",
        "left_option":  "L⌥",
        "right_alt":    "R⌥",
        "left_alt":     "L⌥",
        "right_ctrl":   "R⌃",
        "left_ctrl":    "L⌃",
        "right_shift":  "R⇧",
        "left_shift":   "L⇧",
    }
    c = combo.lower().strip()
    if c in _LONE_MOD_DISPLAY:
        return _LONE_MOD_DISPLAY[c]
    parts = combo.lower().split("+")
    return "".join(_SYMBOL_MAP.get(p.strip(), p.strip().upper()) for p in parts)


def _normalize_key(key) -> object:
    """將左右修飾鍵（cmd_l / cmd_r 等）統一映射到正規名稱（cmd）。

    pynput 會分別回報左右側修飾鍵；組合鍵比對不需要區分左右，統一收斂。
    Lone-modifier 模式需保留側別，請用 `_normalize_key_sided`。
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


def _normalize_key_sided(key) -> object:
    """側別感知的正規化（給 lone-modifier 模式用）。

    左右側 modifier 各自保留（不像 _normalize_key 那樣收斂為 Key.cmd）；
    其他鍵與 _normalize_key 同。
    pynput 在 macOS 上可能回 Key.cmd（無側別後綴）—— 視為「不確定側別」，
    照樣回傳該物件，比對時不會匹配到 Key.cmd_r 等具側別目標，自然忽略。
    """
    if not _PYNPUT_AVAILABLE:
        return key
    # 側別 modifier 保留原樣
    if key in (
        Key.cmd_l,  Key.cmd_r,
        Key.alt_l,  Key.alt_r,
        Key.ctrl_l, Key.ctrl_r,
        Key.shift_l, Key.shift_r,
    ):
        return key
    # 無側別 modifier（少見）也原樣保留
    if key in (Key.cmd, Key.alt, Key.ctrl, Key.shift):
        return key
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
    """DEPRECATED：請改用 HotkeyBindDialog 的 Tk 原生 binding。

    舊版用 pynput Listener 在背景執行緒擷取按鍵，但 macOS 26.4+ 主執行緒
    會觸發 TSM 斷言崩潰；2026-05-22 之後改為由 gui.py HotkeyBindDialog 直接
    用 Tk 原生 `<KeyPress>` binding 在主執行緒收，本函式不再使用。
    保留簽章供舊呼叫端不爆 import，但永遠回傳 None。
    """
    return None


# ── HotkeyManager ─────────────────────────────────────────────────────────────

class HotkeyManager:
    """監聽可設定組合鍵的全域快捷鍵管理器（tap toggle 模式）。

    執行緒安全：所有共享狀態受 _lock 保護，但 callback 在鎖外呼叫，
    避免 callback 內持鎖造成死鎖。

    Tap 語意：
      • 一次「完整按下整個組合 → 任一鍵放開」= 1 個 tap，fire 在 release 上
      • 只按部分組合（例如只按 cmd+alt 沒按 r）→ 不 fire
      • 按住 5 秒再放開 → 仍是 1 個 tap（不重複觸發）
      • 由呼叫端依自身狀態決定 tap 要做的動作（start vs stop）

    使用方式：
        mgr = HotkeyManager(on_tap_cb=toggle_recording)
        mgr.restart("cmd+alt+r")   # 開始監聽
        mgr.stop()                 # 停止監聽（例如 App 關閉時）
    """

    def __init__(self, on_tap_cb: Callable) -> None:
        """初始化管理器（不立即開始監聽；需呼叫 restart()）。

        Args:
            on_tap_cb: 組合鍵 tap（完整按下後放開）時的回呼；
                       NSEvent 後端在主執行緒呼叫；pynput fallback 後端在背景執行緒。
        """
        self._on_tap_cb      = on_tap_cb
        self._hotkeys:       set  = set()            # 目前監聽的鍵值集合
        self._pressed:       Set  = set()            # 目前按住的鍵值集合
        self._combo_active:  bool = False            # 組合鍵是否已完整按下（pending release 觸發 tap）
        self._combo_active_at: float = 0.0           # 最後一次完整按下的時間戳
        self._last_event_at:   float = 0.0           # 最後一次事件的時間戳
        self._monitor_global = None                    # NSEvent 後端：其他 App focus 時收事件
        self._monitor_local  = None                    # NSEvent 後端：本 App focus 時收事件
        self._backend:       str = "none"              # "nsevent" / "none"
        # 相容性殼：watchdog 仍會檢查 `_listener is not None`（commit 3 才砍）。
        # 啟用 NSEvent 後此屬性永遠為 None；不再 spawn pynput Listener。
        self._listener = None
        self._lock = threading.Lock()
        # Lone-modifier 模式狀態
        # 啟用條件：len(_hotkeys) == 1 且該鍵為側別 modifier
        # _is_lone_mode      —— 啟用 lone-modifier 邏輯
        # _lone_target_key   —— 目標 modifier（如 Key.cmd_r）
        # _other_key_during_press —— 目標 modifier 按住期間，有沒有其他鍵被按
        self._is_lone_mode:   bool = False
        self._lone_target_key      = None
        self._other_key_during_press: bool = False
        # _first_press_logged：診斷用，首次 press 事件抵達時打一行 log，
        # 之後不再重複；listener 重啟時會重置
        self._first_press_logged: bool = False

    def restart(self, combo: str) -> None:
        """停止舊的監聽器並以新組合鍵啟動新監聽器。

        後端優先序：NSEvent（PyObjC 可用時，預設）→ pynput Listener（fallback）。

        Args:
            combo: 組合鍵字串，例如 "cmd+alt+r" 或 lone modifier "right_cmd"。
        """
        self.stop()
        self._hotkeys = parse_hotkey(combo)
        self._combo_str = combo
        # Lone-modifier 模式偵測：單一鍵 + 該鍵為側別 modifier
        if len(self._hotkeys) == 1:
            only_key = next(iter(self._hotkeys))
            if _is_lone_modifier_key(only_key):
                self._is_lone_mode = True
                self._lone_target_key = only_key
            else:
                self._is_lone_mode = False
                self._lone_target_key = None
        else:
            self._is_lone_mode = False
            self._lone_target_key = None
        self._other_key_during_press = False
        self._first_press_logged = False
        mode = "lone-modifier" if self._is_lone_mode else "combo"

        # ── 後端：NSEvent only ─────────────────────────────────────
        # 2026-05-22 起 pynput Listener 完全砍除（macOS 26.4+ 連環 crash）。
        # PyObjC 不可用時靜默降級，App 不崩潰但快捷鍵不工作。
        if _NSEVENT_AVAILABLE:
            self._backend = "nsevent"
            log.info(
                f"HOTKEY: Starting NSEvent monitor for combo='{combo}' "
                f"keys={self._hotkeys} mode={mode}"
            )
            self._start_nsevent_monitors()
            log.info(f"HOTKEY: listener fully started at t={time.monotonic():.3f}")
            return

        self._backend = "none"
        log.warning("HOTKEY: NSEvent backend unavailable; global hotkey disabled")

    # ── NSEvent 後端 ────────────────────────────────────────────────────────

    def _start_nsevent_monitors(self) -> None:
        """安裝 NSEvent global + local monitor。

        global monitor 只在其他 App 焦點時收事件（Cocoa 不發給「自己」），
        所以同時加 local monitor 讓本 App 焦點時也能收。兩個 handler 共用
        相同的事件處理函式（_handle_ns_event），語意一致。
        """
        mask = (
            NSEventMaskFlagsChanged
            | NSEventMaskKeyDown
            | NSEventMaskKeyUp
        )
        self._monitor_global = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask, self._on_ns_event_global,
        )
        # Local monitor：回傳 event 讓事件繼續傳遞給 Tk；回傳 None 會吃掉事件。
        self._monitor_local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            mask, self._on_ns_event_local,
        )

    def _on_ns_event_global(self, event) -> None:
        """Global monitor handler——其他 App 焦點時觸發。

        handler 由 Cocoa 在主執行緒呼叫；try/except 包整段防 exception
        外洩到 Cocoa runtime（會 abort 整個進程）。
        """
        try:
            self._handle_ns_event(event)
        except Exception:
            log_error("hotkey_ns_global_failed")

    def _on_ns_event_local(self, event):
        """Local monitor handler——本 App 焦點時觸發。

        必須回傳 event（讓事件繼續往下傳到 Tk），否則會把所有 key event
        吃掉，App 自己的鍵盤輸入會壞掉。
        """
        try:
            self._handle_ns_event(event)
        except Exception:
            log_error("hotkey_ns_local_failed")
        return event   # 不要 suppress

    def _handle_ns_event(self, event) -> None:
        """NSEvent → 既有 _on_p / _on_r 邏輯的橋接器。

        三種事件型別：
          • FlagsChanged (12)：modifier 改變狀態；用 modifierFlags() 判定按下/放開
          • KeyDown (10)：一般按鍵按下
          • KeyUp (11)：一般按鍵放開

        對每個事件決定它是 press 還是 release，再呼叫 _on_p / _on_r。
        傳給 _on_p / _on_r 的 key 物件刻意做成「與 pynput 相容」的型別
        （Key.cmd_r、字元 'r' 等），讓既有比對邏輯不必改。
        """
        evt_type = event.type()
        keycode  = event.keyCode()

        if evt_type == 12:   # NSEventTypeFlagsChanged
            sided_name = _KEYCODE_TO_SIDED_MOD.get(keycode)
            if sided_name is None:
                return   # 不認識的 modifier keycode，忽略
            bit = _modifier_bit_for(sided_name)
            is_pressed = bool(event.modifierFlags() & bit)
            key_obj = _sided_name_to_pynput_key(sided_name)
            if is_pressed:
                self._on_p(key_obj)
            else:
                self._on_r(key_obj)
            return

        if evt_type == 10:   # NSEventTypeKeyDown
            self._on_p(self._ns_keycode_to_key(event, keycode))
            return

        if evt_type == 11:   # NSEventTypeKeyUp
            self._on_r(self._ns_keycode_to_key(event, keycode))
            return

    @staticmethod
    def _ns_keycode_to_key(event, keycode: int):
        """把 KeyDown/KeyUp 事件的 keycode 轉成「pynput-相容」的 key。

        字母鍵回傳小寫字元 str（與 parse_hotkey 對單字元的處理一致）；
        space 回傳 Key.space；其他 fallback 用 charactersIgnoringModifiers。
        """
        ch = _KEYCODE_TO_CHAR.get(keycode)
        if ch == " " and _PYNPUT_AVAILABLE:
            return Key.space
        if ch is not None:
            return ch
        # Fallback：問 NSEvent 自己的字元（忽略 modifier 影響）
        try:
            chars = event.charactersIgnoringModifiers()
            if chars and len(chars) >= 1:
                return chars[0].lower()
        except Exception:
            pass
        return f"keycode_{keycode}"

    def stop(self) -> None:
        """停止監聽器並清空所有狀態。

        設計重點：先在鎖內取出 monitor 參照、清空狀態，
        再在鎖外呼叫 removeMonitor:，避免持鎖時死鎖。
        """
        mon_global = None
        mon_local  = None
        with self._lock:
            mon_global       = self._monitor_global
            mon_local        = self._monitor_local
            self._monitor_global  = None
            self._monitor_local   = None
            self._pressed.clear()
            self._combo_active = False
            self._other_key_during_press = False

        # NSEvent monitors：在鎖外移除
        if _NSEVENT_AVAILABLE and (mon_global is not None or mon_local is not None):
            log.info("HOTKEY: Removing NSEvent monitors...")
            try:
                if mon_global is not None:
                    NSEvent.removeMonitor_(mon_global)
                if mon_local is not None:
                    NSEvent.removeMonitor_(mon_local)
            except Exception:
                log_error("hotkey_ns_remove_failed")

    def get_event_tap(self):
        """DEPRECATED：NSEvent backend 沒有 CGEventTap 的概念。

        舊 watchdog Layer 2（Fix 6 Step 3）用此函式拿 pynput 的 _tap handle
        做 CGEventTapIsEnabled 體檢；NSEvent 後端不會被 timeout disable，
        永遠回 None 讓 watchdog Layer 2 跳過（commit 3 會把整段砍掉）。
        """
        return None

    def _normalize(self, key) -> object:
        """正規化左右修飾鍵（委派給模組層函式）。"""
        return _normalize_key(key)

    def _on_p(self, key) -> None:
        """pynput 按鍵事件：兩種模式分流。

        Combo 模式（既有）：追蹤按下的鍵，標記 combo 是否已完整按下（不 fire）。
        Lone-modifier 模式：偵測目標 modifier 單獨按下；其他鍵介入則 disarm。

        Tap 模式下 press 階段**不**觸發 callback；fire 在 release 上。

        穩定性（Fix 2 / 2026-05-21）：整個函式體包 try/except，任何 callback
        例外都不能讓 pynput Listener 停掉（不同 pynput 版本對 callback exception
        行為不一致，統一在這吃掉 + log_error）。
        """
        try:
            # 診斷：第一個 press 事件抵達時間戳（B 任務）—— 任何鍵都算數
            if not self._first_press_logged:
                self._first_press_logged = True
                log.info(
                    f"HOTKEY: first press event received at t={time.monotonic():.3f}, key={key!r}"
                )

            # ── 模式分流 ──
            if self._is_lone_mode:
                self._on_p_lone(key)
                return

            # ── Combo 模式（既有邏輯）──
            nk  = self._normalize(key)
            now = time.monotonic()
            armed  = False
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

                # 所有必要鍵都已按下 → 標記「已 armed，等 release 觸發 tap」（不重複標記）
                if not self._combo_active and self._hotkeys.issubset(self._pressed):
                    self._combo_active    = True
                    self._combo_active_at = now
                    armed = True

            if healed:
                log.warning("HOTKEY: self-heal — stale combo_active cleared")
            if nk in self._hotkeys:
                log.debug(f"HOTKEY: press   {nk!r:>10}  pressed={sorted(self._pressed, key=str)}")
            if armed:
                combo = getattr(self, "_combo_str", "?")
                log.debug(f"HOTKEY: combo armed (combo={combo}), waiting for release to fire tap")
        except Exception:
            log_error("hotkey_on_p_failed", key=repr(key))

    def _on_r(self, key) -> None:
        """pynput 放開事件：兩種模式分流。

        穩定性（Fix 2 / 2026-05-21）：整個函式體包 try/except；同 _on_p 註解。
        """
        try:
            # ── 模式分流 ──
            if self._is_lone_mode:
                self._on_r_lone(key)
                return

            # ── Combo 模式（既有邏輯）──
            nk  = self._normalize(key)
            now = time.monotonic()
            fire = False

            with self._lock:
                # 只有「combo 已完整按下」（armed）且放開的是組合中的鍵時才 fire tap
                if self._combo_active and nk in self._hotkeys:
                    self._combo_active = False
                    fire = True
                self._pressed.discard(nk)
                self._last_event_at = now

            if nk in self._hotkeys:
                log.debug(f"HOTKEY: release {nk!r:>10}  pressed={sorted(self._pressed, key=str)}")
            if fire:
                combo = getattr(self, "_combo_str", "?")
                log_action("hotkey_tap", combo=combo)
                self._on_tap_cb()   # 在鎖外呼叫，避免 callback 內持鎖死鎖
        except Exception:
            log_error("hotkey_on_r_failed", key=repr(key))

    # ── Lone-modifier 模式分支 ─────────────────────────────────────────────

    def _on_p_lone(self, key) -> None:
        """Lone-modifier 模式 press handler。

        Press 階段不 fire；只記錄：
          • 目標 modifier 被「乾淨地」按下（按下前 _pressed 為空）→ armed
          • armed 期間有其他鍵被按下 → 設 _other_key_during_press = True，
            release 時不會 fire（這是正常的 modifier + 字母組合，例如
            Right Option + e 打 é；保護不誤觸發）

        穩定性（Fix 2 / 2026-05-21）：整個函式體包 try/except。
        """
        try:
            nk_sided = _normalize_key_sided(key)
            now      = time.monotonic()
            armed    = False
            healed   = False

            with self._lock:
                # 自我修復：armed 卡太久也清掉
                if self._combo_active and (now - self._combo_active_at) > _STALE_COMBO_SEC:
                    healed = True
                    self._combo_active = False
                    self._other_key_during_press = False
                    self._pressed.clear()

                is_target = (nk_sided == self._lone_target_key)
                self._last_event_at = now

                if is_target:
                    # 目標 modifier press —— 只在當下沒有其他鍵按住時才 armed
                    if not self._combo_active and len(self._pressed) == 0:
                        self._combo_active = True
                        self._combo_active_at = now
                        self._other_key_during_press = False
                        armed = True
                    self._pressed.add(nk_sided)
                else:
                    # 非目標鍵 press —— 若已 armed 則標記 disarm（不重置 _combo_active，
                    # 等 release 時判斷是否該抑制 fire）
                    if self._combo_active:
                        self._other_key_during_press = True
                    self._pressed.add(nk_sided)

            if healed:
                log.warning("HOTKEY: self-heal — stale lone-mode active cleared")
            if armed:
                log.debug(f"HOTKEY: lone armed (target={self._lone_target_key!r})")
        except Exception:
            log_error("hotkey_on_p_lone_failed", key=repr(key))

    def _on_r_lone(self, key) -> None:
        """Lone-modifier 模式 release handler。

        放開目標 modifier 時：
          • _combo_active=True 且 _other_key_during_press=False → fire tap
          • _combo_active=True 且 _other_key_during_press=True  → 不 fire
            （這是 modifier + 其他鍵組合，例如 Right Option + e 打 é，保護不誤觸發）
        無論是否 fire，狀態都重置。

        穩定性（Fix 2 / 2026-05-21）：整個函式體包 try/except。
        """
        try:
            nk_sided = _normalize_key_sided(key)
            now      = time.monotonic()
            fire     = False

            with self._lock:
                self._pressed.discard(nk_sided)
                self._last_event_at = now
                is_target = (nk_sided == self._lone_target_key)
                if is_target and self._combo_active:
                    if not self._other_key_during_press:
                        fire = True
                    # 不論是否 fire，目標 modifier release 後重置狀態
                    self._combo_active = False
                    self._other_key_during_press = False

            if fire:
                combo = getattr(self, "_combo_str", "?")
                log_action("hotkey_tap", combo=combo)
                self._on_tap_cb()
        except Exception:
            log_error("hotkey_on_r_lone_failed", key=repr(key))
