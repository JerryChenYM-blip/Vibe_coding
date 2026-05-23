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
from typing import Optional, Union

from logger import get_logger, log_error

log = get_logger("dictionary")

DEFAULT_PATH = Path.home() / ".whisper_app" / "dictionary.json"

# 公開的 path 型別：接受 str 或 Path（gui.py 多處都把字串路徑直接傳進來）
PathLike = Union[str, Path]

# 內建範例，讓使用者 json 檔不存在時有東西可以參考
_BOOTSTRAP = {
    "terms": [
        {"term": "Whisper Pro", "note": "專案名稱，不要翻譯"},
        {"term": "Ollama",      "note": "本地 LLM runtime"},
    ],
    "_comment": "每行一個 term 物件；note 可選。Whisper 取最多 30 個、Ollama 取最多 50 個。",
}


def ensure_file(path: PathLike = DEFAULT_PATH) -> Path:
    """檔案不存在時寫入一個最小範例；回傳最終路徑（總是 Path）。"""
    p = Path(path).expanduser() if not isinstance(path, Path) else path
    if p.exists():
        return p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(_BOOTSTRAP, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        log_error("dictionary_ensure_file_failed", path=str(p))
    return p


def load_terms(path: PathLike = DEFAULT_PATH) -> list[str]:
    """讀字典，回傳純 term 字串 list；任何錯誤回空 list，永不拋例外。

    `path` 可以是 `str` 或 `Path`（gui.py 多處傳純字串）。
    支援兩種 schema：
      • 純 string list：`["term1", "term2"]`
      • 物件 list：`{"terms": [{"term": "..."}, ...]}`
    """
    try:
        p = Path(path).expanduser() if not isinstance(path, Path) else path
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        # 支援頂層直接是 list（最簡單格式）或 {"terms": [...]} 物件包裝
        if isinstance(data, list):
            raw = data
        elif isinstance(data, dict):
            raw = data.get("terms", [])
        else:
            raw = []

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
    except Exception:
        log_error("dictionary_load_terms_failed", path=str(path))
        return []


def load_corrections(path: PathLike = DEFAULT_PATH) -> list[tuple[str, str]]:
    """讀字典的 `corrections` 段（v2.13.0），回傳 (from, to) tuple list。

    用途：規則式預先校正 — Whisper 系統性誤辨識（例：Cloud Code → Claude Code）
    走字串替換 < 1ms 解決，不需要靠 LLM 推理。校正在 transcribe() 完後、polish
    之前套用，連 polish 都 disable 的情況也生效。

    Schema：
      {
        "corrections": [
          {"from": "Cloud Code", "to": "Claude Code", "note": "..."}
        ]
      }

    保序：依字典出現順序套用、user 可控優先級。長字串應該排前面（避免短字串
    先 match 吃掉部分）。
    """
    try:
        p = Path(path).expanduser() if not isinstance(path, Path) else path
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        raw = data.get("corrections", [])
        out: list[tuple[str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            src = str(entry.get("from", "")).strip()
            dst = str(entry.get("to", "")).strip()
            if src and dst and src != dst:
                out.append((src, dst))
        return out
    except Exception:
        log_error("dictionary_load_corrections_failed", path=str(path))
        return []


def apply_corrections(text: str, corrections: list[tuple[str, str]]) -> str:
    """套用 corrections list 到 text；保序、不對已替換的內容再 match。

    用 Python str.replace 一條條套用。corrections 排序由 caller 決定
    （load_corrections 保留字典順序、user 可控）。
    """
    if not text or not corrections:
        return text
    out = text
    for src, dst in corrections:
        if src in out:
            out = out.replace(src, dst)
    return out
