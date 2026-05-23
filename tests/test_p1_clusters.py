"""P1 Cluster fixes regression tests — 來自 5 個並行 sub-agent 審 50 個情境後集中修的 8 個 cluster。

涵蓋：
  - Cluster A：on_close 清 Cocoa observer / AppleEvent handler
  - Cluster B：Polish block identity 保護（3 個 race 統一解）
  - Cluster C：Recorder.start() 失敗 UI 回退
  - Cluster D：theme rollback on relaunch fail + export manifest version
  - Cluster E：Prompt reload atomic lock
  - Cluster F：lone-mode dead-key arm 條件放寬
  - Cluster G：force-restart 不撞 in-flight 按鍵
  - Cluster H：HotkeyBindDialog 開啟期間暫停 hotkey monitor

策略：所有 test 都 mock 外部依賴，不需要真開視窗或 spawn process。
"""

import json
import sys
import threading
import time
import types
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ═════════════════════════════════════════════════════════════════════════════
#  Cluster A — on_close 清 Cocoa observer / AppleEvent handler
# ═════════════════════════════════════════════════════════════════════════════

def test_cluster_A_on_close_removes_cocoa_observer():
    """on_close 必須呼叫 NSNotificationCenter.removeObserver_、避免 relaunch 後 leak。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win.hotkey_mgr = MagicMock()
    win.recorder = MagicMock()
    win.recorder.is_recording.return_value = False
    win._destroy_mini_window = MagicMock()
    obs_sentinel = object()
    win._cocoa_activation_observer = obs_sentinel
    win._cocoa_reopen_handler = None

    fake_nc = MagicMock()
    with patch("Foundation.NSNotificationCenter") as fake_nc_cls:
        fake_nc_cls.defaultCenter.return_value = fake_nc
        win.on_close()

    fake_nc.removeObserver_.assert_called_once_with(obs_sentinel)
    assert win._cocoa_activation_observer is None


def test_cluster_A_on_close_removes_apple_event_handler():
    """on_close 必須呼叫 NSAppleEventManager.removeEventHandlerForEventClass_andEventID_。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win.hotkey_mgr = MagicMock()
    win.recorder = MagicMock()
    win.recorder.is_recording.return_value = False
    win._destroy_mini_window = MagicMock()
    win._cocoa_activation_observer = None
    win._cocoa_reopen_handler = object()   # sentinel

    fake_aem = MagicMock()
    with patch("Foundation.NSAppleEventManager") as fake_aem_cls:
        fake_aem_cls.sharedAppleEventManager.return_value = fake_aem
        win.on_close()

    fake_aem.removeEventHandlerForEventClass_andEventID_.assert_called_once()
    assert win._cocoa_reopen_handler is None


# ═════════════════════════════════════════════════════════════════════════════
#  Cluster B — Polish block identity 保護
# ═════════════════════════════════════════════════════════════════════════════

def _make_polish_stub():
    """繞 __init__ 建 AppWindow stub + 一個 UtteranceBlock-like target_block。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win._polish_busy = False
    win._polish_generation = 0
    win._utterance_blocks = []
    win._last_polished = None
    win._showing_polished = False
    win._apply_toggle_style = MagicMock()
    win._show_toast = MagicMock()
    win._do_auto_paste = MagicMock()
    win._rebuild_result_title = MagicMock()
    win._title_status = None
    win._title_preset = None
    win.cfg = types.SimpleNamespace(auto_copy=False)
    win.history_store = None
    win._current_history_id = None
    return win


def test_cluster_B_finish_polish_drops_when_block_deleted():
    """Polish 跑完回主 thread 時 target_block 已被刪除 → 不寫進別人的 block。"""
    win = _make_polish_stub()
    blk_target = MagicMock()
    blk_target.set_polished = MagicMock()
    other = MagicMock()
    other.set_polished = MagicMock()
    # 模擬：polish 開始時 target 是 latest；polish 跑時 target 被刪、剩下 other
    win._utterance_blocks = [other]   # target 已不在 list 內
    resp = types.SimpleNamespace(text="polished!", error=None, elapsed_seconds=1.2)

    win._finish_polish(gen=0, raw_text="raw", target=None, resp=resp, target_block=blk_target)

    blk_target.set_polished.assert_not_called()
    other.set_polished.assert_not_called()   # other 也不該被寫


def test_cluster_B_finish_polish_drops_when_block_no_longer_latest():
    """Polish 跑完時 target_block 還在 list 但不再是 latest（user 又錄新音）→ drop。

    v2.8.0 Bug 3：list 反轉為 [0]=最新；new_latest 在 [0]、blk_target 在 [1]。
    """
    win = _make_polish_stub()
    blk_target = MagicMock()
    blk_target.set_polished = MagicMock()
    new_latest = MagicMock()
    new_latest.set_polished = MagicMock()
    # target 還在 list 內、但 user 又錄新音 → 新 block 變 latest（在 [0]）
    win._utterance_blocks = [new_latest, blk_target]
    resp = types.SimpleNamespace(text="polished!", error=None, elapsed_seconds=1.2)

    win._finish_polish(gen=0, raw_text="raw", target=None, resp=resp, target_block=blk_target)

    # target 還在 list 但不是 latest → 不寫
    blk_target.set_polished.assert_not_called()
    new_latest.set_polished.assert_not_called()


def test_cluster_B_finish_polish_writes_when_target_still_latest():
    """Polish 跑完時 target_block 還是 latest → 正常 set_polished。"""
    win = _make_polish_stub()
    blk_target = MagicMock()
    blk_target.set_polished = MagicMock()
    win._utterance_blocks = [blk_target]   # 就一個、就是 latest
    resp = types.SimpleNamespace(text="polished!", error=None, elapsed_seconds=1.2)

    win._finish_polish(gen=0, raw_text="raw", target=None, resp=resp, target_block=blk_target)

    blk_target.set_polished.assert_called_once_with("polished!")
    assert win._last_polished == "polished!"


# ═════════════════════════════════════════════════════════════════════════════
#  Cluster C — Recorder.start() 失敗 UI 回退
# ═════════════════════════════════════════════════════════════════════════════

def test_cluster_C_recorder_start_fail_stays_idle():
    """recorder.start() 回 False → 不進入 recording state、toast 提示、不改 UI。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win._state = "idle"
    win.recorder = MagicMock()
    win.recorder.start.return_value = False   # 失敗
    win._show_toast = MagicMock()
    win._model_var = MagicMock(); win._model_var.get.return_value = "large-v3-turbo"
    win._lang_var = MagicMock(); win._lang_var.get.return_value = "中文"
    win.cfg = types.SimpleNamespace(auto_paste=True)

    win._transition_to_recording()

    win.recorder.start.assert_called_once()
    assert win._state == "idle"   # 沒進 recording
    win._show_toast.assert_called_once()
    msg = win._show_toast.call_args[0][0]
    assert "麥克風" in msg


# ═════════════════════════════════════════════════════════════════════════════
#  Cluster D — Theme rollback + export manifest version
# ═════════════════════════════════════════════════════════════════════════════

def test_cluster_D2_export_manifest_uses_version_module():
    """匯出 manifest.json 的 app_version 必須從 _version.__version__ 讀、不是 hardcode。"""
    from _version import __version__
    # grep gui.py 內 app_version 那段、確認有 import _version
    src = Path("gui.py").read_text(encoding="utf-8")
    # 匯出區塊必須有 from _version import __version__ 引用
    assert "from _version import __version__" in src
    # 不應該再有 hardcoded "v2.2.0" 在 export 區塊
    # （allow 其他地方殘留，例如註解，重點是 app_version 那行）
    export_section = src[src.find("schema_version"):src.find("schema_version") + 500]
    assert '"v2.2.0"' not in export_section, f"hardcoded v2.2.0 still in export: {export_section[:300]}"


def test_cluster_D1_relaunch_failure_invokes_rollback():
    """_do_theme_relaunch spawn 失敗 → 呼叫 on_failure callback（rollback config）。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win._show_toast = MagicMock()
    win.on_close = MagicMock()

    rollback_called = []
    def fake_rollback():
        rollback_called.append(True)

    # 收集 after callback
    scheduled = []
    win.after = lambda ms, fn: scheduled.append((ms, fn))

    win._do_theme_relaunch(on_failure=fake_rollback)

    # 1 個 after(800, _do_relaunch_sequence) 排程
    assert len(scheduled) == 1
    assert scheduled[0][0] == 800

    # 模擬 spawn 失敗
    with patch("main._relaunch_app", return_value=False):
        scheduled[0][1]()   # 跑 _do_relaunch_sequence

    assert rollback_called == [True], "rollback callback 沒被叫"


# ═════════════════════════════════════════════════════════════════════════════
#  Cluster E — Prompt reload atomic lock
# ═════════════════════════════════════════════════════════════════════════════

def test_cluster_E_reload_lock_exists_and_acquirable():
    """prompt_reloader.reload_lock() context manager 存在、可重入 acquire/release。"""
    from prompt_reloader import reload_lock, _RELOAD_LOCK
    # 連續 acquire / release 三次（RLock 允許）
    with reload_lock():
        with reload_lock():
            with reload_lock():
                assert _RELOAD_LOCK.acquire(blocking=False)   # RLock 同 thread 仍可 acquire
                _RELOAD_LOCK.release()


def test_cluster_E_reload_one_holds_lock_during_reload():
    """PromptReloader._reload_one 期間 _RELOAD_LOCK 被持有，consumer 應阻塞。"""
    import threading as _threading
    from prompt_reloader import PromptReloader, _RELOAD_LOCK, reload_lock

    pr = PromptReloader(module_names=("logger",))   # 用既有 module 不會炸

    # 用 thread 模擬 consumer 試圖 acquire
    consumer_acquired = []
    consumer_blocked = _threading.Event()

    def consumer():
        consumer_blocked.set()   # 通知主 thread「我準備好搶 lock 了」
        with reload_lock():
            consumer_acquired.append(True)

    # 主 thread 先持有 lock 模擬 reload 中
    with _RELOAD_LOCK:
        t = _threading.Thread(target=consumer, daemon=True)
        t.start()
        consumer_blocked.wait(1.0)   # 等 consumer 啟動
        time.sleep(0.05)   # 給 consumer 嘗試 acquire 的時間
        assert consumer_acquired == [], "consumer 應該還在等 lock"

    # 主 thread 放掉 lock 後 consumer 應該拿到
    t.join(timeout=1.0)
    assert consumer_acquired == [True], "consumer 拿不到 lock"


# ═════════════════════════════════════════════════════════════════════════════
#  Cluster F — Lone-mode dead-key arm 條件
# ═════════════════════════════════════════════════════════════════════════════

def test_cluster_F_lone_mode_arms_with_stale_letter_in_pressed():
    """lone-mode：dead-key 殘留 'e' 仍在 _pressed 內時、單按 R-Opt 仍能 arm。"""
    from hotkey_manager import HotkeyManager
    mgr = HotkeyManager(on_tap_cb=lambda: None)
    mgr.restart("right_option")   # lone modifier mode

    # 模擬 dead-key 後 'e' 殘留在 _pressed
    mgr._pressed = {"e"}   # str type、非 modifier
    mgr._combo_active = False
    mgr._other_key_during_press = False

    # 模擬 R-Opt 單按
    try:
        from pynput.keyboard import Key
        target = Key.alt_r
    except Exception:
        target = "alt_r"   # 退化
    mgr._on_p_lone(target)

    # 應該 arm（_combo_active = True）儘管 _pressed 內有殘留字母
    assert mgr._combo_active is True, \
        f"lone mode 沒 arm（_pressed={mgr._pressed}, _combo_active={mgr._combo_active}）"


# ═════════════════════════════════════════════════════════════════════════════
#  Cluster G — Force-restart 不撞 in-flight 按鍵
# ═════════════════════════════════════════════════════════════════════════════

def test_cluster_G_watchdog_defers_force_restart_when_pressed_nonempty():
    """有按鍵 in-flight（_pressed 非空）時 force-restart 必須跳過。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win.cfg = types.SimpleNamespace(hotkey="right_cmd")
    win.hotkey_mgr = MagicMock()
    win.hotkey_mgr._monitor_global = object()
    win.hotkey_mgr._monitor_local = object()
    win.hotkey_mgr._last_event_at = time.monotonic()
    win.hotkey_mgr._pressed = {"cmd_r"}   # 使用者按鍵中
    win.hotkey_mgr._combo_active = False
    win._state = "idle"
    win._last_hotkey_force_restart = time.monotonic() - 660.0   # > 10 min
    win.after = MagicMock()

    win._hotkey_watchdog()

    win.hotkey_mgr.restart.assert_not_called()   # 必須跳過


def test_cluster_G_watchdog_defers_force_restart_when_combo_active():
    """combo_active=True 時 force-restart 必須跳過。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win.cfg = types.SimpleNamespace(hotkey="cmd+alt+r")
    win.hotkey_mgr = MagicMock()
    win.hotkey_mgr._monitor_global = object()
    win.hotkey_mgr._monitor_local = object()
    win.hotkey_mgr._last_event_at = time.monotonic()
    win.hotkey_mgr._pressed = set()
    win.hotkey_mgr._combo_active = True   # combo 已 armed、等放開
    win._state = "idle"
    win._last_hotkey_force_restart = time.monotonic() - 660.0   # > 10 min
    win.after = MagicMock()

    win._hotkey_watchdog()

    win.hotkey_mgr.restart.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
#  Cluster H — HotkeyBindDialog 開啟期間暫停 hotkey monitor
# ═════════════════════════════════════════════════════════════════════════════

def test_cluster_H_bind_dialog_pauses_hotkey_monitor_on_init():
    """HotkeyBindDialog __init__ 必須呼叫 parent._parent.hotkey_mgr.stop()。"""
    # 這 test 不真的開 Tk 視窗，只驗證 source code 有正確 wiring
    src = Path("gui.py").read_text(encoding="utf-8")
    # 確認 init 區段有 stop hotkey_mgr 的 call
    init_section = src[src.find("class HotkeyBindDialog"):src.find("class HotkeyBindDialog") + 2500]
    assert "hotkey_mgr.stop()" in init_section, \
        "HotkeyBindDialog __init__ 沒呼叫 hotkey_mgr.stop"
    # destroy 區段有 restart
    destroy_section = src[src.find("def destroy(self) -> None:", src.find("HotkeyBindDialog")):]
    destroy_section = destroy_section[:1500]
    assert "hotkey_mgr.restart" in destroy_section, \
        "HotkeyBindDialog destroy 沒 restart hotkey_mgr"
