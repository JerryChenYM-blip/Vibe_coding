"""
Ollama LLM 後處理客戶端（Phase 1 + Phase 2 · Speakly 對標）

設計要點：
  * **永不阻塞主執行緒**：所有網路 I/O 都設有超時、由呼叫端丟背景執行緒
  * **Health check 拆成同步／非同步兩條路**：避免 App 冷啟被 Ollama 離線
    的 timeout 卡住
  * **失敗降級**：任何錯誤都回傳帶 `error` 欄位的 OllamaResponse，`text`
    欄位回填為原始輸入，讓 GUI 直接貼原文即可
  * **低溫度**：潤飾不需要創意
  * **Prompt 可覆寫 + 動態查詢**：`process(..., prompt_template=...)` 供
    Phase 2 情境 preset 用；未提供時每次呼叫動態讀 prompts.OLLAMA_POLISH_PROMPT
    以支援 prompt_reloader 熱重載
  * **字典注入**：process(..., dictionary_terms=...) 會追加「務必保留下列
    術語」約束到 prompt，不改動原 preset 文本
  * **Polish log**：成功潤飾會附加一行 JSONL 到 ~/.whisper_app/polish_log.jsonl
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

import requests

import prompts

from logger import get_logger, log_error

log = get_logger("ollama")

# ── 常數 ──────────────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL    = "qwen2.5:3b-instruct"

# 潤飾任務用的 LLM 參數（規劃書 6.3）
# v2.13.0：num_predict 從固定 1024 改成 process() 內依輸入長度自適應。
# top_p 留 0.9（穩定）、temperature 0.2（少量創意修錯字、不太敢加料）。
_POLISH_OPTIONS_BASE: dict = {
    "temperature": 0.2,
    "top_p":       0.9,
}

# v2.13.0：Ollama keep_alive — 模型載入 VRAM 後保留多久。
# Ollama 預設 5 分鐘，user 反映「使用後 GPU 一路高負載」其實是 VRAM 佔用，
# compute 為 0。縮成 "2m" — 連續使用仍快、停止 2 分鐘後釋放 ~12GB VRAM。
_OLLAMA_KEEP_ALIVE = "2m"

# health check 用較短 timeout
_HEALTH_TIMEOUT_SEC = 1.5

# Polish log 路徑
_POLISH_LOG_PATH = Path.home() / ".whisper_app" / "polish_log.jsonl"


# ── Dataclass 契約 ────────────────────────────────────────────────────────────

@dataclass
class OllamaConfig:
    base_url:        str  = DEFAULT_BASE_URL
    model:           str  = DEFAULT_MODEL
    timeout_seconds: int  = 30
    stream:          bool = False        # Phase 1 用非 streaming，避免 UI 閃爍
    enabled:         bool = False
    log_enabled:     bool = True         # 是否寫 polish_log.jsonl


@dataclass
class OllamaResponse:
    text:   str
    model:  str
    done:   bool
    error:  Optional[str]  = None
    elapsed_seconds: float = 0.0
    preset_name:     str   = "default"


# ─────────────────────────────────────────────────────────────────────────────

class OllamaClient:
    """執行緒安全的 Ollama HTTP 客戶端。"""

    # D5-S7（v2.10.0 / 2026-05-23）：health cache TTL（秒）
    # 預設 30s——平衡「不要每秒 ping、也不要長時間 stale」。Ollama 服務
    # 通常 start/stop 後 user 不會立刻錄音，30s 內若有狀態改變下次自然 expire。
    HEALTH_TTL_SEC = 30.0

    def __init__(self, config: Optional[OllamaConfig] = None) -> None:
        self.config = config or OllamaConfig()
        self._session = requests.Session()
        self._health_ok: Optional[bool] = None
        # D5-S7：cache 寫入時間。read 時若超過 TTL 視為過期、強制重 check。
        self._health_cached_at: float = 0.0
        self._health_lock = threading.Lock()

    # ── 設定同步 ─────────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        self.config.enabled = enabled
        with self._health_lock:
            self._health_ok = None

    def apply_app_config(self, cfg) -> None:
        """從 app `Config` 物件同步 Ollama 參數。"""
        changed = (
            self.config.base_url        != cfg.ollama_base_url
            or self.config.model        != cfg.ollama_model
            or self.config.enabled      != cfg.ollama_enabled
            or self.config.timeout_seconds != cfg.ollama_timeout
        )
        self.config.base_url        = cfg.ollama_base_url
        self.config.model           = cfg.ollama_model
        self.config.enabled         = cfg.ollama_enabled
        self.config.timeout_seconds = cfg.ollama_timeout
        self.config.log_enabled     = getattr(cfg, "polish_log_enabled", True)
        if changed:
            with self._health_lock:
                self._health_ok = None

    # ── Health check ─────────────────────────────────────────────────────────

    @property
    def health_ok(self) -> Optional[bool]:
        """讀 cached health 狀態。D5-S7：超過 TTL 視為 None（強制重 check）。"""
        with self._health_lock:
            if self._health_ok is None:
                return None
            # D5-S7：cache 過期 → 視同未知，呼叫端應 trigger re-check
            if (time.monotonic() - self._health_cached_at) > self.HEALTH_TTL_SEC:
                return None
            return self._health_ok

    def health_check_sync(self) -> bool:
        if not self.config.enabled:
            with self._health_lock:
                self._health_ok = False
                self._health_cached_at = time.monotonic()
            return False
        try:
            resp = self._session.get(
                f"{self.config.base_url}/api/tags",
                timeout=_HEALTH_TIMEOUT_SEC,
            )
            ok = resp.status_code == 200
        except requests.exceptions.RequestException:
            ok = False
        except Exception:
            log_error("ollama_health_check_unexpected")
            ok = False
        with self._health_lock:
            self._health_ok = ok
            self._health_cached_at = time.monotonic()   # D5-S7：戳新時間
        return ok

    def health_check_async(
        self,
        on_result: Optional[Callable[[bool], None]] = None,
    ) -> None:
        def _run():
            ok = self.health_check_sync()
            if on_result is not None:
                try:
                    on_result(ok)
                except Exception:
                    log_error("ollama_health_callback_failed")
        threading.Thread(target=_run, daemon=True).start()

    def is_available(self) -> bool:
        """向後相容 shim（test_full_app.py 用）。"""
        cached = self.health_ok
        if cached is not None:
            return cached
        return self.health_check_sync()

    def get_models(self) -> list[str]:
        try:
            resp = self._session.get(
                f"{self.config.base_url}/api/tags", timeout=3,
            )
            resp.raise_for_status()
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            log_error("ollama_get_models_failed", base_url=self.config.base_url)
            return []

    # ── 核心：潤飾 ───────────────────────────────────────────────────────────

    def process(
        self,
        text: str,
        prompt_template: Optional[str]             = None,
        dictionary_terms: Optional[Iterable[str]]  = None,
        preset_name: str                           = "default",
        on_token: Optional[Callable[[str], None]]  = None,
    ) -> OllamaResponse:
        """送一段轉錄文字給 Ollama 做潤飾。

        prompt_template: None → 動態讀 prompts.OLLAMA_POLISH_PROMPT（預設 prompt；
                          支援 prompt_reloader 熱重載）。
        dictionary_terms: 若提供，約束會被 format_polish_prompt 注入到 prompt 尾端。
        preset_name: 只用於 log / 回應標註；真實 prompt 由 prompt_template 決定。
        """
        if not self.config.enabled:
            return OllamaResponse(
                text=text, model="disabled", done=True,
                error="Ollama 未啟用", preset_name=preset_name,
            )

        if not text or not text.strip():
            return OllamaResponse(
                text=text, model=self.config.model, done=True,
                error="輸入為空", preset_name=preset_name,
            )

        # Fix Cluster E / 2026-05-23：動態查詢 prompt 與 prompt_reloader 互斥，
        # 避免讀到 importlib.reload 中途的不一致 attribute（AttributeError / 半新半舊）。
        from prompt_reloader import reload_lock as _reload_lock
        with _reload_lock():
            # 動態查詢 prompt（支援熱重載）
            if prompt_template is None:
                prompt_template = prompts.OLLAMA_POLISH_PROMPT

            # 注入字典約束（若有）
            try:
                prompt_template = prompts.format_polish_prompt(
                    prompt_template, dictionary_terms,
                )
            except Exception:
                log_error("format_polish_prompt_failed")

        prompt = prompt_template.format(text=text)
        # v2.13.0：num_predict 自適應 — 校正輸出長度 ≈ 輸入（不增刪內容）
        # 預留 2x buffer 避免長句裁切；下限 64 給超短句 headroom。
        adaptive_num_predict = max(64, int(len(text) * 2.0))
        options = {**_POLISH_OPTIONS_BASE, "num_predict": adaptive_num_predict}
        payload = {
            "model":      self.config.model,
            "prompt":     prompt,
            "stream":     self.config.stream and on_token is not None,
            "options":    options,
            # v2.13.0：縮短 VRAM keep_alive（避免 user 看到「不用也高負載」）
            "keep_alive": _OLLAMA_KEEP_ALIVE,
            # v2.13.1：關 Qwen 3+ / DeepSeek-R1 等 reasoning model 的 thinking mode。
            # polish 是 deterministic 校正任務不需要 chain-of-thought；thinking 會
            # 把整個 num_predict 配額耗在 <think>...</think> 內、最後輸出空字串。
            # Ollama /api/generate 自 v0.x 支援這個 top-level 參數；老版本忽略不傷。
            "think":      False,
        }
        # v2.13.0：instruct 模型加 system role 鎖定行為。
        # qwen2.5:3b-instruct 對純 prompt 易過度發揮（擴寫、翻譯成英文）
        # → 用 system 欄位明確 role。format_polish_prompt 已注入字典約束到 prompt
        # 尾端，這裡只負責 role 鎖定。對不支援 system 的舊模型 Ollama 會忽略。
        _system = getattr(prompts, "OLLAMA_POLISH_SYSTEM", None)
        if _system:
            payload["system"] = _system

        t0 = time.perf_counter()
        try:
            if payload["stream"]:
                collected: list[str] = []
                for token in self._stream_response(payload):
                    collected.append(token)
                    if on_token is not None:
                        on_token(token)
                final_text = "".join(collected).strip()
            else:
                resp = self._session.post(
                    f"{self.config.base_url}/api/generate",
                    json=payload,
                    timeout=self.config.timeout_seconds,
                )
                resp.raise_for_status()
                data = resp.json()
                final_text = (data.get("response") or "").strip()

            if not final_text:
                raise ValueError("Ollama 回傳空字串")

            final_text = _strip_wrapper_quotes(final_text)
            elapsed = time.perf_counter() - t0

            response = OllamaResponse(
                text=final_text,
                model=self.config.model,
                done=True,
                elapsed_seconds=elapsed,
                preset_name=preset_name,
            )
            self._log_polish(text, final_text, elapsed, preset_name, error=None)
            return response

        except requests.exceptions.Timeout:
            elapsed = time.perf_counter() - t0
            resp = OllamaResponse(
                text=text, model=self.config.model, done=True,
                error=f"潤飾超時（> {self.config.timeout_seconds}s）",
                elapsed_seconds=elapsed, preset_name=preset_name,
            )
            self._log_polish(text, "", elapsed, preset_name, error=resp.error)
            return resp
        except requests.exceptions.ConnectionError:
            with self._health_lock:
                self._health_ok = False
            elapsed = time.perf_counter() - t0
            resp = OllamaResponse(
                text=text, model=self.config.model, done=True,
                error="無法連線 Ollama 服務",
                elapsed_seconds=elapsed, preset_name=preset_name,
            )
            self._log_polish(text, "", elapsed, preset_name, error=resp.error)
            return resp
        except Exception as e:
            log_error("ollama_process_failed", model=self.config.model)
            elapsed = time.perf_counter() - t0
            resp = OllamaResponse(
                text=text, model=self.config.model, done=True,
                error=str(e), elapsed_seconds=elapsed, preset_name=preset_name,
            )
            self._log_polish(text, "", elapsed, preset_name, error=str(e))
            return resp

    # ── Streaming（預設不啟用）──────────────────────────────────────────────

    def _stream_response(self, payload: dict) -> Iterator[str]:
        with self._session.post(
            f"{self.config.base_url}/api/generate",
            json=payload,
            stream=True,
            timeout=self.config.timeout_seconds,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                token = data.get("response", "")
                if token:
                    yield token
                if data.get("done"):
                    break

    # ── Polish log（JSONL）──────────────────────────────────────────────────

    def _log_polish(
        self,
        text_in:  str,
        text_out: str,
        elapsed:  float,
        preset:   str,
        error:    Optional[str],
    ) -> None:
        """附加一行 JSONL 到 ~/.whisper_app/polish_log.jsonl；任何 I/O 失敗靜默。"""
        if not self.config.log_enabled:
            return
        try:
            _POLISH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts":          time.strftime("%Y-%m-%dT%H:%M:%S"),
                "model":       self.config.model,
                "preset":      preset,
                "elapsed_s":   round(elapsed, 3),
                "len_in":      len(text_in),
                "len_out":     len(text_out),
                "error":       error,
            }
            with _POLISH_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            log_error("polish_log_write_failed", path=str(_POLISH_LOG_PATH))


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _strip_wrapper_quotes(text: str) -> str:
    pairs = [('"', '"'), ("“", "”"), ("「", "」"), ("'", "'")]
    s = text.strip()
    for a, b in pairs:
        if len(s) >= 2 and s.startswith(a) and s.endswith(b):
            inner = s[1:-1]
            if a not in inner and b not in inner:
                return inner.strip()
    return s
