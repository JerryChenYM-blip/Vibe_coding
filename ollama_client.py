"""
Ollama LLM post-processing client.

Now fully integrated into the GUI settings.
Requires Ollama to be installed and running locally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

import requests
import prompts

from logger import get_logger, log_error

log = get_logger("ollama")

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2"
DEFAULT_PROMPT = prompts.OLLAMA_SYSTEM_PROMPT


@dataclass
class OllamaConfig:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: int = 60
    stream: bool = True
    enabled: bool = False


@dataclass
class OllamaResponse:
    text: str
    model: str
    done: bool
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────


class OllamaClient:
    def __init__(self, config: Optional[OllamaConfig] = None) -> None:
        self.config = config or OllamaConfig()
        self._session = requests.Session()

    def set_enabled(self, enabled: bool) -> None:
        self.config.enabled = enabled

    def is_available(self) -> bool:
        """Probe Ollama server. Returns False if not installed or not running."""
        if not self.config.enabled:
            return False
        try:
            resp = self._session.get(
                f"{self.config.base_url}/api/tags", timeout=2
            )
            return resp.status_code == 200
        except requests.exceptions.ConnectionError:
            return False

    def get_models(self) -> list[str]:
        """Return list of locally available model names."""
        if not self.config.enabled:
            return []
        try:
            resp = self._session.get(
                f"{self.config.base_url}/api/tags", timeout=5
            )
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def process(
        self,
        text: str,
        prompt_template: str = DEFAULT_PROMPT,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> OllamaResponse:
        """
        Send transcription through Ollama for LLM post-processing.
        """
        if not self.config.enabled:
            return OllamaResponse(
                text="[Ollama 未啟用。請在設定中啟用 AI 潤飾並重啟 App]",
                model="stub",
                done=True,
            )

        prompt = prompt_template.format(text=text)
        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": self.config.stream,
        }

        try:
            if self.config.stream and on_token is not None:
                collected = []
                for token in self._stream_response(payload):
                    collected.append(token)
                    on_token(token)
                final_text = "".join(collected).strip()
                if not final_text:
                    raise ValueError("AI returned empty text")
                return OllamaResponse(
                    text=final_text,
                    model=self.config.model,
                    done=True,
                )
            else:
                payload["stream"] = False
                resp = self._session.post(
                    f"{self.config.base_url}/api/generate",
                    json=payload,
                    timeout=self.config.timeout_seconds,
                )
                resp.raise_for_status()
                data = resp.json()
                final_text = data.get("response", "").strip()
                if not final_text:
                    raise ValueError("AI returned empty text")
                return OllamaResponse(
                    text=final_text,
                    model=data.get("model", self.config.model),
                    done=data.get("done", True),
                )
        except Exception as e:
            log_error("ollama_request_failed", model=self.config.model)
            return OllamaResponse(
                text=text, # 防呆：發生錯誤時回傳原始文字而非空字串
                model=self.config.model,
                done=True,
                error=str(e),
            )

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
                if line:
                    data = _json.loads(line)
                    token = data.get("response", "")
                    if token:
                        yield token
                    if data.get("done"):
                        break
