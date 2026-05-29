"""
Whisper 語音轉文字封裝器。

後端自動偵測（啟動時判斷一次）：
  • Apple Silicon (arm64) + mlx_whisper 已安裝 → mlx-whisper（Metal GPU / Neural Engine）
  • 其他情況                                   → faster-whisper（CPU int8）

模型快取位置：~/.cache/huggingface/hub/（兩種後端共用同一目錄）

防護機制：
  • 靜音偵測：RMS < _MIN_RMS 直接跳過推論，避免空白音訊觸發幻覺
  • 幻覺過濾：比對已知幻覺字串列表，比對到就替換為「未偵測到語音」
"""

from __future__ import annotations

import gc
import os
import platform
import time
import threading
from dataclasses import dataclass, field
from typing import Optional

import prompts

from logger import get_logger, log_error

log = get_logger("transcriber")

# v2.19.0：observation hooks — audit JSONL + pipeline_id event + suspicious audio capture。
# 模組未就緒時（Agent N 並行開發中）整段 import 失敗 → noop 取代、絕不影響轉錄。
try:
    import audit_log as _audit_log
except Exception:
    _audit_log = None
try:
    import pipeline_id as _pipeline_id
except Exception:
    _pipeline_id = None


def _safe_pipeline_id() -> str:
    """安全取 current pipeline ID；模組缺席或拋例外都回空字串。"""
    if _pipeline_id is None:
        return ""
    try:
        return _pipeline_id.get_current() or ""
    except Exception:
        return ""


def _safe_pipeline_event(event_name: str, **fields) -> None:
    """安全發 pipeline 事件；模組缺席或拋例外都吞掉。"""
    if _pipeline_id is None:
        return
    try:
        _pipeline_id.event(event_name, **fields)
    except Exception:
        pass


def _audio_quality(audio) -> dict:
    """算 peak / rms / 前後 500ms RMS / clipping ratio。

    所有回傳值都用內建 float，已為 JSON 序列化做好準備。
    Empty audio 回 0 值字典（避免上層分支）。
    """
    import numpy as np
    if audio is None or audio.size == 0:
        return {
            "peak": 0.0, "rms": 0.0,
            "rms_first_500ms": 0.0, "rms_last_500ms": 0.0,
            "clipping_ratio": 0.0,
            "samples": 0, "duration_s": 0.0,
        }
    peak = float(np.abs(audio).max())
    rms = float(np.sqrt(np.mean(audio ** 2)))
    head_n = min(8000, len(audio))
    tail_n = min(8000, len(audio))
    rms_head = float(np.sqrt(np.mean(audio[:head_n] ** 2)))
    rms_tail = float(np.sqrt(np.mean(audio[-tail_n:] ** 2)))
    clip = float((np.abs(audio) > 0.95).sum()) / len(audio)
    return {
        "peak": peak, "rms": rms,
        "rms_first_500ms": rms_head, "rms_last_500ms": rms_tail,
        "clipping_ratio": clip,
        "samples": int(len(audio)), "duration_s": float(len(audio) / 16_000),
    }


# ── 後端偵測 ──────────────────────────────────────────────────────────────────

def _detect_backend() -> str:
    """偵測最佳推論後端：Apple Silicon + mlx 已安裝則用 mlx，否則用 ctranslate2。

    Returns:
        "mlx" 或 "ctranslate"。
    """
    if platform.machine() != "arm64":
        return "ctranslate"   # 非 Apple Silicon，只能跑 CPU 版
    try:
        import mlx_whisper  # noqa: F401 — 只測試能否 import
        return "mlx"
    except ImportError:
        return "ctranslate"   # mlx_whisper 未安裝，退而求其次


# 模組載入時偵測一次，整個 session 共用
BACKEND = _detect_backend()


def _detect_mlx_whisper_kwargs() -> set[str]:
    """v2.19.0：偵測這台機器上的 mlx_whisper.transcribe 支援哪些 kwargs。

    取代以前「全丟、TypeError 再 fallback」的盲試法——萬一加新 kwarg
    （例：hallucination_silence_threshold）不被支援，會把 temperature=0 /
    condition_on_previous_text=False 連坐 fallback 掉，導致退步。

    回傳 mlx_whisper.transcribe 的所有 parameter 名稱（set）；BACKEND != mlx
    或偵測失敗時回空 set（呼叫端用 `kw in _MLX_WHISPER_KWARGS` 一律 False、
    走最保守 path）。
    """
    if BACKEND != "mlx":
        return set()
    try:
        import inspect
        import mlx_whisper
        sig = inspect.signature(mlx_whisper.transcribe)
        return set(sig.parameters.keys())
    except Exception:
        return set()


_MLX_WHISPER_KWARGS = _detect_mlx_whisper_kwargs()

# faster-whisper 模型名稱 → mlx-community HuggingFace 倉庫 ID 的映射
# 注意：tiny/base 的 `mlx-community/whisper-tiny`、`whisper-base` repo 已於
# 2026 年改名/下架，會回 401。全部統一使用 `-mlx-4bit` 量化版本；若 MLX 載入
# 失敗，transcribe() 會自動 fallback 到 CTranslate2 CPU。
#
# large-v3 fp16 嘗試紀錄（2026-05-24 / v2.13.x）：
#   試過 `mlx-community/whisper-large-v3-fp16`、ValueError「load_npz Input
#   must be a zip file」。原因：該 repo 用檔名 `model.safetensors`，但
#   mlx_whisper/load_models.py 只認 `weights.safetensors`（turbo repo 用
#   這個命名），對 HF 標準命名不相容 → fallback 誤走 .npz loader 炸。
#   暫退回 `-mlx-4bit`（雖準確度較差）等 mlx_whisper 升級或找到相容 fp16 repo。
_MLX_MODEL_MAP: dict[str, str] = {
    "tiny":           "mlx-community/whisper-tiny-mlx-4bit",
    "base":           "mlx-community/whisper-base-mlx-4bit",
    "small":          "mlx-community/whisper-small-mlx-4bit",
    "medium":         "mlx-community/whisper-medium-mlx-4bit",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3":       "mlx-community/whisper-large-v3-mlx-4bit",
}

# ── v2.14.0 Qwen3-ASR 整合 ──────────────────────────────────────────────────
#
# Qwen3-ASR-0.6B：阿里 2026 年新出的 ASR 模型，本質是 Qwen LLM tuned for ASR。
# 跟 Whisper 比的優勢：
#   • 中文 acoustic 層更強（實測「潤」字 turbo 聽成「論」、Qwen3 正確）
#   • Biasing 用真正的 LLM system prompt（context= 參數），比 Whisper 的
#     initial_prompt decoder prefix 強度高
#   • 原生 MLX 寫、GPU + Neural Engine
#   • 模型更小（0.6B vs turbo 的 0.8B）、暖機後推論更快
# 缺點：
#   • 預設輸出簡體中文（要靠 opencc s2twp 後處理轉繁體）
#   • 新套件（mlx-qwen3-asr v0.3.5）可能有未知 edge cases
#   • 不能 fallback 到 CPU（CTranslate2 沒對應實作、純 MLX-only）
# v2.15.0：兩個 Qwen3-ASR 變體並存
#   • qwen3-asr       = 0.6B（速度優先、預設、官方 DEFAULT_MODEL_ID）
#   • qwen3-asr-large = 1.7B（準度優先、官方 ACCURACY_MODEL_ID）
# 切換時 _ensure_qwen3_session 會卸載舊 model 再載新的（單 slot、避免 RAM 翻倍）
_QWEN3_ASR_MODELS: dict[str, str] = {
    "qwen3-asr":       "Qwen/Qwen3-ASR-0.6B",   # ~600 MB、~1.2 GB RAM、推論 0.85-1.6s
    "qwen3-asr-large": "Qwen/Qwen3-ASR-1.7B",   # ~3.4 GB、~3.4 GB RAM、推論 2-4s
}


def _is_qwen3_model(model_size: str) -> bool:
    """判斷 model_size 是不是 Qwen3-ASR 系列（與 Whisper 走完全不同 backend）。"""
    return model_size.lower() in _QWEN3_ASR_MODELS


def _qwen3_hf_repo(model_size: str) -> str:
    """model_size → HuggingFace repo ID；未知名稱 fallback 到 0.6B。"""
    return _QWEN3_ASR_MODELS.get(model_size.lower(), _QWEN3_ASR_MODELS["qwen3-asr"])


# OpenCC 簡↔繁轉換器 lazy cache：variant → OpenCC 實例（或 None=載入失敗）
# 第一次使用時 lazy 載 opencc 套件，避免無此需求的使用者付 import 成本
_opencc_converters: dict[str, object] = {}

# variant 字串 → opencc config 名稱對應
_OPENCC_VARIANT_MAP: dict[str, str] = {
    "traditional_tw": "s2twp",   # 簡 → 繁 + 台灣慣用語（软件→軟體、视频→影片）
    "traditional":    "s2t",     # 純字體轉換（不轉地區用語）
}


def _get_opencc_converter(variant: str):
    """Lazy 載 opencc + 快取；回 None 代表「不用轉」（off 或載入失敗）。"""
    if variant == "off" or not variant:
        return None
    if variant in _opencc_converters:
        return _opencc_converters[variant]
    cfg_name = _OPENCC_VARIANT_MAP.get(variant, "s2twp")
    try:
        from opencc import OpenCC
        cc = OpenCC(cfg_name)
        _opencc_converters[variant] = cc
        return cc
    except Exception:
        log_error("opencc_init_failed", variant=variant, cfg=cfg_name)
        _opencc_converters[variant] = None
        return None


def _apply_opencc(text: str, variant: str) -> str:
    """套用 opencc 轉換到 text；任何失敗回原文不拋例外。"""
    if not text or variant == "off" or not variant:
        return text
    cc = _get_opencc_converter(variant)
    if cc is None:
        return text
    try:
        return cc.convert(text)
    except Exception:
        log_error("opencc_convert_failed", variant=variant)
        return text


# ── 靜音偵測與幻覺防護 ────────────────────────────────────────────────────────

# 最小 RMS 閾值：低於此值視為靜音，跳過推論。
# v2.19.0：0.002 → 0.004（從 -54 dBFS 收緊到 -48 dBFS）。
#   原因：實機 log 看到 0.002 漏掉「peak ~0.005 純底噪」案例，這類音檔被送進
#   Qwen3-ASR 後觸發字典 dump（00:42:02：「Qwen3-ASR Qwen3 Qwen Large V3...」）。
#   _normalize_volume 對 peak < 0.005 不會 boost，所以 RMS 仍會落在 0.001-0.003
#   區間 → 漏網。0.004 對「嗯」「好」等輕音仍可抓住（這類經正規化後 RMS ≥ 0.05）。
_MIN_RMS = 0.004

# v2.19.0：最小音檔長度（秒）— 短於此值視為「使用者誤觸 hotkey」、直接 short-circuit。
# 不送 ASR 避免模型對 <0.4s 片段亂編（streaming 最後一個 chunk + tail padding
# 常落在 0.2-0.4s 區間、是字典 dump / 假新詞 / 重複病 三隻幻覺的高發地帶）。
_MIN_DURATION_S = 0.4

# 已知 Whisper 幻覺字串（模型在靜音或噪音上常輸出這些）
# 比對時不分大小寫、去除空白
_HALLUCINATIONS: tuple[str, ...] = (
    "作詞", "作曲", "李宗盛",          # 常見中文歌曲幻覺
    "字幕由", "amara",                  # 字幕工具幻覺
    "請訂閱", "訂閱頻道", "謝謝收看",   # YouTube 幻覺
    "敬請期待",
    "please subscribe", "thanks for watching",
    "subtitles by", "transcript by",
    "請原文保留英文單字", "技術術語",   # 把 initial_prompt 內容幻覺回來
)


def _is_silence(audio) -> bool:
    """判斷音訊 RMS 能量是否低於閾值（視為靜音）。"""
    import numpy as np
    if audio.size == 0:
        return True
    rms = float(np.sqrt(np.mean(audio ** 2)))
    return rms < _MIN_RMS


# ── v2.19.x Silero VAD v5：神經前置過濾 ──────────────────────────────────────
# 補 RMS gate 漏網的「有能量但非人聲」（鍵盤、紙張、冷氣、背景音樂、麥克風拍打）。
# 注意：silero-vad v5+ API 用 load_silero_vad() 而非 torch.hub.load。
# 模型 ~9 MB（v6.2.1）、PyTorch backend；lazy load + module-level 快取，
# 載入失敗一次就不再試（避免每次 transcribe 重試造成停頓）。
_silero_vad_model = None
_silero_vad_lock = threading.Lock()
_silero_vad_load_failed = False


def _ensure_silero_vad():
    """Lazy load Silero VAD v5 model；回 None 表示載入失敗、呼叫端自行 fallback。"""
    global _silero_vad_model, _silero_vad_load_failed
    if _silero_vad_model is not None:
        return _silero_vad_model
    if _silero_vad_load_failed:
        return None
    with _silero_vad_lock:
        if _silero_vad_model is not None:
            return _silero_vad_model
        if _silero_vad_load_failed:
            return None
        try:
            from silero_vad import load_silero_vad
            # PyTorch 版（CPU）；onnx 版速度差不多但會多一個 onnxruntime 重依賴
            _silero_vad_model = load_silero_vad(onnx=False)
            log.info("WHISPER: Silero VAD v5 loaded successfully")
            return _silero_vad_model
        except Exception as e:
            log_error("silero_vad_load_failed", error=str(e))
            log.warning(
                f"WHISPER: Silero VAD load failed ({e}); falling back to RMS-only gate"
            )
            _silero_vad_load_failed = True
            return None


def _silero_no_voice(audio, threshold: float = 0.35) -> bool:
    """用 Silero VAD v5 判斷整段音檔是否「不含人聲」。

    回傳：
      True  = 確認沒人聲（雜音 / 鍵盤 / 環境音）→ 呼叫端 short-circuit、不送 ASR
      False = 有人聲（或 VAD 不可用 / 出錯）→ 呼叫端正常進 ASR

    保守策略：任何 exception 都回 False（讓 ASR 正常跑、不要因 VAD 故障吃掉錄音）。
    """
    import numpy as np
    model = _ensure_silero_vad()
    if model is None:
        return False   # VAD 不可用、不 short-circuit
    try:
        from silero_vad import get_speech_timestamps
        import torch
        # Silero VAD v5 需要 1-D torch tensor、float32、16 kHz
        audio_tensor = torch.from_numpy(audio.astype(np.float32))
        # threshold 是「>= threshold 才算 speech」、不是「<」
        # min_speech_duration_ms=200：比 faster-whisper 內建 VAD（50ms）嚴格、
        # 避免極短噪聲（按鍵 click）被當人聲；min_silence 給 300ms 是 VAD 預設值。
        speech_segments = get_speech_timestamps(
            audio_tensor,
            model,
            threshold=threshold,
            sampling_rate=16_000,
            min_speech_duration_ms=200,
            min_silence_duration_ms=300,
            return_seconds=False,
        )
        # 沒任何 speech segment = 整段純雜音
        return len(speech_segments) == 0
    except Exception as e:
        log_error("silero_vad_inference_failed", error=str(e))
        return False   # 故障時不擋


def _is_dict_terms_hallucination(text: str, dict_terms: Optional[list[str]] = None) -> bool:
    """v2.16.3：偵測 Qwen3-ASR 把 context= 字典詞表整段「轉錄」回來的幻覺。

    觸發 pattern：tail audio 短（0.5-1.5s）+ 些底噪、模型認不出真實 voice、
    傾向把 system prompt 裡的 context 詞表 spit 出來當答案。

    判定條件：dict_terms ≥ 10 個、且輸出文字 ≥ 80% 字元能拼出自 dict_terms。
    保守門檻避免誤殺真實轉錄（例：user 真的講「潤飾」一個詞時不能殺）。
    """
    if not text or not dict_terms or len(dict_terms) < 10:
        return False
    # 拼湊出 dict 全部詞的「總字元集」（聯集去重）
    dict_char_set = set("".join(dict_terms))
    if not dict_char_set:
        return False
    # 算 text 裡有多少 char 屬於 dict_char_set
    text_clean = text.replace(" ", "").replace("\n", "").replace("　", "")
    if len(text_clean) < 15:
        return False  # 短文字不夠 sample、跳過避免誤殺
    in_dict_chars = sum(1 for c in text_clean if c in dict_char_set)
    coverage = in_dict_chars / len(text_clean)
    if coverage < 0.85:
        return False
    # 更精準：看是否多個 dict_terms 出現
    matched = sum(1 for t in dict_terms if t and len(t) >= 2 and t in text)
    return matched >= 5


def _is_hallucination(text: str) -> bool:
    """判斷文字是否包含已知 Whisper 幻覺片段或重複 pattern。

    雙層防護：
      1. 重複 pattern：Whisper 在純噪音/合成音訊卡住時會輸出
         「我先記我先記我先記...」這類同一短 unit 重複 ≥80% 的文字。
         這類即使長度 ≥20 字也要當幻覺。
      2. 短句關鍵字：只對「< 20 字」才用子字串檢查訂閱／字幕／作詞等
         常見幻覺。長句即使含這些關鍵字也視為正當（避免誤殺）。
    """
    if not text:
        return True   # 空字串也算幻覺
    # 去除空白與換行後進行子字串比對
    lower = text.lower().replace(" ", "").replace("\n", "")

    # 第一層：偵測重複 pattern（卡迴圈型幻覺）
    if _is_repetitive(lower):
        return True

    # 第二層：長句（≥ 20 字）免檢關鍵字 — 幾乎不會有 20 字以上的純幻覺
    if len(lower) >= 20:
        return False
    return any(h.lower().replace(" ", "") in lower for h in _HALLUCINATIONS)


def _is_repetitive(text: str) -> bool:
    """偵測 Whisper 卡迴圈產生的重複 pattern。

    判定條件：存在某長度 2–6 字的 unit，重複 ≥ 8 次且佔總文字 ≥ 80%。
    短文字（< 16 字）跳過此檢查（短重複是正常的口語特徵）。
    """
    n = len(text)
    if n < 16:
        return False
    for unit_len in range(2, 7):
        # 從前 5 個位置取 unit 試算（不需 O(n²) 全掃）
        for start in range(min(5, n - unit_len)):
            unit = text[start:start + unit_len]
            if not unit.strip():
                continue
            count = text.count(unit)
            # 重複 ≥ 8 次且 unit × count 佔總長 ≥ 80% → 卡迴圈
            if count >= 8 and count * unit_len / n >= 0.8:
                return True
    return False


# v2.19.0：n-gram 連續重複 dedupe（中度退化、整段砍嫌過火）
# _is_repetitive 抓 ≥ 8 次「整段都壞掉」的極端案例（整段砍）。
# 本函式抓 3-7 次的「**局部重複**」——只砍重複部分、其他句子保留。
#
# 實機證據（00:42:02 + 01:01:33）：
#   「全自動、自行、自行。」  ← 「自行」連 2 次，要砍成「全自動、自行。」
#   「麥克風測試 麥克風測試 麥克風測試」← 4-7 字 unit 重複 3+ 次
#
# 風險控管：unit_len 只試 2-5（不試 1，避免砍「好好好」這類強調）；
#         需要至少 3 次連續重複才觸發（2 次重複可能是合法強調）。
import re as _re_for_dedupe   # 避免汙染模組級命名空間
_DEDUPE_PATTERNS = [
    _re_for_dedupe.compile(
        r'(.{' + str(unit_len) + r'})((?:[、,，。\s]?\1){2,})'
    )
    for unit_len in (5, 4, 3, 2)   # 先試長 unit 避免子串先 match
]


def _dedupe_repetitive_ngrams(text: str) -> str:
    """砍掉「2-5 字 unit 連續重複 ≥ 3 次」的退化輸出，保留首次出現。

    例：
      「全自動、自行、自行、自行」→「全自動、自行」
      「測試 測試 測試 OK」→「測試 OK」
      「我我我」→ 不動（unit_len=1 不檢查）
      「我會、我會」→ 不動（只重複 2 次、未達 3 次）
    """
    if not text or len(text) < 6:
        return text
    out = text
    for pattern in _DEDUPE_PATTERNS:
        out = pattern.sub(r'\1', out)
    return out


def _normalize_volume(audio, target_peak: float = 0.9, min_peak: float = 0.5):
    """若音訊峰值過小（<min_peak）則線性放大到 target_peak。

    避免：使用者小聲說話時峰值過低 → VAD 誤判為靜音 → 整段被丟。
    防爆：放大倍率上限 12x，避免把底噪放大到爆音。
    """
    import numpy as np
    if audio.size == 0:
        return audio
    peak = float(np.abs(audio).max())
    # 峰值太小（純噪音）或已足夠大（正常音量）就不動
    if peak <= 0.005 or peak >= min_peak:
        return audio
    scale = min(target_peak / peak, 12.0)
    return (audio * scale).astype(audio.dtype)


# ── 資料類別 ──────────────────────────────────────────────────────────────────

@dataclass
class TranscriptionResult:
    """單次轉錄的完整結果，包含文字、語言、耗時與 segment 詳情。"""
    text:             str            # 轉錄文字（主要輸出）
    language:         str            # 偵測到的語言代碼（例如 "zh"）
    duration_seconds: float          # 音訊長度（秒）
    elapsed_seconds:  float          # 推論耗時（秒）
    segments:         list[dict] = field(default_factory=list)  # 逐 segment 資訊


# ── 主要類別 ──────────────────────────────────────────────────────────────────

class Transcriber:
    """執行緒安全的 Whisper 轉錄器，支援 lazy model loading。

    自動選擇最佳後端（MLX Metal GPU 或 CTranslate2 CPU int8）。
    模型在第一次 transcribe() 時載入；可透過 warmup() 提前預熱。

    個人字典整合：
        set_dictionary_terms() 更新術語列表，下次轉錄時自動注入
        到 Whisper 的 initial_prompt，提升專有名詞辨識準確度。
    """

    def __init__(self) -> None:
        self._model              = None              # faster-whisper 模型實例（CPU 後端）
        self._loaded_model_size: Optional[str] = None
        self._lock               = threading.Lock()  # 保護模型載入（防止並行載入兩次）
        self._transcription_lock = threading.Lock()  # 防止同時進行兩個轉錄任務
        self._dictionary_terms:  list[str] = []     # 個人字典術語列表
        # D2-S3（v2.9.0）：保護 _dictionary_terms 的 reassign 與 snapshot。
        # transcribe() 入口 acquire 一次取 snapshot 給整次推論用，避免
        # 推論跑時 user 改字典 + hot reload 觸發、TypeError fallback path
        # 第二次讀 `self._dictionary_terms` 拿到不一致的狀態。
        self._dictionary_lock    = threading.Lock()
        # v2.14.0 Qwen3-ASR Session（lazy load、第一次 transcribe qwen3 時建）
        # v2.15.0：加 _qwen3_loaded_model 追蹤目前載入哪個變體（0.6B / 1.7B）；
        #         切換時 _ensure_qwen3_session 會卸載舊的、避免 RAM 翻倍
        self._qwen3_session = None
        self._qwen3_loaded_model: Optional[str] = None
        # v2.19.0：可疑音檔保留開關（由 gui.py 套用 config 時呼叫 setter）。
        # 預設關閉、避免新使用者意外累積大量音檔。
        self._suspicious_capture_enabled: bool = False
        self._suspicious_max_mb: int = 200
        # v2.19.x：Silero VAD v5 設定（gui.py 套用 config 時透過 setter 推進來）
        self._silero_vad_enabled: bool = True
        self._silero_vad_threshold: float = 0.35

    def set_silero_vad(self, enabled: bool, threshold: float = 0.35) -> None:
        """v2.19.x：設定 Silero VAD v5 神經前置過濾。

        由 gui.py 在啟動 / 套用 config 時呼叫。VAD 在 RMS gate 之後執行、
        擋「有能量但非人聲」的雜音、抑制 ASR 幻覺。
        """
        self._silero_vad_enabled = bool(enabled)
        self._silero_vad_threshold = float(threshold)

    def set_suspicious_capture(self, enabled: bool, max_mb: int = 200) -> None:
        """v2.19.0：設定可疑音檔是否保留落地。

        由 gui.py 在啟動 / 套用 config 時呼叫；transcribe() 偵測到字典 dump /
        blacklist hit / dedupe triggered / RTF slow 時、會把原始音檔存到
        ~/.whisper_app/suspicious_audio/。
        """
        self._suspicious_capture_enabled = bool(enabled)
        self._suspicious_max_mb = int(max_mb) if max_mb else 200

    def set_dictionary_terms(self, terms: list[str]) -> None:
        """更新個人字典術語列表，下次 transcribe() 時注入 initial_prompt。

        Args:
            terms: 術語字串列表，例如 ["Kubernetes", "Whisper Pro"]。
        """
        with self._dictionary_lock:
            self._dictionary_terms = list(terms) if terms else []

    def _snapshot_dictionary_terms(self) -> list[str]:
        """D2-S3：lock 內取 _dictionary_terms 的淺拷貝快照。

        transcribe() 入口呼叫一次，整次推論都用同一份 snapshot，
        確保 MLX / CTranslate2 兩次 _build_initial_prompt（try + TypeError
        fallback）拿到的字典完全一致、不受 mid-flight set_dictionary_terms 影響。
        """
        with self._dictionary_lock:
            return list(self._dictionary_terms)

    def _build_initial_prompt(self, terms: Optional[list[str]] = None) -> str:
        """動態組合傳給 Whisper 的 initial_prompt（支援 prompts.py 熱重載）。

        每次轉錄前呼叫，確保 prompt_reloader 重載後的新 prompt 立即生效。

        Args:
            terms: 字典術語 snapshot。None 時 fallback 讀 `self._dictionary_terms`
                   （給 warmup 等不需要 snapshot 的呼叫路徑）。
                   transcribe 路徑必須傳 snapshot（D2-S3）。
        """
        if terms is None:
            terms = self._dictionary_terms
        try:
            return prompts.format_whisper_prompt(terms)
        except Exception:
            log_error("format_whisper_prompt_failed")
            return prompts.WHISPER_INITIAL_PROMPT   # 降級回基礎 prompt

    # ── 公開介面 ──────────────────────────────────────────────────────────────

    def transcribe(
        self,
        audio,                     # np.ndarray float32，16 kHz
        model_size: str = "base",
        language: Optional[str] = None,
        chinese_variant: str = "off",   # v2.14.0：Qwen3-ASR 配套（Whisper 不受影響）
    ) -> TranscriptionResult:
        """執行完整轉錄（阻塞呼叫，應在背景執行緒執行）。

        Args:
            audio:      float32 numpy 陣列，振幅 [-1.0, 1.0]，16 kHz 單聲道。
            model_size: Whisper 模型大小，例如 "large-v3-turbo"。
            language:   語言代碼（None 代表自動偵測）。

        Returns:
            TranscriptionResult 實例（保證不拋例外）。
        """
        import numpy as np

        # v2.19.0：transcribe 入口先抓 pipeline_id（每個 return path 都用到）
        pid = _safe_pipeline_id()

        # 空音訊：立即回傳錯誤訊息，不進行推論
        if audio is None or len(audio) == 0:
            log.error("WHISPER: Empty audio buffer received.")
            # v2.19.0：仍寫一筆 audit（便於統計使用者誤觸頻率）
            self._emit_audit(
                pid=pid, model_size=model_size, language=language,
                quality={"peak": 0.0, "rms": 0.0, "rms_first_500ms": 0.0,
                         "rms_last_500ms": 0.0, "clipping_ratio": 0.0,
                         "samples": 0, "duration_s": 0.0},
                duration=0.0, backend=BACKEND, result=None,
                dict_terms_snapshot=[], gate_short=False, gate_silent=False,
                error="empty_audio",
            )
            return TranscriptionResult(
                text="（沒有偵測到音訊，請確認麥克風是否正常運作）",
                language="", duration_seconds=0.0, elapsed_seconds=0.0,
            )

        # 確保 float32 格式，並正規化可能超出 [-1, 1] 的 int16 轉換結果
        audio = audio.astype(np.float32)
        if audio.max() > 1.0:
            audio = audio / 32768.0   # int16 最大值，還原到 [-1, 1]

        # v2.19.0：在 _normalize_volume 之前先抓 audio quality
        # ——之後抓會被加分（peak boost 後 rms 也會跟著拉高）、無法反映原始品質
        quality = _audio_quality(audio)

        # 音量自動正規化：小聲說話時避免 VAD 把整段判為靜音
        pre_peak = float(np.abs(audio).max()) if audio.size else 0.0
        audio = _normalize_volume(audio)
        post_peak = float(np.abs(audio).max()) if audio.size else 0.0
        if post_peak > pre_peak * 1.5:
            log.info(
                f"WHISPER: Volume normalized (peak {pre_peak:.3f} → {post_peak:.3f})"
            )

        duration = len(audio) / 16_000
        t0 = time.perf_counter()
        log.info(
            f"WHISPER: Starting transcription. Backend={BACKEND}, Model={model_size}, "
            f"Lang={language}, AudioDuration={duration:.2f}s"
        )
        _safe_pipeline_event(
            "transcribe_start", backend=BACKEND, model=model_size,
            duration_s=duration, language=language or "",
        )

        # v2.19.0：長度閘 — 過短音檔直接 short-circuit，不送 ASR。
        # 0.4s 以下通常是 hotkey 誤觸 / streaming 殘餘 chunk，送進去會觸發
        # 字典 dump、假新詞、重複病三類幻覺。
        if duration < _MIN_DURATION_S:
            log.info(
                f"WHISPER: Guard - Audio duration={duration:.2f}s < {_MIN_DURATION_S}s, "
                f"skipping inference (too short, likely accidental trigger)."
            )
            self._emit_audit(
                pid=pid, model_size=model_size, language=language,
                quality=quality, duration=duration, backend=BACKEND,
                result=None, dict_terms_snapshot=[],
                gate_short=True, gate_silent=False,
                elapsed=time.perf_counter() - t0,
            )
            return TranscriptionResult(
                text="（未偵測到語音內容）",
                language="", duration_seconds=duration,
                elapsed_seconds=time.perf_counter() - t0,
            )

        # 靜音防護：能量太低就直接跳過，避免觸發幻覺
        if _is_silence(audio):
            _peak = float(np.abs(audio).max()) if audio.size else 0.0
            _rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
            log.info(
                f"WHISPER: Guard - Audio peak={_peak:.6f} rms={_rms:.6f} "
                f"below threshold={_MIN_RMS} (duration={duration:.2f}s), skipping inference."
            )
            self._emit_audit(
                pid=pid, model_size=model_size, language=language,
                quality=quality, duration=duration, backend=BACKEND,
                result=None, dict_terms_snapshot=[],
                gate_short=False, gate_silent=True,
                elapsed=time.perf_counter() - t0,
            )
            return TranscriptionResult(
                text="（未偵測到語音內容）",
                language="", duration_seconds=duration,
                elapsed_seconds=time.perf_counter() - t0,
            )

        # v2.19.x Silero VAD gate：抓「有能量但非人聲」的雜音。
        # 放在 RMS gate 之後——RMS 砍掉真靜音、Silero 砍掉鍵盤/紙張/冷氣/背景音樂。
        # 兩層加起來把 ASR 暴露在「無真實 voice」的機率壓到接近 0。
        # 觸發時走 audit_log 的 silero_vad_block event（非 transcribe entry）；
        # transcribe entry 的 gates 不會出現這次、不衝突。
        if self._silero_vad_enabled and _silero_no_voice(audio, self._silero_vad_threshold):
            log.info(
                f"WHISPER: Guard - Silero VAD detected no speech "
                f"(threshold={self._silero_vad_threshold}, duration={duration:.2f}s), "
                f"skipping inference."
            )
            if _audit_log is not None:
                try:
                    _audit_log.write_event(
                        "silero_vad_block",
                        pid or "",
                        duration_s=float(duration),
                        threshold=float(self._silero_vad_threshold),
                    )
                except Exception:
                    pass
            return TranscriptionResult(
                text="（未偵測到語音內容）",
                language="", duration_seconds=duration,
                elapsed_seconds=time.perf_counter() - t0,
            )

        # D2-S3（v2.9.0）：入口 snapshot dictionary terms，整次推論用同一份。
        # 避免 transcribe 跑 ~3s 間 user 改字典 + hot reload → fallback 路徑
        # 拿到不一致字典，或 MLX → CTranslate fallback 時兩個後端用不同字典。
        dict_terms_snapshot = self._snapshot_dictionary_terms()

        # v2.19.0：實際走的 backend（fallback 後會改、影響 audit 與可疑音檔 metadata）
        actual_backend = "qwen3-asr" if _is_qwen3_model(model_size) else BACKEND

        # v2.14.0：Qwen3-ASR 走獨立 backend（與 Whisper 不同套件、無 CTranslate fallback）
        if _is_qwen3_model(model_size):
            if BACKEND != "mlx":
                # Qwen3-ASR 只有 MLX 實作、非 Apple Silicon 直接擋
                log.warning(
                    "WHISPER: Qwen3-ASR 需要 Apple Silicon MLX；"
                    f"當前 backend={BACKEND}、無法執行"
                )
                self._emit_audit(
                    pid=pid, model_size=model_size, language=language,
                    quality=quality, duration=duration, backend="qwen3-asr",
                    result=None, dict_terms_snapshot=dict_terms_snapshot,
                    gate_short=False, gate_silent=False,
                    elapsed=time.perf_counter() - t0,
                    error="qwen3_requires_mlx",
                )
                return TranscriptionResult(
                    text="（Qwen3-ASR 需要 Apple Silicon MLX、請於設定切回 large-v3-turbo）",
                    language="", duration_seconds=duration,
                    elapsed_seconds=time.perf_counter() - t0,
                )
            try:
                result = self._transcribe_qwen3(
                    audio, model_size, language, dict_terms_snapshot, chinese_variant
                )
            except Exception as exc:
                # Qwen3 無 fallback path、user 重試或切回 Whisper
                log_error("qwen3_backend_failed", model=model_size)
                log.error(
                    "WHISPER: Qwen3-ASR 失敗、無 fallback；"
                    "user 須重試或於設定切回 large-v3-turbo"
                )
                self._emit_audit(
                    pid=pid, model_size=model_size, language=language,
                    quality=quality, duration=duration, backend="qwen3-asr",
                    result=None, dict_terms_snapshot=dict_terms_snapshot,
                    gate_short=False, gate_silent=False,
                    elapsed=time.perf_counter() - t0,
                    error=f"qwen3_failed:{type(exc).__name__}",
                )
                return TranscriptionResult(
                    text="（Qwen3-ASR 轉錄失敗、請重試或於設定切回 large-v3-turbo）",
                    language="", duration_seconds=duration,
                    elapsed_seconds=time.perf_counter() - t0,
                )
        # 依後端分發推論（Whisper backend）
        elif BACKEND == "mlx":
            try:
                result = self._transcribe_mlx(audio, model_size, language, dict_terms_snapshot)
            except Exception as exc:
                # MLX 失敗時降級到 CPU，確保功能不中斷
                log_error("mlx_backend_failed", model=model_size)
                log.warning(
                    f"WHISPER: Falling back to CPU CTranslate2 backend "
                    f"(reason={type(exc).__name__})."
                )
                actual_backend = "ctranslate"   # fallback 後 backend 改變
                result = self._transcribe_ctranslate(audio, model_size, language, dict_terms_snapshot)
        else:
            result = self._transcribe_ctranslate(audio, model_size, language, dict_terms_snapshot)

        # 填入實際的音訊長度與推論耗時（兩個後端的 _transcribe_* 不填這兩欄）
        result.duration_seconds = duration
        result.elapsed_seconds  = time.perf_counter() - t0
        rtf = (result.elapsed_seconds / duration) if duration > 0 else 0
        log.info(f"WHISPER: Inference finished in {result.elapsed_seconds:.2f}s. RTF={rtf:.3f}")
        # Fix 12b（v2.13.0）：RTF > 1.0 視為「比實時還慢」、印警告方便日後 debug。
        # warmup 第一次例外（cold start RTF 可能 0.5-1.5）；之後若仍 > 1 通常表示
        # temperature fallback 被觸發或硬體有問題。
        if rtf > 1.0:
            log.warning(
                f"WHISPER: RTF={rtf:.2f} > 1.0 (slower than realtime)。"
                f"可能 fallback chain 觸發或硬體瓶頸；若反覆出現請回報。"
            )

        # v2.19.0：保留 ASR raw_text（未過任何 filter）用於 audit
        raw_text = result.text

        # 幻覺過濾：優先逐段過濾（保留合法段），沒 segments 資訊才退回整段檢查
        # v2.16.3：Qwen3-ASR 特有：context= 字典詞表整段被當成輸出（短 tail
        # 音檔 + 底噪觸發）。額外用 _is_dict_terms_hallucination 抓這 pattern。
        def _is_any_hallucination(text: str) -> bool:
            return _is_hallucination(text) or _is_dict_terms_hallucination(text, dict_terms_snapshot)

        # v2.19.0：追蹤過濾結果（給 audit / 可疑音檔判定）
        hallucination_removed = 0
        dict_dump_detected = False
        blacklist_hit = False

        if result.segments:
            kept = [
                s for s in result.segments
                if not _is_any_hallucination(s.get("text", "").strip())
            ]
            removed = len(result.segments) - len(kept)
            if removed > 0:
                hallucination_removed = removed
                log.warning(
                    f"WHISPER: Guard - Removed {removed}/{len(result.segments)} "
                    f"hallucination segment(s), kept {len(kept)}."
                )
                # 被砍的 segments 中若有任一是 dict-dump / blacklist → 旗標起來
                for s in result.segments:
                    if s not in kept:
                        t = s.get("text", "").strip()
                        if _is_dict_terms_hallucination(t, dict_terms_snapshot):
                            dict_dump_detected = True
                        if _is_hallucination(t):
                            blacklist_hit = True
                result.segments = kept
                if kept:
                    result.text = "".join(s["text"].strip() for s in kept).strip()
                else:
                    result.text = "（未偵測到語音內容）"
        elif _is_any_hallucination(result.text):
            dict_dump_detected = _is_dict_terms_hallucination(result.text, dict_terms_snapshot)
            blacklist_hit = _is_hallucination(result.text)
            log.warning(
                f"WHISPER: Guard - Detected hallucination "
                f"(dict-terms type={dict_dump_detected}): "
                f"'{result.text[:50]}...'"
            )
            result.text = "（未偵測到語音內容）"

        # v2.19.0：追蹤 dedupe 之後的文字（給 audit 「after_dedupe」欄位）
        # 注意：「after_dedupe」實際是「過幻覺 filter 之後、跑 dedupe 之前」的文字
        # 因為 dedupe 在 corrections 之後跑。命名上我們把「整個 post-asr 中間狀態」
        # 看作 dedupe＋corrections 一組、audit schema 用 after_dedupe / after_corrections
        # 表達跑完各步驟後的快照。
        after_hallucination = result.text

        # v2.13.0：規則式校正（< 1ms）— Whisper 系統性誤辨識（Cloud Code → Claude Code 等）
        # 走純字串替換，不需 LLM 推理。讀 ~/.whisper_app/dictionary.json 的 corrections 段。
        # 在 polish 之前套用 → 即使 polish 關閉、原文也已校正；polish 開啟也少做事。
        corrections_count = 0
        try:
            from dictionary import load_corrections, apply_corrections
            corrections = load_corrections()
            if corrections and result.text and not result.text.startswith("（"):
                before = result.text
                result.text = apply_corrections(result.text, corrections)
                if before != result.text:
                    corrections_count = len(corrections)
                    log.info(
                        f"WHISPER: applied {len(corrections)} corrections"
                        f" (len {len(before)}→{len(result.text)})"
                    )
        except Exception:
            log_error("apply_corrections_failed")

        after_corrections = result.text

        # v2.19.x：pinyin guard（拼音 fuzzy match 修中文 ASR 同音/近音誤辨）
        # 跟 apply_corrections 同層、always-on、polish 關閉也跑
        # 在 apply_corrections 之後、dedupe 之前：先 exact rule 修，再 fuzzy 補
        try:
            from dictionary import apply_pinyin_guard
            if result.text and not result.text.startswith("（"):
                before = result.text
                result.text, pinyin_fixes = apply_pinyin_guard(result.text)
                if pinyin_fixes:
                    log.info(
                        f"WHISPER: pinyin guard fixed {len(pinyin_fixes)} term(s): "
                        f"{pinyin_fixes[:5]}"
                    )
                    # 寫 audit log（失敗 silent、不影響主流程）
                    try:
                        import audit_log
                        import pipeline_id as _pid_mod
                        audit_log.write_event(
                            "pinyin_guard_hit",
                            _pid_mod.get_current() or "",
                            fixes=[{"from": f, "to": t} for f, t in pinyin_fixes],
                        )
                    except Exception:
                        pass
        except Exception:
            log_error("apply_pinyin_guard_failed")

        # v2.19.0：n-gram 連續重複砍除（局部 dedupe）
        # 在 corrections 之後跑——corrections 改完詞才測重複；否則「Cloud Cloud Cloud」
        # 會先被砍成「Cloud」、apply_corrections 才有機會修「Cloud → Claude」。
        dedupe_hit = False
        if result.text and not result.text.startswith("（"):
            before = result.text
            result.text = _dedupe_repetitive_ngrams(result.text)
            if before != result.text:
                dedupe_hit = True
                log.info(
                    f"WHISPER: n-gram dedupe (len {len(before)}→{len(result.text)}): "
                    f"'{before[:60]}...' → '{result.text[:60]}...'"
                )

        # 記錄轉錄結果（前 100 字，方便未來 debug hallucination / 準確度）
        preview = result.text[:100].replace("\n", " ")
        log.info(f"WHISPER: Result (lang={result.language}) text='{preview}'")

        # v2.19.0：寫 audit JSONL（成功路徑、最完整）
        self._emit_audit(
            pid=pid, model_size=model_size, language=language,
            quality=quality, duration=duration, backend=actual_backend,
            result=result, dict_terms_snapshot=dict_terms_snapshot,
            gate_short=False, gate_silent=False,
            elapsed=result.elapsed_seconds,
            raw_text=raw_text, after_dedupe=after_hallucination,
            after_corrections=after_corrections,
            dedupe_hit=dedupe_hit, corrections_count=corrections_count,
            hallucination_segments_removed=hallucination_removed,
            dict_dump_detected=dict_dump_detected,
        )

        # v2.19.0：可疑音檔保留（在 audit 寫完後才做、避免兩者互相干擾）。
        # 觸發條件四選一：dict-dump / blacklist hit / dedupe triggered / RTF > 1.5。
        self._maybe_save_suspicious_audio(
            audio=audio, pid=pid, model_size=model_size,
            duration=duration, rtf=rtf, quality=quality,
            dict_dump=dict_dump_detected,
            blacklist_hit=blacklist_hit,
            dedupe_hit=dedupe_hit,
        )

        return result

    # ── v2.19.0：observation helper ────────────────────────────────────────────

    def _emit_audit(
        self, *, pid: str, model_size: str, language: Optional[str],
        quality: dict, duration: float, backend: str,
        result: Optional[TranscriptionResult],
        dict_terms_snapshot: list[str],
        gate_short: bool, gate_silent: bool,
        elapsed: float = 0.0,
        raw_text: str = "",
        after_dedupe: str = "",
        after_corrections: str = "",
        dedupe_hit: bool = False,
        corrections_count: int = 0,
        hallucination_segments_removed: int = 0,
        dict_dump_detected: bool = False,
        error: Optional[str] = None,
    ) -> None:
        """組 audit entry + 寫 JSONL；任何錯誤都吞掉、絕不讓 transcribe() 失敗。

        所有 numpy float 都已用 float() 包裝、可安全 JSON 序列化。
        """
        if _audit_log is None:
            return  # 模組未就緒（Agent N 並行開發中）
        try:
            final_text = result.text if result else ""
            result_lang = result.language if result else (language or "")
            segments_count = len(result.segments) if result else 0
            rtf_val = (elapsed / duration) if duration > 0 else 0.0

            # 字典效益：哪些 terms 出現在 final_text 裡（取前 30 避免 entry 過大）
            terms_in_text: list[str] = []
            if dict_terms_snapshot and final_text:
                terms_in_text = [
                    t for t in dict_terms_snapshot
                    if t and len(t) >= 2 and t in final_text
                ][:30]

            entry = {
                "type": "transcribe",
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "pipeline_id": pid or "",
                "audio": {
                    "duration_s": float(quality.get("duration_s", duration)),
                    "peak": float(quality.get("peak", 0.0)),
                    "rms": float(quality.get("rms", 0.0)),
                    "rms_first_500ms": float(quality.get("rms_first_500ms", 0.0)),
                    "rms_last_500ms": float(quality.get("rms_last_500ms", 0.0)),
                    "clipping_ratio": float(quality.get("clipping_ratio", 0.0)),
                    "samples": int(quality.get("samples", 0)),
                },
                "backend": backend,
                "model": model_size,
                "language": result_lang or language or "",
                "elapsed_s": float(elapsed),
                "rtf": float(rtf_val),
                "gates": {
                    "duration_short": bool(gate_short),
                    "rms_silent": bool(gate_silent),
                    # v2.19.x：Silero VAD 觸發時 transcribe() 直接 return、不會走到
                    # _emit_audit，所以這個欄位在 transcribe entry 裡永遠是 False；
                    # 真實 VAD block 統計請看 audit_log 的 silero_vad_block events。
                    # 留欄位讓未來 schema 完整（避免 jq filter 寫 .gates.silero_no_voice 找不到）。
                    "silero_no_voice": False,
                    "is_warmup": False,   # 標記給未來 warmup 路徑；此 emit 點固定 False
                },
                "raw_text": raw_text or final_text or "",
                "after_dedupe": after_dedupe or final_text or "",
                "after_corrections": after_corrections or final_text or "",
                "final_text": final_text or "",
                "post_filters": {
                    "dedupe_hit": bool(dedupe_hit),
                    "corrections_count": int(corrections_count),
                    "hallucination_segments_removed": int(hallucination_segments_removed),
                    "dict_dump_detected": bool(dict_dump_detected),
                },
                "segments_count": int(segments_count),
                "dict_terms_count": int(len(dict_terms_snapshot)),
                "dict_effectiveness": {
                    "terms_in_text": terms_in_text,
                    "corrections_hit": {},   # 未來填：term → 命中次數
                },
                "error": error,
            }
            _audit_log.write_transcribe(entry)
        except Exception:
            # 觀測點不能影響主流程；只在 logger 留痕、不 re-raise
            try:
                log_error("audit_emit_failed")
            except Exception:
                pass

    def _maybe_save_suspicious_audio(
        self, *, audio, pid: str, model_size: str,
        duration: float, rtf: float, quality: dict,
        dict_dump: bool, blacklist_hit: bool, dedupe_hit: bool,
    ) -> None:
        """v2.19.0：四條件任一觸發 → 落地音檔給日後檢視。

        條件：
          • dict_dump：Qwen3-ASR 字典詞表 dump（_is_dict_terms_hallucination 命中）
          • blacklist_hit：傳統黑名單關鍵字命中（_is_hallucination）
          • dedupe_hit：n-gram 連續重複 dedupe 觸發
          • RTF > 1.5：推論顯著慢於實時、可能 fallback chain 被觸發

        設定關閉時整段 noop（save_suspicious_audio 收到 enabled=False 內部處理）。
        """
        if _audit_log is None:
            return
        # 找出觸發原因（多條件命中 → 取最具診斷價值的一個）
        reason: Optional[str] = None
        if dict_dump:
            reason = "dict_dump"
        elif blacklist_hit:
            reason = "blacklist_hit"
        elif dedupe_hit:
            reason = "dedupe_triggered"
        elif rtf > 1.5:
            reason = "rtf_slow"
        if reason is None:
            return  # 沒可疑事件、不保留

        try:
            metadata = {
                "pipeline_id": pid or "",
                "model": model_size,
                "duration_s": float(duration),
                "rtf": float(rtf),
                "audio_peak": float(quality.get("peak", 0.0)),
                "audio_rms": float(quality.get("rms", 0.0)),
                "reasons": {
                    "dict_dump": bool(dict_dump),
                    "blacklist_hit": bool(blacklist_hit),
                    "dedupe_triggered": bool(dedupe_hit),
                    "rtf_slow": bool(rtf > 1.5),
                },
            }
            _audit_log.save_suspicious_audio(
                audio, 16000, pid, reason, metadata,
                enabled=self._suspicious_capture_enabled,
                max_size_mb=self._suspicious_max_mb,
            )
        except Exception:
            try:
                log_error("save_suspicious_audio_failed", reason=reason)
            except Exception:
                pass

    def transcribe_fast(
        self,
        audio,
        language: Optional[str] = None,
    ) -> TranscriptionResult:
        """低延遲轉錄，針對短片段（≤ 8 秒）最佳化。

        使用 beam_size=1（貪婪解碼）和較小模型，速度優先於精準度。
        目前主要保留給中段串流（streaming transcription）使用。

        Args:
            audio:    float32 numpy 陣列，16 kHz 單聲道。
            language: 語言代碼（None 代表自動偵測）。
        """
        import numpy as np

        if audio is None or len(audio) == 0:
            return TranscriptionResult(
                text="（沒有偵測到音訊，請確認麥克風是否正常運作）",
                language="", duration_seconds=0.0, elapsed_seconds=0.0,
            )

        audio = audio.astype(np.float32)
        if audio.max() > 1.0:
            audio = audio / 32768.0

        # 音量正規化 — 和 transcribe() 同策略
        audio = _normalize_volume(audio)

        duration = len(audio) / 16_000
        t0 = time.perf_counter()

        # v2.19.0：長度閘 — 跟 transcribe() 同策略
        if duration < _MIN_DURATION_S:
            return TranscriptionResult(
                text="（未偵測到語音內容）", language="",
                duration_seconds=duration, elapsed_seconds=0.0,
            )

        # 靜音防護
        if _is_silence(audio):
            return TranscriptionResult(
                text="（未偵測到語音內容）", language="",
                duration_seconds=duration, elapsed_seconds=0.0,
            )

        # v2.19.x Silero VAD gate：streaming 路徑同樣受神經 VAD 保護。
        # 雖然 streaming chunk 一般較短、被當人聲機率高，但鍵盤敲擊 / 拍打麥克風
        # 同樣會 leak 進來、抑制這類雜音同樣重要。
        if self._silero_vad_enabled and _silero_no_voice(audio, self._silero_vad_threshold):
            return TranscriptionResult(
                text="（未偵測到語音內容）", language="",
                duration_seconds=duration, elapsed_seconds=time.perf_counter() - t0,
            )

        # 用 "small" 模型做快速推論（在此場景下 small 的速度/品質比最佳）
        model = self._ensure_model("small")

        # D2-S3：snapshot dict 鎖在當下狀態（streaming 路徑同樣保護）
        initial_prompt = self._build_initial_prompt(self._snapshot_dictionary_terms())

        with self._transcription_lock:
            segments_iter, info = model.transcribe(
                audio,
                language=language,
                beam_size=1,                              # 貪婪解碼，最快
                initial_prompt=initial_prompt,
                condition_on_previous_text=False,         # 不依賴前文，避免幻覺傳播
                vad_filter=True,                          # 內建 VAD 過濾靜音
                # 寬鬆 VAD：threshold 降低讓小聲也偵測到；silence 拉長避免句中停頓
                # 被切段；min_speech 降到 50ms 保留短詞。
                vad_parameters={
                    "threshold":              0.3,
                    "min_silence_duration_ms": 800,
                    "min_speech_duration_ms":  50,
                },
                word_timestamps=False,   # 不需要詞級時間戳，省時間
                temperature=0,           # temperature=0 = 確定性輸出
            )
            segments = []
            full_text_parts = []
            for seg in segments_iter:
                segments.append({"start": seg.start, "end": seg.end, "text": seg.text})
                full_text_parts.append(seg.text.strip())

        text = "".join(full_text_parts).strip()
        if not text or _is_hallucination(text):
            text = "（未偵測到語音內容）"

        return TranscriptionResult(
            text=text,
            language=info.language,
            duration_seconds=duration,
            elapsed_seconds=time.perf_counter() - t0,
            segments=segments,
        )

    def warmup(self, model_size: str) -> None:
        """預先載入模型並跑一次靜音片段，暖機完成後第一次錄音不會卡頓。

        Args:
            model_size: 要預熱的模型大小。
        """
        # v2.14.0：Qwen3-ASR 走獨立 backend
        if _is_qwen3_model(model_size):
            self._warmup_qwen3(model_size)
        elif BACKEND == "mlx":
            self._warmup_mlx(model_size)
        else:
            self._warmup_ctranslate(model_size)

    def unload(self) -> None:
        """釋放模型記憶體（CPU 與 MLX）。App 切換到省電模式時可呼叫。"""
        with self._lock:
            if self._model is not None:
                del self._model
                self._model = None
            self._loaded_model_size = None
            # v2.14.0：釋放 Qwen3-ASR Session（模型權重在 Session 內）
            if self._qwen3_session is not None:
                del self._qwen3_session
                self._qwen3_session = None
                self._qwen3_loaded_model = None   # v2.15.0：清變體追蹤

        # MLX 後端需要手動清除 Metal GPU 快取
        if BACKEND == "mlx":
            try:
                import mlx.core as mx
                mx.metal.clear_cache()
            except Exception:
                pass

        gc.collect()   # 強制觸發 Python GC，確保記憶體立即釋放

    @staticmethod
    def active_backend() -> str:
        """回傳目前使用的後端名稱（"mlx" 或 "ctranslate"）。"""
        return BACKEND

    # ── MLX 後端（Apple Silicon Metal GPU）────────────────────────────────────

    def _warmup_mlx(self, model_size: str) -> None:
        """MLX 後端暖機：載入模型並跑一次 1.5s 低振幅 sine wave。

        Fix 11 / 2026-05-23 — 為什麼不是 silence：
          原本跑 0.2s zeros，但 mlx_whisper 內建 VAD 偵測到靜音會 fast-skip，
          Metal shader（encoder / decoder / cross-attention 等）**完全沒被編譯**，
          第一次正式錄音仍是 cold path。曾觀察：warmup 完成後立刻錄 1.6s 音檔，
          推論花 12 秒（RTF 7.5）；之後同樣 size 的音檔降到 0.2 秒（RTF 0.05）。
          診斷依據是 5/22 一整天 RTF 都 0.06-0.13，但 5/23 重啟 App 後第一次
          推論 RTF 7.5。

        修法：用 1.5s 440Hz sine wave at amplitude 0.1
          • 通過內建 VAD（有能量 → 不被 fast-skip）
          • 強制走完整 encoder + decoder pipeline → Metal shader 全部編譯
          • 加 beam_size=1 / temperature=0 / condition_on_previous_text=False
            斷掉 temperature fallback chain（避免 sine wave 觸發幻覺重試把
            warmup 自己拖到 10 秒以上）

        Warmup 耗時：~2s → ~5s，但之後第一次正式錄音 RTF 可以維持 ~0.06。
        """
        import mlx_whisper
        import numpy as np
        hf_repo = _MLX_MODEL_MAP.get(model_size, _MLX_MODEL_MAP["large-v3-turbo"])
        sr = 16_000
        t_axis = np.arange(int(sr * 1.5), dtype=np.float32) / sr
        audio  = (0.1 * np.sin(2 * np.pi * 440.0 * t_axis)).astype(np.float32)
        with self._transcription_lock:
            try:
                # Fix 14 / 2026-05-23：mlx_whisper 偵測到 beam_size kwarg 就試圖走
                # beam-search path（即使值是 1），但 mlx_whisper 該 path 還沒實作
                # → NotImplementedError("Beam search decoder is not yet implemented")。
                # 只用 temperature=0 + condition_on_previous_text=False 切斷 fallback chain；
                # 不要傳 beam_size。greedy decode 是 mlx_whisper 預設行為。
                try:
                    mlx_whisper.transcribe(
                        audio,
                        path_or_hf_repo=hf_repo,
                        language="zh",
                        verbose=False,
                        temperature=0,
                        condition_on_previous_text=False,
                    )
                except TypeError:
                    mlx_whisper.transcribe(
                        audio,
                        path_or_hf_repo=hf_repo,
                        language="zh",
                        verbose=False,
                    )
                log.info(f"WHISPER: Warmup MLX complete (repo={hf_repo}, sine-wave 1.5s)")
            except Exception:
                log_error("warmup_mlx_failed", model=model_size, repo=hf_repo)

    def _transcribe_mlx(
        self,
        audio,
        model_size: str,
        language: Optional[str],
        dict_terms: Optional[list[str]] = None,   # D2-S3：dict snapshot
    ) -> TranscriptionResult:
        """使用 mlx-whisper 進行推論（Metal GPU 加速）。

        Fix 12 / 2026-05-23 — 短音檔快速路徑（< 3s）原版
        Fix 12b / 2026-05-24 — **fast-path 邊界從 3s 拉到 30s**：
          實機回報 4.1s 重複內容（「麥克風測試 麥克風測試 麥克風測試」）跑 8.46s
          RTF=2.064。根因：mlx_whisper 預設 temperature fallback chain =
          (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)。重複內容讓模型認為「quality bad」
          一路 fallback 到 1.0、每段重跑 decode → RTF 數倍放大。

          修法：所有 < 30s 音檔（涵蓋日常 99% 場景）強制 temperature=0 +
          condition_on_previous_text=False。對中英混講 / 重複話語 / 短句的
          辨識率影響微乎其微（語意已由音訊內容決定），但 RTF 從 ~2 降到 ~0.1-0.3。
          長音檔（≥ 30s）才保留預設 fallback chain（quality 優先、上下文有用）。
        """
        import mlx_whisper

        hf_repo  = _MLX_MODEL_MAP.get(model_size, _MLX_MODEL_MAP["large-v3-turbo"])
        duration = len(audio) / 16_000
        # Fix 12b（v2.13.0）：邊界從 3s 拉到 30s
        # Fix 12c（v2.13.0）：邊界再拉到 60s，覆蓋幾乎所有日常錄音場景
        # 實機看 log：30.40s 走 fallback chain 跑 7.86s (RTF=0.259)，比 fast path 慢
        # 5 倍；user 邊講邊累積場景常落在 30-60s 區間。60s 以上才走品質優先 path
        # （此時 conversational context 開始有用、worth the time）。
        is_short = duration < 60.0

        # 短/中音檔（< 30s）走 fast path；長音檔維持 mlx_whisper 預設（quality 優先）
        # Fix 14 / 2026-05-23：不要傳 beam_size（mlx_whisper 偵測 kwarg 就試
        # beam-search path、該 path 還沒實作 → NotImplementedError）。
        # v2.19.0：動態依 _MLX_WHISPER_KWARGS 組 kwargs，只傳這版 mlx_whisper
        #   實際支援的參數——避免新加的 hallucination_silence_threshold 不被支援時，
        #   把舊有的 temperature / condition_on_previous_text 連坐 fallback 掉。
        short_kwargs: dict = {}
        if is_short:
            # 既有抗幻覺基底（v2.13.0 起）
            if "temperature" in _MLX_WHISPER_KWARGS:
                short_kwargs["temperature"] = 0
            if "condition_on_previous_text" in _MLX_WHISPER_KWARGS:
                short_kwargs["condition_on_previous_text"] = False
            # v2.19.0 新加（OpenAI 官方參數、靜音 ≥ 2 秒 decoder 停止編輯）
            if "hallucination_silence_threshold" in _MLX_WHISPER_KWARGS:
                short_kwargs["hallucination_silence_threshold"] = 2.0

        # v2.19.0：印一行決策 log，方便 debug 為什麼 RTF 突然變慢
        if is_short:
            log.info(
                f"WHISPER: mlx fast-path (duration={duration:.2f}s, "
                f"kwargs={sorted(short_kwargs.keys())})"
            )
        else:
            log.info(f"WHISPER: mlx default-path (duration={duration:.2f}s)")

        # D2-S3：build initial_prompt 用入口 snapshot；try + TypeError fallback
        # 都用同一份 prompt string、保證一致
        initial_prompt = self._build_initial_prompt(dict_terms)

        with self._transcription_lock:
            try:
                try:
                    output = mlx_whisper.transcribe(
                        audio,
                        path_or_hf_repo=hf_repo,
                        language=language,
                        initial_prompt=initial_prompt,
                        verbose=False,
                        **short_kwargs,
                    )
                except TypeError:
                    # 安全降級：mlx_whisper 版本不支援這些 kwargs 時用預設參數
                    if is_short:
                        log.warning(
                            "WHISPER: short-audio fast-path kwargs unsupported, "
                            "falling back to default mlx_whisper params"
                        )
                    output = mlx_whisper.transcribe(
                        audio,
                        path_or_hf_repo=hf_repo,
                        language=language,
                        initial_prompt=initial_prompt,
                        verbose=False,
                    )
            except Exception as e:
                raise RuntimeError(f"MLX transcription failed: {e}")

        # 從 mlx_whisper 的輸出格式中提取 segments 與文字
        segments_raw = output.get("segments", [])
        segments     = [
            {"start": s["start"], "end": s["end"], "text": s["text"]}
            for s in segments_raw
        ]
        full_text_parts = [s["text"].strip() for s in segments_raw]
        text            = "".join(full_text_parts).strip()
        detected_lang   = output.get("language", "") or ""

        return TranscriptionResult(
            text=text,
            language=detected_lang,
            duration_seconds=0.0,   # 由呼叫端 transcribe() 填入
            elapsed_seconds=0.0,
            segments=segments,
        )

    # ── CTranslate2 後端（CPU int8）──────────────────────────────────────────

    def _warmup_ctranslate(self, model_size: str) -> None:
        """CTranslate2 後端暖機：確保模型載入，並跑一次靜音。"""
        import numpy as np
        try:
            model   = self._ensure_model(model_size)
            silence = np.zeros(3200, dtype=np.float32)   # 0.2 秒靜音
            with self._transcription_lock:
                segs, _ = model.transcribe(silence, beam_size=1, temperature=0)
                list(segs)   # 必須迭代才會真正執行推論
            log.info(f"WHISPER: Warmup CTranslate complete (model={model_size})")
        except Exception:
            log_error("warmup_ctranslate_failed", model=model_size)

    def _transcribe_ctranslate(
        self,
        audio,
        model_size: str,
        language: Optional[str],
        dict_terms: Optional[list[str]] = None,   # D2-S3：dict snapshot
    ) -> TranscriptionResult:
        """使用 faster-whisper（CTranslate2）進行 CPU int8 推論。"""
        model = self._ensure_model(model_size)

        # 長音訊（≥ 30s）開啟 condition_on_previous_text，讓模型利用前文
        # 上下文，減少長錄音的斷裂與不一致。短音訊維持關閉，避免 bad
        # start 造成 cascading error。
        long_audio = len(audio) >= 30 * 16_000

        # D2-S3：snapshot dictionary terms → initial_prompt 鎖在當下狀態
        initial_prompt = self._build_initial_prompt(dict_terms)

        with self._transcription_lock:
            segments_iter, info = model.transcribe(
                audio,
                language=language,
                beam_size=5,               # beam 越大越準，但越慢
                repetition_penalty=1.1,    # 輕微懲罰重複，抑制幻覺循環
                initial_prompt=initial_prompt,
                condition_on_previous_text=long_audio,   # 長音訊才依賴前文
                vad_filter=True,           # 內建 VAD，過濾靜音段落
                # 寬鬆 VAD — 避免漏字/漏段：
                #   threshold 0.5→0.3：更包容弱訊號
                #   silence 300→800ms：句中小停頓不切段
                #   speech 150→50ms：短詞（如「是」「對」）也保留
                vad_parameters={
                    "threshold":              0.3,
                    "min_silence_duration_ms": 800,
                    "min_speech_duration_ms":  50,
                },
                word_timestamps=False,
                temperature=0,
            )
            segments        = []
            full_text_parts = []
            for seg in segments_iter:
                segments.append({"start": seg.start, "end": seg.end, "text": seg.text})
                full_text_parts.append(seg.text.strip())

        text = "".join(full_text_parts).strip()

        return TranscriptionResult(
            text=text,
            language=info.language,
            duration_seconds=0.0,   # 由呼叫端 transcribe() 填入
            elapsed_seconds=0.0,
            segments=segments,
        )

    # ── v2.14.0 Qwen3-ASR backend（MLX-only、無 CPU fallback）─────────────────

    def _ensure_qwen3_session(self, model_size: str):
        """Lazy-load mlx-qwen3-asr Session、快取在 self._qwen3_session。

        v2.15.0：支援 0.6B / 1.7B 兩個變體。切換時卸載舊 Session、清 Metal
        cache、重 GC、再載新的——避免兩個 session 同時佔 RAM（1.7B 是 3.4 GB）。

        Session 持有模型 weights + tokenizer，第一次建構時會下載模型（cache 在
        ~/.cache/huggingface/hub/）。後續同 model_size 重複用、避免 reload 成本。

        執行緒安全：用 self._lock（跟 Whisper model 共用）保護建構。
        """
        hf_repo = _qwen3_hf_repo(model_size)
        with self._lock:
            # 已載且同變體 → 直接回快取
            if self._qwen3_session is not None and self._qwen3_loaded_model == model_size:
                return self._qwen3_session
            # 變體不同 → 卸載舊的
            if self._qwen3_session is not None:
                old = self._qwen3_loaded_model
                log.info(f"WHISPER: Unloading Qwen3-ASR '{old}' to switch to '{model_size}'...")
                try:
                    del self._qwen3_session
                except Exception:
                    log_error("qwen3_session_del_failed", model=old)
                self._qwen3_session = None
                self._qwen3_loaded_model = None
                # 清 Metal GPU cache（避免 1.7B 載入失敗 OOM）
                try:
                    import mlx.core as mx
                    mx.metal.clear_cache()
                except Exception:
                    pass
                gc.collect()

            t_start = time.perf_counter()
            # v2.19.0：顯式標記為 cold-load、方便 grep / 對應 audit 上的高 RTF 異常
            log.info(f"WHISPER: qwen3 cold-load (model={model_size}, repo={hf_repo})")
            try:
                from mlx_qwen3_asr import Session
                self._qwen3_session = Session(model=hf_repo)
                self._qwen3_loaded_model = model_size
                elapsed = time.perf_counter() - t_start
                log.info(f"WHISPER: Qwen3-ASR session loaded in {elapsed:.2f}s.")
                return self._qwen3_session
            except Exception as e:
                log_error("qwen3_session_load_failed", model=hf_repo)
                raise RuntimeError(f"無法載入 Qwen3-ASR session ({hf_repo}): {e}")

    def _warmup_qwen3(self, model_size: str) -> None:
        """Qwen3-ASR 暖機：載入 session + 跑 1.5s sine wave 編譯 Metal shader。

        類似 _warmup_mlx 的策略——用 sine wave 而非 silence、強制走完整
        encoder + decoder pipeline 把 Metal shader 全部編譯起來。
        """
        import numpy as np
        sr = 16_000
        t_axis = np.arange(int(sr * 1.5), dtype=np.float32) / sr
        audio = (0.1 * np.sin(2 * np.pi * 440.0 * t_axis)).astype(np.float32)
        hf_repo = _qwen3_hf_repo(model_size)
        with self._transcription_lock:
            try:
                session = self._ensure_qwen3_session(model_size)
                session.transcribe(audio, language="Chinese", context="")
                log.info(f"WHISPER: Warmup Qwen3-ASR complete ({model_size}, sine-wave 1.5s)")
            except Exception:
                log_error("warmup_qwen3_failed", model=hf_repo)

    def _transcribe_qwen3(
        self,
        audio,
        model_size: str,
        language: Optional[str],
        dict_terms: Optional[list[str]] = None,
        chinese_variant: str = "off",
    ) -> TranscriptionResult:
        """使用 Qwen3-ASR-0.6B 進行 ASR 推論（MLX GPU + Neural Engine）。

        差異於 Whisper：
          • biasing 用 `context=` 參數（真正的 Qwen LLM system prompt、強度高）
          • 預設輸出簡體；config.chinese_variant 控制 opencc 後處理
          • language 接受 "zh"/"zh-tw"/None；內部 normalize 成 "Chinese"

        D2-S3 一致：dict_terms 是入口 snapshot、整次推論用同一份。
        """
        # 字典術語 → Qwen3-ASR context（空白分隔的純詞表）
        context_str = prompts.format_qwen3_context(dict_terms)

        # language normalize：Qwen3-ASR 用「Chinese」/「English」等英文 label
        # Whisper 用「zh」/「en」等代碼。對應一下、None 保留（自動偵測）
        qwen3_lang = None
        if language:
            lang_lower = language.lower()
            if lang_lower in ("zh", "zh-tw", "zh-cn", "cmn", "mandarin"):
                qwen3_lang = "Chinese"
            elif lang_lower in ("en", "english"):
                qwen3_lang = "English"
            elif lang_lower in ("ja", "japanese"):
                qwen3_lang = "Japanese"
            elif lang_lower in ("ko", "korean"):
                qwen3_lang = "Korean"
            else:
                qwen3_lang = language  # 直接 pass 過去、不認得會 fallback 到 auto

        with self._transcription_lock:
            session = self._ensure_qwen3_session(model_size)
            try:
                output = session.transcribe(
                    audio,
                    language=qwen3_lang,
                    context=context_str,
                    return_timestamps=True,   # 要 segments 給 hallucination filter 用
                )
            except Exception as e:
                raise RuntimeError(f"Qwen3-ASR transcription failed: {e}")

        # 從 Qwen3-ASR output 提取 text + segments + language
        raw_text = (output.text or "").strip()
        detected_lang = (output.language or "") if hasattr(output, "language") else ""

        # opencc 簡 → 繁（只在中文場景套用、純詞 segments 也要轉）
        is_chinese = detected_lang and "chinese" in detected_lang.lower()
        # 即使 detected_lang 不明、只要 chinese_variant != off 就嘗試套
        # （Qwen3-ASR 對 detected_lang 的回填不一定完整）
        if chinese_variant and chinese_variant != "off":
            raw_text = _apply_opencc(raw_text, chinese_variant)

        # segments：mlx-qwen3-asr 回的 segments 結構可能不同、用 getattr 安全取
        segments: list[dict] = []
        try:
            for seg in (output.segments or []):
                seg_text = getattr(seg, "text", None) or seg.get("text", "")
                seg_start = getattr(seg, "start", None)
                if seg_start is None and isinstance(seg, dict):
                    seg_start = seg.get("start", 0.0)
                seg_end = getattr(seg, "end", None)
                if seg_end is None and isinstance(seg, dict):
                    seg_end = seg.get("end", 0.0)
                # segments text 也套 opencc 保持一致
                if chinese_variant and chinese_variant != "off":
                    seg_text = _apply_opencc(seg_text, chinese_variant)
                segments.append({
                    "start": float(seg_start or 0.0),
                    "end":   float(seg_end or 0.0),
                    "text":  seg_text,
                })
        except Exception:
            log_error("qwen3_segments_parse_failed")
            segments = []

        return TranscriptionResult(
            text=raw_text,
            language=detected_lang or (qwen3_lang or ""),
            duration_seconds=0.0,   # 由呼叫端 transcribe() 填入
            elapsed_seconds=0.0,
            segments=segments,
        )

    # ── 私有方法 ──────────────────────────────────────────────────────────────

    def _ensure_model(self, model_size: str):
        """Lazy-load faster-whisper 模型（CPU 後端）。

        若已載入且相同 model_size，直接回傳快取實例。
        model_size 不同時，先釋放舊模型記憶體再載入新的。

        Args:
            model_size: 模型大小字串（例如 "large-v3-turbo"）。

        Returns:
            WhisperModel 實例。

        Raises:
            RuntimeError: 模型載入失敗時。
        """
        with self._lock:
            # 若已有相同大小的模型就直接回傳
            if self._model is None or self._loaded_model_size != model_size:
                t_start = time.perf_counter()

                # 釋放舊模型記憶體
                if self._model is not None:
                    log.info(f"WHISPER: Unloading old model '{self._loaded_model_size}'...")
                    try:
                        del self._model
                    except Exception:
                        log_error("whisper_model_del_failed")
                    gc.collect()

                from faster_whisper import WhisperModel
                # CPU 執行緒數：不超過 4，避免與 UI / pynput 執行緒競爭
                cpu_threads = min(4, os.cpu_count() or 4)
                log.info(f"WHISPER: Loading CPU model '{model_size}' with {cpu_threads} threads...")

                try:
                    self._model = WhisperModel(
                        model_size,
                        device="cpu",
                        compute_type="int8",      # int8 量化，省記憶體且速度快
                        cpu_threads=cpu_threads,
                        num_workers=1,            # 單 worker，避免 thread pool 過多
                    )
                    self._loaded_model_size = model_size
                    elapsed = time.perf_counter() - t_start
                    log.info(f"WHISPER: Model '{model_size}' loaded successfully in {elapsed:.2f}s.")
                except Exception as e:
                    log_error("whisper_model_load_failed", model=model_size)
                    raise RuntimeError(f"無法載入 Whisper 模型 {model_size}: {e}")

            return self._model
