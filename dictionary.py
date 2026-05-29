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
import re
import time
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


# ─────────────────────────────────────────────────────────────────────────────
# v2.19.x Pinyin guard：拼音 fuzzy match 修中文 ASR 同音/近音誤辨
#
# 動機：apply_corrections 是純字串替換，只能修「已知 exact 字面」的常見錯誤
# （如 Cloud Code → Claude Code）。但中文 ASR 常生出**拼音相近、字面不同**
# 的幻覺，例如：
#   潤飾 → 論視 / 潤世 / 潤示   （rùn shì 同聲母韻母、聲調不同）
#   辨識 → 辯式 / 變勢          （biàn shì）
#   程式 → 城市                  （chéng shí）
#   介面 → 界面                  （jiè miàn、簡繁互換）
# 這些 corrections 無法事先窮舉。改用拼音 edit distance ≤ 1 比對 user
# dictionary terms：拼音夠近就替換成 canonical term。always-on、polish 關閉也跑。
#
# 設計取捨：
#   • 只索引純中文 term（混英文 fuzzy match 沒意義）
#   • 只索引長度 ≥ 2 字元的 term（單字假陽性太多）
#   • edit distance ≤ 1 才替換（保守、避免誤殺）
#   • 模組層 cache + mtime 失效（dictionary.json 改了會自動重 build）
# ─────────────────────────────────────────────────────────────────────────────

# Module-level cache：避免每次轉錄都重算 pinyin index
_pinyin_index_cache: Optional[dict] = None
_pinyin_index_mtime: float = 0.0


def _normalize_pinyin(text: str) -> str:
    """把字串轉成 plain pinyin string（去聲調、空白、特殊字元）方便比對。

    範例：「潤飾」→ "runshi"、「介面」→ "jiemian"

    非中文字符直接保留 lowercase（讓「sub agent」這類混合詞也能比對）
    """
    from pypinyin import lazy_pinyin, Style
    parts = lazy_pinyin(text, style=Style.NORMAL, errors='default')
    return "".join(p.lower() for p in parts if p).replace(" ", "")


def _build_pinyin_index(path: PathLike = DEFAULT_PATH) -> dict:
    """Build pinyin → canonical term mapping from dictionary.json terms。

    回 {pinyin_str: canonical_term}、例如：
      {"runshi": "潤飾", "biaoshi": "標識", ...}

    用於 fuzzy match：ASR 輸出「論視」(lunshi) → 拼音 edit distance vs
    「runshi」（差 1）→ suggest 替換成「潤飾」。

    優先用 dictionary terms（user 真正關心的詞）；不擴展到全部 ASR 輸出。
    """
    global _pinyin_index_cache, _pinyin_index_mtime

    p = Path(path).expanduser() if not isinstance(path, Path) else path
    if not p.exists():
        _pinyin_index_cache = {}
        _pinyin_index_mtime = 0.0
        return _pinyin_index_cache

    try:
        mtime = p.stat().st_mtime
        if _pinyin_index_cache is not None and mtime == _pinyin_index_mtime:
            return _pinyin_index_cache

        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            raw = data
        elif isinstance(data, dict):
            raw = data.get("terms", [])
        else:
            raw = []

        index: dict[str, str] = {}
        for entry in raw:
            term = entry.get("term", "").strip() if isinstance(entry, dict) else str(entry).strip()
            if not term or len(term) < 2:
                continue
            # 只索引純中文 term（混英文的 fuzzy match 沒意義）
            if not any('一' <= c <= '鿿' for c in term):
                continue
            try:
                py = _normalize_pinyin(term)
                if py and len(py) >= 4:   # 太短 false positive 多、跳過
                    # 若拼音碰撞、保留第一個進來的 term（dictionary 順序為主）
                    index.setdefault(py, term)
            except Exception:
                continue
        _pinyin_index_cache = index
        _pinyin_index_mtime = mtime
        return index
    except Exception:
        log_error("dictionary_build_pinyin_index_failed", path=str(p))
        return _pinyin_index_cache or {}


def _edit_distance(s1: str, s2: str) -> int:
    """Levenshtein distance、輕量實作（不裝 python-Levenshtein 避免額外依賴）"""
    if len(s1) < len(s2):
        return _edit_distance(s2, s1)
    if not s2:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


def apply_pinyin_guard(
    text: str,
    max_edit_distance: int = 0,
    path: PathLike = DEFAULT_PATH,
) -> tuple[str, list[tuple[str, str]]]:
    """掃 text 裡的中文片段、找拼音接近字典 term 的、替換成 canonical。

    回 (修正後 text, 修正紀錄 [(原詞, 修正詞), ...])

    範例：
      input: "請改用界面來說"
      → "請改用介面來說", [("界面", "介面")]

    max_edit_distance：拼音 edit distance ≤ 此值才替換（保守、避免誤殺）
      • 0 (預設) = 純同音字（lazy_pinyin 去聲調）——「界面/介面」「辯式/辨識」
                  「城市/程式」這類最常見的同音字錯誤；幾乎無假陽性。
      • 1 = 多接受聲母差 1 字母——能修「論視→潤飾」這類，但會誤殺跨詞片段
            （例：「文件是」裡的「件是」誤配「辨識」）。caller 自己 trade-off。
    """
    if not text or not text.strip():
        return text, []

    index = _build_pinyin_index(path)
    if not index:
        return text, []

    # 先抓連續中文段、再對每段用 sliding window 取 2-4 字子串。
    # 用 sliding window（不是 re.findall {2,4} greedy）才能抓到「論視這段」
    # 裡的「論視」子串——greedy 模式只會回整段 4 字 chunk、漏掉 2 字 sub-window。
    runs = re.findall(r'[一-鿿]+', text)
    if not runs:
        return text, []

    # 收集 (chunk_str, chunk_len) candidates、依字串長度集合去重
    candidates: list[str] = []
    seen_chunks: set[str] = set()
    # 知道 index 裡 canonical term 的字元長度範圍、limit window size
    canonical_lens = {len(v) for v in index.values()}
    if not canonical_lens:
        return text, []
    min_n = max(2, min(canonical_lens))
    max_n = min(4, max(canonical_lens))
    for run in runs:
        L = len(run)
        for n in range(min_n, min(max_n, L) + 1):
            for i in range(L - n + 1):
                sub = run[i:i + n]
                if sub not in seen_chunks:
                    seen_chunks.add(sub)
                    candidates.append(sub)

    # 已是 canonical term 的片段直接跳過（避免重複比對）
    canonical_set = set(index.values())

    corrections: list[tuple[str, str]] = []
    out = text
    for chunk in candidates:
        if chunk in canonical_set:
            continue
        try:
            chunk_py = _normalize_pinyin(chunk)
            if not chunk_py or len(chunk_py) < 4:
                continue
            # 預先算每個字的單字拼音、給 anchor 檢查用
            chunk_per_char = [_normalize_pinyin(c) for c in chunk]
            # 找 pinyin index 中 edit distance ≤ max_edit_distance 的 term
            best_match: Optional[str] = None
            best_dist = max_edit_distance + 1
            for py, canonical in index.items():
                # 字元長度必須相同（避免「論視」→「轉錄」、「程式碼」這種亂配）
                if len(canonical) != len(chunk):
                    continue
                # 拼音字串長度也要相近（聲母差 1 字母可接受）
                if abs(len(py) - len(chunk_py)) > 1:
                    continue
                dist = _edit_distance(chunk_py, py)
                if dist < best_dist:
                    # Anchor 保護：拼音 edit distance == 0（純同音字）放行；
                    # edit distance == 1 時要 per-char pinyin 也夠像——
                    # 否則「件是」(jian shi) vs「辨識」(bian shi) 這種跨詞邊界亂配
                    # 也會被替換（edit dist 1、聲母差 1 字母）。
                    # 規則：
                    #   • 2 字 term：兩字單字拼音必須完全相同（避免跨詞 noise）
                    #   • 3+ 字 term：至少 n-1 字位置單字拼音完全相同
                    if dist >= 1:
                        canonical_per_char = [_normalize_pinyin(c) for c in canonical]
                        same_pos = sum(
                            1 for a, b in zip(chunk_per_char, canonical_per_char)
                            if a == b
                        )
                        n = len(canonical)
                        required = n if n <= 2 else n - 1
                        if same_pos < required:
                            continue
                    best_dist = dist
                    best_match = canonical
            if best_match and best_match != chunk and chunk in out:
                out = out.replace(chunk, best_match)
                corrections.append((chunk, best_match))
        except Exception:
            continue

    return out, corrections
