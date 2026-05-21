"""穩定性層 unit tests（Fix 1-4 / 2026-05-21）。

每個 fix 對應一組 test。不啟動真正的 Tk root / AppWindow（過於重量）；
改用 ``AppWindow.__new__`` 繞過 __init__ 注入最小依賴。
HotkeyManager 因為構造輕量，直接 instantiate。

對應 plan：docs/superpowers/plans/2026-05-21-stability-watchdog-and-recovery.md §5.1
"""

import time
import types
from unittest.mock import MagicMock

import pytest

from hotkey_manager import HotkeyManager, parse_hotkey, is_pynput_available
from transcriber import TranscriptionResult


def _make_manager_no_listener(combo: str) -> HotkeyManager:
    """建立 HotkeyManager 但**不**啟動真正的 pynput Listener。

    跑 mgr.restart() 會 spawn pynput Listener thread；在 pytest 無頭環境下
    macOS TCC 拒絕 + CGEventTap 取不到，會直接 segfault。
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
    # 偽造 hotkey_mgr
    win.hotkey_mgr = MagicMock()
    win.hotkey_mgr._listener = MagicMock()
    # after 攔成 no-op（不然會卡到真正的 Tk 排程）
    win.after = MagicMock()
    return win


@pytest.mark.skipif(not is_pynput_available(), reason="pynput 未安裝；watchdog 早返")
def test_hotkey_watchdog_restarts_dead_listener():
    """listener.running=False → 應呼叫 hotkey_mgr.restart 並重新排程下一次。"""
    win = _make_stub_appwindow_for_watchdog()
    win.hotkey_mgr._listener.running = False
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_called_once_with("cmd+alt+r")
    # 應重新排程
    win.after.assert_called_once()


@pytest.mark.skipif(not is_pynput_available(), reason="pynput 未安裝；watchdog 早返")
def test_hotkey_watchdog_noop_when_listener_alive():
    """listener.running=True → 不該 restart，但要排下一次檢查。"""
    win = _make_stub_appwindow_for_watchdog()
    win.hotkey_mgr._listener.running = True
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_not_called()
    win.after.assert_called_once()


@pytest.mark.skipif(not is_pynput_available(), reason="pynput 未安裝；watchdog 早返")
def test_hotkey_watchdog_swallows_exception_and_reschedules():
    """watchdog 自己 throw → 也要被吞掉並排下一次（finally 保證）。"""
    win = _make_stub_appwindow_for_watchdog()
    # 模擬 mgr._listener 存取就 throw
    type(win.hotkey_mgr).__getattribute__ = MagicMock(side_effect=RuntimeError("boom"))
    # 不應拋出；finally 仍要排程
    try:
        win._hotkey_watchdog()
    except Exception:
        pytest.fail("watchdog 不該拋例外")
    # after 仍應被呼叫（finally 排程）
    win.after.assert_called_once()
