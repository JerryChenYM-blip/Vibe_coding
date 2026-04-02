import time
from pynput import keyboard
from hotkey_manager import HotkeyManager, parse_hotkey

print("Starting Mac hotkey simulation test...")

def on_press():
    print(">>> HOTKEY TRIGGERED (RECORDING) <<<")

def on_release():
    print(">>> HOTKEY RELEASED (TRANSCRIBING) <<<")

mgr = HotkeyManager(on_press_cb=on_press, on_release_cb=on_release)
# default combo
mgr.start("cmd+shift+space")
time.sleep(1)

# print internal parsed hotkeys
print(f"Target hotkeys: {mgr._hotkeys}")

# Simulate press one by one, and check internal state
mgr._on_press(keyboard.Key.cmd_l)
print(f"State after cmd_l: {mgr._pressed}")

mgr._on_press(keyboard.Key.shift_l)
print(f"State after shift_l: {mgr._pressed}")

mgr._on_press(keyboard.Key.space)
print(f"State after space: {mgr._pressed}")

# Check if combo active
print(f"Combo active? {mgr._combo_active}")

# Simulate release
mgr._on_release(keyboard.Key.space)
print(f"State after space release: {mgr._pressed}")
print(f"Combo active? {mgr._combo_active}")
