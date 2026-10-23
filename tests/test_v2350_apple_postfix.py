"""v2.35.0：蘋果辨識結果的自動修正——英文詞比對字典。

依據 docs/工作紀錄/2026-10-23_蘋果語音辨識原理與提升準確度研究.md §2、§7，
以及總監定案的修正（scratchpad/fix_v2350.md）。

原本還有一道 A「重新轉繁體」，已拿掉：App 實際路徑上既有的 _renormalize_apple_chinese
（v2.31.1，s2twp＋台灣用語修正）已經修掉同一批錯字，加 A 之後錯誤數 74 → 74，沒有增益。
這裡有測試確認 A 真的不在了（免得下一個人以為忘了做而補回來）。

這一版 B 的第一優先是「不改錯」：蘋果轉出來的正常英文，改錯會直接貼到使用者游標處。
所以 B 先把「正常英文字」全部排除，只處理「不是正常字、又很像字典詞」的東西；
每一條防護規則（門檻、正常字過濾、候選長度下限、網址防護、只差數字或 s、大小寫只對非正常字）
都至少有一個測試在它被拿掉時轉紅。

測試全部用假結果：不開麥克風、不載真模型、不呼叫真的蘋果 helper、不寫真的 audit 檔。
正常英文字表用 macOS 內建的 /usr/share/dict/words（沒有這個檔的機器會跳過依賴它的測試）。
"""

import os

import numpy as np
import pytest

import transcriber as tr
from transcriber import Transcriber, TranscriptionResult

needs_words = pytest.mark.skipif(
    not os.path.exists("/usr/share/dict/words"), reason="沒有 /usr/share/dict/words")

# 使用者這類真實的字典組合（含 LINE、Codex、Claude、Notion、Cloud 這類同時也是正常英文字的詞）
DICT = ["LINE", "Codex", "Claude", "Claude Code", "Notion", "Cloud", "GitHub", "Gemini",
        "Ollama", "Palantir", "Opus", "CRM", "Claude Sonnet", "vibe coding", "NVIDIA",
        "Figma", "Parquet", "Whisper", "TradingView", "Qwen3", "Python", "GPT4o"]


def _eng(text, terms=DICT):
    return tr._apple_match_english(text, terms)[0]


# ═════════════════════════════════════════════════════════════════════════════
#  A 已拿掉
# ═════════════════════════════════════════════════════════════════════════════

def test_A_已拿掉_相關東西都不在了():
    """為什麼拿掉見交接紀錄：既有的 s2twp 重整已修掉同一批錯字，加 A 沒有增益（74 → 74）。"""
    assert not hasattr(tr, "_apple_retraditionalize")
    assert not hasattr(tr, "_APPLE_RETRAD_WHITELIST")
    assert not hasattr(tr, "_APPLE_RETRAD_REASONS")
    assert "traditional_tw_chars" not in tr._OPENCC_VARIANT_MAP


# ═════════════════════════════════════════════════════════════════════════════
#  B. 正常英文一個字都不能被改（審查實測的兩份清單，全部變成回歸測試）
# ═════════════════════════════════════════════════════════════════════════════

NORMAL_SENTENCES = [
    # 審查第一份：現版會改錯的
    "the line chart is fine",          # line→LINE
    "fine tuning the model",           # fine→LINE
    "online and inline",               # online/inline→LINE
    "review the code",                 # code→Codex
    "source code",
    "open source",
    "motion graphics",                 # motion→Notion
    "the cloud",
    "clause",                          # clause→Claude
    "we must fix this",
    "trust the process",
    "black box",
    "room for improvement",
    "terms and conditions",
    "the team meeting",
    "string",
    "Python script",
    "shift key",
    "mini",                            # mini↔Gemini 0.33
    "pilot",
    "GitLab",                          # GitLab↔GitHub 0.33
    "Llama",                           # Llama→Ollama
    "Python3",
    "lines",
    "Qwen",
    "clad",
    "planter",                         # planter→Palantir
    # 審查第二份：網址、識別字、片語
    "https://cloud.google.com/run",    # 網址裡 cloud→Cloud、cloud.google.com→…CRM…
    "cloud_run",
    "line_number",
    "push the code to GitLab",
    "use Llama 3",
    "online meeting",
    "deploy to cloud",
    "the Teams call",
    "a mini app",
    "motion design",
    "the notion of",
    "line by line",
    "codes and scripts",
    "Gemini mini",
    # 總監點名的其他：Cloud Code→Cloud Codex、Qwens
    "Cloud Code",
    "Qwens",
    # 第二輪審查（2026-10-23）實測被改錯的，總監修補後全部要原樣保留
    "我把檔案放在 iCloud 上", "我用 iCloud Drive 同步",               # iCloud→Cloud（頭多一個 i）
    "我們用 WhisperX 做 diarization", "WhisperPro 這個 App",          # 尾巴多幾個字＝另一個產品
    "Wispr Flow 這個產品你用過嗎",                                     # 語音輸入競品
    "在 SwiftUI 的 Treeview 裡面展開節點",                             # tree＋view 黏在一起的正常字
    "我們要用 Sessionize 做議程", "I Googled it", "YouTubers love it",
    "請問 GPT4 跟 GPT4o 差在哪",                                       # GPT4 是真的型號，不是 GPT4o 拼錯
    "這個影片的 audio codec 是 AAC", "JSON 格式要對", "the clade of dinosaurs", "the skilful engineer",
    "他是一個 vibe coder", "vibe code", "vibe cooking",                # 不一樣的那個詞是正常字
    "Vibe Coding 這個詞是 Karpathy 講的",                              # 句首大寫不要被改成字典的全小寫
    "the wispier hair", "Whiskier than usual",                        # y 結尾的字變形
    "that Viber thing",                                               # 整組夠近、但 thing 跟 coding 差太多
]


@needs_words
@pytest.mark.parametrize("句子", NORMAL_SENTENCES)
def test_B_正常英文一個字都不改(句子):
    assert _eng(句子) == 句子
    # 夾在中文句裡也一樣
    assert _eng(f"我說 {句子} 這樣") == f"我說 {句子} 這樣"


@needs_words
def test_B_正常英文也不改大小寫():
    """line／notion／cloud 在字典裡是 LINE／Notion／Cloud，但它們是正常字，連大小寫都不碰。"""
    for s in ("line", "notion", "cloud", "Line", "NOTION", "the Cloud"):
        assert _eng(s) == s


# ═════════════════════════════════════════════════════════════════════════════
#  B. 該換的
# ═════════════════════════════════════════════════════════════════════════════

@needs_words
@pytest.mark.parametrize("蘋果寫的,應該是", [
    ("我用 clardcode 寫程式",        "我用 Claude Code 寫程式"),
    ("程式放在 gitub 上",            "程式放在 GitHub 上"),
    ("這就是 wibecoding 的魅力",     "這就是 vibe coding 的魅力"),
    ("Nervidia 的股價",              "NVIDIA 的股價"),
    ("存成 Parquite 格式",           "存成 Parquet 格式"),
    ("用 Wisper 轉文字",             "用 Whisper 轉文字"),
    ("選 clad sonet 這個模型",       "選 Claude Sonnet 這個模型"),    # 兩個詞對一個字典詞
    ("在 tradingiew 看線圖",         "在 TradingView 看線圖"),
    ("用 Clawd Code 寫",             "用 Claude Code 寫"),            # Code 是正常字、Clawd 不是 → 整組換
    ("用 claud code 寫",             "用 Claude Code 寫"),            # 單字 claud 也比得上 Claude，但兩個詞一組優先
])
def test_B_該換的全部換對(蘋果寫的, 應該是):
    assert _eng(蘋果寫的) == 應該是


@needs_words
@needs_words
def test_B_兩個詞一組_不一樣的詞都是正常字就不換():
    """字典有 Google Meet 時，Google Maps／Google Sheets 不能被換成 Google Meet（第二輪審查實測）。"""
    d = DICT + ["Google Meet", "Jason", "Kevin"]
    for s in ("我用 Google Maps 導航", "打開 Google Sheets", "在 Google Chat 回覆"):
        assert _eng(s, d) == s
    assert _eng("JSON 格式要對", d) == "JSON 格式要對"


@needs_words
def test_B_一邊只是另一邊頭尾多幾個字_不換():
    for s in ("Sessionize", "WhisperX", "iCloud", "TradingViewer", "Palantiri"):
        assert _eng(s, DICT + ["session"]) == s


@needs_words
def test_B_大小寫修正_只對不是正常字的():
    assert _eng("我用 gpt4o 寫") == "我用 GPT4o 寫"
    assert _eng("看 nvidia 的財報") == "看 NVIDIA 的財報"
    assert _eng("放在 github") == "放在 GitHub"
    # tradingview＝trading＋view 兩個正常字黏在一起，現在算正常字、不改大小寫（為了不把 Treeview 改成 review，總監 2026-10-23）
    assert _eng("用 tradingview") == "用 tradingview"
    # 正常字不做（上面已測 line／notion／cloud），這裡再確認「寫法已經一樣」不算換
    out, hits, n = tr._apple_match_english("用 GitHub 跟 NVIDIA", DICT)
    assert (out, hits, n) == ("用 GitHub 跟 NVIDIA", {}, 0)


@needs_words
def test_B_同一句多處同時換_並回報():
    out, hits, n = tr._apple_match_english("用 clardcode 開 gitub 再用 clardcode", DICT)
    assert out == "用 Claude Code 開 GitHub 再用 Claude Code"
    assert n == 3
    assert hits == {"clardcode": "Claude Code", "gitub": "GitHub"}


@needs_words
def test_B_英文夾在英文句子裡也找得到():
    # 前面的 use 是正常字，不能把後面的 clardcode 一起吃掉
    assert _eng("please use clardcode today") == "please use Claude Code today"


@needs_words
def test_B_兩個詞一組_不吃掉冠詞():
    """兩個詞一組只對「字典詞也是兩個詞」成立，否則冠詞 a 會被整組吃掉。
    （原本用 a Figmar；Figmar 只是 Figma 尾巴多一個字，現在刻意不修，改用 Parquite。）"""
    assert _eng("make a Parquite file") == "make a Parquet file"


@needs_words
def test_B_兩個詞一組成立就整組換_不再各自比對單字():
    # claud 單字會比到 Claude；整組成立時只能得到 Claude Code，不能是 Claude Claude Code 之類
    assert _eng("claud code") == "Claude Code"
    assert _eng("clad sonet") == "Claude Sonnet"


@needs_words
def test_B_兩個詞要剛好一個空格隔開():
    assert _eng("clad  sonet") == "clad  sonet"      # 兩個空格
    assert _eng("clad\nsonet") == "clad\nsonet"      # 換行


@needs_words
def test_B_標點與中文緊貼也找得到():
    assert _eng("(clardcode)") == "(Claude Code)"
    assert _eng("用clardcode寫") == "用Claude Code寫"


# ═════════════════════════════════════════════════════════════════════════════
#  B. 每一條防護規則
# ═════════════════════════════════════════════════════════════════════════════

@needs_words
@pytest.mark.parametrize("句子", [
    "https://gitub.com/foo",     # 網址：前面是 //、後面是 .
    "gitub_repo",                # 識別字：後面是 _
    "my_gitub",                  # 前面是 _
    "my-clardcode",              # 前面是 -
    "clardcode-cli",             # 後面是 -
    "clardcode.py",              # 檔名：後面是 .
    "src/clardcode",             # 路徑：前面是 /
    "mail@gitub",                # @
    "key:clardcode",             # :
    "clardcode:",                # 後面是 :
])
def test_B_網址檔名識別字不碰(句子):
    """比對單位前一個或後一個字元是 . _ - / @ : 就整個跳過。沒有這條，上面每一個都會被換成字典詞。"""
    assert _eng(句子) == 句子
    # 對照：同樣的拼錯、旁邊是空白或中文就會換（確認這個測試不是因為別的原因才不換）
    assert _eng("用 clardcode 寫") == "用 Claude Code 寫"


@needs_words
@pytest.mark.parametrize("文字,字典", [
    ("Python3", ["Python"]),        # 只差結尾數字
    ("Qwen",    ["Qwen3"]),        # 字典有數字、文字沒有
    ("Qwens",   ["Qwen3"]),        # 只差結尾 s（與數字）
    ("Figmas",  ["Figma"]),
    ("Parquets", ["Parquet"]),
    ("Claude4",  ["Claude"]),
])
def test_B_只差結尾數字或s不換(文字, 字典):
    assert _eng(文字, 字典) == 文字


@needs_words
def test_B_門檻是零點三零():
    # Notxxn↔Notion：2/6=0.333。舊門檻 0.34 會換，現在要擋
    assert _eng("Notxxn", ["Notion"]) == "Notxxn"
    # 剛好 0.30（3/10）要換；浮點誤差不能讓它變成不換
    assert _eng("abcdefgxyz", ["abcdefghij"]) == "abcdefghij"
    # 0.4 當然不換
    assert _eng("Figxx", ["Figma"]) == "Figxx"
    assert _eng("Zzzzz", ["Figma"]) == "Zzzzz"


@needs_words
def test_B_模糊比對只用夠長的字典詞():
    """候選正規化後 < 5 字元（LINE、Opus、Rust、Zoom 這類）不當模糊比對的目標。
    Opuz↔Opus 是 1/4=0.25，若候選長度下限只有 3 就會被換。"""
    assert _eng("Opuz", ["Opus"]) == "Opuz"
    assert _eng("Rusx", ["Rust"]) == "Rusx"
    assert _eng("Zoon", ["Zoom"]) == "Zoon"
    # 剛好 5 字元的候選可以用
    assert _eng("Figmx", ["Figma"]) == "Figma"


@needs_words
def test_B_短字典詞仍可做大小寫修正():
    # 短詞不當模糊目標，但「正規化完全相同、不是正常字」的大小寫修正照做
    assert _eng("用 crm 系統", ["CRM"]) == "用 CRM 系統"


@needs_words
def test_B_兩個候選一樣接近就不換():
    assert _eng("Figmu", ["Figma", "Figmo"]) == "Figmu"


@needs_words
def test_B_最近的要比第二近至少少_0點10():
    # 差距剛好 0.10：1/10=0.1、2/10=0.2 → 換
    assert _eng("abcdefghiy", ["abcdefghix", "abcdefghxx"]) == "abcdefghix"
    # 差距只有 0.05 → 不換
    unit = "abcdefghijklmnopqrst"
    a = "abcdefghijklmnopqrsx"      # 距離 1 → 0.05
    b = "abcdefghijklmnopqrxx"      # 距離 2 → 0.10
    assert _eng(unit, [a, b]) == unit


@needs_words
def test_B_只有一個候選時不需要比第二名():
    assert _eng("gitub", ["GitHub"]) == "GitHub"


@needs_words
def test_B_少於三個字元不換():
    assert _eng("ab", ["abcde"]) == "ab"
    assert _eng("ai", ["AI"]) == "ai"       # 太短，連大小寫修正也不做


@needs_words
def test_B_不認識的英文不動():
    assert _eng("let me think about the weather") == "let me think about the weather"
    assert _eng("xqzvk") == "xqzvk"


@needs_words
def test_B_中文與數字不動():
    s = "今天是2026年10月23日，我有3個想法，花了150元。"
    assert tr._apple_match_english(s, DICT) == (s, {}, 0)
    # 數字黏在前面的不比對
    assert _eng("買了 3gitub") == "買了 3gitub"


def test_B_空字典整步跳過():
    assert tr._apple_match_english("clardcode gitub", []) == ("clardcode gitub", {}, 0)
    # 字典只有中文詞、沒有英文詞，也等於空
    assert tr._apple_match_english("clardcode", ["紫微斗數", "潤飾"]) == ("clardcode", {}, 0)


@needs_words
def test_B_含中文的字典詞不當候選():
    # Claudd 離 Claude 只差 1 字；若把「Claude 程式」的英文部分當候選，就會被換成含中文的詞
    assert _eng("Claudd", ["Claude 程式"]) == "Claudd"


@needs_words
def test_B_字典詞正規化相同只取第一個_不互相打平():
    assert _eng("gitub", ["GitHub", "Github"]) == "GitHub"


# ═════════════════════════════════════════════════════════════════════════════
#  正常英文字表
# ═════════════════════════════════════════════════════════════════════════════

@needs_words
@pytest.mark.parametrize("w", [
    # web2 有的
    "line", "code", "cloud", "notion", "planter", "clause", "pilot",
    # 簡單字尾還原
    "lines", "codes", "scripts", "Teams", "coding", "coded", "teacher", "carefully", "boxes",
    # web2 缺的最常用字（拿約 400 個常用字對照時發現的）
    "box", "has", "paid", "internet", "laptop",
    # web2 沒有、補充字表要有（現代常見字）
    "online", "offline", "inline", "email", "app", "apps", "login", "deploy", "token", "cache",
    "merge", "commit", "review", "session", "prompt", "workflow", "dashboard", "plugin",
    "template", "mini", "gitlab", "llama",
])
def test_字表_正常字(w):
    assert tr._is_normal_english(w)


@needs_words
@pytest.mark.parametrize("w", [
    "clardcode", "gitub", "wibecoding", "Nervidia", "Figmar", "Parquite", "Wisper",
    "sonet", "tradingiew", "Clawd", "claud", "github", "gpt4o", "Qwen3", "Python3",
])
def test_字表_不是正常字(w):
    """特別是 Wisper：'er' 字尾還原若不設下限，會變成 wisp+er 而被當成正常字，就修不到 Whisper 了。"""
    assert not tr._is_normal_english(w)


def test_字表檔不存在_B整步跳過_只記一次錯(monkeypatch):
    monkeypatch.setattr(tr, "_ENGLISH_WORDS_PATH", "/nonexistent/words")
    monkeypatch.setattr(tr, "_english_words", None)
    monkeypatch.setattr(tr, "_english_words_tried", False)
    errors = []
    monkeypatch.setattr(tr, "log_error", lambda *a, **k: errors.append(a))
    assert tr._apple_match_english("用 clardcode 開 gitub", DICT) == ("用 clardcode 開 gitub", {}, 0)
    assert tr._apple_match_english("用 clardcode 開 gitub", DICT) == ("用 clardcode 開 gitub", {}, 0)
    assert len(errors) == 1       # 寧可不修也不要亂修；而且不要每次轉錄都記一次


def test_字表檔太小_視為壞檔_B整步跳過(monkeypatch, tmp_path):
    """檔案壞了或根本不是字表時，所有英文字都會變成「不正常」而被拿去比對——沒有字表比壞字表安全。"""
    f = tmp_path / "words"
    f.write_text("alpha\nbeta\n", encoding="utf-8")
    monkeypatch.setattr(tr, "_ENGLISH_WORDS_PATH", str(f))
    monkeypatch.setattr(tr, "_english_words", None)
    monkeypatch.setattr(tr, "_english_words_tried", False)
    monkeypatch.setattr(tr, "log_error", lambda *a, **k: None)
    assert tr._apple_match_english("用 clardcode", DICT) == ("用 clardcode", {}, 0)


@needs_words
def test_字表只載一次(monkeypatch):
    monkeypatch.setattr(tr, "_english_words", None)
    monkeypatch.setattr(tr, "_english_words_tried", False)
    first = tr._get_english_words()
    assert isinstance(first, frozenset) and len(first) > 100_000
    assert tr._get_english_words() is first


# ═════════════════════════════════════════════════════════════════════════════
#  整合：走 transcribe() 的真實後處理順序
# ═════════════════════════════════════════════════════════════════════════════

class _FakeAudit:
    def __init__(self):
        self.events = []

    def write_event(self, name, pid, **kw):
        self.events.append((name, kw))


def _run(monkeypatch, text, *, model="apple-speech", terms=(), corrections=(),
         variant="traditional_tw", audit=None):
    """跑一次 transcribe()：推論那一步換成假的，其餘後處理照實走。回傳 (結果, audit 事件)。"""
    monkeypatch.setattr(tr, "IS_MAC", True)
    monkeypatch.setattr(tr, "BACKEND", "mlx")
    fake_audit = audit if audit is not None else _FakeAudit()
    monkeypatch.setattr(tr, "_audit_log", fake_audit)

    def fake_infer(self, *a, **k):
        return TranscriptionResult(text=text, language="zh", duration_seconds=0.0, elapsed_seconds=0.0)

    monkeypatch.setattr(Transcriber, "_transcribe_apple", fake_infer)
    monkeypatch.setattr(Transcriber, "_transcribe_qwen3", fake_infer)
    monkeypatch.setattr(Transcriber, "_transcribe_mlx", fake_infer)
    monkeypatch.setattr(Transcriber, "_emit_audit", lambda self, **k: None)
    monkeypatch.setattr(Transcriber, "_maybe_save_suspicious_audio", lambda self, **k: None)

    import dictionary
    monkeypatch.setattr(dictionary, "load_corrections", lambda *a, **k: list(corrections))

    t = Transcriber()
    t._silero_vad_enabled = False
    t.set_dictionary_terms(list(terms))
    # 5 秒、振幅 0.3 的音：不會被長度閘或靜音閘擋掉
    audio = (0.3 * np.sin(np.linspace(0, 2000, 80_000))).astype("float32")
    result = t.transcribe(audio, model, "zh", chinese_variant=variant)
    return result, fake_audit.events


@needs_words
def test_整合_蘋果跑英文比對_中文簡繁一個字不動(monkeypatch):
    """A 已拿掉：錶格、係統這類字在這個步驟之後仍維持蘋果原樣（要修是 _finish_apple_payload 既有重整的事）。"""
    r, _ = _run(monkeypatch, "用 clardcode 開錶格，係統很好，合並", terms=["Claude Code"])
    assert r.text == "用 Claude Code 開錶格，係統很好，合並"


@needs_words
@pytest.mark.parametrize("model", ["qwen3-asr", "qwen3-asr-large", "large-v3-turbo"])
def test_整合_非蘋果引擎一個字都不能變(monkeypatch, model):
    text = "用 clardcode 開 gitub，gpt4o"
    r, events = _run(monkeypatch, text, model=model, terms=["Claude Code", "GitHub", "GPT4o"])
    assert r.text == text
    assert not [e for e in events if e[1].get("filter", "").startswith("apple_")]


@needs_words
def test_整合_系統訊息不處理(monkeypatch):
    msg = "（未偵測到語音內容）clardcode"
    r, _ = _run(monkeypatch, msg, terms=["Claude Code"])
    assert r.text == msg


@needs_words
def test_整合_在_corrections_之後才跑(monkeypatch):
    # B 先跑會把 gitub 換成 GitHub，規則（gitub→Git-Hub）就找不到來源。
    r, _ = _run(monkeypatch, "看 gitub 上的碼", terms=["GitHub"], corrections=[("gitub", "Git-Hub")])
    assert r.text == "看 Git-Hub 上的碼"


@needs_words
def test_整合_corrections_的英文目標也當候選(monkeypatch):
    # 字典 terms 沒有 Whisper 但規則的目標有：使用者自己指定的拼法，也是可信的候選
    r, _ = _run(monkeypatch, "用 Wisper 轉", terms=[], corrections=[("whisperx", "Whisper")])
    assert r.text == "用 Whisper 轉"


@needs_words
def test_整合_corrections_的中文目標不當英文候選(monkeypatch):
    r, _ = _run(monkeypatch, "用 Claudd", terms=[], corrections=[("甲乙", "Claude 程式")])
    assert r.text == "用 Claudd"


@needs_words
def test_整合_英文比對那步炸掉_轉錄照常成功(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("爆了")
    monkeypatch.setattr(tr, "_apple_match_english", boom)
    errors = []
    monkeypatch.setattr(tr, "log_error", lambda *a, **k: errors.append(a))
    r, _ = _run(monkeypatch, "用 clardcode 開錶格", terms=["Claude Code"])
    assert r.text == "用 clardcode 開錶格"
    assert any("apple" in str(e) for e in errors)


@needs_words
def test_整合_使用者關掉簡繁轉換_英文比對與簡繁無關照跑(monkeypatch):
    r, _ = _run(monkeypatch, "用 clardcode 開錶格", terms=["Claude Code"], variant="off")
    assert r.text == "用 Claude Code 開錶格"


@needs_words
def test_整合_日誌只寫數量不寫文字(monkeypatch):
    lines = []
    monkeypatch.setattr(tr.log, "info", lambda msg, *a, **k: lines.append(str(msg)))
    _run(monkeypatch, "用 clardcode 開 gitub，錶格", terms=["Claude Code", "GitHub"])
    hit = [l for l in lines if "英文比對換了" in l]
    assert hit, lines
    assert "英文比對換了 2 處" in hit[0]
    assert "重新轉繁體" not in hit[0]
    # 使用者說的話不能進日誌
    assert "clardcode" not in hit[0] and "gitub" not in hit[0] and "錶" not in hit[0]


@needs_words
def test_整合_沒有換任何東西就不寫這行日誌(monkeypatch):
    lines = []
    monkeypatch.setattr(tr.log, "info", lambda msg, *a, **k: lines.append(str(msg)))
    _run(monkeypatch, "今天天氣很好 the line chart", terms=["Claude Code", "LINE"])
    assert not [l for l in lines if "英文比對換了" in l]


@needs_words
def test_整合_audit_有記濾鏡名稱與配對(monkeypatch):
    _, events = _run(monkeypatch, "用 clardcode 開錶格，係統", terms=["Claude Code"])
    by = {kw["filter"]: kw for name, kw in events if name == "post_filter_applied" and "filter" in kw}
    assert by["apple_english_match"]["hits"] == {"clardcode": "Claude Code"}
    assert "apple_retraditionalize" not in by
    k = by["apple_english_match"]
    assert "before_len" in k and "after_len" in k
    # hits 只放「原→新」配對，audit 事件不能夾使用者說的整段話
    assert "before_text" not in k and "after_text" not in k


@needs_words
def test_整合_audit_寫入失敗不影響轉錄(monkeypatch):
    class Bad:
        events = []

        def write_event(self, *a, **k):
            raise OSError("磁碟滿了")
    r, _ = _run(monkeypatch, "用 clardcode 開錶格", terms=["Claude Code"], audit=Bad())
    assert r.text == "用 Claude Code 開錶格"


def test_整合_字表檔不存在_轉錄照常成功_文字不變(monkeypatch):
    monkeypatch.setattr(tr, "_ENGLISH_WORDS_PATH", "/nonexistent/words")
    monkeypatch.setattr(tr, "_english_words", None)
    monkeypatch.setattr(tr, "_english_words_tried", False)
    monkeypatch.setattr(tr, "log_error", lambda *a, **k: None)
    r, _ = _run(monkeypatch, "用 clardcode 開錶格", terms=["Claude Code"])
    assert r.text == "用 clardcode 開錶格"
