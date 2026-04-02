import time
import customtkinter as ctk
from gui import HotkeyBindDialog

def test_on_apply(combo):
    print(f"Callback received: {combo}")

def run_test():
    root = ctk.CTk()
    dialog = HotkeyBindDialog(root, on_apply=test_on_apply)

    def simulate_timeout():
        print("Simulating timeout / destroying window safely...")
        if root.winfo_exists():
            root.destroy()

    root.after(2000, simulate_timeout)
    root.mainloop()
    print("Main loop finished safely.")

run_test()
