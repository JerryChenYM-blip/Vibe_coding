"""
Persistent configuration stored at ~/.whisper_app/config.json
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path.home() / ".whisper_app"
CONFIG_PATH = CONFIG_DIR / "config.json"

MODEL_INFO: dict[str, str] = {
    "tiny":           "速度最快，適合英文速記（39M 參數）",
    "base":           "速度與精準度平衡，推薦入門（74M 參數）",
    "small":          "較高精準度，中文效果更佳（244M 參數，GPU 4bit 量化）",
    "medium":         "高精準度，需較多記憶體（769M 參數，GPU 4bit 量化）",
    "large-v3-turbo": "★ 推薦：中英混合最佳，Metal GPU 加速（809M 參數）",
    "large-v3":       "最高精準度，Metal GPU 加速（1550M 參數，4bit 量化）",
}

LANGUAGE_OPTIONS: dict[str, str | None] = {
    "自動偵測": None,
    "中文":     "zh",
    "English":  "en",
    "日本語":   "ja",
    "한국어":   "ko",
    "Español":  "es",
    "Français": "fr",
}


@dataclass
class Config:
    hotkey: str = "cmd+shift+space"
    model: str = "large-v3-turbo"
    language: str = "自動偵測"
    input_device: Optional[str] = None
    append_results: bool = True
    auto_copy: bool = False
    auto_paste: bool = True
    ollama_enabled: bool = False

    # ── persistence ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls) -> "Config":
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # 濾除損壞或不存在的欄位，確保版本相容
            valid_data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**valid_data)
        except Exception as e:
            print(f"CRITICAL: Config corrupt ({e}). Backing up and resetting.")
            try:
                CONFIG_PATH.rename(CONFIG_PATH.with_suffix(".json.bak"))
            except: pass
            cfg = cls()
            cfg.save()
            return cfg

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            temp_path = CONFIG_PATH.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(asdict(self), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            # 原子性替換，防止寫入一半斷電或崩潰
            temp_path.replace(CONFIG_PATH)
        except Exception as e:
            print(f"ERROR: Failed to save config: {e}")
            # 不拋出異常，避免 UI 因設定儲存失敗而卡死

    # ── helpers ───────────────────────────────────────────────────────────────

    def get_whisper_language(self) -> str | None:
        """Return the language code passed to Whisper, or None for auto."""
        return LANGUAGE_OPTIONS.get(self.language)

    def format_hotkey_display(self) -> str:
        """Format hotkey string for display, e.g. 'cmd+shift+space' → '⌘⇧Space'."""
        symbols = {
            "cmd":   "⌘",
            "ctrl":  "⌃",
            "alt":   "⌥",
            "shift": "⇧",
            "space": "Space",
        }
        parts = self.hotkey.lower().split("+")
        return "".join(symbols.get(p, p.capitalize()) for p in parts)
