"""
使用者個人字典（#4 擴充項）。

檔案路徑：~/.whisper_app/dictionary.json

Schema：
{
  "terms": [
    {"term": "cyberpunk",  "note": "常被 Whisper 錯認成瑟柏"},
    {"term": "Kubernetes", "note": "K8s 原文"},
    {"term": "Jerry Chen"}
  ]
}

功能：
  • 啟動／設定儲存時 load_terms() 讀進記憶體
  • 傳給 Whisper 的 initial_prompt 附加「常用詞彙：A、B、C。」
  • 傳給 Ollama 的 polish prompt 追加「務必逐字保留下列術語」約束

Speakly 永遠做不到這件事（雲端不能存使用者私密字典）— 這是差異化核心。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

DEFAULT_PATH = Path.home() / ".whisper_app" / "dictionary.json"

# 內建範例，讓使用者 json 檔不存在時有東西可以參考
_BOOTSTRAP = {
    "terms": [
        {"term": "Whisper Pro", "note": "專案名稱，不要翻譯"},
        {"term": "Ollama",      "note": "本地 LLM runtime"},
    ],
    "_comment": "每行一個 term 物件；note 可選。Whisper 取最多 30 個、Ollama 取最多 50 個。",
}


def ensure_file(path: Path = DEFAULT_PATH) -> Path:
    """檔案不存在時寫入一個最小範例；回傳最終路徑。"""
    if path.exists():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_BOOTSTRAP, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"DICTIONARY: ensure_file failed: {e}")
    return path


def load_terms(path: Path = DEFAULT_PATH) -> list[str]:
    """讀字典，回傳純 term 字串 list；任何錯誤回空 list，永不拋例外。"""
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("terms", [])
        out: list[str] = []
        for entry in raw:
            if isinstance(entry, str):
                t = entry.strip()
                if t:
                    out.append(t)
            elif isinstance(entry, dict):
                t = str(entry.get("term", "")).strip()
                if t:
                    out.append(t)
        # 去重但保序
        seen = set()
        dedup = []
        for t in out:
            k = t.lower()
            if k in seen:
                continue
            seen.add(k)
            dedup.append(t)
        return dedup
    except Exception as e:
        print(f"DICTIONARY: load_terms failed: {e}")
        return []
