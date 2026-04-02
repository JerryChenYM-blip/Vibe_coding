import time
import threading
from pynput import keyboard
from hotkey_manager import HotkeyManager, parse_hotkey

print("Starting simulation test...")

pressed_flag = False
released_flag = False

def on_press():
    global pressed_flag
    print(">>> HOTKEY TRIGGERED (RECORDING) <<<")
    pressed_flag = True

def on_release():
    global released_flag
    print(">>> HOTKEY RELEASED (TRANSCRIBING) <<<")
    released_flag = True

mgr = HotkeyManager(on_press_cb=on_press, on_release_cb=on_release)
# Default combo is cmd+shift+space
mgr.start("cmd+shift+space")

# Wait for listener to start
time.sleep(1)

print(f"Target hotkeys are: {mgr._hotkeys}")

controller = keyboard.Controller()

# Simulate pressing Cmd, Shift, Space
print("Simulating key presses...")
controller.press(keyboard.Key.cmd)
time.sleep(0.1)
controller.press(keyboard.Key.shift)
time.sleep(0.1)
controller.press(keyboard.Key.space)
time.sleep(1)

print(f"Internal pressed state: {mgr._pressed}")

# Simulate releasing
print("Simulating key release...")
controller.release(keyboard.Key.space)
time.sleep(0.1)
controller.release(keyboard.Key.shift)
time.sleep(0.1)
controller.release(keyboard.Key.cmd)
time.sleep(0.5)

mgr.stop()

if pressed_flag and released_flag:
    print("✅ TEST PASSED: Both press and release were triggered.")
else:
    print(f"❌ TEST FAILED: pressed={pressed_flag}, released={released_flag}")
