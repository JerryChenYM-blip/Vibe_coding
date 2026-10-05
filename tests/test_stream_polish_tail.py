"""開 AI 潤飾時，長錄音最後一段開頭掉字（v2.32.2）。

背景（2026-10-05 v2.32.1 審查發現，既有問題）：長錄音邊錄邊轉、每段轉完就先潤飾；放開後只潤飾
「最後一段」。最後一段是用「全文長度 − 各段原文長度」切出來的，但全文合併時會拿掉接縫上的假句號
（v2.21.3 stitch_streaming_seams），全文比各段加起來短，切的位置往後偏——最後一段開頭的字被吃掉。
例：各段「今天天氣很好。」「我們去公園散步。」「走了很久。」＋尾段「然後回家吃飯。」→ 只潤飾「家吃飯。」。

另一個同源問題：從「歷史紀錄」重新潤飾舊的一筆時，上一次錄音的「已潤飾段落」還留著，
會被當成這一筆的前半段接上去——貼出別次錄音的內容。

要的行為：
  (a) 最後一段用合併時真正接上去的那段原文，不靠長度推算
  (b) 從歷史重新潤飾一律整篇潤飾，不碰上一次錄音的段落
  (c) 沒切過段（短錄音）照舊整篇潤飾
"""

import types

import pytest

from transcriber import TranscriptionResult


CHUNKS = ["今天天氣很好。", "我們去公園散步。", "走了很久。"]
TAIL = "然後回家吃飯。"


class _SyncThread:
    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args
    def start(self):
        self._target(*self._args)


def _polish_win(monkeypatch):
    """假視窗：已經有 3 段邊錄邊潤飾好的結果（「潤1」「潤2」「潤3」）。"""
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    sent = []

    def process(text, **k):
        sent.append(text)
        return types.SimpleNamespace(text=f"<{text}>", error=None)

    win.__dict__.update(
        _recording_pid="p1", _frontmost_app="Notes", _utterance_blocks=[], _skeleton_mode=False,
        _dictionary_terms=[], _rebuild_result_title=lambda: None,
        cfg=types.SimpleNamespace(preset_routing_enabled=False, dictionary_enabled=False,
                                  polish_backend="local", ollama_model="m"),
        _stream_generation=1, _stream_chunks=list(CHUNKS), _stream_polished=["潤1", "潤2", "潤3"],
        _stream_polish_dispatched=3, _stream_polish_completed=3,
        STREAM_CHUNK_JOIN_TIMEOUT_S=0.1,
        polish=types.SimpleNamespace(process=process),
    )
    done = []
    win.after = lambda ms, fn, *a: done.append(a)
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread)
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    return gui, win, sent, done


def _merged_text():
    from transcriber import stitch_streaming_seams
    return stitch_streaming_seams(CHUNKS, TAIL)


def test_合併後的全文確實比各段加起來短():
    """前提（對照組）：不成立的話這組測試就測不到原本的錯。"""
    assert len(_merged_text()) < len("".join(CHUNKS)) + len(TAIL)


def test_最後一段一個字都不能少(monkeypatch):
    gui, win, sent, done = _polish_win(monkeypatch)
    gui.AppWindow._start_polish(win, 1, _merged_text(), None, stream_tail=TAIL)
    assert sent == [TAIL], f"送去潤飾的最後一段不對：{sent}"
    resp = done[0][3]
    assert resp.text == "潤1潤2潤3" + f"<{TAIL}>"


def test_最後一段沒有字就不送潤飾(monkeypatch):
    gui, win, sent, done = _polish_win(monkeypatch)
    gui.AppWindow._start_polish(win, 1, "".join(CHUNKS), None, stream_tail="")
    assert sent == []
    assert done[0][3].text == "潤1潤2潤3"


def test_從歷史重新潤飾_不能接上一次錄音的段落(monkeypatch):
    gui, win, sent, done = _polish_win(monkeypatch)
    gui.AppWindow._start_polish(win, 1, "很久以前的一筆紀錄。", None, from_history=True)
    assert sent == ["很久以前的一筆紀錄。"]
    assert "潤1" not in done[0][3].text


def test_沒有給最後一段就整篇潤飾(monkeypatch):
    """不知道合併時接了哪段尾巴（例如舊的呼叫端），就不要猜，整篇潤飾最安全。"""
    gui, win, sent, done = _polish_win(monkeypatch)
    gui.AppWindow._start_polish(win, 1, _merged_text(), None)
    assert sent == [_merged_text()]


# ── 合併端：把真正接上去的尾段交出來 ──

def _merge(monkeypatch, tail_text):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    win.__dict__.update(
        _la_buffer=None, _stream_chunks=list(CHUNKS), _stream_failed=[],
        _stream_dispatched=3, _stream_completed=3, STREAM_CHUNK_JOIN_TIMEOUT_S=0.1,
        _full_audio_s=40.0, cfg=types.SimpleNamespace(chinese_variant="off"),
        _apple_shadow_active=lambda: False,
        transcriber=types.SimpleNamespace(transcribe=lambda *a, **k: TranscriptionResult(
            text=tail_text, language="zh", duration_seconds=5.0, elapsed_seconds=0.2)),
    )
    delivered = []
    win.after = lambda ms, fn, *a: delivered.extend(a)
    gui.AppWindow._run_transcription(win, [0.0] * 100, "qwen3-asr", "zh", None)
    return delivered[0]


def test_合併時記下真正接上去的尾段(monkeypatch):
    assert _merge(monkeypatch, TAIL).stream_tail == TAIL


def test_尾段是系統訊息時_記成空的(monkeypatch):
    assert _merge(monkeypatch, "（未偵測到語音內容）").stream_tail == ""


def test_轉錄完成時把尾段交給潤飾(monkeypatch):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    got = {}
    win.__dict__.update(
        _state="processing", _pipeline_t0=None, _polish_generation=0, _paste_target=None,
        _skeleton_mode=False, _utterance_blocks=[], _frontmost_app="Notes",
        cfg=types.SimpleNamespace(auto_copy=False, auto_paste=False, ollama_enabled=True,
                                  polish_backend="local", ollama_paste_strategy="wait"),
        ollama=types.SimpleNamespace(health_ok=True),
        _model_var=types.SimpleNamespace(get=lambda: "qwen3-asr"),
        history_store=None,
        _transition_to_idle=lambda result=None: None,
        _show_toast=lambda *a, **k: None, _apply_toggle_style=lambda: None,
        _start_polish=lambda gen, text, target, **k: got.update(k),
    )
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    r = TranscriptionResult(text=_merged_text(), language="zh", duration_seconds=40.0,
                            elapsed_seconds=0.3)
    r.stream_tail = TAIL
    gui.AppWindow._on_transcription_done(win, r)
    assert got.get("stream_tail") == TAIL


# ── 同一段程式的同類問題（審查發現，既有）：潤飾版整段不見 ──

def test_潤飾服務沒設定好_每一段都還在(monkeypatch):
    """後端設成 Vertex 但沒填專案時 polish 是空的：各段都沒潤飾，原本只剩最後一句。"""
    gui, win, sent, done = _polish_win(monkeypatch)
    win._stream_polished = ["", "", ""]
    win.polish = None
    gui.AppWindow._start_polish(win, 1, _merged_text(), None, stream_tail=TAIL)
    assert done[0][3].text == "".join(CHUNKS) + TAIL


def test_等太久還沒潤飾完的段落_用原文補上(monkeypatch):
    gui, win, sent, done = _polish_win(monkeypatch)
    win._stream_polished = ["潤1", "", "潤3"]
    win._stream_polish_completed = 2            # 第二段還在潤飾，等到截止也沒回來
    gui.AppWindow._start_polish(win, 1, _merged_text(), None, stream_tail=TAIL)
    assert done[0][3].text == "潤1" + CHUNKS[1] + "潤3" + f"<{TAIL}>"


def test_潤飾中途開始新錄音_這次的段落不能被清掉(monkeypatch):
    """新的錄音會把各段欄位換成空的；這次的潤飾要用開始潤飾那一刻抓住的資料。"""
    import gui
    gui_mod, win, sent, done = _polish_win(monkeypatch)
    win._stream_polish_completed = 2            # 還在等最後一段的潤飾
    runs = []

    class _LaterThread:
        def __init__(self, target=None, args=(), daemon=None):
            runs.append(target)
        def start(self):
            pass

    monkeypatch.setattr(gui.threading, "Thread", _LaterThread)
    gui.AppWindow._start_polish(win, 1, _merged_text(), None, stream_tail=TAIL)
    # 背景執行緒還沒跑，使用者就開始新的錄音（_transition_to_recording 的重設）
    win._stream_generation = getattr(win, "_stream_generation", 0) + 1
    win._stream_polished, win._stream_chunks = [], []
    win._stream_polish_dispatched = win._stream_polish_completed = 0
    win.STREAM_CHUNK_JOIN_TIMEOUT_S = 5.0
    import time
    t0 = time.monotonic()
    runs[0]()
    assert time.monotonic() - t0 < 1, "新錄音開始後，舊段落不會再寫回來，不用等到截止"
    text = done[0][3].text
    assert text.startswith("潤1潤2") and text.endswith(f"<{TAIL}>"), text
