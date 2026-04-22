#!/usr/bin/env python3
"""
Phase 1.5 回歸測試執行器——Speakly 對標計畫的測試基盤。

輸入：
  tests/golden_set/*.wav          — 測試音訊（WAV 格式）
  tests/golden_set/*.expected.txt — 期望輸出（同名，選填）
  tests/golden_set/*.meta.json    — 測試元資料（preset / app，選填）

處理管線：
  WAV 音訊 → Whisper 轉錄 → Ollama 潤飾（可選）

輸出：
  tests/reports/{timestamp}.csv   — 每列一個測試案例的詳細結果
  console                         — 彙總統計（準確率、英文保留率、Filler 刪除量等）

CSV 欄位說明：
  id, preset               — 案例識別與使用的 preset
  raw_text, polished_text  — Whisper 原文與潤飾後文字
  expected_text            — 期望輸出（.expected.txt 的內容）
  whisper_ms, polish_ms    — 各階段耗時（毫秒）
  len_raw/polished/expected — 三個版本的字元數
  english_preserved_ratio   — 英文單字保留率（1.0 = 完全保留）
  filler_removed_estimate   — 估計刪除的填充詞數量
  matches_expected          — 是否完全符合期望輸出
  polish_error              — 潤飾錯誤訊息（無錯誤時為空字串）

用法：
  venv/bin/python3 eval_runner.py               # 跑所有案例
  venv/bin/python3 eval_runner.py --id 001      # 只跑指定 ID
  venv/bin/python3 eval_runner.py --no-polish   # 只跑 Whisper（跳過潤飾）
  venv/bin/python3 eval_runner.py --model qwen2.5:3b  # 指定 Ollama 模型
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

# 路徑常數
ROOT    = Path(__file__).parent
GOLDEN  = ROOT / "tests" / "golden_set"   # 測試案例目錄
REPORTS = ROOT / "tests" / "reports"       # 報告輸出目錄


# ── 資料讀取 ──────────────────────────────────────────────────────────────────

@dataclass
class Case:
    """單一測試案例的所有必要資料。"""
    id:       str            # 案例識別名（通常是 WAV 檔名）
    wav_path: Path           # WAV 音訊路徑
    expected: str            # 期望輸出文字（.expected.txt）
    preset:   Optional[str]  # 指定 preset（來自 .meta.json）
    app:      Optional[str]  # 指定前景 App（來自 .meta.json，供 preset 路由用）


def discover_cases(target_id: Optional[str]) -> list[Case]:
    """掃描 golden_set 目錄，回傳所有有效測試案例。

    每個案例必須有 .wav 和 .expected.txt；.meta.json 為選填。
    target_id 非 None 時只回傳符合的案例。

    Args:
        target_id: 過濾指定案例 ID，None 代表回傳全部。

    Returns:
        Case 列表，依 WAV 檔名字母排序。
    """
    if not GOLDEN.exists():
        return []

    cases: list[Case] = []
    for wav in sorted(GOLDEN.glob("*.wav")):
        stem    = wav.stem
        # 案例 ID 取底線前的部分（例如 "001_hello" → "001"）
        case_id = stem.split("_", 1)[0] if "_" in stem else stem

        if target_id and case_id != target_id:
            continue   # 不符合過濾條件，跳過

        # 必須有期望輸出才算有效案例
        exp_path = wav.with_name(stem + ".expected.txt")
        if not exp_path.exists():
            print(f"SKIP {stem}: missing .expected.txt")
            continue

        expected = exp_path.read_text(encoding="utf-8").strip()

        # 嘗試讀取選填的 meta 資料（preset / app）
        meta_path = wav.with_name(stem + ".meta.json")
        preset    = None
        app       = None
        if meta_path.exists():
            try:
                meta   = json.loads(meta_path.read_text(encoding="utf-8"))
                preset = meta.get("preset")
                app    = meta.get("app")
            except Exception as e:
                print(f"WARN: {meta_path.name} parse failed: {e}")

        cases.append(Case(id=stem, wav_path=wav, expected=expected, preset=preset, app=app))

    return cases


def load_wav(path: Path) -> np.ndarray:
    """讀取 WAV 檔並回傳 float32 單聲道 16 kHz numpy 陣列。

    支援 int16（PCM）與 float32 兩種 WAV 格式。
    若原始採樣率不是 16 kHz，會進行線性重取樣（eval 不追求極致音質）。

    Args:
        path: WAV 檔路徑。

    Returns:
        float32 一維 numpy 陣列，振幅範圍 [-1.0, 1.0]，16 kHz。

    Raises:
        ValueError: 不支援的位元深度（非 int16 / float32）。
    """
    import wave
    with wave.open(str(path), "rb") as wf:
        sr  = wf.getframerate()    # 原始採樣率
        ch  = wf.getnchannels()    # 聲道數
        sw  = wf.getsampwidth()    # 位元組深度（2=int16, 4=float32）
        n   = wf.getnframes()      # 總幀數
        raw = wf.readframes(n)     # 原始 PCM 位元組

    # 根據位元深度解析為 numpy 陣列
    if sw == 2:
        # int16 PCM → 正規化到 [-1.0, 1.0]
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        # 直接當 float32 讀取（已在 [-1.0, 1.0] 範圍）
        audio = np.frombuffer(raw, dtype=np.float32)
    else:
        raise ValueError(f"Unsupported sample width {sw} in {path.name}")

    # 多聲道 → 取各聲道平均（downmix 為單聲道）
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)

    # 重取樣到 16 kHz（Whisper 要求）
    if sr != 16000:
        ratio   = 16000 / sr
        new_len = int(len(audio) * ratio)
        xs_old  = np.linspace(0, 1, len(audio))
        xs_new  = np.linspace(0, 1, new_len)
        audio   = np.interp(xs_new, xs_old, audio).astype(np.float32)

    return audio


# ── 評估指標 ──────────────────────────────────────────────────────────────────

# 比對英文單字的 regex（2 個字母以上才算，過濾單字母無意義字元）
_ENGLISH_WORD = re.compile(r"[a-zA-Z]{2,}")

# 常見填充詞 pattern（中文語氣詞 + 英文 filler）
_FILLER_PATTERNS = [
    r"嗯+", r"呃+", r"啊+", r"那個",
    r"\b(um|uh|like|you know)\b",
]


def count_english_words(text: str) -> set[str]:
    """從文字中提取所有英文單字（小寫，去重）。"""
    return {m.group(0).lower() for m in _ENGLISH_WORD.finditer(text)}


def english_preserved_ratio(raw: str, polished: str) -> float:
    """計算潤飾後英文單字的保留率。

    1.0 = 所有英文都保留，0.5 = 一半被刪掉或翻譯。
    若原文沒有英文單字，回傳 1.0（不扣分）。

    Args:
        raw:      Whisper 原文。
        polished: 潤飾後文字。

    Returns:
        保留率，範圍 [0.0, 1.0]，四捨五入到小數後三位。
    """
    raw_words = count_english_words(raw)
    if not raw_words:
        return 1.0   # 原文無英文，不評估此指標

    pol_words = count_english_words(polished)
    kept      = len(raw_words & pol_words)   # 兩個集合的交集 = 保留的單字數
    return round(kept / len(raw_words), 3)


def filler_removed_estimate(raw: str, polished: str) -> int:
    """估計潤飾後刪除了多少填充詞。

    負值代表潤飾反而增加了填充詞（異常情況）。

    Args:
        raw:      Whisper 原文。
        polished: 潤飾後文字。

    Returns:
        刪除的填充詞次數（正值=有刪、負值=異常增加）。
    """
    def _count(t: str) -> int:
        return sum(len(re.findall(p, t, re.IGNORECASE)) for p in _FILLER_PATTERNS)
    return _count(raw) - _count(polished)


# ── 主要執行邏輯 ──────────────────────────────────────────────────────────────

def run(cases: list[Case], use_polish: bool, model_override: Optional[str]) -> list[dict]:
    """執行所有測試案例，回傳每個案例的詳細結果。

    Args:
        cases:          測試案例列表（由 discover_cases() 取得）。
        use_polish:     是否執行 Ollama 潤飾階段。
        model_override: 覆寫設定檔中的 Ollama 模型名稱（None 代表使用設定值）。

    Returns:
        每個案例一個 dict 的列表，欄位與 CSV 輸出格式相同。
    """
    from transcriber import Transcriber
    from ollama_client import OllamaClient, OllamaConfig
    from config import Config
    import presets as _presets
    import dictionary as _dictionary

    # 載入設定
    cfg         = Config.load()
    transcriber = Transcriber()
    transcriber.warmup(cfg.model)   # 預熱以避免第一筆案例耗時失真

    # 初始化 Ollama 客戶端（若需要潤飾）
    ollama = None
    if use_polish:
        ollama = OllamaClient(OllamaConfig(
            base_url=cfg.ollama_base_url,
            model=model_override or cfg.ollama_model,
            timeout_seconds=cfg.ollama_timeout,
            enabled=True,
        ))
        if not ollama.health_check_sync():
            print("ERROR: Ollama 不可用；加 --no-polish 只跑 Whisper")
            sys.exit(2)

    # 讀取個人字典術語
    dict_terms = _dictionary.load_terms(_dictionary.DEFAULT_PATH) if cfg.dictionary_enabled else []
    transcriber.set_dictionary_terms(dict_terms)

    rows: list[dict] = []
    for c in cases:
        # 讀取並正規化 WAV
        audio = load_wav(c.wav_path)

        # Whisper 轉錄
        t0         = time.perf_counter()
        r          = transcriber.transcribe(audio, model_size=cfg.model, language=cfg.get_whisper_language())
        whisper_ms = int((time.perf_counter() - t0) * 1000)
        raw        = r.text.strip()

        # Ollama 潤飾（選填）
        polished    = ""
        polish_ms   = 0
        polish_err  = None
        preset_name = c.preset or "default"

        if ollama:
            # 決定 preset：meta 指定的優先，否則走自動路由
            if c.preset and c.preset in _presets.PRESETS:
                # meta.json 明確指定 preset → 直接使用
                p      = _presets.PRESETS[c.preset]
                llm_in = raw
            else:
                # 自動路由：依 raw 文字與前景 App 選擇 preset
                sel         = _presets.select_preset(raw, c.app, enabled=cfg.preset_overrides)
                p           = sel.preset
                llm_in      = sel.text      # 可能已剝除關鍵字前綴
                preset_name = p.name

            t0        = time.perf_counter()
            resp      = ollama.process(
                llm_in,
                prompt_template=p.resolve_prompt(),
                dictionary_terms=dict_terms if cfg.dictionary_enabled else None,
                preset_name=p.name,
            )
            polish_ms  = int((time.perf_counter() - t0) * 1000)
            polished   = resp.text
            polish_err = resp.error

        # 組裝結果列
        row = {
            "id":                       c.id,
            "preset":                   preset_name,
            "raw_text":                 raw,
            "polished_text":            polished,
            "expected_text":            c.expected,
            "whisper_ms":               whisper_ms,
            "polish_ms":                polish_ms,
            "len_raw":                  len(raw),
            "len_polished":             len(polished),
            "len_expected":             len(c.expected),
            "english_preserved_ratio":  english_preserved_ratio(raw, polished or raw),
            "filler_removed_estimate":  filler_removed_estimate(raw, polished or raw),
            "matches_expected":         (polished.strip() == c.expected.strip()) if polished else False,
            "polish_error":             polish_err or "",
        }
        rows.append(row)

        # 即時輸出案例結果
        ok = "✓" if row["matches_expected"] else ("–" if not polished else "×")
        print(f"  [{ok}] {c.id}  whisper={whisper_ms}ms polish={polish_ms}ms  "
              f"raw='{raw[:40]}...' → polished='{(polished or '(n/a)')[:40]}...'")

    return rows


def write_csv(rows: list[dict]) -> Path:
    """將結果寫入時間戳命名的 CSV 檔。

    Args:
        rows: run() 回傳的結果列表。

    Returns:
        CSV 檔的路徑。
    """
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out   = REPORTS / f"{stamp}.csv"

    if not rows:
        out.write_text("", encoding="utf-8")
        return out

    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    return out


def print_summary(rows: list[dict]) -> None:
    """在 console 輸出彙總統計。"""
    n = len(rows)
    if n == 0:
        print("\n(沒有測試案例；把 .wav + .expected.txt 放進 tests/golden_set/)")
        return

    matches  = sum(1 for r in rows if r["matches_expected"])
    fill_sum = sum(r["filler_removed_estimate"] for r in rows)
    eng_avg  = sum(r["english_preserved_ratio"] for r in rows) / n
    w_ms_avg = sum(r["whisper_ms"] for r in rows) / n
    p_ms_avg = sum(r["polish_ms"] for r in rows) / n
    errs     = sum(1 for r in rows if r["polish_error"])

    print(f"\n── 彙總（{n} 條）──")
    print(f"  完全符合預期:        {matches}/{n}")
    print(f"  英文保留率平均:      {eng_avg:.1%}")
    print(f"  Filler 刪除總計:     {fill_sum}")
    print(f"  Whisper 平均耗時:    {w_ms_avg:.0f} ms")
    print(f"  Ollama 平均耗時:     {p_ms_avg:.0f} ms")
    print(f"  潤飾錯誤:            {errs}")


def main() -> None:
    """CLI 入口：解析參數、執行測試、輸出報告。"""
    ap = argparse.ArgumentParser(description="Whisper Pro 回歸測試執行器")
    ap.add_argument("--id",        type=str, default=None, help="只跑指定 ID")
    ap.add_argument("--no-polish", action="store_true",    help="只跑 Whisper（跳過潤飾）")
    ap.add_argument("--model",     type=str, default=None, help="覆寫 Ollama 模型名")
    args = ap.parse_args()

    cases = discover_cases(args.id)
    if not cases:
        print("找不到任何測試案例。請把 .wav + .expected.txt 放入 tests/golden_set/")
        print(f"（目錄：{GOLDEN}）")
        sys.exit(1)

    print(f"共 {len(cases)} 條測試案例")
    rows     = run(cases, use_polish=not args.no_polish, model_override=args.model)
    csv_path = write_csv(rows)
    print_summary(rows)
    print(f"\nCSV：{csv_path}")


if __name__ == "__main__":
    main()
