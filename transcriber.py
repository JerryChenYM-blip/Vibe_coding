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

# faster-whisper 模型名稱 → mlx-community HuggingFace 倉庫 ID 的映射
_MLX_MODEL_MAP: dict[str, str] = {
    "tiny":           "mlx-community/whisper-tiny",
    "base":           "mlx-community/whisper-base",
    "small":          "mlx-community/whisper-small-mlx-4bit",
    "medium":         "mlx-community/whisper-medium-mlx-4bit",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3":       "mlx-community/whisper-large-v3-mlx-4bit",
}


# ── 靜音偵測與幻覺防護 ────────────────────────────────────────────────────────

# 最小 RMS 閾值：低於此值視為靜音，跳過推論。
# 0.002 是實測下限：可抓「嗯」「好」等短促輕音；再低就會把真靜音也送進去引發幻覺。
_MIN_RMS = 0.002

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


def _is_hallucination(text: str) -> bool:
    """判斷文字是否包含已知 Whisper 幻覺片段。"""
    if not text:
        return True   # 空字串也算幻覺
    # 去除空白與換行後進行子字串比對
    lower = text.lower().replace(" ", "").replace("\n", "")
    return any(h.lower().replace(" ", "") in lower for h in _HALLUCINATIONS)


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

    def set_dictionary_terms(self, terms: list[str]) -> None:
        """更新個人字典術語列表，下次 transcribe() 時注入 initial_prompt。

        Args:
            terms: 術語字串列表，例如 ["Kubernetes", "Whisper Pro"]。
        """
        self._dictionary_terms = list(terms) if terms else []

    def _build_initial_prompt(self) -> str:
        """動態組合傳給 Whisper 的 initial_prompt（支援 prompts.py 熱重載）。

        每次轉錄前呼叫，確保 prompt_reloader 重載後的新 prompt 立即生效。
        """
        try:
            return prompts.format_whisper_prompt(self._dictionary_terms)
        except Exception as e:
            print(f"WHISPER: format_whisper_prompt failed (fallback): {e}")
            return prompts.WHISPER_INITIAL_PROMPT   # 降級回基礎 prompt

    # ── 公開介面 ──────────────────────────────────────────────────────────────

    def transcribe(
        self,
        audio,                     # np.ndarray float32，16 kHz
        model_size: str = "base",
        language: Optional[str] = None,
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

        # 空音訊：立即回傳錯誤訊息，不進行推論
        if audio is None or len(audio) == 0:
            print("WHISPER: ERROR - Empty audio buffer received.")
            return TranscriptionResult(
                text="（沒有偵測到音訊，請確認麥克風是否正常運作）",
                language="", duration_seconds=0.0, elapsed_seconds=0.0,
            )

        # 確保 float32 格式，並正規化可能超出 [-1, 1] 的 int16 轉換結果
        audio = audio.astype(np.float32)
        if audio.max() > 1.0:
            audio = audio / 32768.0   # int16 最大值，還原到 [-1, 1]

        duration = len(audio) / 16_000
        t0 = time.perf_counter()
        print(f"WHISPER: Starting transcription. Backend={BACKEND}, Model={model_size}, Lang={language}, AudioDuration={duration:.2f}s")

        # 靜音防護：能量太低就直接跳過，避免觸發幻覺
        if _is_silence(audio):
            print("WHISPER: Guard - Audio level below threshold, skipping inference.")
            return TranscriptionResult(
                text="（未偵測到語音內容）",
                language="", duration_seconds=duration,
                elapsed_seconds=time.perf_counter() - t0,
            )

        # 依後端分發推論
        if BACKEND == "mlx":
            try:
                result = self._transcribe_mlx(audio, model_size, language)
            except Exception as e:
                # MLX 失敗時降級到 CPU，確保功能不中斷
                print(f"WHISPER ERROR: MLX Backend failed: {e}. Falling back to CPU.")
                result = self._transcribe_ctranslate(audio, model_size, language)
        else:
            result = self._transcribe_ctranslate(audio, model_size, language)

        # 填入實際的音訊長度與推論耗時（兩個後端的 _transcribe_* 不填這兩欄）
        result.duration_seconds = duration
        result.elapsed_seconds  = time.perf_counter() - t0
        print(f"WHISPER: Inference finished in {result.elapsed_seconds:.2f}s. RTF={(result.elapsed_seconds/duration) if duration>0 else 0:.3f}")

        # 幻覺防護：替換已知幻覺輸出
        if _is_hallucination(result.text):
            print(f"WHISPER: Guard - Detected hallucination in result: '{result.text[:50]}...'")
            result.text = "（未偵測到語音內容）"

        return result

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

        duration = len(audio) / 16_000
        t0 = time.perf_counter()

        # 靜音防護
        if _is_silence(audio):
            return TranscriptionResult(
                text="（未偵測到語音內容）", language="",
                duration_seconds=duration, elapsed_seconds=0.0,
            )

        # 用 "small" 模型做快速推論（在此場景下 small 的速度/品質比最佳）
        model = self._ensure_model("small")

        with self._transcription_lock:
            segments_iter, info = model.transcribe(
                audio,
                language=language,
                beam_size=1,                              # 貪婪解碼，最快
                initial_prompt=self._build_initial_prompt(),
                condition_on_previous_text=False,         # 不依賴前文，避免幻覺傳播
                vad_filter=True,                          # 內建 VAD 過濾靜音
                vad_parameters={
                    "threshold": 0.6,                     # 比主轉錄更嚴格（減少誤判）
                    "min_silence_duration_ms": 200,
                    "min_speech_duration_ms": 100,
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
        if BACKEND == "mlx":
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
        """MLX 後端暖機：載入模型並跑一次 0.2 秒靜音。"""
        import mlx_whisper
        import numpy as np
        hf_repo = _MLX_MODEL_MAP.get(model_size, _MLX_MODEL_MAP["large-v3-turbo"])
        silence  = np.zeros(3200, dtype=np.float32)   # 0.2 秒靜音
        with self._transcription_lock:
            try:
                mlx_whisper.transcribe(
                    silence,
                    path_or_hf_repo=hf_repo,
                    language="zh",    # 指定語言可加速暖機
                    verbose=False,
                )
            except Exception as e:
                print(f"Warmup MLX failed: {e}")

    def _transcribe_mlx(
        self,
        audio,
        model_size: str,
        language: Optional[str],
    ) -> TranscriptionResult:
        """使用 mlx-whisper 進行推論（Metal GPU 加速）。"""
        import mlx_whisper

        hf_repo = _MLX_MODEL_MAP.get(model_size, _MLX_MODEL_MAP["large-v3-turbo"])

        with self._transcription_lock:
            try:
                output = mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=hf_repo,
                    language=language,
                    initial_prompt=self._build_initial_prompt(),
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
        except Exception as e:
            print(f"Warmup CTranslate failed: {e}")

    def _transcribe_ctranslate(
        self,
        audio,
        model_size: str,
        language: Optional[str],
    ) -> TranscriptionResult:
        """使用 faster-whisper（CTranslate2）進行 CPU int8 推論。"""
        model = self._ensure_model(model_size)

        with self._transcription_lock:
            segments_iter, info = model.transcribe(
                audio,
                language=language,
                beam_size=5,               # beam 越大越準，但越慢
                repetition_penalty=1.1,    # 輕微懲罰重複，抑制幻覺循環
                initial_prompt=self._build_initial_prompt(),
                condition_on_previous_text=False,   # 不依賴前文（單次錄音場景）
                vad_filter=True,           # 內建 VAD，過濾靜音段落
                vad_parameters={
                    "threshold": 0.5,
                    "min_silence_duration_ms": 300,
                    "min_speech_duration_ms": 150,
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
                    print(f"WHISPER: Unloading old model '{self._loaded_model_size}'...")
                    try:
                        del self._model
                    except Exception:
                        pass
                    gc.collect()

                from faster_whisper import WhisperModel
                # CPU 執行緒數：不超過 4，避免與 UI / pynput 執行緒競爭
                cpu_threads = min(4, os.cpu_count() or 4)
                print(f"WHISPER: Loading CPU model '{model_size}' with {cpu_threads} threads...")

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
                    print(f"WHISPER: Model '{model_size}' loaded successfully in {elapsed:.2f}s.")
                except Exception as e:
                    print(f"WHISPER ERROR: Failed to load model '{model_size}'. Detail: {e}")
                    raise RuntimeError(f"無法載入 Whisper 模型 {model_size}: {e}")

            return self._model
