#!/usr/bin/env python3
"""
Test script for hotkey manager functionality.
"""

import threading
import time
from hotkey_manager import HotkeyManager, format_hotkey, parse_hotkey

def test_hotkey_parsing():
    print("=== Testing Hotkey Parsing ===")

    # Test parsing
    combo = "cmd+shift+space"
    hotkeys = parse_hotkey(combo)
    print(f"Parsed '{combo}' into {len(hotkeys)} keys")
    for key in hotkeys:
        print(f"  - {key}")

    # Test formatting
    display = format_hotkey(combo)
    print(f"Formatted display: {display}")
    assert display == "⌘⇧Space", f"Expected '⌘⇧Space', got '{display}'"
    print("✓ Hotkey parsing works\n")

def test_hotkey_manager():
    print("=== Testing HotkeyManager ===")

    press_called = threading.Event()
    release_called = threading.Event()

    def on_press():
        print("  → Hotkey pressed!")
        press_called.set()

    def on_release():
        print("  → Hotkey released!")
        release_called.set()

    mgr = HotkeyManager(on_press_cb=on_press, on_release_cb=on_release)
    print("Created HotkeyManager")

    # Start listening
    mgr.start("cmd+shift+space")
    print("Started listening for cmd+shift+space")
    print("\nNote: To test, press and hold ⌘⇧Space (Cmd+Shift+Space)")
    print("Waiting for hotkey input (20 seconds timeout)...\n")

    # Wait for events with timeout
    pressed = press_called.wait(timeout=20)
    if pressed:
        print("✓ Hotkey press detected!")
        released = release_called.wait(timeout=5)
        if released:
            print("✓ Hotkey release detected!")
        else:
            print("✗ Hotkey release not detected within timeout")
    else:
        print("⚠ Hotkey press not detected (may need accessibility permission)")

    mgr.stop()
    print("Stopped listening\n")

def test_accessibility():
    print("=== Testing Accessibility Permission ===")
    from hotkey_manager import check_accessibility, is_pynput_available

    if not is_pynput_available():
        print("✗ pynput is not available")
        return

    has_access = check_accessibility()
    if has_access:
        print("✓ Accessibility permission is granted")
    else:
        print("⚠ Accessibility permission is NOT granted")
        print("  To grant permission:")
        print("  1. Open System Settings > Privacy & Security > Accessibility")
        print("  2. Add Terminal (or Python) to the list")
        print("  3. Restart this app")

if __name__ == "__main__":
    test_hotkey_parsing()
    test_accessibility()

    # Only run interactive test if accessibility is available
    from hotkey_manager import is_pynput_available, check_accessibility
    if is_pynput_available() and check_accessibility():
        test_hotkey_manager()
    else:
        print("=== Skipping HotkeyManager Interactive Test ===")
        print("Accessibility permission not available\n")

    print("=== All tests completed ===")
