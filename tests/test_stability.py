"""穩定性層 unit tests（Fix 1-4 / 2026-05-21、Fix 7 / 2026-05-22）。

每個 fix 對應一組 test。不啟動真正的 Tk root / AppWindow（過於重量）；
改用 ``AppWindow.__new__`` 繞過 __init__ 注入最小依賴。
HotkeyManager 因為構造輕量，直接 instantiate。

對應 plan：docs/superpowers/plans/2026-05-21-stability-watchdog-and-recovery.md §5.1
Fix 7 / 2026-05-22：pynput Listener 換成 NSEvent monitor。watchdog Layer 2/3
（CGEventTap re-enable + 定時 force restart）對應 tests 已砍除——NSEvent 不
需要這兩層。新增 NSEvent handler 邏輯的 unit tests。
"""

import time
import types
from unittest.mock import MagicMock

import pytest

from hotkey_manager import HotkeyManager, parse_hotkey, is_pynput_available
from transcriber import TranscriptionResult


def _make_manager_no_listener(combo: str) -> HotkeyManager:
    """建立 HotkeyManager 但**不**安裝真正的 NSEvent monitor。

    跑 mgr.restart() 會註冊 NSEvent global/local monitor；在 pytest 無頭環境
    雖然不會 crash（NSEvent 安全），但仍會影響整個 process 的事件流。
    這裡手動填上 restart() 會設的狀態，足以測試 callback 邏輯。
    """
    mgr = HotkeyManager(on_tap_cb=lambda: None)
    mgr._hotkeys = parse_hotkey(combo)
    mgr._combo_str = combo
    # 偵測 lone-modifier 模式（與 restart() 邏輯相同）
    if len(mgr._hotkeys) == 1:
        from hotkey_manager import _is_lone_modifier_key
        only = next(iter(mgr._hotkeys))
        if _is_lone_modifier_key(only):
            mgr._is_lone_mode = True
            mgr._lone_target_key = only
    return mgr


# ═════════════════════════════════════════════════════════════════════════════
#  Fix 2 — pynput callback 例外被吃掉，不殺死 listener
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not is_pynput_available(), reason="pynput 未安裝")
def test_on_p_swallows_exception():
    """_on_p 內部任何 throw 都不可外洩（避免 pynput stop listener）。"""
    mgr = _make_manager_no_listener("cmd+alt+r")
    # 注入會 throw 的 _normalize
    mgr._normalize = MagicMock(side_effect=RuntimeError("boom"))
    # 不應拋出
    mgr._on_p("fake_key")


@pytest.mark.skipif(not is_pynput_available(), reason="pynput 未安裝")
def test_on_r_swallows_exception():
    """_on_r 內部任何 throw 都不可外洩。"""
    mgr = _make_manager_no_listener("cmd+alt+r")
    mgr._normalize = MagicMock(side_effect=RuntimeError("boom"))
    mgr._on_r("fake_key")


@pytest.mark.skipif(not is_pynput_available(), reason="pynput 未安裝")
def test_on_p_lone_swallows_exception():
    """_on_p_lone（lone-modifier 模式）也包 try/except。"""
    mgr = _make_manager_no_listener("right_cmd")
    assert mgr._is_lone_mode
    # 用會 raise 的 _lock 強制觸發例外路徑
    class _BadLock:
        def __enter__(self): raise RuntimeError("boom")
        def __exit__(self, *a): pass
    mgr._lock = _BadLock()
    mgr._on_p_lone("fake_key")   # 不應拋出


@pytest.mark.skipif(not is_pynput_available(), reason="pynput 未安裝")
def test_on_r_lone_swallows_exception():
    """_on_r_lone（lone-modifier 模式）也包 try/except。"""
    mgr = _make_manager_no_listener("right_cmd")
    assert mgr._is_lone_mode
    class _BadLock:
        def __enter__(self): raise RuntimeError("boom")
        def __exit__(self, *a): pass
    mgr._lock = _BadLock()
    mgr._on_r_lone("fake_key")


# ═════════════════════════════════════════════════════════════════════════════
#  Fix 3 — 轉錄失敗能切回 idle
# ═════════════════════════════════════════════════════════════════════════════

def _make_stub_appwindow():
    """繞過 __init__ 建出一個只夠跑被測方法的 AppWindow 殼。

    避免拉起 Tk / 模型 / 設定檔等重量依賴。需要哪些屬性就在這裡 setattr 哪些。
    """
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win._state = "processing"
    # 攔截 _transition_to_idle / _show_toast，驗證被呼叫 + 改 _state
    def _fake_transition_to_idle(result=None):
        win._state = "idle"
        win._last_result = result
    win._transition_to_idle = _fake_transition_to_idle
    win._show_toast = MagicMock()
    win._last_result = None
    return win


def test_on_transcription_failed_recovers_from_processing():
    """processing 狀態下呼叫 _on_transcription_failed → state 切回 idle。"""
    win = _make_stub_appwindow()
    win._state = "processing"
    win._on_transcription_failed("test error")
    assert win._state == "idle"
    win._show_toast.assert_called_once()
    assert win._last_result is not None
    assert win._last_result.text == "（轉錄失敗，請查看 log）"


def test_on_transcription_failed_noop_when_not_processing():
    """非 processing 狀態下呼叫不會誤觸發 transition；但 toast 仍會顯示。"""
    win = _make_stub_appwindow()
    win._state = "idle"
    win._on_transcription_failed("late callback")
    assert win._state == "idle"   # 維持原狀
    assert win._last_result is None
    win._show_toast.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
#  Fix 4 — Processing 超過 60s 自動恢復
# ═════════════════════════════════════════════════════════════════════════════

def test_processing_timeout_recovers_when_stuck():
    """processing 卡 >60s → 強制切回 idle 並顯示 toast。"""
    win = _make_stub_appwindow()
    win._state = "processing"
    # 偽造 70 秒前進入 processing
    win._processing_started_at = time.monotonic() - 70.0
    win._processing_timeout_check()
    assert win._state == "idle"
    win._show_toast.assert_called_once()
    assert win._last_result.text == "（轉錄超時，請重試）"


def test_processing_timeout_noop_when_not_stuck():
    """processing 剛開始（< 60s）→ 不該誤殺。"""
    win = _make_stub_appwindow()
    win._state = "processing"
    win._processing_started_at = time.monotonic() - 5.0   # 才 5 秒
    win._processing_timeout_check()
    assert win._state == "processing"   # 不變
    win._show_toast.assert_not_called()


def test_processing_timeout_noop_when_already_idle():
    """轉錄已正常完成（state=idle）→ 即使 after 仍延遲觸發，也不該動。"""
    win = _make_stub_appwindow()
    win._state = "idle"
    win._processing_started_at = time.monotonic() - 100.0
    win._processing_timeout_check()
    assert win._state == "idle"
    win._show_toast.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
#  Fix 1 — Listener watchdog 偵測死亡並 restart
# ═════════════════════════════════════════════════════════════════════════════

def _make_stub_appwindow_for_watchdog():
    """繞過 __init__ 建出含 hotkey_mgr / cfg / after 的 AppWindow stub。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    # 偽造 cfg.hotkey
    win.cfg = types.SimpleNamespace(hotkey="cmd+alt+r")
    # 偽造 hotkey_mgr，預設「monitor 活著」（_monitor_global 非 None）
    win.hotkey_mgr = MagicMock()
    win.hotkey_mgr._monitor_global = object()
    win.hotkey_mgr._monitor_local  = object()
    # after 攔成 no-op（不然會卡到真正的 Tk 排程）
    win.after = MagicMock()
    return win


def test_hotkey_watchdog_restarts_when_monitor_missing():
    """Fix 7：NSEvent monitor 雙雙缺席 → 應呼叫 hotkey_mgr.restart 並重排程。"""
    win = _make_stub_appwindow_for_watchdog()
    win.hotkey_mgr._monitor_global = None
    win.hotkey_mgr._monitor_local  = None
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_called_once_with("cmd+alt+r")
    win.after.assert_called_once()


def test_hotkey_watchdog_noop_when_monitor_alive():
    """Fix 7：NSEvent monitor 至少一個非 None → 不該 restart，但要排下一次。"""
    win = _make_stub_appwindow_for_watchdog()
    # 只有 global 在線也算活
    win.hotkey_mgr._monitor_local = None
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_not_called()
    win.after.assert_called_once()


def test_hotkey_watchdog_swallows_exception_and_reschedules():
    """watchdog 自己 throw → 也要被吞掉並排下一次（finally 保證）。"""
    win = _make_stub_appwindow_for_watchdog()
    # 模擬 mgr 屬性存取就 throw
    type(win.hotkey_mgr).__getattribute__ = MagicMock(side_effect=RuntimeError("boom"))
    try:
        win._hotkey_watchdog()
    except Exception:
        pytest.fail("watchdog 不該拋例外")
    win.after.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
#  Fix 6 Step 2 — App Nap 抑制（NSEvent backend 仍保留，主執行緒 responsive）
# ═════════════════════════════════════════════════════════════════════════════

def test_app_nap_token_is_held_module_level():
    """Fix 6 Step 2：_APP_NAP_TOKEN 必須 module-level 強參照（不能被 GC）。"""
    import main
    main._disable_app_nap()
    # macOS 上應拿到 NSObject token；其他平台早返、token 維持 None 也算通過
    import sys
    if sys.platform == "darwin":
        assert main._APP_NAP_TOKEN is not None
    # 必須是 module attribute（不能只是 local）
    assert "_APP_NAP_TOKEN" in vars(main)


# ═════════════════════════════════════════════════════════════════════════════
#  Fix 7 — NSEvent backend：handler 邏輯 unit tests（2026-05-22）
# ═════════════════════════════════════════════════════════════════════════════

def _make_fake_ns_event(evt_type: int, keycode: int, modifier_flags: int = 0, chars: str = ""):
    """偽造 NSEvent，只實作 _handle_ns_event 用到的 3 個 method。"""
    ev = MagicMock()
    ev.type.return_value = evt_type
    ev.keyCode.return_value = keycode
    ev.modifierFlags.return_value = modifier_flags
    ev.charactersIgnoringModifiers.return_value = chars
    return ev


@pytest.mark.skipif(not is_pynput_available(), reason="pynput 未安裝；Key 物件需要")
def test_nsevent_lone_modifier_tap_fires():
    """Fix 7：FlagsChanged 中 right_cmd 按下→放開，無其他鍵介入 → fire tap。"""
    from AppKit import NSEventModifierFlagCommand
    fired = []
    mgr = HotkeyManager(on_tap_cb=lambda: fired.append(True))
    mgr._hotkeys = parse_hotkey("right_cmd")
    mgr._combo_str = "right_cmd"
    mgr._is_lone_mode = True
    from pynput.keyboard import Key
    mgr._lone_target_key = Key.cmd_r

    # 按下 right cmd（keycode 54）：modifierFlags 含 Command bit
    mgr._handle_ns_event(_make_fake_ns_event(12, 54, NSEventModifierFlagCommand))
    # 放開 right cmd：modifierFlags 不再含 Command bit
    mgr._handle_ns_event(_make_fake_ns_event(12, 54, 0))

    assert fired == [True]


@pytest.mark.skipif(not is_pynput_available(), reason="pynput 未安裝；Key 物件需要")
def test_nsevent_lone_modifier_with_other_key_no_fire():
    """Fix 7：right_option + e（dead-key 打 é）→ 不該 fire tap。"""
    from AppKit import NSEventModifierFlagOption
    fired = []
    mgr = HotkeyManager(on_tap_cb=lambda: fired.append(True))
    mgr._hotkeys = parse_hotkey("right_option")
    mgr._combo_str = "right_option"
    mgr._is_lone_mode = True
    from pynput.keyboard import Key
    mgr._lone_target_key = Key.alt_r

    # 按下 right option（keycode 61）
    mgr._handle_ns_event(_make_fake_ns_event(12, 61, NSEventModifierFlagOption))
    # 同時按下 e（keycode 14）
    mgr._handle_ns_event(_make_fake_ns_event(10, 14, NSEventModifierFlagOption, "e"))
    # 放開 e
    mgr._handle_ns_event(_make_fake_ns_event(11, 14, NSEventModifierFlagOption, "e"))
    # 放開 right option
    mgr._handle_ns_event(_make_fake_ns_event(12, 61, 0))

    assert fired == []   # 不該觸發


@pytest.mark.skipif(not is_pynput_available(), reason="pynput 未安裝；Key 物件需要")
def test_nsevent_combo_fires_on_release():
    """Fix 7：cmd+alt+r 三鍵全按 → 任一鍵 release 時 fire。"""
    from AppKit import NSEventModifierFlagCommand, NSEventModifierFlagOption
    fired = []
    mgr = HotkeyManager(on_tap_cb=lambda: fired.append(True))
    mgr._hotkeys = parse_hotkey("cmd+alt+r")
    mgr._combo_str = "cmd+alt+r"

    cmd_bit  = NSEventModifierFlagCommand
    opt_bit  = NSEventModifierFlagOption

    # cmd_l 按下（keycode 55）
    mgr._handle_ns_event(_make_fake_ns_event(12, 55, cmd_bit))
    # alt_l 按下（keycode 58）
    mgr._handle_ns_event(_make_fake_ns_event(12, 58, cmd_bit | opt_bit))
    # r 按下（keycode 15）
    mgr._handle_ns_event(_make_fake_ns_event(10, 15, cmd_bit | opt_bit, "r"))
    # r 放開 → armed combo release → fire
    mgr._handle_ns_event(_make_fake_ns_event(11, 15, cmd_bit | opt_bit, "r"))

    assert fired == [True]


def test_nsevent_unknown_keycode_ignored():
    """Fix 7：FlagsChanged 收到不在 _KEYCODE_TO_SIDED_MOD 的 keycode 應靜默忽略。"""
    mgr = HotkeyManager(on_tap_cb=lambda: None)
    mgr._hotkeys = parse_hotkey("right_cmd") if is_pynput_available() else set()
    mgr._combo_str = "right_cmd"
    # 不該拋例外
    mgr._handle_ns_event(_make_fake_ns_event(12, 999, 0))


def test_nsevent_handler_swallows_exception():
    """Fix 7：handler 內部 raise → 不可外洩到 Cocoa runtime（會 abort 進程）。"""
    mgr = HotkeyManager(on_tap_cb=lambda: None)
    mgr._hotkeys = parse_hotkey("cmd+alt+r") if is_pynput_available() else set()
    mgr._combo_str = "cmd+alt+r"
    # 偽造一個會 raise 的 event
    bad_event = MagicMock()
    bad_event.type.side_effect = RuntimeError("boom")
    # global handler 應吃掉
    mgr._on_ns_event_global(bad_event)
    # local handler 應吃掉並回傳 event 不 suppress
    ret = mgr._on_ns_event_local(bad_event)
    assert ret is bad_event
