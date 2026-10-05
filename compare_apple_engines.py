"""比較蘋果「一般引擎」與「聽寫引擎」在使用者真人錄音上的表現（v2.32.0 第三階段）。

背景：2026-10-05 合成語音實驗——聽寫引擎中文較準（紫微斗數對、繁體原生正確），但會把句中英文
吃掉。合成語音的英文發音很差，不能下結論。v2.32.0 起，每次蘋果辨識完會在背景用聽寫引擎把同一段
錄音再辨識一次（到 cfg.apple_shadow_until 為止），兩份文字都寫進 ~/.whisper_app/transcribe_log.jsonl。
這支工具把兩份配對起來比較，用來決定「哪種講話該用哪個引擎」。

沒有標準答案（App 不存錄音、使用者沒有逐句校對），所以這裡量的是：
  ① 英文被吃掉的比例：一般引擎有英文字、聽寫引擎沒有（或變少）
  ② 兩者不一樣的程度：中文字的編輯距離
  ③ 字典詞命中：使用者字典裡的詞各出現幾次（專有名詞辨識的間接指標）
  ④ 列出不一樣的句子給人判斷（--show N），只印在終端機、不寫檔

用法：
    venv/bin/python3 compare_apple_engines.py              # 只看統計
    venv/bin/python3 compare_apple_engines.py --show 30    # 加印 30 組不一樣的句子

隱私：讀的是本機紀錄、輸出只到終端機；不要把 --show 的輸出貼進版控或文件。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

LOG = Path.home() / ".whisper_app" / "transcribe_log.jsonl"
DICT = Path.home() / ".whisper_app" / "dictionary.json"

_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9.+\-]*")
_CJK = re.compile(r"[一-鿿]")


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
        prev = cur
    return prev[-1]


def _cjk_only(text: str) -> str:
    return "".join(_CJK.findall(text))


def load_pairs(log_path: Path) -> list[dict]:
    """依錄音編號（pipeline_id）把一般引擎的轉錄紀錄與聽寫引擎的對照紀錄配對。"""
    main: dict[str, str] = {}
    shadow: dict[str, str] = {}
    for line in log_path.open(encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        pid = d.get("pipeline_id") or ""
        if not pid:
            continue
        if d.get("type") == "transcribe" and d.get("model") == "apple-speech" \
                and (d.get("apple") or {}).get("streamed"):
            # raw_text：一般引擎的結果（還沒套換字規則），跟對照組在同一個比較基準上
            text = (d.get("raw_text") or "").strip()
            if text and not text.startswith("（"):
                main[pid] = text
        elif d.get("type") == "apple_shadow" and d.get("ok"):
            shadow[pid] = (d.get("text") or "").strip()
    return [{"pid": p, "speech": main[p], "dictation": shadow[p]} for p in main if p in shadow]


def summarize(pairs: list[dict], terms: list[str]) -> dict:
    with_english = [p for p in pairs if _LATIN_WORD.search(p["speech"])]
    dropped = [p for p in with_english
               if len(_LATIN_WORD.findall(p["dictation"])) < len(_LATIN_WORD.findall(p["speech"]))]
    chinese_only = [p for p in pairs if not _LATIN_WORD.search(p["speech"])]
    diffs = []
    for p in chinese_only:
        a, b = _cjk_only(p["speech"]), _cjk_only(p["dictation"])
        if a or b:
            diffs.append(_edit_distance(a, b) / max(1, len(a), len(b)))
    term_hits = {"speech": 0, "dictation": 0}
    for p in pairs:
        for t in terms:
            term_hits["speech"] += p["speech"].count(t)
            term_hits["dictation"] += p["dictation"].count(t)
    return {
        "pairs": len(pairs),
        "with_english": len(with_english),
        "english_dropped": len(dropped),
        "chinese_only": len(chinese_only),
        "chinese_identical": sum(1 for x in diffs if x == 0),
        "chinese_diff_median": sorted(diffs)[len(diffs) // 2] if diffs else None,
        "term_hits": term_hits,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--show", type=int, default=0, help="印出幾組不一樣的句子給人判斷")
    ap.add_argument("--log", type=Path, default=LOG)
    args = ap.parse_args()

    # 跟 App 用同一支讀法：兩種檔案格式都吃、空字串會跳過（空字串會讓命中次數暴增）
    from dictionary import load_terms
    terms = load_terms(DICT)

    pairs = load_pairs(args.log)
    if not pairs:
        print("還沒有可以配對的資料（需要 v2.32.0 以後、開著對照組錄的蘋果辨識）。")
        return
    s = summarize(pairs, terms)
    print(f"配對成功 {s['pairs']} 組")
    print(f"① 含英文的 {s['with_english']} 組，其中聽寫引擎英文變少／不見：{s['english_dropped']} 組")
    if s["chinese_diff_median"] is not None:
        print(f"② 純中文 {s['chinese_only']} 組：完全一樣 {s['chinese_identical']} 組，"
              f"差異程度中位數 {s['chinese_diff_median']:.1%}")
    print(f"③ 字典詞出現次數：一般 {s['term_hits']['speech']}／聽寫 {s['term_hits']['dictation']}")
    print()
    print("怎麼解讀（判斷要看 ④ 的實際句子，數字只是方向）：")
    print("  • ① 的比例高 → 有英文的句子不能用聽寫引擎")
    print("  • 純中文差異處，逐句看哪邊對；聽寫引擎多數較對 → 可以考慮「純中文用聽寫引擎」")

    if args.show:
        print(f"\n④ 不一樣的句子（最多 {args.show} 組）：")
        shown = 0
        for p in pairs:
            if p["speech"] == p["dictation"]:
                continue
            print(f"  一般：{p['speech']}\n  聽寫：{p['dictation']}\n")
            shown += 1
            if shown >= args.show:
                break


if __name__ == "__main__":
    main()
