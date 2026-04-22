"""
Ollama LLM 後處理客戶端（Phase 1 · Speakly 對標）

設計要點（規劃書 6.1 / 6.5 / 附錄 B）：
  * **永不阻塞主執行緒**：所有網路 I/O 都設有超時、由呼叫端丟背景執行緒。
  * **Health check 拆成同步／非同步兩條路**：UI 啟動時用 async，避免 App 冷啟
    被 Ollama 離線的 2 秒 timeout 卡住。
  * **失敗降級**：任何錯誤都回傳帶 `error` 欄位的 OllamaResponse，`text` 欄位回
    填為原始輸入，讓 GUI 直接貼原文即可。
  * **低溫度**：溫度預設 0.2；潤飾不需要創意。
  * **Prompt 可覆寫**：`process(..., prompt_template=...)` 允許 Phase 2 情境
    preset 動態切換，不必改本檔。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

import requests

import prompts

# ── 常數 ──────────────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL    = "qwen2.5:3b-instruct"
DEFAULT_PROMPT   = prompts.OLLAMA_POLISH_PROMPT

# 潤飾任務用的 LLM 參數（規劃書 6.3）
_POLISH_OPTIONS: dict = {
    "temperature": 0.2,
    "top_p":       0.9,
    # num_predict 上限（字元 token 上限）— 避免 LLM 失控暴走
    "num_predict": 1024,
}

# health check 用較短 timeout，避免拖累 UI
_HEALTH_TIMEOUT_SEC = 1.5


# ── Dataclass 契約 ────────────────────────────────────────────────────────────

@dataclass
class OllamaConfig:
    base_url:        str  = DEFAULT_BASE_URL
    model:           str  = DEFAULT_MODEL
    timeout_seconds: int  = 30
    stream:          bool = False        # Phase 1 用非 streaming，避免 UI 閃爍
    enabled:         bool = False


@dataclass
class OllamaResponse:
    text:   str
    model:  str
    done:   bool
    error:  Optional[str] = None
    elapsed_seconds: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────

class OllamaClient:
    """執行緒安全的 Ollama HTTP 客戶端。

    `health_ok` 是由非同步 probe 更新的快取值；UI 可直接讀取避免重新打網路。
    """

    def __init__(self, config: Optional[OllamaConfig] = None) -> None:
        self.config = config or OllamaConfig()
        self._session = requests.Session()
        # 快取的健康狀態（None 代表尚未檢查）
        self._health_ok: Optional[bool] = None
        self._health_lock = threading.Lock()

    # ── 設定同步 ─────────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        self.config.enabled = enabled
        # 狀態變更立刻清掉快取，讓下一次 probe 重新判斷
        with self._health_lock:
            self._health_ok = None

    def apply_app_config(self, cfg) -> None:
        """從 app `Config` 物件同步 Ollama 參數。

        呼叫時機：App 啟動、設定儲存後。
        """
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
        if changed:
            with self._health_lock:
                self._health_ok = None

    # ── Health check ─────────────────────────────────────────────────────────

    @property
    def health_ok(self) -> Optional[bool]:
        """快取的健康狀態；None 代表尚未檢查。"""
        with self._health_lock:
            return self._health_ok

    def health_check_sync(self) -> bool:
        """同步探測 `/api/tags`。**會阻塞 ~1.5 秒**，勿在主執行緒呼叫。

        會更新 `self._health_ok` 快取。
        """
        if not self.config.enabled:
            with self._health_lock:
                self._health_ok = False
            return False
        try:
            resp = self._session.get(
                f"{self.config.base_url}/api/tags",
                timeout=_HEALTH_TIMEOUT_SEC,
            )
            ok = resp.status_code == 200
        except requests.exceptions.RequestException:
            ok = False
        except Exception as e:
            print(f"OLLAMA: health_check unexpected error: {e}")
            ok = False
        with self._health_lock:
            self._health_ok = ok
        return ok

    def health_check_async(
        self,
        on_result: Optional[Callable[[bool], None]] = None,
    ) -> None:
        """背景探測並呼叫 callback。`on_result` 必須處理執行緒切換。"""
        def _run():
            ok = self.health_check_sync()
            if on_result is not None:
                try:
                    on_result(ok)
                except Exception as e:
                    print(f"OLLAMA: health_check callback error: {e}")

        threading.Thread(target=_run, daemon=True).start()

    # ── 向後相容舊 API（gui.py _build_action_bar 曾經使用）─────────────────
    def is_available(self) -> bool:
        """⚠️ 保留作為向後相容 shim：若還沒做過 async probe，退回同步探測。

        新程式碼請改讀 `health_ok` 屬性 + 呼叫 `health_check_async`。
        """
        cached = self.health_ok
        if cached is not None:
            return cached
        return self.health_check_sync()

    # ── 取得本機模型列表（用於設定 UI 的「測試連線」）─────────────────────
    def get_models(self) -> list[str]:
        """回傳本機已 pull 的模型清單；失敗時回傳空 list。"""
        try:
            resp = self._session.get(
                f"{self.config.base_url}/api/tags", timeout=3,
            )
            resp.raise_for_status()
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception as e:
            print(f"OLLAMA: get_models failed: {e}")
            return []

    # ── 核心：潤飾 ───────────────────────────────────────────────────────────

    def process(
        self,
        text: str,
        prompt_template: str = DEFAULT_PROMPT,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> OllamaResponse:
        """送一段轉錄文字給 Ollama 做潤飾。

        失敗時 `text` 欄位會保留原輸入，`error` 欄位說明原因。呼叫端可以無腦
        拿 `.text` 去貼上而不用判斷是否成功。
        """
        import time

        if not self.config.enabled:
            return OllamaResponse(
                text=text, model="disabled", done=True,
                error="Ollama 未啟用",
            )

        if not text or not text.strip():
            return OllamaResponse(
                text=text, model=self.config.model, done=True,
                error="輸入為空",
            )

        prompt = prompt_template.format(text=text)
        payload = {
            "model":   self.config.model,
            "prompt":  prompt,
            "stream":  self.config.stream and on_token is not None,
            "options": _POLISH_OPTIONS,
        }

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

            # 防呆：模型有時會不顧指令加前後引號，這裡做最小修剪
            final_text = _strip_wrapper_quotes(final_text)

            return OllamaResponse(
                text=final_text,
                model=self.config.model,
                done=True,
                elapsed_seconds=time.perf_counter() - t0,
            )

        except requests.exceptions.Timeout:
            return OllamaResponse(
                text=text, model=self.config.model, done=True,
                error=f"潤飾超時（> {self.config.timeout_seconds}s）",
                elapsed_seconds=time.perf_counter() - t0,
            )
        except requests.exceptions.ConnectionError:
            # 常見情境：Ollama 服務沒開；清掉健康狀態快取
            with self._health_lock:
                self._health_ok = False
            return OllamaResponse(
                text=text, model=self.config.model, done=True,
                error="無法連線 Ollama 服務",
                elapsed_seconds=time.perf_counter() - t0,
            )
        except Exception as e:
            print(f"OLLAMA: process error: {e}")
            return OllamaResponse(
                text=text, model=self.config.model, done=True,
                error=str(e),
                elapsed_seconds=time.perf_counter() - t0,
            )

    # ── Streaming（Phase 1 預設不啟用；保留給 Phase 2 視情況用）─────────
    def _stream_response(self, payload: dict) -> Iterator[str]:
        import json as _json
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
                    data = _json.loads(line)
                except Exception:
                    continue
                token = data.get("response", "")
                if token:
                    yield token
                if data.get("done"):
                    break


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _strip_wrapper_quotes(text: str) -> str:
    """去除 LLM 有時會加的整段外層引號（"..." 或 「...」）。

    只在首尾「同時」是引號時才剝，避免誤傷對話中的引號。
    """
    pairs = [('"', '"'), ("“", "”"), ("「", "」"), ("'", "'")]
    s = text.strip()
    for a, b in pairs:
        if len(s) >= 2 and s.startswith(a) and s.endswith(b):
            # 確認中間沒有再出現該引號（否則可能是結構性內容）
            inner = s[1:-1]
            if a not in inner and b not in inner:
                return inner.strip()
    return s
