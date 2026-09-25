"""蘋果原生語音辨識後端 unit tests（v2.31.0）。

這個後端跟其他三個最大的不同：模型不在 process 裡，實際辨識由
native/apple_stt/ 編出來的 Swift helper 做，Python 只負責寫暫存 wav、
呼叫它、解一行 JSON。

因此測試的重點不是「辨識準不準」（那要真人錄音，見 T02），而是
「Python 這一側的膠水有沒有寫對」：

  (a) 純函式：model 判別、locale 對應
  (b) 成功路徑：helper 回傳的 text 有被正確取出
  (c) **失敗路徑真的會失敗**——每一種 error code 都要對到看得懂的訊息，
      而且絕對不能被誤判成成功（CLAUDE.md §9「閘要能判 FAIL」）
  (d) 暫存檔一定要刪：那是使用者的真實語音，不能留在 /tmp
  (e) 錯誤訊息用「（」開頭——transcribe() 後段的字典校正與幻覺過濾
      都靠這個前綴跳過系統訊息，前綴掉了會讓錯誤訊息被拿去改字

helper 一律用 monkeypatch 假造，所以這些測試在沒有 macOS 26、
甚至在 Windows 上也跑得動。
"""

import os

import numpy as np
import pytest

import transcriber as tr
from transcriber import (
    AppleSTTError,
    Transcriber,
    _apple_locale_for,
    _is_apple_model,
    is_system_message,
)


SR = 16_000


def _audio(seconds: float = 1.0) -> np.ndarray:
    """一段可辨識長度的假音訊（內容無所謂，helper 被假造掉了）。"""
    return np.zeros(int(SR * seconds), dtype=np.float32)


# ── (a) 純函式 ────────────────────────────────────────────────────────────

def test_is_apple_model_只認蘋果那一個():
    assert _is_apple_model("apple-speech") is True
    assert _is_apple_model("APPLE-SPEECH") is True      # 大小寫不敏感
    assert _is_apple_model("qwen3-asr") is False
    assert _is_apple_model("large-v3-turbo") is False
    assert _is_apple_model("") is False


@pytest.mark.parametrize("language,expected", [
    (None,      "zh-TW"),     # 自動偵測 → 繁中（新引擎沒有自動偵測選項）
    ("zh",      "zh-TW"),
    ("zh-TW",   "zh-TW"),
    ("en",      "en-US"),
    ("ja",      "ja-JP"),
    ("ko",      "ko-KR"),
    ("沒看過的", "zh-TW"),     # 未知語言 fallback，不可以拋例外
])
def test_locale_對應(language, expected):
    assert _apple_locale_for(language) == expected


# ── (b) 成功路徑 ──────────────────────────────────────────────────────────

def test_成功時取出文字與語言(monkeypatch):
    captured = {}

    def fake_helper(self, args, timeout):
        captured["args"] = args
        captured["timeout"] = timeout
        return {"ok": True, "text": "  今天天氣很好  ", "locale": "zh_TW"}

    monkeypatch.setattr(Transcriber, "_run_apple_helper", fake_helper)
    result = Transcriber()._transcribe_apple(_audio(), "apple-speech", None, None)

    assert result.text == "今天天氣很好"          # 前後空白要修掉
    assert result.language == "zh"                # locale zh_TW → Whisper 風格代碼
    # duration/elapsed 由呼叫端 transcribe() 填，後端一律留 0
    assert result.duration_seconds == 0.0


def test_字典術語有被傳進去而且有上限(monkeypatch):
    captured = {}

    def fake_helper(self, args, timeout):
        captured["args"] = args
        # helper 讀得到這個檔才算真的傳進去
        idx = args.index("--terms-file")
        with open(args[idx + 1], encoding="utf-8") as fh:
            captured["terms"] = fh.read().splitlines()
        return {"ok": True, "text": "x"}

    monkeypatch.setattr(Transcriber, "_run_apple_helper", fake_helper)
    many = [f"詞{i}" for i in range(500)]
    Transcriber()._transcribe_apple(_audio(), "apple-speech", None, many)

    assert len(captured["terms"]) == tr._APPLE_MAX_TERMS   # 截到上限
    assert captured["terms"][0] == "詞0"


def test_沒有字典時不傳_terms_file(monkeypatch):
    captured = {}

    def fake_helper(self, args, timeout):
        captured["args"] = args
        return {"ok": True, "text": "x"}

    monkeypatch.setattr(Transcriber, "_run_apple_helper", fake_helper)
    Transcriber()._transcribe_apple(_audio(), "apple-speech", None, [])
    assert "--terms-file" not in captured["args"]


# ── (c) 失敗路徑：每一種都要真的失敗 ──────────────────────────────────────

@pytest.mark.parametrize("error_code,關鍵字", [
    ("asset_not_installed", "尚未安裝"),
    ("helper_missing",      "build.sh"),     # 要告訴使用者怎麼修
    ("locale_unsupported",  "不支援"),
    ("helper_timeout",      "失敗"),
    ("helper_bad_json",     "失敗"),
    ("天外飛來的錯誤",       "失敗"),          # 未知錯誤也要有 fallback 訊息
])
def test_每種錯誤都拋例外而不是靜靜回傳(monkeypatch, error_code, 關鍵字):
    """失敗一定要用例外表達。

    早期版本是「回傳一個 text 是錯誤訊息的 TranscriptionResult」，結果
    transcribe() 走完整條成功路徑、audit log 把它記成 error=None 的成功紀錄，
    之後統計成功率與 RTF 會把失敗算成成功。
    """
    monkeypatch.setattr(Transcriber, "_run_apple_helper",
                        lambda self, args, timeout: {"ok": False, "error": error_code})
    with pytest.raises(AppleSTTError) as excinfo:
        Transcriber()._transcribe_apple(_audio(), "apple-speech", None, None)

    assert excinfo.value.code == error_code
    assert 關鍵字 in excinfo.value.message
    # 全形括號包住整句 = 系統訊息的慣例，下游靠它認出「這不是逐字稿」
    assert is_system_message(excinfo.value.message)


def test_helper_回傳空的也不會被當成有結果(monkeypatch):
    """ok 欄位缺席 = 失敗。不可以因為 .get('ok') 是 None 就當成功。"""
    monkeypatch.setattr(Transcriber, "_run_apple_helper",
                        lambda self, args, timeout: {})
    with pytest.raises(AppleSTTError):
        Transcriber()._transcribe_apple(_audio(), "apple-speech", None, None)


def test_空音訊直接擋掉不送給_helper(monkeypatch):
    """0 影格的 wav 會讓 helper 的結果串流永遠等不到結束訊號、整支掛住。"""
    called = {"n": 0}

    def fake_helper(self, args, timeout):
        called["n"] += 1
        return {"ok": True, "text": "不該走到這裡"}

    monkeypatch.setattr(Transcriber, "_run_apple_helper", fake_helper)
    with pytest.raises(AppleSTTError) as excinfo:
        Transcriber()._transcribe_apple(np.array([], dtype=np.float32),
                                        "apple-speech", None, None)
    assert excinfo.value.code == "empty_audio"
    assert called["n"] == 0, "空音訊不應該開子程序"


def test_語言代碼回的是_whisper_風格不是_locale(monkeypatch):
    """四個後端共寫 language 這個欄位，格式混用會讓依語言篩選的查詢漏資料。"""
    monkeypatch.setattr(Transcriber, "_run_apple_helper",
                        lambda self, args, timeout: {"ok": True, "text": "x", "locale": "zh_TW"})
    result = Transcriber()._transcribe_apple(_audio(), "apple-speech", None, None)
    assert result.language == "zh"        # 不是 "zh_TW"


@pytest.mark.parametrize("text,是系統訊息", [
    ("（未偵測到語音內容）", True),
    ("（蘋果語音辨識模型尚未安裝、請重開 App 讓它在背景下載）", True),
    ("今天天氣很好", False),
    ("（笑）他說他不去了", False),      # 開頭有括號但不是整句包住 → 是逐字稿
    ("", False),
    ("（第一行）\n第二行", False),      # 多行 → 是逐字稿
])
def test_系統訊息判斷(text, 是系統訊息):
    """這個判斷是「錯誤訊息會不會被自動貼進使用者游標」的唯一防線。"""
    assert is_system_message(text) is 是系統訊息


def test_helper_不存在時走的是_helper_missing(monkeypatch):
    """不假造 _run_apple_helper，改把路徑指到不存在的地方——
    驗的是真正的那支函式，不是測試替身。"""
    monkeypatch.setattr(tr, "_APPLE_HELPER_PATH", "/nowhere/whisperpro-apple-stt")
    payload = Transcriber()._run_apple_helper(["--probe"], timeout=5.0)
    assert payload["ok"] is False
    assert payload["error"] == "helper_missing"


# ── (d) 暫存檔清理 ────────────────────────────────────────────────────────

def test_暫存音檔一定會被刪掉(monkeypatch):
    seen = {}

    def fake_helper(self, args, timeout):
        wav = args[args.index("--audio") + 1]
        seen["wav"] = wav
        assert os.path.exists(wav), "helper 執行時暫存檔應該還在"
        return {"ok": True, "text": "x"}

    monkeypatch.setattr(Transcriber, "_run_apple_helper", fake_helper)
    Transcriber()._transcribe_apple(_audio(), "apple-speech", None, ["詞"])

    assert not os.path.exists(seen["wav"]), "使用者的語音不可以留在暫存目錄"
    assert not os.path.exists(os.path.dirname(seen["wav"])), "暫存目錄也要收掉"


def test_helper_拋例外時暫存檔還是會被刪掉(monkeypatch):
    seen = {}

    def boom(self, args, timeout):
        seen["wav"] = args[args.index("--audio") + 1]
        raise RuntimeError("helper 炸了")

    monkeypatch.setattr(Transcriber, "_run_apple_helper", boom)
    with pytest.raises(RuntimeError):
        Transcriber()._transcribe_apple(_audio(), "apple-speech", None, None)

    assert not os.path.exists(seen["wav"]), "例外路徑也不可以留下使用者的語音"


# ── (e) 選單註冊 ──────────────────────────────────────────────────────────

def test_模型選單只在_macOS_出現這個選項():
    from config import MODEL_INFO
    from platform_util import IS_MAC

    assert ("apple-speech" in MODEL_INFO) is bool(IS_MAC)
    # 既有三個模型一個都不能少
    for m in ("large-v3-turbo", "qwen3-asr", "qwen3-asr-large"):
        assert m in MODEL_INFO


# ── (f) 排版：全形標點前的多餘空白 ────────────────────────────────────────

@pytest.mark.parametrize("原文,期望", [
    # 蘋果引擎會在中文與全形標點之間插空格（2026-09-17 實測 81% 的輸出都有）
    ("哈嘍 ，你這個北七。",        "哈嘍，你這個北七。"),
    ("語音辨識引擎 ，看看它",      "語音辨識引擎，看看它"),
    ("結束了 。",                 "結束了。"),
    ("他說 「好」",               "他說 「好」"),      # 前引號不動，那是開頭不是結尾
    # 中文與英數之間的空格是正確排版，**不可以**被砍掉
    ("還不如 Qwen3 的那個",       "還不如 Qwen3 的那個"),
    ("它只有 0.6B 的大小",        "它只有 0.6B 的大小"),
    # 本來就正常的不要動
    ("正常的句子，沒有問題。",      "正常的句子，沒有問題。"),
    ("", ""),
])
def test_清掉全形標點前的空白(原文, 期望):
    assert tr._tidy_apple_spacing(原文) == 期望


def test_轉錄結果有套用排版清理(monkeypatch):
    """不是只有純函式對——實際走 _transcribe_apple 出來也要是乾淨的。"""
    monkeypatch.setattr(Transcriber, "_run_apple_helper",
                        lambda self, args, timeout: {
                            "ok": True, "text": "哈嘍 ，你這個北七。", "locale": "zh_TW"})
    result = Transcriber()._transcribe_apple(_audio(), "apple-speech", None, None)
    assert result.text == "哈嘍，你這個北七。"


# ── (g) 簡轉繁重整（v2.31.1）──────────────────────────────────────────────
#
# 蘋果 zh-TW 引擎自己轉的繁體錯字率是 Qwen3 的 40 倍（2026-09-21～24 真實使用：
# 每千字 8.1 vs 0～0.2）。修法是 繁→簡→依設定繁，與 Qwen3 走同一條 opencc。

try:
    import opencc  # noqa: F401
    _HAS_OPENCC = True
except Exception:
    _HAS_OPENCC = False

needs_opencc = pytest.mark.skipif(not _HAS_OPENCC, reason="沒裝 opencc")


@needs_opencc
@pytest.mark.parametrize("蘋果寫的,應該是", [
    # 全部取自使用者三天真實輸出裡出現過的錯字
    ("我隻有三個",       "我只有三個"),
    ("最多隻能喝兩杯",   "最多只能喝兩杯"),
    ("前麵很復雜",       "前面很複雜"),
    ("係統都安裝好了",   "系統都安裝好了"),
    ("紫微鬥數跟八字",   "紫微斗數跟八字"),
    ("按照錶定時間",     "按照表定時間"),
    ("調不迴來",         "調不回來"),
    ("重市場關註",       "重市場關注"),
])
def test_簡轉繁錯字會被修掉(蘋果寫的, 應該是):
    assert tr._renormalize_apple_chinese(蘋果寫的, "traditional_tw") == 應該是


@needs_opencc
@pytest.mark.parametrize("正確的", [
    # 量詞「隻」是對的，不能被改成「只」——transcriber.py 的 _FANJIAN_FIXES 有一條
    # 「禁單字」鐵律，就是因為使用者說過「一隻美股」、單字替換把它改壞過
    "一隻美股",
    "養了一隻貓",
    "兩隻手",
    # 「優化」不能被轉成「最佳化」（v2.21.0 使用者明確要求保留）
    "我們要優化這個流程",
    # 本來就對的句子、英數混排，一個字都不能動
    "今天天氣很好。",
    "還不如 Qwen3 的那個",
])
def test_本來就對的不能被改壞(正確的):
    assert tr._renormalize_apple_chinese(正確的, "traditional_tw") == 正確的


def test_使用者關掉簡繁轉換就原樣不動():
    assert tr._renormalize_apple_chinese("我隻有三個", "off") == "我隻有三個"


def test_轉換器載不起來時回蘋果原文_不是回簡體(monkeypatch):
    """拿到簡體比拿到有幾個錯字的繁體更糟——任何失敗都要退回原文。"""
    monkeypatch.setattr(tr, "_get_opencc_t2s", lambda: None)
    assert tr._renormalize_apple_chinese("我隻有三個", "traditional_tw") == "我隻有三個"


@needs_opencc
def test_轉到一半炸掉也回蘋果原文(monkeypatch):
    class Boom:
        def convert(self, text):
            raise RuntimeError("opencc 炸了")
    monkeypatch.setattr(tr, "_get_opencc_converter", lambda variant: Boom())
    # t2s 那一步會成功、s2t 那一步炸——此時手上是簡體，絕對不能回傳它
    assert tr._renormalize_apple_chinese("我隻有三個", "traditional_tw") == "我隻有三個"


@needs_opencc
def test_實際轉錄路徑有套用重整(monkeypatch):
    """不是只有函式對——走 _transcribe_apple 出來的也要是修好的。"""
    monkeypatch.setattr(Transcriber, "_run_apple_helper",
                        lambda self, args, timeout: {
                            "ok": True, "text": "我隻有三個係統 ，前麵很復雜。", "locale": "zh_TW"})
    result = Transcriber()._transcribe_apple(_audio(), "apple-speech", None, None,
                                             chinese_variant="traditional_tw")
    # 空格清理與簡轉繁重整兩件事都要發生
    assert result.text == "我只有三個系統，前面很複雜。"


@needs_opencc
def test_英文語言不做簡繁轉換(monkeypatch):
    called = {"n": 0}
    real = tr._renormalize_apple_chinese

    def spy(text, variant):
        called["n"] += 1
        return real(text, variant)

    monkeypatch.setattr(tr, "_renormalize_apple_chinese", spy)
    monkeypatch.setattr(Transcriber, "_run_apple_helper",
                        lambda self, args, timeout: {"ok": True, "text": "hello", "locale": "en_US"})
    Transcriber()._transcribe_apple(_audio(), "apple-speech", "en", None,
                                    chinese_variant="traditional_tw")
    assert called["n"] == 0


@needs_opencc
@pytest.mark.xfail(strict=True, reason=(
    "已知限制：t2s 把「裏」退成「里」後，s2twp 在「電話會里」這個上下文沒能還原成「裡」。"
    "三天真實資料 44 處改動中唯一改壞的一處。strict=True：哪天 opencc 修好了這個測試會"
    "變成 XPASS 而失敗，提醒下一個人把這條 xfail 拿掉、並更新交接紀錄。"
))
def test_已知限制_電話會裏會被改成里():
    assert tr._renormalize_apple_chinese("電話會裏的情形", "traditional_tw") == "電話會裡的情形"
