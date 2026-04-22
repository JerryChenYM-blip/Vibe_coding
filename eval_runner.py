#!/usr/bin/env python3
"""
Phase 1.5 Regression Eval Runner — Speakly 對標計畫的測試基盤。

輸入：tests/golden_set/*.wav + 同名 .expected.txt（選填 .meta.json）
管線：Whisper → Ollama polish（可選）
輸出：
  tests/reports/{timestamp}.csv  —— 每列一個測試案例
  console                         —— 彙總統計

CSV 欄位：
  id, raw_text, polished_text, expected_text,
  preset, whisper_ms, polish_ms,
  len_raw, len_polished, len_expected,
  english_preserved_ratio, filler_removed_estimate,
  matches_expected, polish_error

用法：
  venv/bin/python3 eval_runner.py                    # 跑全部
  venv/bin/python3 eval_runner.py --id 001           # 只跑指定 ID
  venv/bin/python3 eval_runner.py --no-polish        # 只跑 Whisper
  venv/bin/python3 eval_runner.py --model qwen2.5:3b # 指定 Ollama 模型
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

ROOT = Path(__file__).parent
GOLDEN = ROOT / "tests" / "golden_set"
REPORTS = ROOT / "tests" / "reports"


# ── 資料讀取 ─────────────────────────────────────────────────────────────────

@dataclass
class Case:
    id:       str
    wav_path: Path
    expected: str
    preset:   Optional[str]
    app:      Optional[str]


def discover_cases(target_id: Optional[str]) -> list[Case]:
    if not GOLDEN.exists():
        return []
    cases: list[Case] = []
    for wav in sorted(GOLDEN.glob("*.wav")):
        stem = wav.stem
        case_id = stem.split("_", 1)[0] if "_" in stem else stem
        if target_id and case_id != target_id:
            continue
        exp_path = wav.with_name(stem + ".expected.txt")
        if not exp_path.exists():
            print(f"SKIP {stem}: missing .expected.txt")
            continue
        expected = exp_path.read_text(encoding="utf-8").strip()
        meta_path = wav.with_name(stem + ".meta.json")
        preset = None
        app    = None
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                preset = meta.get("preset")
                app    = meta.get("app")
            except Exception as e:
                print(f"WARN: {meta_path.name} parse failed: {e}")
        cases.append(Case(id=stem, wav_path=wav, expected=expected, preset=preset, app=app))
    return cases


def load_wav(path: Path) -> np.ndarray:
    """回傳 float32 mono 16 kHz numpy。支援 int16 / float32 WAV。"""
    import wave
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        n  = wf.getnframes()
        raw = wf.readframes(n)
    if sw == 2:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        audio = np.frombuffer(raw, dtype=np.float32)
    else:
        raise ValueError(f"Unsupported sample width {sw} in {path.name}")
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        # 簡單線性重取樣：eval 不追求極致音質
        ratio = 16000 / sr
        new_len = int(len(audio) * ratio)
        xs_old = np.linspace(0, 1, len(audio))
        xs_new = np.linspace(0, 1, new_len)
        audio = np.interp(xs_new, xs_old, audio).astype(np.float32)
    return audio


# ── 指標 ──────────────────────────────────────────────────────────────────────

_ENGLISH_WORD = re.compile(r"[a-zA-Z]{2,}")
_FILLER_PATTERNS = [
    r"嗯+",  r"呃+",  r"啊+", r"那個",
    r"\b(um|uh|like|you know)\b",
]


def count_english_words(text: str) -> set[str]:
    return {m.group(0).lower() for m in _ENGLISH_WORD.finditer(text)}


def english_preserved_ratio(raw: str, polished: str) -> float:
    raw_words = count_english_words(raw)
    if not raw_words:
        return 1.0
    pol_words = count_english_words(polished)
    kept = len(raw_words & pol_words)
    return round(kept / len(raw_words), 3)


def filler_removed_estimate(raw: str, polished: str) -> int:
    """數 raw 中的 filler 出現次數，減掉 polished 中的。負值代表反而增加。"""
    def _count(t: str) -> int:
        return sum(len(re.findall(p, t, re.IGNORECASE)) for p in _FILLER_PATTERNS)
    return _count(raw) - _count(polished)


# ── 主跑流程 ─────────────────────────────────────────────────────────────────

def run(cases: list[Case], use_polish: bool, model_override: Optional[str]) -> list[dict]:
    from transcriber import Transcriber
    from ollama_client import OllamaClient, OllamaConfig
    from config import Config
    import presets as _presets
    import dictionary as _dictionary

    cfg = Config.load()
    transcriber = Transcriber()
    transcriber.warmup(cfg.model)

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

    dict_terms = _dictionary.load_terms(_dictionary.DEFAULT_PATH) if cfg.dictionary_enabled else []
    transcriber.set_dictionary_terms(dict_terms)

    rows: list[dict] = []
    for c in cases:
        audio = load_wav(c.wav_path)
        t0 = time.perf_counter()
        r = transcriber.transcribe(audio, model_size=cfg.model, language=cfg.get_whisper_language())
        whisper_ms = int((time.perf_counter() - t0) * 1000)
        raw = r.text.strip()

        polished = ""
        polish_ms = 0
        polish_err = None
        preset_name = c.preset or "default"
        if ollama:
            # 根據 meta preset/app 決定路由；若 meta 指定 preset 直接取該 prompt
            if c.preset and c.preset in _presets.PRESETS:
                p = _presets.PRESETS[c.preset]
                llm_in = raw
            else:
                sel = _presets.select_preset(raw, c.app, enabled=cfg.preset_overrides)
                p = sel.preset
                llm_in = sel.text
                preset_name = p.name
            t0 = time.perf_counter()
            resp = ollama.process(
                llm_in,
                prompt_template=p.resolve_prompt(),
                dictionary_terms=dict_terms if cfg.dictionary_enabled else None,
                preset_name=p.name,
            )
            polish_ms = int((time.perf_counter() - t0) * 1000)
            polished = resp.text
            polish_err = resp.error

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
        ok = "✓" if row["matches_expected"] else ("–" if not polished else "×")
        print(f"  [{ok}] {c.id}  whisper={whisper_ms}ms polish={polish_ms}ms  "
              f"raw='{raw[:40]}...' → polished='{(polished or '(n/a)')[:40]}...'")
    return rows


def write_csv(rows: list[dict]) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = REPORTS / f"{stamp}.csv"
    if not rows:
        out.write_text("", encoding="utf-8")
        return out
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return out


def print_summary(rows: list[dict]) -> None:
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--id",        type=str, default=None, help="只跑指定 ID")
    ap.add_argument("--no-polish", action="store_true",    help="只跑 Whisper")
    ap.add_argument("--model",     type=str, default=None, help="覆寫 Ollama 模型名")
    args = ap.parse_args()

    cases = discover_cases(args.id)
    if not cases:
        print("找不到任何測試案例。請把 .wav + .expected.txt 放入 tests/golden_set/")
        print(f"（目錄：{GOLDEN}）")
        sys.exit(1)
    print(f"共 {len(cases)} 條測試案例")

    rows = run(cases, use_polish=not args.no_polish, model_override=args.model)
    csv_path = write_csv(rows)
    print_summary(rows)
    print(f"\nCSV：{csv_path}")


if __name__ == "__main__":
    main()
