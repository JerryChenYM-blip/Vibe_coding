"""
Global hotkey listener using pynput.

On macOS, global key monitoring requires Accessibility permission.
This module detects whether that permission is available and exposes
an is_accessible() helper so the GUI can show a guidance dialog.

Push-to-talk model:
  on_press  callback → called when the full hotkey combination is pressed
  on_release callback → called when any key in the combination is released
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
    """
    On macOS, probe whether Accessibility permission is granted using AXIsProcessTrusted.
    Returns True if granted (or on non-macOS).
    """
    import platform
    if platform.system() != "Darwin":
        return True
    
    try:
        from ApplicationServices import AXIsProcessTrusted
        return AXIsProcessTrusted()
    except (ImportError, AttributeError):
        # Fallback to pynput probe if pyobjc is missing
        if not _PYNPUT_AVAILABLE:
            return False
        try:
            from pynput.keyboard import Listener as L
            sentinel = threading.Event()
            exc_holder: list[Exception] = []

            def on_press(key):
                sentinel.set()
                return False

            def on_err(exc):
                exc_holder.append(exc)
                sentinel.set()

            l = L(on_press=on_press, on_error=on_err)
            l.start()
            sentinel.wait(timeout=0.05)
            l.stop()
            return len(exc_holder) == 0
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────────────────

_SYMBOL_MAP: dict[str, str] = {
    "cmd":   "⌘",
    "ctrl":  "⌃",
    "alt":   "⌥",
    "shift": "⇧",
    "space": "Space",
    "f13":   "F13",
    "f14":   "F14",
    "f15":   "F15",
}


def parse_hotkey(combo: str) -> set:
    """
    Parse a hotkey string like 'cmd+shift+space' into a set of pynput Key objects.
    Returns an empty set if pynput is not available.
    """
    if not _PYNPUT_AVAILABLE:
        return set()

    result = set()
    for part in combo.lower().split("+"):
        part = part.strip()
        if part in ("cmd", "command"):
            result.add(Key.cmd)
        elif part == "ctrl":
            result.add(Key.ctrl)
        elif part == "alt":
            result.add(Key.alt)
        elif part == "shift":
            result.add(Key.shift)
        elif part == "space":
            result.add(Key.space)
        elif part.startswith("f") and part[1:].isdigit():
            try:
                result.add(getattr(Key, part, None))
            except AttributeError: pass
        else:
            # Single character key
            if len(part) == 1:
                result.add(KeyCode.from_char(part))
            else:
                # Try to find in Key enum anyway
                try:
                    result.add(getattr(Key, part, None))
                except AttributeError: pass
    result.discard(None)
    return result


def format_hotkey(combo: str) -> str:
    """Return a pretty display string, e.g. 'cmd+shift+space' → '⌘⇧Space'."""
    parts = combo.lower().split("+")
    return "".join(_SYMBOL_MAP.get(p.strip(), p.strip().capitalize()) for p in parts)


def capture_hotkey(timeout: float = 10.0) -> Optional[str]:
    """
    Block until the user presses and releases a key combination, then return
    the combo string (e.g. 'cmd+shift+r').  Returns None on timeout or error.
    Only usable from a non-main thread.
    """
    if not _PYNPUT_AVAILABLE:
        return None

    pressed: Set = set()
    captured: list[set] = []
    done = threading.Event()

    def _canonical(key) -> str:
        if key == Key.cmd or key == Key.cmd_l or key == Key.cmd_r:
            return "cmd"
        if key == Key.ctrl or key == Key.ctrl_l or key == Key.ctrl_r:
            return "ctrl"
        if key == Key.alt or key == Key.alt_l or key == Key.alt_r:
            return "alt"
        if key == Key.shift or key == Key.shift_l or key == Key.shift_r:
            return "shift"
        if key == Key.space:
            return "space"
        if hasattr(key, "name"):
            return key.name
        if hasattr(key, "char") and key.char:
            return key.char.lower()
        return str(key)

    def on_press(key):
        pressed.add(_canonical(key))

    def on_release(key):
        if pressed and not captured:
            captured.append(set(pressed))
        pressed.discard(_canonical(key))
        if not pressed and captured:
            done.set()
            return False  # stop listener

    listener = _kb.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    done.wait(timeout=timeout)
    listener.stop()

    if not captured:
        return None

    # Build canonical order: modifiers first, then key
    modifier_order = ["ctrl", "alt", "shift", "cmd"]
    keys = list(captured[0])
    mods = [k for k in modifier_order if k in keys]
    rest = [k for k in keys if k not in modifier_order]
    return "+".join(mods + rest)


# ─────────────────────────────────────────────────────────────────────────────

class HotkeyManager:
    """
    Listens globally for a configurable hotkey combination.

    on_press_cb  → called (on pynput thread) when full combo is first pressed
    on_release_cb → called (on pynput thread) when any combo key is released
                   while the combo was active

    Both callbacks should be fast; use tkinter's master.after(0, fn) to
    marshal work back to the main thread.
    """

    def __init__(
        self,
        on_press_cb: Callable[[], None],
        on_release_cb: Callable[[], None],
    ) -> None:
        self._on_press_cb = on_press_cb
        self._on_release_cb = on_release_cb
        self._hotkeys: set = set()
        self._pressed: Set = set()
        self._combo_active = False
        self._listener: Optional[object] = None
        self._lock = threading.Lock()

    def set_hotkey(self, combo: str) -> None:
        """Update the active hotkey combination."""
        self._hotkeys = parse_hotkey(combo)

    def start(self, combo: str) -> None:
        """Start the global listener with the given hotkey combo."""
        if not _PYNPUT_AVAILABLE:
            print("ERROR: pynput not available. Hotkeys disabled.")
            return
        self.set_hotkey(combo)
        print(f"INFO: Starting hotkey listener for '{combo}'...")
        self._listener = _kb.Listener(
            on_press=lambda key: self._on_press(key),
            on_release=lambda key: self._on_release(key),
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def restart(self, combo: str) -> None:
        self.stop()
        self.start(combo)

    # ── internal ─────────────────────────────────────────────────────────────

    def _normalize(self, key) -> object:
        """Normalize left/right variants to canonical Key values."""
        if not _PYNPUT_AVAILABLE:
            return key

        aliases = {
            Key.cmd_l: Key.cmd, Key.cmd_r: Key.cmd,
            Key.ctrl_l: Key.ctrl, Key.ctrl_r: Key.ctrl,
            Key.alt_l: Key.alt, Key.alt_r: Key.alt,
            Key.shift_l: Key.shift, Key.shift_r: Key.shift,
        }

        # pynput 有時候會在 macOS 傳入特異的 keyCode，確保正確對應
        if hasattr(key, "name") and key.name in ("cmd", "cmd_l", "cmd_r"):
            return Key.cmd
        if hasattr(key, "name") and key.name in ("shift", "shift_l", "shift_r"):
            return Key.shift
        if hasattr(key, "name") and key.name in ("alt", "alt_l", "alt_r"):
            return Key.alt
        if hasattr(key, "name") and key.name in ("ctrl", "ctrl_l", "ctrl_r"):
            return Key.ctrl

        return aliases.get(key, key)

    def _on_press(self, key) -> None:
        if not self._hotkeys:
            return
        try:
            key = self._normalize(key)
            with self._lock:
                self._pressed.add(key)
                # Debug print to help identify issues
                # print(f"DEBUG: Pressed {key}, Current set: {self._pressed}")
                if not self._combo_active and self._hotkeys.issubset(self._pressed):
                    print(f"DEBUG: Hotkey Combo Detected! Triggering callback.")
                    self._combo_active = True
                    try:
                        self._on_press_cb()
                    except Exception as e:
                        print(f"DEBUG: Callback error: {e}")
        except Exception as e:
            print(f"DEBUG: _on_press error: {e}")

    def _on_release(self, key) -> None:
        try:
            key = self._normalize(key)
            with self._lock:
                self._pressed.discard(key)
                if self._combo_active and key in self._hotkeys:
                    print(f"DEBUG: Hotkey Released.")
                    self._combo_active = False
                    try:
                        self._on_release_cb()
                    except Exception as e:
                        print(f"DEBUG: Release callback error: {e}")
        except Exception as e:
            print(f"DEBUG: _on_release error: {e}")
