"""
Auto-paste: detect the frontmost macOS app before recording starts,
then inject transcribed text directly into it via clipboard + ⌘V.

Requirements:
  • pyperclip   — clipboard write
  • pynput      — keyboard simulation (requires Accessibility permission)
  • osascript   — activate the target window (always available on macOS)
"""

from __future__ import annotations

import subprocess
import time
from typing import Optional


def get_frontmost_app() -> Optional[str]:
    """
    Return the display name of the current frontmost process, or None on error.
    Runs a tiny AppleScript via osascript — always available on macOS.
    """
    try:
        result = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" '
                'to get name of first process whose frontmost is true',
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        name = result.stdout.strip()
        return name or None
    except Exception as e:
        print(f"AUTO-PASTE: get_frontmost_app failed: {e}")
        return None


def paste_to_app(
    text: str,
    app_name: Optional[str],
    activate_delay: float = 0.18,
) -> bool:
    """
    1. Write *text* to the system clipboard.
    2. Activate *app_name* (bring its window to front).
    3. Simulate ⌘V to paste.

    Returns True on success, False on any failure.
    Requires Accessibility permission for pynput keyboard simulation.
    """
    # ── 1. Clipboard ──────────────────────────────────────────────────────────
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception as e:
        print(f"AUTO-PASTE: clipboard write failed: {e}")
        return False

    # ── 2. Activate target app ────────────────────────────────────────────────
    if app_name:
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "{app_name}" to activate'],
                timeout=3,
            )
            time.sleep(activate_delay)   # give the app time to come to front
        except Exception as e:
            print(f"AUTO-PASTE: activate '{app_name}' failed: {e}")
            # Continue anyway — paste into whatever window is now focused

    # ── 3. Send ⌘V ───────────────────────────────────────────────────────────
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        with kb.pressed(Key.cmd):
            kb.tap("v")
        print(f"AUTO-PASTE: ⌘V sent → '{app_name}'")
        return True
    except Exception as e:
        print(f"AUTO-PASTE: keyboard simulation failed: {e}")
        return False
