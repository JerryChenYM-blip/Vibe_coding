"""v2.32.0 第一階段：快速修正的回歸測試。

2026-10-05 全面審查找到、這一版修掉的項目：
  (a) 重複數字被「去除重複」吃掉（000000→00）——正確性 bug
  (b) 紀錄裡蘋果的 backend 被寫成 mlx——統計會分錯組
  (c) 切到蘋果之後 Qwen3／Whisper 的權重沒放掉——佔 3.6～6.3 GB
  (d) 不潤飾時，先貼上、再畫結果卡與寫歷史——使用者少等一段

不啟動真正的 Tk 視窗：比照 tests/test_chunking.py，用 AppWindow.__new__ 繞過 __init__。
"""

import types

import pytest

import transcriber as tr
from transcriber import TranscriptionResult


# ── (a) 數字不去重 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("原文", [
    "驗證碼是000000",
    "電話0912121212",
    "金額888888元",
    "零九一二一二一二一二",      # Qwen3 會把數字寫成國字，一樣要保住
    "帳號 1212 1212 1212",
])
def test_重複的數字不能被砍(原文):
    assert tr._dedupe_repetitive_ngrams(原文) == 原文


@pytest.mark.parametrize("原文,期望", [
    ("全自動、自行、自行、自行", "全自動、自行"),   # 原本要清的「辨識卡住」照樣清
    ("測試 測試 測試 OK", "測試 OK"),
    ("我會、我會", "我會、我會"),                   # 只重複 2 次，本來就不動
])
def test_原本該清的重複照樣清(原文, 期望):
    assert tr._dedupe_repetitive_ngrams(原文) == 期望


# ── (b) 紀錄寫對引擎 ──────────────────────────────────────────────────────

def test_紀錄的引擎欄位依模型決定():
    assert tr._backend_for("apple-speech") == "apple-speech"
    assert tr._backend_for("qwen3-asr") == "qwen3-asr"
    assert tr._backend_for("qwen3-asr-large") == "qwen3-asr"
    assert tr._backend_for("large-v3-turbo") == tr.BACKEND


# ── (c) 切到蘋果時放掉本機模型 ────────────────────────────────────────────

def test_切到蘋果時放掉本機模型權重(monkeypatch):
    t = tr.Transcriber()
    t._qwen3_session = object()          # 假裝 Qwen3 還載在記憶體裡
    calls = []
    monkeypatch.setattr(tr.Transcriber, "unload", lambda self: calls.append("unload"))
    monkeypatch.setattr(tr.Transcriber, "_warmup_apple", lambda self, m, l=None: calls.append("warmup"))
    t.warmup("apple-speech")
    assert calls == ["unload", "warmup"], "要先放掉權重、再暖機蘋果"


def test_沒載任何模型時不做多餘的卸載(monkeypatch):
    t = tr.Transcriber()
    calls = []
    monkeypatch.setattr(tr.Transcriber, "unload", lambda self: calls.append("unload"))
    monkeypatch.setattr(tr.Transcriber, "_warmup_apple", lambda self, m, l=None: calls.append("warmup"))
    t.warmup("apple-speech")
    assert calls == ["warmup"]


def test_卸載要等正在跑的轉錄結束(monkeypatch):
    """卸載必須拿 _transcription_lock——推論時都拿著它，拿到 = 沒人在用權重。"""
    t = tr.Transcriber()
    t._qwen3_session = object()
    held = {}
    monkeypatch.setattr(tr.Transcriber, "unload",
                        lambda self: held.setdefault("locked", self._transcription_lock.locked()))
    monkeypatch.setattr(tr.Transcriber, "_warmup_apple", lambda self, m, l=None: None)
    t.warmup("apple-speech")
    assert held["locked"] is True


def test_切到其他模型不會誤卸(monkeypatch):
    t = tr.Transcriber()
    t._qwen3_session = object()
    calls = []
    monkeypatch.setattr(tr.Transcriber, "unload", lambda self: calls.append("unload"))
    monkeypatch.setattr(tr.Transcriber, "_warmup_qwen3", lambda self, m: calls.append("qwen3"))
    t.warmup("qwen3-asr")
    assert calls == ["qwen3"]


# ── (d) 先貼上、再畫結果卡與寫歷史 ────────────────────────────────────────

def _fake_window(monkeypatch, *, polish: bool):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    order = []
    win._state = "processing"
    win._pipeline_t0 = None
    win._polish_generation = 0
    win._paste_target = "Notes"
    win._skeleton_mode = False
    win._utterance_blocks = []
    win._frontmost_app = "Notes"
    win.cfg = types.SimpleNamespace(auto_copy=False, auto_paste=True,
                                    ollama_enabled=polish, polish_backend="local")
    win.ollama = types.SimpleNamespace(health_ok=True)
    win._model_var = types.SimpleNamespace(get=lambda: "apple-speech")
    win.history_store = types.SimpleNamespace(insert=lambda **k: order.append("history") or 1)
    win._transition_to_idle = lambda result=None: order.append("draw")
    win._show_toast = lambda msg: order.append("toast")
    win._do_auto_paste = lambda text, target: order.append("paste")
    win._apply_toggle_style = lambda: None
    win._emit_pipeline_timing = lambda **k: order.append("timing")
    win._start_polish = lambda gen, text, target: order.append("polish")
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    return gui, win, order


def test_不潤飾時先貼上再畫畫面與寫歷史(monkeypatch):
    gui, win, order = _fake_window(monkeypatch, polish=False)
    gui.AppWindow._on_transcription_done(win, TranscriptionResult(
        text="今天天氣很好", language="zh", duration_seconds=1.0, elapsed_seconds=0.3))
    assert order.index("paste") < order.index("draw") < order.index("history")
    assert order.index("toast") < order.index("paste"), \
        "「轉錄完成」要先跳，否則會蓋掉貼上失敗的提示"
    assert order.count("paste") == 1, "不能貼兩次"


def test_走潤飾時維持原本順序_不提早貼上(monkeypatch):
    gui, win, order = _fake_window(monkeypatch, polish=True)
    win.cfg.ollama_paste_strategy = "wait"
    gui.AppWindow._on_transcription_done(win, TranscriptionResult(
        text="今天天氣很好", language="zh", duration_seconds=1.0, elapsed_seconds=0.3))
    assert "paste" not in order, "等潤飾完才貼，這裡不該貼"
    assert order[-1] == "polish"


def test_系統訊息不會被貼上(monkeypatch):
    gui, win, order = _fake_window(monkeypatch, polish=False)
    gui.AppWindow._on_transcription_done(win, TranscriptionResult(
        text="（未偵測到語音內容）", language="", duration_seconds=1.0, elapsed_seconds=0.3))
    assert "paste" not in order
    assert "timing" in order, "沒貼上也要記這次的時間"


# ── (e) 尾音等待：放開前已經安靜很久就只多等一點點 ──────────────────────

def _tail_win(rms):
    import gui
    import numpy as np
    win = gui.AppWindow.__new__(gui.AppWindow)
    win.recorder = types.SimpleNamespace(get_tail=lambda n: np.full(n, rms, dtype=np.float32))
    return gui, win


def test_放開前一直很安靜_只多等一點點():
    gui, win = _tail_win(0.0003)
    assert gui.AppWindow._tail_padding_ms(win) == gui.AppWindow.TAIL_PADDING_SHORT_MS


def test_放開前還有聲音_照原本等滿():
    gui, win = _tail_win(0.02)
    assert gui.AppWindow._tail_padding_ms(win) == gui.AppWindow.TAIL_PADDING_MS


def test_縮短後仍留一點緩衝_不會降到零():
    """聲音從麥克風到 App 有延遲，放開那一刻最後的聲音可能還在半路上。"""
    import gui
    assert gui.AppWindow.TAIL_PADDING_SHORT_MS > 0


def test_讀不到錄音時照原本等滿():
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    win.recorder = types.SimpleNamespace(get_tail=lambda n: (_ for _ in ()).throw(RuntimeError("壞了")))
    assert gui.AppWindow._tail_padding_ms(win) == gui.AppWindow.TAIL_PADDING_MS


def test_錄音太短_不足半秒時照原本等滿():
    import gui
    import numpy as np
    win = gui.AppWindow.__new__(gui.AppWindow)
    win.recorder = types.SimpleNamespace(get_tail=lambda n: np.zeros(3_000, dtype=np.float32))
    assert gui.AppWindow._tail_padding_ms(win) == gui.AppWindow.TAIL_PADDING_MS


# ── (f) 錄音編號要帶進背景執行緒 ──────────────────────────────────────────

def test_背景執行緒拿得到錄音編號():
    """pipeline_id 存在 thread-local：沒帶過去的話，背景執行緒讀到的永遠是空的
    （9/21 之後 641 筆轉錄紀錄 0 筆有編號，就是這個原因）。"""
    import threading
    import gui
    import pipeline_id
    got = {}

    def worker():
        got["pid"] = pipeline_id.get_current()

    pipeline_id.set_current("rec-123")
    try:
        t = threading.Thread(target=worker); t.start(); t.join()
        assert got["pid"] is None, "沒包裝時拿不到——這就是原本的缺口（測試的對照組）"
        t = threading.Thread(target=gui._with_pid("rec-123", worker)); t.start(); t.join()
        assert got["pid"] == "rec-123"
    finally:
        pipeline_id.set_current(None)


# ── (g) 紀錄補洞：版本號、資源用量 ────────────────────────────────────────

def test_每筆紀錄都帶版本號(tmp_path, monkeypatch):
    """之前只能靠日期猜是哪一版錄的，改版前後的數字比不出來。"""
    import json
    import audit_log
    from _version import __version__
    written = []
    monkeypatch.setattr(audit_log, "_append_jsonl", lambda rec: written.append(rec))
    audit_log.write_event("paste_done", "p1", ok=True)
    audit_log.write_transcribe({"model": "apple-speech"})
    assert [r["app_version"] for r in written] == [__version__, __version__]
    json.dumps(written)   # 要能寫成一行 JSON


def test_資源快照有記憶體和CPU():
    import gui
    snap = gui.AppWindow._resource_snapshot()
    assert snap["rss_mb"] > 0
    assert snap["cpu_s"] >= 0
    assert "children_rss_mb" in snap


def test_資源快照讀不到時不拖垮健康紀錄(monkeypatch):
    import sys
    import gui
    monkeypatch.setitem(sys.modules, "psutil", None)   # import psutil 會失敗
    assert gui.AppWindow._resource_snapshot() == {}


# ── (h) 對照工具 ──────────────────────────────────────────────────────────

def test_對照工具只配對邊錄邊辨識且對照成功的(tmp_path):
    import json
    import compare_apple_engines as cmp
    recs = [
        {"type": "transcribe", "pipeline_id": "a", "model": "apple-speech",
         "apple": {"streamed": True}, "raw_text": "我在用 Claude Code 寫程式"},
        {"type": "apple_shadow", "pipeline_id": "a", "ok": True, "text": "我在用寫程式"},
        {"type": "transcribe", "pipeline_id": "b", "model": "apple-speech",
         "apple": {"streamed": True}, "raw_text": "今天開會"},
        {"type": "apple_shadow", "pipeline_id": "b", "ok": True, "text": "今天開會"},
        # 以下都不該配進來
        {"type": "transcribe", "pipeline_id": "c", "model": "apple-speech",
         "apple": {"streamed": False}, "raw_text": "退回舊做法的"},
        {"type": "apple_shadow", "pipeline_id": "c", "ok": True, "text": "退回舊做法的"},
        {"type": "transcribe", "pipeline_id": "d", "model": "apple-speech",
         "apple": {"streamed": True}, "raw_text": "對照失敗的"},
        {"type": "apple_shadow", "pipeline_id": "d", "ok": False, "text": ""},
        {"type": "transcribe", "pipeline_id": "e", "model": "apple-speech",
         "apple": {"streamed": True}, "raw_text": "（未偵測到語音內容）"},
        {"type": "apple_shadow", "pipeline_id": "e", "ok": True, "text": ""},
    ]
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n壞掉的一行\n",
                   encoding="utf-8")
    pairs = cmp.load_pairs(log)
    assert sorted(p["pid"] for p in pairs) == ["a", "b"]
    s = cmp.summarize(pairs, ["Claude"])
    assert s["with_english"] == 1 and s["english_dropped"] == 1
    assert s["chinese_only"] == 1 and s["chinese_identical"] == 1
    assert s["term_hits"] == {"speech": 1, "dictation": 0}


# ── (i) 審查後修正：數字保護不能保到辨識卡住的迴圈 ──────────────────────────

@pytest.mark.parametrize("原文,不該留下", [
    ("我們的專案進度 " + "1 " * 18, "1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1"),   # 18 個數字的迴圈
    ("結果是點點點點點點", "點點點點"),                                       # 只有小數點，不是號碼
])
def test_數字迴圈照樣清(原文, 不該留下):
    assert 不該留下 not in tr._dedupe_repetitive_ngrams(原文)


def test_信用卡號16碼照樣保留():
    assert tr._dedupe_repetitive_ngrams("卡號1234123412341234") == "卡號1234123412341234"


# ── (j) 審查後修正：貼上出錯也要回到閒置 ──────────────────────────────────

def test_貼上出錯也要畫結果卡和寫歷史(monkeypatch):
    gui, win, order = _fake_window(monkeypatch, polish=False)
    def boom(text, target):
        order.append("paste")
        raise RuntimeError("貼上壞了")
    win._do_auto_paste = boom
    gui.AppWindow._on_transcription_done(win, TranscriptionResult(
        text="今天天氣很好", language="zh", duration_seconds=1.0, elapsed_seconds=0.3))
    assert "draw" in order and "history" in order, "不然會卡在「處理中」60 秒"


# ── (k) 審查後修正：對照組在貼上之後才跑 ──────────────────────────────────

class _SyncThread:
    """把背景執行緒換成「start() 當下就跑完」，才排得出先後順序。"""
    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args
    def start(self):
        self._target(*self._args)


def _result_with_job(text):
    r = TranscriptionResult(text=text, language="zh", duration_seconds=1.0, elapsed_seconds=0.3)
    r.shadow_job = ("audio", "zh", "p1")
    return r


def test_對照組在貼上之後才跑(monkeypatch):
    gui, win, order = _fake_window(monkeypatch, polish=False)
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread)
    win.transcriber = types.SimpleNamespace(run_apple_shadow=lambda *a: order.append("shadow"))
    r = _result_with_job("今天天氣很好")
    gui.AppWindow._on_transcription_done(win, r)
    assert order.index("paste") < order.index("shadow")
    assert r.shadow_job is None, "結果會留在畫面與歷史裡，錄音不能跟著留在記憶體"


def test_被擋下的結果不跑對照組(monkeypatch):
    gui, win, order = _fake_window(monkeypatch, polish=False)
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread)
    win.transcriber = types.SimpleNamespace(run_apple_shadow=lambda *a: order.append("shadow"))
    r = _result_with_job("（未偵測到語音內容）")
    gui.AppWindow._on_transcription_done(win, r)
    assert "shadow" not in order
    assert r.shadow_job is None, "沒跑也要放掉，錄音資料不能一直留在記憶體"


def _run_win(monkeypatch, *, streamed, shadow_on=True):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    win._la_buffer = None
    win._stream_dispatched = 0
    win._stream_chunks = []
    win._stream_failed = []
    win._full_audio_s = 1.0
    win.cfg = types.SimpleNamespace(chinese_variant="off")
    win._apple_shadow_active = lambda: shadow_on
    r = TranscriptionResult(text="今天天氣很好", language="zh", duration_seconds=1.0, elapsed_seconds=0.2)
    r.apple_meta = {"streamed": streamed}
    win.transcriber = types.SimpleNamespace(transcribe=lambda *a, **k: r)
    win.delivered = []
    win.after = lambda ms, fn, *a: win.delivered.extend(a)
    win.finish_timeouts = []
    session = types.SimpleNamespace(
        finish=lambda timeout: win.finish_timeouts.append(timeout) or {"ok": True}, failure=None)
    return gui, win, session


def _job(win):
    return getattr(win.delivered[0], "shadow_job", None)


def test_串流成功才排對照組(monkeypatch):
    gui, win, session = _run_win(monkeypatch, streamed=True)
    gui.AppWindow._run_transcription(win, "整段錄音", "apple-speech", "zh", session)
    assert _job(win) is not None and _job(win)[0] == "整段錄音"


def test_退回舊做法時不排對照組(monkeypatch):
    """退回舊做法時 audio 可能只是最後一段，跟整段比沒有意義；比對工具也只看串流的。"""
    gui, win, session = _run_win(monkeypatch, streamed=False)
    gui.AppWindow._run_transcription(win, "整段錄音", "apple-speech", "zh", session)
    assert _job(win) is None


def test_對照組過期就不排(monkeypatch):
    gui, win, session = _run_win(monkeypatch, streamed=True, shadow_on=False)
    gui.AppWindow._run_transcription(win, "整段錄音", "apple-speech", "zh", session)
    assert _job(win) is None


# ── (l) 審查後修正：從歷史重新潤飾不掛最新一次錄音的編號 ──────────────────

@pytest.mark.parametrize("from_history,期望", [(False, "rec-9"), (True, None)])
def test_重新潤飾的錄音編號(monkeypatch, from_history, 期望):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    win._recording_pid = "rec-9"
    win._frontmost_app = "Notes"
    win._utterance_blocks = []
    win._skeleton_mode = False
    win._stream_polish_dispatched = 0
    win._dictionary_terms = []
    win._rebuild_result_title = lambda: None
    win.cfg = types.SimpleNamespace(preset_routing_enabled=False, dictionary_enabled=False,
                                    polish_backend="local")
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(gui, "_with_pid", lambda pid, fn: seen.setdefault("pid", pid) or fn)
    monkeypatch.setattr(gui.threading, "Thread", lambda target=None, daemon=None: types.SimpleNamespace(start=lambda: None))
    gui.AppWindow._start_polish(win, 1, "今天天氣很好", None, from_history=from_history)
    assert seen["pid"] == 期望


# ── (m) 審查後修正：暖機不自動下載聽寫引擎；暫存錄音會被清掉 ──────────────

def test_暖機不下載聽寫引擎(monkeypatch):
    calls = []
    monkeypatch.setattr(tr, "_get_apple_stream_engine",
                        lambda: types.SimpleNamespace(channel=lambda: None))
    monkeypatch.setattr(tr, "_sweep_stale_apple_tmp", lambda: 0)
    monkeypatch.setattr(tr.Transcriber, "_run_apple_helper", lambda self, a, timeout: calls.append(a) or {})
    tr.Transcriber()._prepare_apple_extras({"dictation": {"asset_status": "supported"}}, "zh-TW")
    assert calls == []


def test_對照組遇到沒登記才登記_而且只登記一次(monkeypatch):
    import numpy as np
    calls, events = [], []
    monkeypatch.setattr(tr.Transcriber, "_dictation_register_tried", False)
    monkeypatch.setattr(tr.Transcriber, "_run_apple_oneshot",
                        lambda self, *a: {"ok": False, "error": "asset_not_installed"})
    monkeypatch.setattr(tr.Transcriber, "_run_apple_helper",
                        lambda self, a, timeout: calls.append(a) or {"ok": True})
    monkeypatch.setattr(tr, "_audit_log", types.SimpleNamespace(
        write_event=lambda *a, **k: events.append(k)))
    t = tr.Transcriber()
    for _ in range(2):
        t.run_apple_shadow(np.zeros(16000, dtype=np.float32), None, "p1")
    assert len(calls) == 1 and "--allow-download" in calls[0]
    assert [e["ok"] for e in events] == [False, False]


def test_清掉一小時前留下的暫存錄音(tmp_path, monkeypatch):
    import os
    import tempfile
    import time as _t
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    old = tmp_path / "whisperpro_apple_abc"; old.mkdir(); (old / "audio.wav").write_bytes(b"x")
    new = tmp_path / "whisperpro_apple_new"; new.mkdir()
    other = tmp_path / "別人的暫存"; other.mkdir()
    two_hours_ago = _t.time() - 7200
    for d in (old, other):
        os.utime(d, (two_hours_ago, two_hours_ago))
    assert tr._sweep_stale_apple_tmp() == 1
    assert not old.exists(), "App 中途被關掉留下的錄音要清掉"
    assert new.exists(), "正在用的不能刪"
    assert other.exists(), "不是自己的不能碰"


# ── (n) 審查後修正：比對工具跟 App 用同一支字典讀法 ──────────────────────

def test_比對工具略過空白的字典詞(tmp_path, monkeypatch, capsys):
    import json
    import sys
    import compare_apple_engines as cmp
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"type": "transcribe", "pipeline_id": "a", "model": "apple-speech",
         "apple": {"streamed": True}, "raw_text": "用 Claude 寫"},
        {"type": "apple_shadow", "pipeline_id": "a", "ok": True, "text": "用寫"},
    ]), encoding="utf-8")
    d = tmp_path / "dict.json"
    d.write_text(json.dumps({"terms": [{"term": ""}, {"term": "Claude"}]}), encoding="utf-8")
    monkeypatch.setattr(cmp, "DICT", d)
    monkeypatch.setattr(sys, "argv", ["x", "--log", str(log)])
    cmp.main()
    assert "一般 1／聽寫 0" in capsys.readouterr().out, "空字串會讓命中次數暴增"


def test_收尾等待上限_長錄音不會等到好幾分鐘(monkeypatch):
    """「收完聲音卻不回結果」時要等滿上限才退回。原本 34 分鐘的錄音要白等 204 秒（第二輪審查），
    現在是 0.03 × 秒數 = 61 秒（「卡住」另由 finish() 自己 3 秒內發現，不用等到這裡）。"""
    import numpy as np
    gui, win, session = _run_win(monkeypatch, streamed=True)
    gui.AppWindow._run_transcription(win, np.zeros(16_000 * 60 * 34, dtype=np.float32),
                                     "apple-speech", "zh", session)
    assert win.finish_timeouts[0] <= 34 * 60 * 0.03 + 0.1
    gui.AppWindow._run_transcription(win, np.zeros(16_000 * 3, dtype=np.float32),
                                     "apple-speech", "zh", session)
    assert win.finish_timeouts[1] == 8.0, "短錄音維持 8 秒的寬裕"


# ── (o) 第二輪審查後修正 ──────────────────────────────────────────────────

@pytest.mark.parametrize("原文", [
    "匯款帳號 888-888-888-888",     # 用「-」分組的帳號
    "卡號1234-1234-1234-1234",
    "零九兩兩兩兩兩兩兩兩",          # 「兩」也是數字
])
def test_分組或用兩唸的號碼也保留(原文):
    assert tr._dedupe_repetitive_ngrams(原文) == 原文


def test_超過16個數字就當成迴圈():
    """邊界：16 個保留（上面的信用卡號測試），17 個就清。"""
    assert tr._dedupe_repetitive_ngrams("數字" + "1 " * 17) != "數字" + "1 " * 17


def test_提示出錯也照樣貼上(monkeypatch):
    gui, win, order = _fake_window(monkeypatch, polish=False)
    win._show_toast = lambda msg: (_ for _ in ()).throw(RuntimeError("提示壞了"))
    gui.AppWindow._on_transcription_done(win, TranscriptionResult(
        text="今天天氣很好", language="zh", duration_seconds=1.0, elapsed_seconds=0.3))
    assert "paste" in order


def test_貼上前先收起錄音小窗(monkeypatch):
    """維持舊順序：小窗原本在回到閒置（貼上之前）收起。"""
    gui, win, order = _fake_window(monkeypatch, polish=False)
    win._mini_window = types.SimpleNamespace(hide=lambda: order.append("hide"))
    gui.AppWindow._on_transcription_done(win, TranscriptionResult(
        text="今天天氣很好", language="zh", duration_seconds=1.0, elapsed_seconds=0.3))
    assert order.index("hide") < order.index("paste")


def test_Windows_尾音照原本等滿(monkeypatch):
    gui, win = _tail_win(0.0003)
    monkeypatch.setattr(gui, "IS_MAC", False)
    assert gui.AppWindow._tail_padding_ms(win) == gui.AppWindow.TAIL_PADDING_MS


def test_關掉串流就不先開常駐程式(monkeypatch):
    spawned, swept = [], []
    monkeypatch.setattr(tr, "_get_apple_stream_engine",
                        lambda: types.SimpleNamespace(channel=lambda: spawned.append(1)))
    monkeypatch.setattr(tr, "_sweep_stale_apple_tmp", lambda: swept.append(1))
    t = tr.Transcriber()
    t.apple_prespawn = False
    t._prepare_apple_extras({}, "zh-TW")
    assert spawned == [] and swept == [1], "暫存錄音照樣要清"
    t.apple_prespawn = True
    t._prepare_apple_extras({}, "zh-TW")
    assert spawned == [1]


@pytest.mark.parametrize("payload", [
    {"ok": True, "parts": ["好"], "elapsed_ms": 1.0},
    {"ok": False, "error": "finalize_failed"},
])
def test_對照組其他結果不觸發登記(monkeypatch, payload):
    import numpy as np
    calls = []
    monkeypatch.setattr(tr.Transcriber, "_dictation_register_tried", False)
    monkeypatch.setattr(tr.Transcriber, "_run_apple_oneshot", lambda self, *a: payload)
    monkeypatch.setattr(tr.Transcriber, "_run_apple_helper",
                        lambda self, a, timeout: calls.append(a) or {"ok": True})
    monkeypatch.setattr(tr, "_audit_log", types.SimpleNamespace(write_event=lambda *a, **k: None))
    tr.Transcriber().run_apple_shadow(np.zeros(16000, dtype=np.float32), None, "p1")
    assert calls == []


def test_串流結果差一點點在容許範圍內就照用():
    assert tr._stream_payload_complete({"ok": True, "audio_seconds": 1.9, "bad_chunks": 0}, 32_000)


def test_從歷史重新潤飾時不掛錄音編號(monkeypatch):
    """實際的呼叫端要帶 from_history=True（只測 _start_polish 的話，呼叫端漏帶也不會紅燈）。"""
    import gui

    class _FakeBlock:
        def __init__(self, *a, **k): pass
        def set_result(self, **k): pass

    monkeypatch.setattr(gui, "UtteranceBlockV2", _FakeBlock)
    win = gui.AppWindow.__new__(gui.AppWindow)
    win.cfg = types.SimpleNamespace(ollama_enabled=True)
    win.ollama = types.SimpleNamespace(health_ok=True)
    win._polish_busy = False
    win._polish_generation = 0
    win._skeleton_mode = True
    win._blocks_container = None
    win._on_block_copy = win._on_block_save = None
    win._clear_placeholder = lambda: None
    win._stream_insert_latest_block = lambda block, epoch=None: None
    win._apply_toggle_style = lambda: None
    seen = {}
    win._start_polish = lambda gen, text, target=None, from_history=False: seen.setdefault("fh", from_history)
    entry = types.SimpleNamespace(id=1, raw_text="舊的", target_app="Notes", timestamp=0.0,
                                  duration_s=1.0, model_whisper="m", language="zh")
    gui.AppWindow._repolish_from_history(win, entry)
    assert seen["fh"] is True


# ── (p) 第三輪審查後修正 ──────────────────────────────────────────────────

def _processing_win(monkeypatch, *, stream_samples, total):
    """跑真的 _transition_to_processing，攔下交給辨識執行緒的聲音。"""
    import gui
    import numpy as np
    win = gui.AppWindow.__new__(gui.AppWindow)
    full = np.arange(total, dtype=np.float32)
    ui = types.SimpleNamespace(configure=lambda **k: None)
    win.__dict__.update(
        _state="recording", _rec_start=0.0, _wave_engine=None, _skeleton_mode=False,
        _stream_tick_id=None, _processing_timeout_id=None, _la_buffer=None,
        _apple_session=None, _tail_release_samples=None, _tail_padding_used_ms=None,
        _stream_samples=stream_samples, _mini_window=None, _recording_pid="p1",
        _timer_label=ui, _hotkey_hint=ui, _target_label=ui, _status_dot=ui, _status_label=ui,
        recorder=types.SimpleNamespace(stop=lambda: full, set_block_listener=lambda fn: None),
        _model_var=types.SimpleNamespace(get=lambda: "large-v3-turbo"),
        cfg=types.SimpleNamespace(get_whisper_language=lambda: "zh"),
        after=lambda ms, fn, *a: "id",
    )
    captured = {}

    class _CaptureThread:
        def __init__(self, target=None, args=(), daemon=None):
            captured["audio"] = args[0]
        def start(self):
            pass

    monkeypatch.setattr(gui.threading, "Thread", _CaptureThread)
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    gui.AppWindow._transition_to_processing(win)
    return captured["audio"]


def test_切過段時尾段很短也只轉尾段(monkeypatch):
    """放開時剛好切完一段：原本會把整段錄音再轉一次，接在後面變成全文重複兩遍。"""
    audio = _processing_win(monkeypatch, stream_samples=160_000, total=160_400)
    assert len(audio) == 400


def test_沒切過段時照樣轉整段(monkeypatch):
    audio = _processing_win(monkeypatch, stream_samples=0, total=600)
    assert len(audio) == 600


def test_尾段回系統訊息就不接進全文(monkeypatch):
    """接上去之後整串不再是「（…）」，認不出是系統訊息，會被當成逐字稿貼出去。"""
    gui, win, session = _run_win(monkeypatch, streamed=False)
    win._stream_chunks = ["第一段。", "第二段。"]
    win._stream_dispatched = win._stream_completed = 2
    fail = TranscriptionResult(text="（蘋果語音辨識失敗、請重試或於設定切回其他模型）",
                               language="zh", duration_seconds=0.2, elapsed_seconds=0.1)
    win.transcriber = types.SimpleNamespace(transcribe=lambda *a, **k: fail)
    gui.AppWindow._run_transcription(win, "尾段", "apple-speech", "zh", None)
    text = win.delivered[0].text
    assert "第一段" in text and "第二段" in text
    assert "（" not in text


def test_暖機前依設定決定要不要先開常駐程式(monkeypatch):
    import gui
    seen = []
    win = gui.AppWindow.__new__(gui.AppWindow)
    win._model_var = types.SimpleNamespace(get=lambda: "apple-speech")
    win._skeleton_mode = False
    win.after = lambda ms, fn, *a: None
    win.transcriber = types.SimpleNamespace(
        apple_prespawn=True, active_backend=lambda: "apple-speech",
        warmup=lambda model, language=None: seen.append(win.transcriber.apple_prespawn))
    win.cfg = types.SimpleNamespace(apple_streaming=True, ollama_enabled=True,
                                    get_whisper_language=lambda: "zh")
    monkeypatch.setattr(gui.threading, "Thread", _SyncThread)
    gui.AppWindow._warmup_model(win)
    assert seen == [False], "開了潤飾就不用串流，暖機時也不該先開常駐程式"
