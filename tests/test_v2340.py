"""v2.34.0：按下熱鍵時熱身（A）、背景保溫節流（B）、啟動不載 faster_whisper（C）、熱鍵保險絲忽略殘留按鍵（D）。

全部用假的 transcriber／假執行緒：不開麥克風、不載入真的 Qwen3／MLX 模型。
"""
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

import gui
import transcriber as tr
from gui import AppWindow


# ═════════════════════════════════════════════════════════════════════════════
#  共用替身
# ═════════════════════════════════════════════════════════════════════════════

class _QueuedThread:
    """記下要在背景跑什麼、但不跑——證明主執行緒沒有等它。"""
    started: list = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        _QueuedThread.started.append(self)

    def run_now(self):
        return self.target(*self.args, **self.kwargs)


class _SyncThread(_QueuedThread):
    """start() 當場跑完——給不在乎「主執行緒有沒有等」的測試用。"""
    def start(self):
        self.run_now()


def _worker_threads():
    return [t for t in _QueuedThread.started
            if getattr(t.target, "__name__", "") == "_press_warmup_worker"]


class _FakeTranscriber:
    def __init__(self, *, loaded=True, idle=300.0, warmup_ok=True, warmup_takes_lock=False):
        self.loaded = loaded
        self.idle = idle
        self.warmups: list = []
        self.calls: list = []
        self.pings_marked = 0
        self.warmup_ok = warmup_ok
        self.warmup_takes_lock = warmup_takes_lock
        self._transcription_lock = threading.Lock()

    def is_local_model_loaded(self, model):
        self.calls.append("loaded?")
        return self.loaded

    def seconds_since_model_used(self):
        return self.idle

    def warmup(self, model, language=None):
        self.warmups.append(model)
        if self.warmup_takes_lock:
            # 真的 warmup 會 with self._transcription_lock——假的也要拿，才驗得到探測有沒有放掉鎖。
            # 用逾時而不是無限等：探測沒放掉時要紅燈，不能讓測試卡死。
            if not self._transcription_lock.acquire(timeout=1):
                raise RuntimeError("推論鎖沒被放掉")
            self._transcription_lock.release()
        return self.warmup_ok

    def mark_keepalive_ping(self):
        self.pings_marked += 1


def _press_win(monkeypatch, *, model="qwen3-asr", mac=True, loaded=True, idle=300.0,
               state="recording", sync=False, **fake_kw):
    monkeypatch.setattr(gui, "IS_MAC", mac)
    _QueuedThread.started = []
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread if sync else _QueuedThread)
    fake_log = MagicMock()
    monkeypatch.setattr(gui, "log", fake_log)
    win = AppWindow.__new__(AppWindow)
    win.__dict__.update(
        _state=state,
        _model_var=types.SimpleNamespace(get=lambda: model),
        transcriber=_FakeTranscriber(loaded=loaded, idle=idle, **fake_kw),
    )
    return win, fake_log


def _infos(fake_log):
    return [c.args[0] for c in fake_log.info.call_args_list]


# ═════════════════════════════════════════════════════════════════════════════
#  A. Transcriber 端：模型「上一次被用」、是否已載入
# ═════════════════════════════════════════════════════════════════════════════

def test_A_從沒用過_idle_是_None():
    assert tr.Transcriber().seconds_since_model_used() is None


def test_A_idle_用牆上時間_睡眠時間要算進去():
    """perf_counter 在 macOS 睡眠時不前進；闔蓋前剛用過、隔天一開就按，必須被當成「很久沒用」。"""
    t = tr.Transcriber()
    now = time.perf_counter()
    t._last_transcribe_at = t._last_keepalive_ping_at = now   # 單調時鐘看起來「剛用過」
    t._last_used_wall_at = time.time() - 7200                                      # 牆上時鐘：兩小時前
    assert 7190 < t.seconds_since_model_used() < 7210


def test_A_transcribe_會記下牆上使用時間():
    t = tr.Transcriber()
    t._emit_audit = MagicMock()
    assert t._last_used_wall_at is None
    t.transcribe(None, "qwen3-asr")
    assert t.seconds_since_model_used() < 1


def test_A_保溫ping成功會記下牆上使用時間():
    t = tr.Transcriber()
    t.mark_keepalive_ping()
    assert t.seconds_since_model_used() < 1


def test_A_warmup_成功會記下熱身時間(monkeypatch):
    t = tr.Transcriber()
    monkeypatch.setattr(t, "_warmup_qwen3", lambda m: True)
    assert t.seconds_since_model_used() is None
    assert t.warmup("qwen3-asr") is True
    assert t.seconds_since_model_used() < 1


def test_A_warmup_失敗不算用過(monkeypatch):
    """失敗的熱身沒讓權重變熱；若算成「剛用過」，下一次按鍵會被誤判不需要熱身。"""
    t = tr.Transcriber()
    monkeypatch.setattr(t, "_warmup_qwen3", lambda m: False)
    assert t.warmup("qwen3-asr") is False
    assert t.seconds_since_model_used() is None


def test_A_三個本機暖機失敗都回_False(monkeypatch):
    monkeypatch.setattr(tr, "log_error", MagicMock())
    t = tr.Transcriber()
    monkeypatch.setattr(t, "_ensure_qwen3_session", MagicMock(side_effect=RuntimeError("boom")))
    assert t._warmup_qwen3("qwen3-asr") is False
    monkeypatch.setattr(t, "_ensure_model", MagicMock(side_effect=RuntimeError("boom")))
    assert t._warmup_ctranslate("large-v3-turbo") is False
    fake = types.ModuleType("mlx_whisper")
    fake.transcribe = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    assert t._warmup_mlx("large-v3-turbo") is False


def test_A_三個本機暖機成功都回_True(monkeypatch):
    t = tr.Transcriber()
    session = MagicMock()
    monkeypatch.setattr(t, "_ensure_qwen3_session", lambda m: session)
    assert t._warmup_qwen3("qwen3-asr") is True
    model = MagicMock()
    model.transcribe.return_value = ([], None)
    monkeypatch.setattr(t, "_ensure_model", lambda m: model)
    assert t._warmup_ctranslate("large-v3-turbo") is True
    fake = types.ModuleType("mlx_whisper")
    fake.transcribe = lambda *a, **k: {"segments": [], "language": "zh"}
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    assert t._warmup_mlx("large-v3-turbo") is True


def test_A_qwen3_已載入才算(monkeypatch):
    t = tr.Transcriber()
    assert t.is_local_model_loaded("qwen3-asr") is False
    t._qwen3_session, t._qwen3_loaded_model = object(), "qwen3-asr"
    assert t.is_local_model_loaded("qwen3-asr") is True
    assert t.is_local_model_loaded("qwen3-asr-large") is False   # 載的是別的變體


def test_A_蘋果永遠不算本機模型(monkeypatch):
    # 各後端的「已載入」旗標都故意設成跟蘋果模型名字一樣，擋住的只能是蘋果判斷本身
    t = tr.Transcriber()
    t._qwen3_session, t._qwen3_loaded_model = object(), "apple-speech"
    t._mlx_whisper_warm_model = "apple-speech"
    t._model, t._loaded_model_size = object(), "apple-speech"
    for backend in ("mlx", "ctranslate"):
        monkeypatch.setattr(tr, "BACKEND", backend)
        assert t.is_local_model_loaded("apple-speech") is False


def test_A_mlx_whisper_暖機成功後才算已載入_unload_後清掉(monkeypatch):
    monkeypatch.setattr(tr, "BACKEND", "mlx")
    fake = types.ModuleType("mlx_whisper")
    fake.transcribe = lambda *a, **k: {"segments": [], "language": "zh"}
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    t = tr.Transcriber()
    assert t.is_local_model_loaded("large-v3-turbo") is False
    t._warmup_mlx("large-v3-turbo")
    assert t.is_local_model_loaded("large-v3-turbo") is True
    assert t.is_local_model_loaded("large-v3") is False
    t.unload()
    assert t.is_local_model_loaded("large-v3-turbo") is False


def test_A_ctranslate_看模型實例與大小(monkeypatch):
    monkeypatch.setattr(tr, "BACKEND", "ctranslate")
    t = tr.Transcriber()
    assert t.is_local_model_loaded("large-v3-turbo") is False
    t._model, t._loaded_model_size = object(), "large-v3-turbo"
    assert t.is_local_model_loaded("large-v3-turbo") is True
    assert t.is_local_model_loaded("small") is False


# ═════════════════════════════════════════════════════════════════════════════
#  A. GUI 端：_maybe_press_warmup
# ═════════════════════════════════════════════════════════════════════════════

def test_A_Mac_本機模型_閒置夠久_背景熱身被呼叫(monkeypatch):
    win, fake_log = _press_win(monkeypatch)
    win._maybe_press_warmup()
    threads = _worker_threads()
    assert len(threads) == 1
    assert win.transcriber.warmups == [], "主執行緒只排工作、不能自己熱身"
    threads[0].run_now()
    assert win.transcriber.warmups == ["qwen3-asr"]
    done = [m for m in _infos(fake_log) if m.startswith("MLX_PRESS_WARMUP: done in")]
    assert len(done) == 1 and "idle_s=300" in done[0]


def test_A_蘋果模型不做(monkeypatch):
    win, _ = _press_win(monkeypatch, model="apple-speech")
    win._maybe_press_warmup()
    assert _worker_threads() == []
    assert win.transcriber.calls == []


def test_A_Windows_不做(monkeypatch):
    win, _ = _press_win(monkeypatch, mac=False)
    win._maybe_press_warmup()
    assert _worker_threads() == []


def test_A_60秒內用過不做(monkeypatch):
    win, _ = _press_win(monkeypatch, idle=59.0)
    win._maybe_press_warmup()
    assert _worker_threads() == []


def test_A_剛好60秒就做(monkeypatch):
    win, _ = _press_win(monkeypatch, idle=60.0)
    win._maybe_press_warmup()
    assert len(_worker_threads()) == 1


def test_A_從沒用過但模型已載入_要做(monkeypatch):
    win, _ = _press_win(monkeypatch, idle=None)
    win._maybe_press_warmup()
    assert len(_worker_threads()) == 1


def test_A_模型沒載入不做_避免錄音時冷載入(monkeypatch):
    win, _ = _press_win(monkeypatch, loaded=False)
    win._maybe_press_warmup()
    assert _worker_threads() == []
    assert win.transcriber.warmups == []


def test_A_推論鎖忙就跳過(monkeypatch):
    win, fake_log = _press_win(monkeypatch)
    win._maybe_press_warmup()
    win.transcriber._transcription_lock.acquire()
    try:
        _worker_threads()[0].run_now()
    finally:
        win.transcriber._transcription_lock.release()
    assert win.transcriber.warmups == []
    assert not any("MLX_PRESS_WARMUP: done" in m for m in _infos(fake_log))


def test_A_背景開始時已不在錄音中就不做(monkeypatch):
    win, _ = _press_win(monkeypatch)
    win._maybe_press_warmup()
    win._state = "processing"   # 使用者已放開，放開後的真推論優先
    _worker_threads()[0].run_now()
    assert win.transcriber.warmups == []


def test_A_熱身進行中再按不會疊第二個(monkeypatch):
    win, _ = _press_win(monkeypatch)
    win._maybe_press_warmup()
    win._maybe_press_warmup()
    assert len(_worker_threads()) == 1
    _worker_threads()[0].run_now()
    win._maybe_press_warmup()    # 跑完後可以再排
    assert len(_worker_threads()) == 2


def test_A_熱身出錯不會讓錄音壞掉(monkeypatch):
    win, _ = _press_win(monkeypatch)
    win.transcriber.warmup = MagicMock(side_effect=RuntimeError("boom"))
    win._maybe_press_warmup()
    _worker_threads()[0].run_now()          # 不得往外丟
    win.transcriber.warmup = lambda m: None
    win._maybe_press_warmup()               # 旗標已清掉、還能再排
    assert len(_worker_threads()) == 2


def test_A_檢查本身出錯也不往外丟(monkeypatch):
    win, _ = _press_win(monkeypatch)
    win.transcriber.is_local_model_loaded = MagicMock(side_effect=RuntimeError("boom"))
    win._maybe_press_warmup()
    assert _worker_threads() == []


@pytest.mark.parametrize("model", ["qwen3-asr-large", "large-v3-turbo", "large-v3", "small"])
def test_A_只對_qwen3_asr_0點6B_做_其他模型沒量過不做(monkeypatch, model):
    """實驗與 App 數據只有 0.6B；MLX Whisper 的 warmup 自己註解寫 2～5 秒、1.7B 沒量過，
    熱身佔推論鎖會讓短錄音多等更久——量過再開。"""
    win, _ = _press_win(monkeypatch, model=model)
    win._maybe_press_warmup()
    assert _worker_threads() == []
    assert win.transcriber.calls == []


def test_A_熱身失敗記_failed_不記_done(monkeypatch):
    win, fake_log = _press_win(monkeypatch, warmup_ok=False)
    win._maybe_press_warmup()
    _worker_threads()[0].run_now()
    assert win.transcriber.warmups == ["qwen3-asr"]
    infos = _infos(fake_log)
    assert not any("MLX_PRESS_WARMUP: done" in m for m in infos)
    failed = [m for m in infos if m.startswith("MLX_PRESS_WARMUP: failed")]
    assert len(failed) == 1 and "idle_s=300" in failed[0]


def test_A_探測推論鎖後一定放掉(monkeypatch):
    """真的 threading.Lock + 會拿鎖的 warmup：探測拿了沒放的話 warmup 拿不到鎖、不會記 done。"""
    win, fake_log = _press_win(monkeypatch, sync=True, warmup_takes_lock=True)
    lock = win.transcriber._transcription_lock
    win._maybe_press_warmup()
    assert win.transcriber.warmups == ["qwen3-asr"]
    assert any(m.startswith("MLX_PRESS_WARMUP: done") for m in _infos(fake_log)), "warmup 沒拿到鎖"
    assert lock.acquire(blocking=False), "worker 跑完後推論鎖必須是放開的"
    lock.release()


# ── 在 _transition_to_recording 裡的位置 ──

def _recording_win(monkeypatch, *, start_ok=True, sync=False):
    win, fake_log = _press_win(monkeypatch, state="idle", sync=sync)
    order: list = []
    win.recorder = MagicMock(**{"_started_with_fallback": False})
    win.recorder.start.side_effect = lambda: (order.append("start"), start_ok)[1]
    win.recorder.warm_state.return_value = "live"
    ft = win.transcriber
    orig = ft.is_local_model_loaded
    ft.is_local_model_loaded = lambda m: (order.append("check"), orig(m))[1]
    win.__dict__.update(
        _skeleton_mode=False, _status_slot_active=None,
        cfg=types.SimpleNamespace(auto_paste=True, hotkey="right_cmd", apple_streaming=False,
                                  ollama_enabled=False, streaming_algo="fixed_chunk",
                                  format_hotkey_display=lambda: "右 ⌘"),
        _lang_var=MagicMock(), _ripples=[], _wave_engine=None,
        _target_label=MagicMock(), _timer_label=MagicMock(), _hotkey_hint=MagicMock(),
        _model_menu=MagicMock(), _lang_menu=MagicMock(), _set_status=MagicMock(),
        _update_timer=MagicMock(), _stream_generation=0, after=MagicMock(),
        _mini_window=None, _show_toast=MagicMock(),
        _audio_refreshing=False, _audio_ready=True,
    )
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    for name in ("_show_big_wave", "_stream_tick"):
        setattr(win, name, MagicMock())
    return win, order


def test_A_按下後立刻返回_熱身在背景_且在_recorder_start_之後(monkeypatch):
    win, order = _recording_win(monkeypatch)
    t0 = time.monotonic()
    win._transition_to_recording()
    assert time.monotonic() - t0 < 1
    assert win._state == "recording"
    assert order == ["start", "check"], "一定要錄音開始之後才檢查／熱身"
    assert win.transcriber.warmups == [], "熱身不能在主執行緒跑"
    assert len(_worker_threads()) == 1


def test_A_recorder_start_失敗就不熱身(monkeypatch):
    win, order = _recording_win(monkeypatch, start_ok=False)
    win._status_slot_show_banner = lambda *a, **k: None
    win._transition_to_recording()
    assert win._state == "idle"
    assert order == ["start"]
    assert _worker_threads() == []


def test_A_熱身條件檢查炸掉不影響進入錄音(monkeypatch):
    win, _ = _recording_win(monkeypatch)
    win.transcriber.is_local_model_loaded = MagicMock(side_effect=RuntimeError("boom"))
    win._transition_to_recording()
    assert win._state == "recording"


# ═════════════════════════════════════════════════════════════════════════════
#  B. 背景保溫只在最後一次使用後 30 分鐘內
# ═════════════════════════════════════════════════════════════════════════════

def _tick_win(monkeypatch, *, quiet_s, model="qwen3-asr", mac=True, **fake_kw):
    monkeypatch.setattr(gui, "IS_MAC", mac)
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread)
    fake_log = MagicMock()
    monkeypatch.setattr(gui, "log", fake_log)
    win = AppWindow.__new__(AppWindow)
    win.transcriber = _FakeTranscriber(**fake_kw)
    win._state = "idle"
    win._model_var = types.SimpleNamespace(get=lambda: model)
    win._last_ping_at = None
    win._last_user_activity = time.time() - quiet_s
    win.after = MagicMock()
    return win, fake_log


def test_B_Windows_閒置再久也照舊每90秒ping(monkeypatch):
    """Windows 沒有按鍵熱身補償（A 只在 Mac），保溫維持原樣。"""
    win, fake_log = _tick_win(monkeypatch, quiet_s=60 * 60, mac=False)
    win._mlx_keepalive_tick()
    assert win.transcriber.warmups == ["qwen3-asr"]
    assert not any("paused" in m for m in _infos(fake_log))
    win.after.assert_called_once()


@pytest.mark.parametrize("model", ["qwen3-asr-large", "large-v3-turbo"])
def test_B_沒有按鍵熱身補償的模型_閒置再久也照舊ping(monkeypatch, model):
    """大模型與 MLX Whisper 不做按鍵熱身（沒量過）；停了保溫就完全沒有補償（審查找到）。"""
    win, fake_log = _tick_win(monkeypatch, quiet_s=60 * 60, model=model)
    win._mlx_keepalive_tick()
    assert win.transcriber.warmups == [model]
    assert not any("paused" in m for m in _infos(fake_log))


def test_B_閒置時間用牆上時間_睡眠要算進去(monkeypatch):
    """monotonic 在 macOS 睡眠時不前進：牆上時鐘兩小時前、單調時鐘看起來才剛過，仍要休息。"""
    win, _ = _tick_win(monkeypatch, quiet_s=2 * 3600)
    win._mlx_keepalive_tick()
    assert win.transcriber.warmups == []


def test_B_ping失敗不記帳_不算ping完成(monkeypatch):
    win, fake_log = _tick_win(monkeypatch, quiet_s=10, warmup_ok=False)
    win._mlx_keepalive_tick()
    assert win.transcriber.warmups == ["qwen3-asr"]
    assert win.transcriber.pings_marked == 0
    assert not any("ping done" in m for m in _infos(fake_log))


def test_B_ping成功才記帳(monkeypatch):
    win, fake_log = _tick_win(monkeypatch, quiet_s=10)
    win._mlx_keepalive_tick()
    assert win.transcriber.pings_marked == 1
    assert any("ping done" in m for m in _infos(fake_log))


def test_B_三十分鐘內照常ping(monkeypatch):
    win, _ = _tick_win(monkeypatch, quiet_s=29 * 60)
    win._mlx_keepalive_tick()
    assert win.transcriber.warmups == ["qwen3-asr"]
    win.after.assert_called_once()


def test_B_超過三十分鐘不ping_但照常排下一次(monkeypatch):
    win, _ = _tick_win(monkeypatch, quiet_s=31 * 60)
    win._mlx_keepalive_tick()
    assert win.transcriber.warmups == []
    win.after.assert_called_once()


def test_B_休息日誌只記一行_不是每個tick一行(monkeypatch):
    win, fake_log = _tick_win(monkeypatch, quiet_s=31 * 60)
    for _ in range(5):
        win._mlx_keepalive_tick()
    paused = [m for m in _infos(fake_log) if m.startswith("MLX_KEEPALIVE: paused")]
    assert paused == ["MLX_KEEPALIVE: paused — no use for 30 min"]


def test_B_使用後恢復_恢復日誌只一行(monkeypatch):
    win, fake_log = _tick_win(monkeypatch, quiet_s=31 * 60)
    win._mlx_keepalive_tick()
    assert win.transcriber.warmups == []
    win._last_user_activity = time.time()      # 使用者錄了一次音
    for _ in range(3):
        win._mlx_keepalive_tick()
    assert win.transcriber.warmups == ["qwen3-asr"] * 3
    resumed = [m for m in _infos(fake_log) if m.startswith("MLX_KEEPALIVE: resumed")]
    assert len(resumed) == 1


def test_B_一直沒休息過就沒有恢復日誌(monkeypatch):
    win, fake_log = _tick_win(monkeypatch, quiet_s=10)
    win._mlx_keepalive_tick()
    assert not any("resumed" in m or "paused" in m for m in _infos(fake_log))


def test_B_蘋果模型跳過_也不記休息日誌(monkeypatch):
    win, fake_log = _tick_win(monkeypatch, quiet_s=31 * 60, model="apple-speech")
    win._mlx_keepalive_tick()
    assert win.transcriber.warmups == []
    assert not any("paused" in m for m in _infos(fake_log))


def test_B_非閒置狀態維持原樣跳過(monkeypatch):
    win, _ = _tick_win(monkeypatch, quiet_s=10)
    win._state = "recording"
    win._mlx_keepalive_tick()
    assert win.transcriber.warmups == []


def test_B_錄音開始會更新使用時間(monkeypatch):
    win, _ = _recording_win(monkeypatch)
    win._last_user_activity = time.time() - 3600
    win._transition_to_recording()
    assert time.time() - win._last_user_activity < 5


def test_B_轉字完成回到閒置會更新使用時間():
    win = MagicMock()
    win._last_user_activity = time.time() - 3600
    AppWindow._transition_to_idle(win, None)
    assert time.time() - win._last_user_activity < 5


def test_B_啟動時就有初始使用時間():
    """__init__ 太重不實例化；改檢查原始碼：tick 第一次排程之前一定要先設好，否則第一次 tick 會 AttributeError。"""
    import inspect
    src = inspect.getsource(AppWindow.__init__)
    assert src.index("self._last_user_activity") < src.index("self._mlx_keepalive_tick)")


# ═════════════════════════════════════════════════════════════════════════════
#  C. 啟動檢查不載入 faster_whisper
# ═════════════════════════════════════════════════════════════════════════════

def test_C_check_dependencies_不載入_faster_whisper(monkeypatch):
    import builtins
    import main
    for name in [n for n in sys.modules if n == "faster_whisper" or n.startswith("faster_whisper.")]:
        monkeypatch.delitem(sys.modules, name)
    imported = []
    real_import = builtins.__import__

    def _spy(name, *a, **k):
        imported.append(name)
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _spy)
    assert main.check_dependencies() == []
    assert "faster_whisper" not in imported
    assert "faster_whisper" not in sys.modules


def test_C_沒裝_faster_whisper_仍回報缺少(monkeypatch):
    import importlib.util
    import main
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None if name == "faster_whisper" else real(name, *a, **k))
    assert main.check_dependencies() == ["faster_whisper"]


def test_C_其他套件照舊真的載入檢查(monkeypatch):
    """customtkinter／pyperclip 缺了要照樣被擋——只有 faster_whisper 與 sounddevice 改成不載入。"""
    import builtins
    import main
    real_import = builtins.__import__

    def _no_pyperclip(name, *a, **k):
        if name == "pyperclip":
            raise ImportError("fake missing")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _no_pyperclip)
    assert main.check_dependencies() == ["pyperclip"]


# ═════════════════════════════════════════════════════════════════════════════
#  D. 熱鍵保險絲忽略殘留的按鍵紀錄
# ═════════════════════════════════════════════════════════════════════════════

def _dog_win(monkeypatch, *, pressed=("cmd_r",), combo=False, event_age=30.0, held=()):
    monkeypatch.setattr(gui, "IS_MAC", True)
    monkeypatch.setattr(gui, "IS_WINDOWS", False)
    fake_log = MagicMock()
    monkeypatch.setattr(gui, "log", fake_log)
    win = AppWindow.__new__(AppWindow)
    win.cfg = types.SimpleNamespace(hotkey="right_cmd")
    win.hotkey_mgr = MagicMock()
    win.hotkey_mgr._monitor_global = object()
    win.hotkey_mgr._monitor_local = object()
    win.hotkey_mgr._last_event_at = (time.monotonic() - event_age) if event_age is not None else 0.0
    win.hotkey_mgr._pressed = set(pressed)
    win.hotkey_mgr._combo_active = combo
    win.hotkey_mgr._ns_held_modifiers = set(held)
    win._state = "idle"
    win._last_hotkey_force_restart = time.monotonic() - 660.0   # 超過 10 分鐘
    win._hotkey_health_tick_count = 0
    win.after = MagicMock()
    return win, fake_log


def test_D_殘留按鍵超過10秒沒事件_照常重啟並記一行(monkeypatch):
    win, fake_log = _dog_win(monkeypatch, pressed=("cmd_r",), event_age=30.0)
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_called_once_with("right_cmd")
    stale = [m for m in _infos(fake_log) if m.startswith("HOTKEY: watchdog ignoring stale in-flight")]
    assert len(stale) == 1
    assert "pressed=1" in stale[0] and "last_event 30s ago" in stale[0]


def test_D_殘留的_combo_active_也一樣(monkeypatch):
    win, _ = _dog_win(monkeypatch, pressed=(), combo=True, event_age=60.0)
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_called_once()


def test_D_10秒內有事件_照舊延後(monkeypatch):
    win, fake_log = _dog_win(monkeypatch, pressed=("cmd_r",), event_age=3.0)
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_not_called()
    assert not any("ignoring stale" in m for m in _infos(fake_log))


def test_D_邊界_9秒延後_11秒重啟(monkeypatch):
    win, _ = _dog_win(monkeypatch, event_age=9.0)
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_not_called()
    win, _ = _dog_win(monkeypatch, event_age=11.0)
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_called_once()


def test_D_從沒收過事件_按鍵紀錄一定是殘留(monkeypatch):
    win, _ = _dog_win(monkeypatch, pressed=("cmd_r",), event_age=None)
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_called_once()


def test_D_沒有按鍵紀錄時行為不變(monkeypatch):
    win, fake_log = _dog_win(monkeypatch, pressed=(), event_age=30.0)
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_called_once()
    assert not any("ignoring stale" in m for m in _infos(fake_log))


def test_D_側別修飾鍵可能真的按著_照舊延後(monkeypatch):
    """真的按住修飾鍵超過 10 秒時重啟，stop() 會清掉 _ns_held_modifiers，之後放開被當成按下、
    tap 時機反轉到下一次重啟——寧可延後（退回舊行為）。"""
    win, fake_log = _dog_win(monkeypatch, pressed=("cmd_r",), event_age=30.0, held=(54,))
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_not_called()
    assert not any("ignoring stale" in m for m in _infos(fake_log))
    assert any("deferring force-restart" in m for m in _infos(fake_log))


def test_D_修飾鍵記著按住但超過60秒沒事件_當成漏收的殘留照常重啟(monkeypatch):
    """放開事件漏收一次後，按下／放開會一直顛倒；60 秒沒任何事件，不可能是真的按著。"""
    win, fake_log = _dog_win(monkeypatch, pressed=("cmd_r",), event_age=120.0, held=(54,))
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_called_once()
    assert any("ignoring stale" in m for m in _infos(fake_log))


def test_D_修飾鍵門檻邊界_59秒延後_61秒重啟(monkeypatch):
    win, _ = _dog_win(monkeypatch, pressed=("cmd_r",), event_age=59.0, held=(54,))
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_not_called()
    win, _ = _dog_win(monkeypatch, pressed=("cmd_r",), event_age=61.0, held=(54,))
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_called_once()


def test_D_沒有側別修飾鍵被按著_才當殘留(monkeypatch):
    win, _ = _dog_win(monkeypatch, pressed=("a",), event_age=30.0, held=())
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_called_once()


def test_D_門檻與熱鍵管理員的_stale_combo_一致():
    import hotkey_manager
    assert gui._STALE_PRESS_S == hotkey_manager._STALE_COMBO_SEC == 10.0
