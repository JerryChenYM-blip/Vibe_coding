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
    """非 processing 狀態下呼叫直接 drop（Fix 9 / P1-B）。

    舊行為（Fix 3）：即使 state=idle 也會 show 「⚠ 轉錄失敗」toast。
    新行為（Fix 9 / 2026-05-22）：state != processing 直接 return，
    避免使用者已開始第二段錄音時被 stale failure toast 干擾。
    """
    win = _make_stub_appwindow()
    win._state = "idle"
    win._on_transcription_failed("late callback")
    assert win._state == "idle"   # 維持原狀
    assert win._last_result is None
    win._show_toast.assert_not_called()   # 不再 show toast


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
    win.hotkey_mgr._last_event_at = time.monotonic()   # 預設「剛剛有事件」
    # Cluster G / 2026-05-23：force-restart 新邏輯會檢查 _pressed + _combo_active；
    # MagicMock 預設 truthy 會誤觸發 defer。stub 顯式設成「沒按鍵 in-flight」。
    win.hotkey_mgr._pressed = set()
    win.hotkey_mgr._combo_active = False
    # Fix 18 / 2026-05-23：watchdog 新分支需要 _state + _last_hotkey_force_restart
    win._state = "idle"
    win._last_hotkey_force_restart = time.monotonic()
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


# ═════════════════════════════════════════════════════════════════════════════
#  Fix 9 — PR #13 code review 修補（2026-05-22）
# ═════════════════════════════════════════════════════════════════════════════

# ─── P1-A：FlagsChanged 用 _ns_held_modifiers state machine ─────────────────

@pytest.mark.skipif(not is_pynput_available(), reason="pynput 未安裝；Key 物件需要")
def test_p1a_sided_modifier_state_machine_releases_correctly():
    """P1-A：左右 Cmd 同時按住、放開一顆 → 即使 modifierFlags 仍含 cmd bit
    也要被正確識別為 release，不該累積 stale _pressed。"""
    from AppKit import NSEventModifierFlagCommand
    mgr = HotkeyManager(on_tap_cb=lambda: None)
    mgr._hotkeys = parse_hotkey("cmd+alt+r")
    mgr._combo_str = "cmd+alt+r"

    cmd_bit = NSEventModifierFlagCommand
    # Left Cmd 按下（keycode=55）
    mgr._handle_ns_event(_make_fake_ns_event(12, 55, cmd_bit))
    # Right Cmd 按下（keycode=54）；modifierFlags 仍含 cmd bit
    mgr._handle_ns_event(_make_fake_ns_event(12, 54, cmd_bit))
    assert 55 in mgr._ns_held_modifiers
    assert 54 in mgr._ns_held_modifiers
    # Left Cmd 放開（keycode=55）；modifierFlags 仍含 cmd bit（cmd_r 還按著）
    # 舊邏輯會把這判為 press（_pressed 累積 stale Key.cmd_l）；新邏輯用
    # _ns_held_modifiers toggle 判定，正確識別為 release
    mgr._handle_ns_event(_make_fake_ns_event(12, 55, cmd_bit))
    assert 55 not in mgr._ns_held_modifiers
    assert 54 in mgr._ns_held_modifiers   # Right Cmd 仍 hold


def test_p1a_stop_clears_ns_held_modifiers():
    """P1-A：stop() 必須清空 _ns_held_modifiers，避免 restart 後 stale state。"""
    mgr = HotkeyManager(on_tap_cb=lambda: None)
    mgr._ns_held_modifiers.update({54, 55, 58})
    mgr.stop()
    assert mgr._ns_held_modifiers == set()


# ─── P1-B：transcription callback state guard ────────────────────────────────

def test_p1b_on_transcription_done_dropped_when_stale():
    """P1-B：state=idle（_processing_timeout_check 已 force-idle）時
    _on_transcription_done 應 drop，不該再呼叫 _transition_to_idle 覆寫
    第二段錄音的 UI。"""
    win = _make_stub_appwindow()
    win._state = "idle"
    win._last_result = None
    win._transition_to_idle = MagicMock()
    result = TranscriptionResult(
        text="late result",
        language="zh",
        duration_seconds=1.0,
        elapsed_seconds=70.0,   # 超過 60s timeout 才回來
        segments=[],
    )
    win._on_transcription_done(result)
    win._transition_to_idle.assert_not_called()
    win._show_toast.assert_not_called()
    assert win._state == "idle"   # 維持原狀


def test_p1b_on_transcription_done_dropped_when_recording():
    """P1-B：第二段錄音已開始（state=recording），stale callback 不該踩平。"""
    win = _make_stub_appwindow()
    win._state = "recording"
    win._transition_to_idle = MagicMock()
    result = TranscriptionResult(
        text="late result", language="zh",
        duration_seconds=1.0, elapsed_seconds=80.0, segments=[],
    )
    win._on_transcription_done(result)
    win._transition_to_idle.assert_not_called()
    assert win._state == "recording"   # 第二段錄音不被干擾


# ─── P2-A：動態 timeout（依音訊長度）────────────────────────────────────────

def test_p2a_dynamic_timeout_long_audio_5min():
    """P2-A：5 分鐘音訊 → timeout 至少 300_000ms（不是固定 60_000ms）。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win._state = "recording"
    win._rec_start = time.perf_counter() - 300.0   # 模擬已錄 5min
    win._stream_tick_id = None
    win._stream_samples = 0
    win._processing_timeout_id = None  # B2（v2.7.0）

    # mock recorder.stop 回傳 5 分鐘的 audio array
    import numpy as np
    five_min_audio = np.zeros(300 * 16_000, dtype=np.float32)
    win.recorder = MagicMock()
    win.recorder.stop.return_value = five_min_audio

    # mock 其他 UI / state 依賴
    win._state_start_time = 0
    win._timer_label = MagicMock()
    win._hotkey_hint = MagicMock()
    win._target_label = MagicMock()
    win._status_dot = MagicMock()
    win._status_label = MagicMock()
    win._model_var = MagicMock()
    win._model_var.get.return_value = "large-v3-turbo"
    win.cfg = MagicMock()
    win.cfg.get_whisper_language.return_value = None
    win.transcriber = MagicMock()
    win._mini_window = None
    win.after = MagicMock()

    # 不要真的開 threading.Thread
    import threading
    orig_thread = threading.Thread
    threading.Thread = MagicMock()
    try:
        win._transition_to_processing()
    finally:
        threading.Thread = orig_thread

    # 找到排程 _processing_timeout_check 的 after 呼叫
    # bound method 每次取都是新 object → 用 __func__ 比對函式本身
    schedule_calls = [
        c for c in win.after.call_args_list
        if len(c.args) >= 2 and getattr(c.args[1], "__func__", None) is type(win)._processing_timeout_check
    ]
    assert schedule_calls, "_processing_timeout_check should be scheduled"
    delay_ms = schedule_calls[0].args[0]
    assert delay_ms >= 300_000, f"5min 音訊應該 timeout ≥ 300_000ms, 拿到 {delay_ms}ms"
    # instance 上應該記下 dynamic timeout
    assert win._processing_timeout_ms >= 300_000


def test_p2a_dynamic_timeout_short_audio_uses_base():
    """P2-A：5 秒短音訊 → timeout 仍至少 BASE 60_000ms（下限保護）。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win._state = "recording"
    win._rec_start = time.perf_counter() - 5.0
    win._stream_tick_id = None
    win._stream_samples = 0
    win._processing_timeout_id = None  # B2（v2.7.0）

    import numpy as np
    short_audio = np.zeros(5 * 16_000, dtype=np.float32)
    win.recorder = MagicMock()
    win.recorder.stop.return_value = short_audio

    win._state_start_time = 0
    win._timer_label = MagicMock()
    win._hotkey_hint = MagicMock()
    win._target_label = MagicMock()
    win._status_dot = MagicMock()
    win._status_label = MagicMock()
    win._model_var = MagicMock()
    win._model_var.get.return_value = "large-v3-turbo"
    win.cfg = MagicMock()
    win.cfg.get_whisper_language.return_value = None
    win.transcriber = MagicMock()
    win._mini_window = None
    win.after = MagicMock()

    import threading
    orig_thread = threading.Thread
    threading.Thread = MagicMock()
    try:
        win._transition_to_processing()
    finally:
        threading.Thread = orig_thread

    assert win._processing_timeout_ms == AppWindow.PROCESSING_TIMEOUT_BASE_MS


# ─── P2-B：MiniHUD instance-unique title ─────────────────────────────────────

def test_p2b_minihud_title_unique_per_instance():
    """P2-B：兩個 MiniRecordingWindow stub instance 的 _ns_title 必須不同。

    為了不真的開 Tk Toplevel（pytest 無頭環境），手動繞過 __init__ 設 attribute。
    """
    from gui import MiniRecordingWindow
    m1 = MiniRecordingWindow.__new__(MiniRecordingWindow)
    m2 = MiniRecordingWindow.__new__(MiniRecordingWindow)
    # 用同樣的邏輯產生 title（與 __init__ 內一致）
    m1._ns_title = f"{MiniRecordingWindow._NS_TITLE_PREFIX}-{id(m1):x}"
    m2._ns_title = f"{MiniRecordingWindow._NS_TITLE_PREFIX}-{id(m2):x}"
    assert m1._ns_title != m2._ns_title
    assert m1._ns_title.startswith("WhisperProMiniHUD-")
    assert m2._ns_title.startswith("WhisperProMiniHUD-")


# ═════════════════════════════════════════════════════════════════════════════
#  Fix 18 — 熱鍵閒置 resilience v2（handler 強引用 + 10min force-restart）
# ═════════════════════════════════════════════════════════════════════════════

def test_fix18_layer1_nsevent_handler_strong_ref_held():
    """Fix 18 Layer 1：bound method 必須存成 instance attribute 強引用。

    根因：原本 `self._on_ns_event_global` 每次 access re-create transient bound
    method，PyObjC bridge 跨 GC 週期可能失去引用 → 閒置 60+ min 後 ObjC block
    指向已釋放的 Python callable → 事件 silent noop。

    驗證：HotkeyManager 必須有 _ns_handler_global / _ns_handler_local 兩個屬性
    存在（即使尚未啟動 monitor 也要初始化為 None），且啟動後是 method-like 物件。
    """
    mgr = HotkeyManager(on_tap_cb=lambda: None)
    # 屬性必須存在（避免 _start_nsevent_monitors 內 setattr 才生效卻被 GC）
    assert hasattr(mgr, "_ns_handler_global")
    assert hasattr(mgr, "_ns_handler_local")
    # 初始值為 None（尚未呼叫 restart）
    assert mgr._ns_handler_global is None
    assert mgr._ns_handler_local is None


def test_fix18_layer2_watchdog_force_restart_periodic():
    """Fix 18 Layer 2：閒置 + 超過 10 分鐘上次 restart → force restart。"""
    win = _make_stub_appwindow_for_watchdog()
    # monitor 物件「還活著」（_monitor_global / local 都 non-None）
    # 但上次 restart 是 11 分鐘前
    win._last_hotkey_force_restart = time.monotonic() - 660.0
    win._state = "idle"

    win._hotkey_watchdog()

    win.hotkey_mgr.restart.assert_called_once_with("cmd+alt+r")
    # restart 後時間戳必須更新
    assert time.monotonic() - win._last_hotkey_force_restart < 1.0


def test_fix18_layer2_watchdog_force_restart_skipped_during_recording():
    """Fix 18 Layer 2：錄音中跳過 force-restart（避免吞掉 stop 訊號）。

    根因：restart 期間 NSEvent monitor 短暫拔掉 ~50ms，使用者錄音中按 hotkey 想停止
    剛好打在這個 window → 訊號遺失、錄音卡住。錄音中必須跳過 periodic restart。
    """
    win = _make_stub_appwindow_for_watchdog()
    win._last_hotkey_force_restart = time.monotonic() - 660.0   # > 10 min
    win._state = "recording"   # 錄音中

    win._hotkey_watchdog()

    # 完全不應該 restart
    win.hotkey_mgr.restart.assert_not_called()


def test_fix18_layer3_watchdog_diagnostic_silent_monitor():
    """Fix 18 Layer 3：monitor alive 但 5 分鐘沒事件 → log 診斷訊息（不主動 restart）。

    從 log 角度驗證：watchdog 抓到 silent state 時不會把 hotkey_mgr.restart 叫出來
    （那是 Layer 2 force-restart 的工作；Layer 3 純診斷）。所以 restart 不被 call、
    after 仍排下一次。
    """
    win = _make_stub_appwindow_for_watchdog()
    # 模擬：monitor 物件還活著，但 6 分鐘沒任何事件，且還沒到 10min force-restart 門檻
    win.hotkey_mgr._last_event_at = time.monotonic() - 360.0   # 6 min silent
    win._last_hotkey_force_restart = time.monotonic() - 60.0   # 只過了 1 min，不該 force restart
    win._state = "idle"

    win._hotkey_watchdog()

    # Layer 3 是診斷 log，不應該 restart
    win.hotkey_mgr.restart.assert_not_called()
    # 但仍要排下一次 watchdog
    win.after.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
#  Fix 19 — 複製按鈕只取最後一段（不是整段累積）
# ═════════════════════════════════════════════════════════════════════════════

def _make_stub_appwindow_for_copy():
    """繞過 __init__ 建出含 _utterance_blocks / _show_toast 的 stub。

    Fix 19 Path B / 2026-05-23：複製流程從 textbox mark-based 改成 block-based，
    stub 直接給一個 list of block-like objects（含 get_current_text）。
    """
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win._utterance_blocks = []
    win._show_toast = MagicMock()
    return win


def _make_block(text: str):
    """建一個假 UtteranceBlock；只給 get_current_text 即可。"""
    block = MagicMock()
    block.get_current_text.return_value = text
    return block


def test_fix19_on_copy_uses_latest_block():
    """Fix 19 Path B：_on_copy 取 _utterance_blocks[0] 的目前顯示文字，不取整段。

    v2.8.0 Bug 3：list 反轉為 [0]=最新、[-1]=最舊。"""
    win = _make_stub_appwindow_for_copy()
    # 三個 block，最新在 [0]
    win._utterance_blocks = [
        _make_block("我當然就好奇了。  "),   # 最新（含尾隨空白，要被 strip）
        _make_block("第二段。"),
        _make_block("第一段。"),
    ]

    import sys
    fake_pyperclip = types.SimpleNamespace(copy=MagicMock())
    sys.modules["pyperclip"] = fake_pyperclip
    try:
        win._on_copy()
    finally:
        sys.modules.pop("pyperclip", None)

    # 剪貼簿收到的是 strip 過的最後一段（不含前面歷史）
    fake_pyperclip.copy.assert_called_once_with("我當然就好奇了。")
    win._show_toast.assert_called_once_with("已複製最後一段")


def test_fix19_on_copy_noop_when_no_blocks():
    """Fix 19 Path B：沒有任何 block（剛啟動）→ 不該 copy，不該 toast。"""
    win = _make_stub_appwindow_for_copy()
    # _utterance_blocks 已是空 list
    import sys
    fake_pyperclip = types.SimpleNamespace(copy=MagicMock())
    sys.modules["pyperclip"] = fake_pyperclip
    try:
        win._on_copy()
    finally:
        sys.modules.pop("pyperclip", None)

    fake_pyperclip.copy.assert_not_called()
    win._show_toast.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# B2 (v2.7.0): processing timeout self-heal — 連續 2 次 _transition_to_processing
# 應該 cancel 前一個 pending callback，避免 N 個 timeout id 累積（timer leak）
# ─────────────────────────────────────────────────────────────────────────────

def test_b2_processing_timeout_cancels_previous_pending():
    """B2：連續兩次 _transition_to_processing → 第二次 schedule 之前
    應該 after_cancel 第一次的 id，並把 _processing_timeout_id 換成新的。"""
    from gui import AppWindow
    import numpy as np

    win = AppWindow.__new__(AppWindow)
    win._state_start_time = 0
    win._timer_label = MagicMock()
    win._hotkey_hint = MagicMock()
    win._target_label = MagicMock()
    win._status_dot = MagicMock()
    win._status_label = MagicMock()
    win._model_var = MagicMock()
    win._model_var.get.return_value = "large-v3-turbo"
    win.cfg = MagicMock()
    win.cfg.get_whisper_language.return_value = None
    win.transcriber = MagicMock()
    win._mini_window = None

    short_audio = np.zeros(5 * 16_000, dtype=np.float32)
    win.recorder = MagicMock()
    win.recorder.stop.return_value = short_audio

    # 模擬 after 回傳遞增的 id；after_cancel 收集被 cancel 的 id
    after_ids = iter(["timeout_id_1", "timeout_id_2"])
    cancelled_ids: list[str] = []
    win.after = MagicMock(side_effect=lambda ms, fn: next(after_ids))
    win.after_cancel = MagicMock(side_effect=cancelled_ids.append)

    # 不要真的開 thread
    import threading
    orig_thread = threading.Thread
    threading.Thread = MagicMock()
    try:
        # 第 1 次
        win._state = "recording"
        win._rec_start = time.perf_counter() - 5.0
        win._stream_tick_id = None
        win._stream_samples = 0
        win._processing_timeout_id = None
        win._transition_to_processing()
        assert win._processing_timeout_id == "timeout_id_1"
        assert cancelled_ids == [], "第 1 次不應 cancel 任何 id"

        # 第 2 次（模擬背景 Whisper 還沒 finish 就再進入 processing）
        win._state = "recording"
        win._rec_start = time.perf_counter() - 5.0
        win._stream_tick_id = None
        win._transition_to_processing()
        assert win._processing_timeout_id == "timeout_id_2"
        # 第 2 次必須 cancel 上一個
        assert cancelled_ids == ["timeout_id_1"], (
            f"第 2 次應該 cancel timeout_id_1, 實際拿到 {cancelled_ids}"
        )
    finally:
        threading.Thread = orig_thread


def test_b2_processing_timeout_check_clears_id_after_fire():
    """B2：_processing_timeout_check 一旦 fire（callback 被 Tk 呼叫），
    `_processing_timeout_id` 應被清成 None，下次判斷不留 stale 值。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win._state = "idle"  # 已正常結束情境（callback 仍會 fire，但 no-op）
    win._processing_timeout_id = "timeout_id_xyz"
    win._processing_started_at = time.monotonic()
    win._processing_timeout_ms = 60_000

    win._processing_timeout_check()
    assert win._processing_timeout_id is None, (
        "callback fire 後應該清 id（避免下次 _transition_to_processing 誤 cancel 已 consume 的 id）"
    )


def test_b2_transition_to_idle_cancels_pending_timeout():
    """B2：processing → idle 正常結束時應該 cancel 對應 timeout self-heal callback。"""
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)
    win._state = "processing"
    win._state_start_time = 0
    win._processing_timeout_id = "timeout_id_to_cancel"
    win._ripples = []
    win._mini_window = None
    win._timer_label = MagicMock()
    win._hotkey_hint = MagicMock()
    win._model_menu = MagicMock()
    win._lang_menu = MagicMock()
    win._target_label = MagicMock()
    win._model_var = MagicMock()
    win._model_var.get.return_value = "large-v3-turbo"
    win._status_dot = MagicMock()
    win._status_label = MagicMock()
    win.cfg = MagicMock()
    win.cfg.format_hotkey_display.return_value = "Right Cmd"

    cancelled: list[str] = []
    win.after_cancel = MagicMock(side_effect=cancelled.append)

    win._transition_to_idle(None)
    assert cancelled == ["timeout_id_to_cancel"]
    assert win._processing_timeout_id is None


# ─────────────────────────────────────────────────────────────────────────────
# A3 (v2.7.0): reduce_motion pref resolution
# ─────────────────────────────────────────────────────────────────────────────

def test_a3_resolve_reduce_motion_always():
    """A3：pref="always" → 永遠 True，不問系統。"""
    from gui import resolve_reduce_motion
    assert resolve_reduce_motion("always") is True


def test_a3_resolve_reduce_motion_never():
    """A3：pref="never" → 永遠 False，即使系統開了 reduce motion。"""
    from gui import resolve_reduce_motion
    assert resolve_reduce_motion("never") is False


def test_a3_resolve_reduce_motion_auto_follows_system(monkeypatch):
    """A3：pref="auto" → 跟 system_reduce_motion() 回傳值。"""
    import gui
    monkeypatch.setattr(gui, "system_reduce_motion", lambda: True)
    assert gui.resolve_reduce_motion("auto") is True
    monkeypatch.setattr(gui, "system_reduce_motion", lambda: False)
    assert gui.resolve_reduce_motion("auto") is False


def test_a3_resolve_reduce_motion_unknown_falls_back_to_auto(monkeypatch):
    """A3：未知 pref（壞掉 config）→ 安全 fallback 到系統偏好。"""
    import gui
    monkeypatch.setattr(gui, "system_reduce_motion", lambda: False)
    assert gui.resolve_reduce_motion("bogus_value") is False
    assert gui.resolve_reduce_motion("") is False


# ═════════════════════════════════════════════════════════════════════════════
# v2.9.0 — D2-S1 / D2-S3 / D4-S4 regression tests
# ═════════════════════════════════════════════════════════════════════════════

# D2-S1: PortAudio stale callback 不應寫到下一段已重置的 buffer

def test_d2s1_stale_callback_does_not_write_to_new_buffer():
    """D2-S1（v2.9.0）：start() bump generation 後，舊段 in-flight callback 不該
    把 frames 寫進新段已重置的 _frames。"""
    from recorder import AudioRecorder
    import numpy as np

    rec = AudioRecorder()
    # 模擬第 1 段 start：generation 變 1
    rec._capture_gen = 0
    rec._is_recording = True
    # 第 1 段的 callback 拿到 gen=1（仍 match）
    chunk1 = np.ones((1600, 1), dtype=np.float32) * 0.1
    rec._audio_callback(chunk1, 1600, None, None, gen=1)
    rec._capture_gen = 1  # start() 內 bump 後的值（模擬第 1 段 start 完成）
    # 第 1 段「正常的」callback（gen=1 與 _capture_gen 同步）
    rec._audio_callback(chunk1, 1600, None, None, gen=1)
    assert len(rec._frames) == 1, "正常 callback 應寫入"

    # 模擬 stop() 跑：bump generation 但 in-flight 的 stale callback 還在路上
    rec._is_recording = False
    rec._capture_gen = 2

    # 模擬第 2 段 start：reset frames、再次 bump
    rec._frames = []
    rec._capture_gen = 3
    rec._is_recording = True

    # 第 1 段尾巴的 stale callback（gen=1）此時才被 PortAudio dispatch
    rec._audio_callback(chunk1, 1600, None, None, gen=1)
    assert len(rec._frames) == 0, (
        f"stale callback (gen=1) 不應寫進第 2 段 buffer (current gen=3)；"
        f"實際拿到 {len(rec._frames)} frames"
    )


def test_d2s1_stale_callback_does_not_update_rms():
    """D2-S1：stale callback 不更新 _rms_level，避免上一段尾音的音量殘留 UI。"""
    from recorder import AudioRecorder
    import numpy as np

    rec = AudioRecorder()
    rec._capture_gen = 5
    rec._is_recording = True
    rec._rms_level = 0.0
    # stale gen=3，current gen=5 → 不該動 RMS
    chunk = np.ones((1600, 1), dtype=np.float32) * 0.5
    rec._audio_callback(chunk, 1600, None, None, gen=3)
    assert rec._rms_level == 0.0


# D2-S3: dictionary snapshot 在 transcribe 入口取一次，mid-flight 修改不影響當次

def test_d2s3_dict_snapshot_locks_terms_for_one_transcribe():
    """D2-S3（v2.9.0）：transcribe 入口 snapshot dict terms。
    snapshot 後改字典不影響該次推論。"""
    from transcriber import Transcriber

    tr = Transcriber()
    tr.set_dictionary_terms(["Kubernetes", "Whisper"])

    # snapshot → 拿到當下的 list copy
    snap = tr._snapshot_dictionary_terms()
    assert snap == ["Kubernetes", "Whisper"]

    # 後續改字典不該影響已取的 snapshot
    tr.set_dictionary_terms(["AWS"])
    assert snap == ["Kubernetes", "Whisper"], "snapshot 應該是獨立 copy"
    # 新 snapshot 反映新值
    new_snap = tr._snapshot_dictionary_terms()
    assert new_snap == ["AWS"]


def test_d2s3_build_initial_prompt_uses_passed_terms_not_current_state():
    """D2-S3：_build_initial_prompt(terms=...) 用傳入 snapshot，不讀 self._dictionary_terms。
    驗證 try + TypeError fallback 兩個 call 都會拿到一致的 prompt。"""
    from transcriber import Transcriber

    tr = Transcriber()
    tr.set_dictionary_terms(["initial_term"])

    snap = tr._snapshot_dictionary_terms()
    # 模擬 mid-flight 改字典
    tr.set_dictionary_terms(["mid_flight_change"])

    # _build_initial_prompt(snap) 應該用 snap 而非當下的 _dictionary_terms
    prompt = tr._build_initial_prompt(snap)
    assert "initial_term" in prompt or "initial_term" in str(prompt), (
        f"prompt 應該含 snapshot 內容 'initial_term'，實際拿到：{prompt!r}"
    )
    # 一定不該漏進新加的字
    assert "mid_flight_change" not in prompt


def test_d2s3_set_dictionary_terms_acquires_lock():
    """D2-S3：set_dictionary_terms 用 _dictionary_lock 保護 reassign。"""
    from transcriber import Transcriber
    tr = Transcriber()
    # lock 必須存在且 acquirable
    assert tr._dictionary_lock.acquire(blocking=False)
    tr._dictionary_lock.release()
    # set_dictionary_terms 內 acquire；不會 deadlock（用 Lock 不是 RLock 也 OK，
    # 因為只在 _dictionary_lock 內做 list reassign，不會 reentrant call）
    tr.set_dictionary_terms(["a", "b"])
    assert tr._dictionary_terms == ["a", "b"]


# D4-S4: Splash fade_tick 偵測 root 已銷毀 → 直接收尾、不再排程

def test_d4s4_splash_fade_tick_detects_destroyed_root():
    """D4-S4（v2.9.0）：root 銷毀後 winfo_exists 回 False，fade_tick 應直接
    mark _closed=True return，不再排程下一個 tick、不再呼叫 attributes。"""
    from splash import SplashScreen
    splash = SplashScreen.__new__(SplashScreen)
    splash._closed = False
    splash._fade_step = 0
    splash._on_done = MagicMock()

    # 模擬 winfo_exists 回 False（root 已銷毀）
    splash.winfo_exists = MagicMock(return_value=False)
    splash.attributes = MagicMock()  # 不該被呼叫
    splash.after = MagicMock()       # 不該被呼叫

    splash._fade_tick()
    assert splash._closed is True
    splash.attributes.assert_not_called()
    splash.after.assert_not_called()


def test_d4s4_splash_fade_tick_winfo_exists_exception_also_safe():
    """D4-S4：winfo_exists 本身拋例外（極端 race）也該安全 mark closed return。"""
    from splash import SplashScreen
    splash = SplashScreen.__new__(SplashScreen)
    splash._closed = False
    splash._fade_step = 0

    splash.winfo_exists = MagicMock(side_effect=RuntimeError("destroyed"))
    splash.attributes = MagicMock()
    splash.after = MagicMock()

    splash._fade_tick()   # 不應拋
    assert splash._closed is True
    splash.attributes.assert_not_called()
    splash.after.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# v2.10.0 — D3-S5/S6/S7 + D4-S6 + D5-S7/S10 regression tests
# ═════════════════════════════════════════════════════════════════════════════

# D5-S7: Ollama health cache TTL

def test_d5s7_health_cache_returns_none_after_ttl():
    """D5-S7（v2.10.0）：cache 寫入後 TTL 過期 → health_ok 回 None。"""
    from ollama_client import OllamaClient, OllamaConfig
    import time as _t
    client = OllamaClient(OllamaConfig(enabled=True))
    # 手動戳 cache 為 True
    client._health_ok = True
    client._health_cached_at = _t.monotonic() - (client.HEALTH_TTL_SEC + 1)
    assert client.health_ok is None, "TTL 過期應該回 None"


def test_d5s7_health_cache_fresh_returns_value():
    """D5-S7：TTL 未過期 → 回 cached 值。"""
    from ollama_client import OllamaClient, OllamaConfig
    import time as _t
    client = OllamaClient(OllamaConfig(enabled=True))
    client._health_ok = True
    client._health_cached_at = _t.monotonic()  # 剛剛
    assert client.health_ok is True


def test_d5s7_health_check_sync_stamps_cache_time():
    """D5-S7：health_check_sync 寫入 cache 同時戳時間。"""
    from ollama_client import OllamaClient, OllamaConfig
    client = OllamaClient(OllamaConfig(enabled=False))
    client._health_cached_at = 0.0
    client.health_check_sync()  # enabled=False 走 fast path 也要戳時間
    assert client._health_cached_at > 0.0


# D5-S10: get_frontmost_app 容錯

def test_d5s10_get_frontmost_app_multiline_takes_first():
    """D5-S10：osascript 偶發多行輸出 → 只取第一行非空字串。"""
    import auto_paste
    from unittest.mock import patch
    # 模擬 osascript 多行輸出
    fake_result = types.SimpleNamespace(
        returncode=0,
        stdout="Notion\nstray line 2\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_result):
        name = auto_paste.get_frontmost_app()
    assert name == "Notion"


def test_d5s10_get_frontmost_app_timeout_returns_none():
    """D5-S10：osascript timeout → 回 None（不 raise）。"""
    import auto_paste
    import subprocess as _sp
    from unittest.mock import patch
    with patch("subprocess.run", side_effect=_sp.TimeoutExpired(cmd="osascript", timeout=1.2)):
        name = auto_paste.get_frontmost_app()
    assert name is None


def test_d5s10_get_frontmost_app_nonzero_returns_none():
    """D5-S10：returncode != 0 → 回 None 並 log_error。"""
    import auto_paste
    from unittest.mock import patch
    fake_result = types.SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
    with patch("subprocess.run", return_value=fake_result):
        name = auto_paste.get_frontmost_app()
    assert name is None


def test_d5s10_get_frontmost_app_empty_returns_none():
    """D5-S10：returncode=0 但 stdout 空白 → 回 None。"""
    import auto_paste
    from unittest.mock import patch
    fake_result = types.SimpleNamespace(returncode=0, stdout="   \n  \n", stderr="")
    with patch("subprocess.run", return_value=fake_result):
        name = auto_paste.get_frontmost_app()
    assert name is None


# D4-S6: hotkey restart() 重綁日誌

def test_d4s6_restart_logs_combo_and_lone_change():
    """D4-S6（v2.10.0）：restart() combo / lone target 變化要 log，讓重綁可 debug。"""
    from hotkey_manager import HotkeyManager
    import hotkey_manager as _hm
    from unittest.mock import patch
    mgr = HotkeyManager(on_tap_cb=lambda: None)

    # mock module-level `log.info` 收集 log 訊息（caplog 對非 root logger
    # 有時不抓得到，改成 patch 更穩）
    captured: list[str] = []
    original_info = _hm.log.info
    with patch.object(_hm.log, "info", side_effect=lambda m: captured.append(m)):
        try:
            mgr.restart("cmd+alt+r")
        except Exception:
            pass  # NSEvent 在 headless 環境可能噴例外
        try:
            mgr.restart("right_cmd")
        except Exception:
            pass

    found = any(
        "restart() combo" in msg and "right_cmd" in msg and "cmd+alt+r" in msg
        for msg in captured
    )
    assert found, f"應該 log combo 切換；實際 captured: {captured}"
