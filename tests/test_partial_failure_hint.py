"""長錄音切段辨識時，有一段失敗要讓使用者看得到（v2.32.1）。

背景（2026-10-05 v2.32.0 審查）：長錄音會一段一段（約 10～12 秒）邊錄邊轉。某一段辨識失敗時，
那一段被靜悄悄變成空白；尾段失敗則整次錄音當成失敗、前面轉好的全部不見。使用者看不出少了東西。

要的行為：
  (a) 分得出「失敗」跟「這段本來就沒講話」——沒講話不能跳警告
  (b) 失敗的段落記下來，合併時照樣貼出成功的部分、絕不把錯誤訊息貼進文件
  (c) 尾段失敗不連累前面轉好的段落；全部失敗才算整次失敗
  (d) 放開後還沒轉完的段落（等太久被放棄）也算失敗
  (e) 畫面上看得到：完成提示寫出幾段沒辨識出來、狀態列留一條不會自己消失的提示
  (f) 紀錄裡記段數（不記內容）
"""

import types

import pytest

import transcriber as tr
from transcriber import TranscriptionResult


FAIL = "（蘋果語音辨識失敗、請重試或於設定切回其他模型）"
SILENT = "（未偵測到語音內容）"


def _r(text):
    return TranscriptionResult(text=text, language="zh", duration_seconds=10.0, elapsed_seconds=0.3)


# ── (a) 失敗 vs 沒講話 ──

@pytest.mark.parametrize("text,期望", [
    (FAIL, True),
    ("（Qwen3-ASR 轉錄失敗、請重試或於設定切回 large-v3-turbo）", True),
    ("（某個以後才加的新錯誤）", True),           # 沒登記的訊息當失敗：寧可多提醒，不能把失敗藏起來
    (SILENT, False),
    ("（沒有偵測到音訊，請確認麥克風是否正常運作）", False),
    ("今天天氣很好", False),
    ("", False),
])
def test_分得出失敗跟沒講話(text, 期望):
    assert tr.is_failure_message(text) is 期望


# ── (b) 切段時記下失敗的段落 ──

class _SyncThread:
    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args
    def start(self):
        self._target(*self._args)


def _chunk_win(monkeypatch, transcribe):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    win._stream_chunks = []
    win._stream_failed = []
    win._stream_dispatched = win._stream_completed = 0
    win._stream_generation = 1
    win._recording_pid = "p1"
    win._last_voice_at = 0.0
    win.cfg = types.SimpleNamespace(model="apple-speech", ollama_enabled=False,
                                    get_whisper_language=lambda: "zh")
    win.transcriber = types.SimpleNamespace(transcribe=transcribe)
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread)
    return gui, win


@pytest.mark.parametrize("回應,期望", [
    (lambda *a, **k: _r(FAIL), [0]),
    (lambda *a, **k: (_ for _ in ()).throw(RuntimeError("壞了")), [0]),
    (lambda *a, **k: _r(SILENT), []),          # 停頓了 10 秒沒講話，不算失敗
    (lambda *a, **k: _r("第一段"), []),
])
def test_切段失敗會被記下來(monkeypatch, 回應, 期望):
    gui, win = _chunk_win(monkeypatch, 回應)
    gui.AppWindow._dispatch_stream_chunk(win, [0.0] * 10)
    assert win._stream_failed == 期望
    assert win._stream_completed == 1


def test_舊錄音的段落失敗不算到新錄音(monkeypatch):
    def slow(*a, **k):
        win._stream_generation = 2          # 轉到一半使用者開了新的錄音
        return _r(FAIL)
    gui, win = _chunk_win(monkeypatch, slow)
    gui.AppWindow._dispatch_stream_chunk(win, [0.0] * 10)
    assert win._stream_failed == []


# ── (b)(c)(d) 合併 ──

def _merge_win(monkeypatch, *, chunks, failed, tail, completed=None):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    win._la_buffer = None
    win._stream_chunks = list(chunks)
    win._stream_failed = list(failed)
    win._stream_dispatched = len(chunks)
    win._stream_completed = len(chunks) if completed is None else completed
    win.STREAM_CHUNK_JOIN_TIMEOUT_S = 0.1
    win._full_audio_s = 30.0
    win.cfg = types.SimpleNamespace(chinese_variant="off")
    win._apple_shadow_active = lambda: False
    win.transcriber = types.SimpleNamespace(transcribe=tail)
    win.delivered, win.failed_calls = [], []
    win.after = lambda ms, fn, *a: (win.delivered if fn.__name__ == "_on_transcription_done"
                                    else win.failed_calls).extend(a)
    gui.AppWindow._run_transcription(win, [0.0] * 100, "apple-speech", "zh", None)
    return win


def test_中間一段失敗_成功的照樣交出去並記段數(monkeypatch):
    win = _merge_win(monkeypatch, chunks=["第一段。", ""], failed=[1],
                     tail=lambda *a, **k: _r("第三段。"))
    r = win.delivered[0]
    assert "第一段" in r.text and "第三段" in r.text and "（" not in r.text
    assert (r.failed_segments, r.total_segments) == (1, 3)


def test_沒有失敗就不標(monkeypatch):
    win = _merge_win(monkeypatch, chunks=["第一段。", "第二段。"], failed=[],
                     tail=lambda *a, **k: _r("第三段。"))
    assert getattr(win.delivered[0], "failed_segments", 0) == 0


def test_尾段當掉_前面轉好的不能跟著不見(monkeypatch):
    """原本尾段一當掉，整次錄音算失敗、前面轉好的全部不見。"""
    win = _merge_win(monkeypatch, chunks=["第一段。", "第二段。"], failed=[],
                     tail=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("壞了")))
    assert win.failed_calls == []
    r = win.delivered[0]
    assert "第一段" in r.text and "第二段" in r.text
    assert r.failed_segments == 1


def test_尾段回失敗訊息也算一段失敗(monkeypatch):
    win = _merge_win(monkeypatch, chunks=["第一段。"], failed=[],
                     tail=lambda *a, **k: _r(FAIL))
    r = win.delivered[0]
    assert "（" not in r.text and r.failed_segments == 1


def test_全部失敗_顯示失敗而不是沒講話(monkeypatch):
    """全失敗時說「沒偵測到語音」是誤導：使用者會以為麥克風壞了。"""
    win = _merge_win(monkeypatch, chunks=["", ""], failed=[0, 1],
                     tail=lambda *a, **k: _r(FAIL))
    r = win.delivered[0]
    assert tr.is_failure_message(r.text)
    assert r.failed_segments == 3


def test_等太久還沒轉完的段落也算失敗(monkeypatch):
    win = _merge_win(monkeypatch, chunks=["第一段。", ""], failed=[], completed=1,
                     tail=lambda *a, **k: _r("第三段。"))
    assert win.delivered[0].failed_segments == 1


# ── (e)(f) 畫面與紀錄 ──

def _done_win(monkeypatch, *, failed, text="第一段。第三段。", mini=False):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    log = []
    if mini:
        win._mini_window = types.SimpleNamespace(
            hide=lambda: log.append(("hud_hide",)),
            show_warning=lambda msg: log.append(("hud_warn", msg)))
    win.__dict__.update(
        _state="processing", _pipeline_t0=None, _polish_generation=0, _paste_target="Notes",
        _skeleton_mode=True, _utterance_blocks=[], _frontmost_app="Notes",
        cfg=types.SimpleNamespace(auto_copy=False, auto_paste=True, ollama_enabled=False,
                                  polish_backend="local"),
        ollama=types.SimpleNamespace(health_ok=True),
        _model_var=types.SimpleNamespace(get=lambda: "apple-speech"),
        history_store=types.SimpleNamespace(insert=lambda **k: 1),
        _transition_to_idle=lambda result=None: log.append(("idle",)),
        _show_toast=lambda msg, *a, **k: log.append(("toast", msg)),
        _do_auto_paste=lambda text, target: log.append(("paste", text)),
        _apply_toggle_style=lambda: None,
        _emit_pipeline_timing=lambda **k: None,
        _status_slot_show_banner=lambda msg, **k: log.append(("banner", msg)),
    )
    events = []
    def _event(name, **k):
        events.append((name, k))
        log.append(("event", name))
    monkeypatch.setattr(gui, "_pipe_event", _event)
    r = _r(text)
    if failed:
        r.failed_segments, r.total_segments = failed, 3
    gui.AppWindow._on_transcription_done(win, r)
    return log, events


def test_有段落失敗_提示寫出段數而且照樣貼上(monkeypatch):
    log, events = _done_win(monkeypatch, failed=1)
    toasts = [m for k, *m in log if k == "toast"]
    banners = [m for k, *m in log if k == "banner"]
    assert any("1 段" in t[0] for t in toasts), toasts
    assert banners and "1 段" in banners[0][0], "提示幾秒就消失，要留一條在狀態列"
    assert ("paste", "第一段。第三段。") in log
    assert log.index(("idle",)) < log.index(("banner", banners[0][0])), "回到閒置後才放，免得被蓋掉"
    assert ("partial_transcription", {"failed_segments": 1, "total_segments": 3}) in events
    assert log.index(("event", "partial_transcription")) < log.index(("paste", "第一段。第三段。")), \
        "要在貼上之前記：貼上完會清掉錄音編號，之後記的對不回是哪一次錄音"


def test_錄音小窗閃一下提醒(monkeypatch):
    """使用者多半在別的 App 打字，主視窗看不到；浮動小窗是唯一跨 App 看得到的地方。"""
    log, _ = _done_win(monkeypatch, failed=2, mini=True)
    assert ("hud_warn", "2 段沒辨識出來") in log
    assert log.index(("idle",)) < log.index(("hud_warn", "2 段沒辨識出來")), \
        "回到閒置會收起小窗，要在那之後才顯示"


def test_沒有失敗_錄音小窗不提醒(monkeypatch):
    log, _ = _done_win(monkeypatch, failed=0, mini=True)
    assert not [e for e in log if e[0] == "hud_warn"]


def test_沒有失敗_不跳任何警告(monkeypatch):
    log, events = _done_win(monkeypatch, failed=0)
    assert not [m for k, *m in log if k == "banner"]
    assert all("段" not in m[0] for k, *m in log if k == "toast")
    assert not [e for e in events if e[0] == "partial_transcription"]


def test_全部失敗_不貼上但狀態列說辨識失敗(monkeypatch):
    log, events = _done_win(monkeypatch, failed=3, text=FAIL)
    assert not [m for k, *m in log if k == "paste"]
    banners = [m[0] for k, *m in log if k == "banner"]
    assert banners and "失敗" in banners[0]


# ── (g) 提示講的是上一次：新的錄音開始就收掉 ──

def _start_recording(monkeypatch, *, slot):
    """跑真的 _transition_to_recording（麥克風開成功），看狀態列有沒有換回音量條。"""
    from unittest.mock import MagicMock
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    shown = []
    win.__dict__.update(
        _state="idle", _skeleton_mode=True, _status_slot_active=slot,
        _status_slot_show_meter=lambda: shown.append("meter"),
        recorder=MagicMock(**{"start.return_value": True, "_started_with_fallback": False}),
        cfg=types.SimpleNamespace(auto_paste=True, hotkey="right_cmd", apple_streaming=False,
                                  ollama_enabled=False, streaming_algo="fixed_chunk",
                                  format_hotkey_display=lambda: "右 ⌘"),
        _model_var=MagicMock(), _lang_var=MagicMock(), _ripples=[], _wave_engine=None,
        _target_label=MagicMock(), _timer_label=MagicMock(), _hotkey_hint=MagicMock(),
        _model_menu=MagicMock(), _lang_menu=MagicMock(), _set_status=MagicMock(),
        _update_timer=MagicMock(), _stream_generation=0, after=MagicMock(),
        _mini_window=None, _show_toast=MagicMock(),
    )
    monkeypatch.setattr(gui.threading, "Thread",
                        lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    for name in ("_ensure_mini_window", "_show_big_wave", "_stream_tick"):
        if hasattr(gui.AppWindow, name):
            setattr(win, name, MagicMock())
    gui.AppWindow._transition_to_recording(win)
    assert win._state == "recording"
    return shown


def test_新的錄音開始就收掉上一次的提示(monkeypatch):
    assert _start_recording(monkeypatch, slot="banner") == ["meter"]


def test_暖機進度條不被收掉(monkeypatch):
    assert _start_recording(monkeypatch, slot="progress") == []


def test_尾段當掉而且前面也沒文字_照舊算整次失敗(monkeypatch):
    """沒有東西好保住時，維持原本的「轉錄失敗」流程（提示、失敗原因都記得到）。"""
    win = _merge_win(monkeypatch, chunks=["", ""], failed=[0, 1],
                     tail=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("壞了")))
    assert win.delivered == [] and win.failed_calls


def test_剛好在截止時轉完的段落也算進去(monkeypatch):
    """先數「沒轉完」再拍快照：反過來的話，剛好在兩者之間轉完的段落兩邊都不算。"""
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)

    class _CompletesOnSnapshot(list):
        def __iter__(self):
            win._stream_completed = win._stream_dispatched   # 拍快照的那一刻剛好轉完
            return super().__iter__()

    win.__dict__.update(
        _la_buffer=None, _stream_chunks=_CompletesOnSnapshot(["第一段。", ""]), _stream_failed=[],
        _stream_dispatched=2, _stream_completed=1, STREAM_CHUNK_JOIN_TIMEOUT_S=0.05,
        _full_audio_s=30.0, cfg=types.SimpleNamespace(chinese_variant="off"),
        _apple_shadow_active=lambda: False,
        transcriber=types.SimpleNamespace(transcribe=lambda *a, **k: _r("第三段。")),
    )
    delivered = []
    win.after = lambda ms, fn, *a: delivered.extend(a)
    gui.AppWindow._run_transcription(win, [0.0] * 100, "apple-speech", "zh", None)
    assert delivered[0].failed_segments == 1


def test_新錄音開始時清掉上一次的失敗清單(monkeypatch):
    import gui
    # 借用 _start_recording 的假視窗，進去之前塞一個上一次的舊清單
    win_holder = {}
    real = gui.AppWindow._transition_to_recording

    def _spy(self):
        self._stream_failed = [7]
        win_holder["w"] = self
        return real(self)
    monkeypatch.setattr(gui.AppWindow, "_transition_to_recording", _spy)
    _start_recording(monkeypatch, slot="meter")
    assert win_holder["w"]._stream_failed == []


def test_結果卡收到失敗段數(monkeypatch):
    import gui
    got = {}

    class _FakeBlock:
        def __init__(self, *a, **k): pass
        def set_result(self, **k): got.update(k)
        def set_failed(self, msg): got["failed"] = msg

    monkeypatch.setattr(gui, "UtteranceBlockV2", _FakeBlock)
    win = gui.AppWindow.__new__(gui.AppWindow)
    win.__dict__.update(_pending_block=None, _blocks_container=None,
                        _on_block_copy=None, _on_block_save=None,
                        _clear_placeholder=lambda: None,
                        _stream_insert_latest_block=lambda block, epoch: None)
    r = _r("第一段。第三段。")
    r.failed_segments = 2
    gui.AppWindow._display_result_skeleton(win, r, dur=30.0, lang="zh", model="apple-speech")
    assert got.get("failed_segments") == 2


# ── (h) 錄音小窗的提醒：幾秒後自己收；期間開始新錄音不能把小窗收掉 ──

def _hud(monkeypatch):
    import gui
    hud = gui.MiniRecordingWindow.__new__(gui.MiniRecordingWindow)
    calls, timers = [], {}
    items = {}
    hud.__dict__.update(
        _closed=False, _wave_canvas=None, _ns_window=object(), _warn_hide_id=None,
        _canvas=types.SimpleNamespace(itemconfig=lambda item, **k: items.setdefault(item, {}).update(k)),
        _dot_id="dot", _label_id="label", _timer_id="timer",
        _position_at_cursor_screen_bottom=lambda: None,
        _reapply_panel_level=lambda: None,
    )
    monkeypatch.setattr(gui.MiniRecordingWindow, "deiconify", lambda self: calls.append("show"), raising=False)
    monkeypatch.setattr(gui.MiniRecordingWindow, "withdraw", lambda self: calls.append("hide"), raising=False)

    def _after(self, ms, fn):
        timers[len(timers)] = fn
        return len(timers) - 1
    monkeypatch.setattr(gui.MiniRecordingWindow, "after", _after, raising=False)
    monkeypatch.setattr(gui.MiniRecordingWindow, "after_cancel",
                        lambda self, i: timers.pop(i, None), raising=False)
    monkeypatch.setattr(gui.MiniRecordingWindow, "winfo_geometry", lambda self: "", raising=False)
    monkeypatch.setattr(gui.MiniRecordingWindow, "state", lambda self: "normal", raising=False)
    return gui, hud, calls, timers, items


def test_錄音小窗提醒幾秒後自己收(monkeypatch):
    gui, hud, calls, timers, items = _hud(monkeypatch)
    hud.show_warning("1 段沒辨識出來")
    assert calls == ["show"] and items["label"]["text"] == "1 段沒辨識出來"
    assert items["timer"]["text"] == "", "提醒時不顯示計時"
    for fn in list(timers.values()):
        fn()
    assert calls == ["show", "hide"]


def test_提醒期間開始新錄音_小窗不會被收掉(monkeypatch):
    gui, hud, calls, timers, items = _hud(monkeypatch)
    hud.show_warning("1 段沒辨識出來")
    hud.show_recording()
    for fn in list(timers.values()):
        fn()
    assert "hide" not in calls, "新錄音的小窗被上一則提醒的排程收掉了"
