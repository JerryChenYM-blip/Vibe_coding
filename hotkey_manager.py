"""
Global Hotkey Manager for macOS.

Fixes applied vs previous version:
  • Callbacks (_on_press_cb / _on_release_cb) are now invoked OUTSIDE the
    internal lock → eliminates potential deadlock when the callback itself
    triggers any lock-guarded operation.
  • stop() releases the lock before calling listener.stop() → avoids a
    deadlock if the pynput thread is mid-callback holding the lock.
  • capture_hotkey() added — used by the HotkeyBindDialog in the UI.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional, Set

try:
    from pynput import keyboard as _kb
    from pynput.keyboard import Key, KeyCode
    _PYNPUT_AVAILABLE = True
except ImportError:
    _PYNPUT_AVAILABLE = False


def is_pynput_available() -> bool:
    return _PYNPUT_AVAILABLE


def check_accessibility() -> bool:
    import platform
    if platform.system() != "Darwin":
        return True
    try:
        from ApplicationServices import AXIsProcessTrusted
        return AXIsProcessTrusted()
    except Exception:
        return False


# ── Symbol helpers ────────────────────────────────────────────────────────────

_SYMBOL_MAP: dict[str, str] = {
    "cmd":   "⌘",
    "ctrl":  "⌃",
    "alt":   "⌥",
    "shift": "⇧",
    "space": "Space",
}


def parse_hotkey(combo: str) -> set:
    """Parse 'cmd+alt+r' → set of pynput Key / char objects."""
    if not _PYNPUT_AVAILABLE:
        return set()
    res: set = set()
    for p in combo.lower().split("+"):
        p = p.strip()
        if p in ("cmd", "command"):
            res.add(Key.cmd)
        elif p == "shift":
            res.add(Key.shift)
        elif p == "alt":
            res.add(Key.alt)
        elif p == "ctrl":
            res.add(Key.ctrl)
        elif p == "space":
            res.add(Key.space)
        elif len(p) == 1:
            res.add(p)
        else:
            try:
                res.add(getattr(Key, p, p))
            except Exception:
                pass
    return res


def format_hotkey(combo: str) -> str:
    """'cmd+alt+r' → '⌘⌥R'"""
    parts = combo.lower().split("+")
    return "".join(_SYMBOL_MAP.get(p.strip(), p.strip().upper()) for p in parts)


def _normalize_key(key) -> object:
    """Collapse left/right variants to canonical Key."""
    if not _PYNPUT_AVAILABLE:
        return key
    if key in (Key.cmd,   Key.cmd_l,   Key.cmd_r):   return Key.cmd
    if key in (Key.shift, Key.shift_l, Key.shift_r): return Key.shift
    if key in (Key.alt,   Key.alt_l,   Key.alt_r):   return Key.alt
    if key in (Key.ctrl,  Key.ctrl_l,  Key.ctrl_r):  return Key.ctrl
    if hasattr(key, "char") and key.char:
        return key.char.lower()
    return key


def _keys_to_combo(keys: set) -> str:
    """Convert a set of normalized pynput keys → 'cmd+alt+r' string."""
    order = [
        (Key.cmd,   "cmd"),
        (Key.ctrl,  "ctrl"),
        (Key.alt,   "alt"),
        (Key.shift, "shift"),
    ]
    parts: list[str] = []
    remaining = set(keys)
    for k, name in order:
        if k in remaining:
            parts.append(name)
            remaining.discard(k)
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


def capture_hotkey(timeout: float = 15.0) -> Optional[str]:
    """
    Block until the user presses (and begins to release) a key combination.
    Returns the combo string (e.g. 'cmd+alt+r'), or None on timeout.

    Used by HotkeyBindDialog to let users rebind the hotkey.
    """
    if not _PYNPUT_AVAILABLE:
        return None

    done = threading.Event()
    current: set = set()
    max_combo: list[set] = [set()]
    result: list[Optional[str]] = [None]

    def _on_press(key):
        nk = _normalize_key(key)
        current.add(nk)
        if len(current) > len(max_combo[0]):
            max_combo[0] = set(current)

    def _on_release(key):
        if max_combo[0] and not done.is_set():
            result[0] = _keys_to_combo(max_combo[0])
            done.set()
            return False          # stop listener
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
    """
    Listens for a configurable global hotkey combo and fires callbacks.

    Thread-safety: All shared state is guarded by _lock, but callbacks are
    invoked OUTSIDE the lock to prevent deadlocks.
    """

    def __init__(self, on_press_cb: Callable, on_release_cb: Callable) -> None:
        self._on_press_cb   = on_press_cb
        self._on_release_cb = on_release_cb
        self._hotkeys:      set = set()
        self._pressed:      Set = set()
        self._combo_active: bool = False
        self._listener:     Optional[_kb.Listener] = None
        self._lock = threading.Lock()

    def restart(self, combo: str) -> None:
        self.stop()
        self._hotkeys = parse_hotkey(combo)
        print(f"HOTKEY: Starting listener for {self._hotkeys}")
        self._listener = _kb.Listener(
            on_press=self._on_p,
            on_release=self._on_r,
            suppress=False,
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        # Grab the listener reference and clear state under the lock,
        # then stop the listener OUTSIDE the lock to avoid deadlock
        # (the pynput thread might be mid-callback holding the lock).
        listener_to_stop = None
        with self._lock:
            listener_to_stop = self._listener
            self._listener = None
            self._pressed.clear()
            self._combo_active = False
        if listener_to_stop is not None:
            print("HOTKEY: Stopping listener...")
            try:
                listener_to_stop.stop()
            except Exception:
                pass

    def _normalize(self, key) -> object:
        return _normalize_key(key)

    def _on_p(self, key) -> None:
        nk = self._normalize(key)
        fire = False
        with self._lock:
            self._pressed.add(nk)
            if not self._combo_active and self._hotkeys.issubset(self._pressed):
                self._combo_active = True
                fire = True
        if fire:
            print("HOTKEY: Triggered!")
            self._on_press_cb()   # called OUTSIDE lock

    def _on_r(self, key) -> None:
        nk = self._normalize(key)
        fire = False
        with self._lock:
            if self._combo_active and nk in self._hotkeys:
                self._combo_active = False
                fire = True
            self._pressed.discard(nk)
        if fire:
            print("HOTKEY: Released!")
            self._on_release_cb()  # called OUTSIDE lock
