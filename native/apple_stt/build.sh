#!/bin/bash
# 編譯 whisperpro-apple-stt（macOS 26 原生語音辨識的命令列包裝）
#
# 為什麼要有這一步：這是整個專案唯一需要編譯的東西。build_app.sh 只做組裝
# （複製檔案、寫 plist、簽章），不編譯任何原生程式碼，所以刻意分成獨立腳本，
# 不塞進 build_app.sh——打包 .app 跟編譯 helper 是兩件互不相干的事，
# 綁在一起會讓「只想重打包」的人被迫等編譯。
#
# 產出：bin/whisperpro-apple-stt
#       Python 端用 transcriber.py 的 _APPLE_HELPER_PATH 定位，算法是
#       os.path.dirname(os.path.abspath(__file__))——也就是相對於 transcriber.py
#       這個檔案自己的位置，跟行程的工作目錄無關。所以 helper 不進 .app bundle，
#       只要它跟 transcriber.py 維持同一份原始碼樹的相對位置就找得到。
#       （要搬動的話，改 _APPLE_HELPER_PATH，不是改工作目錄。）
#
# 用法：bash native/apple_stt/build.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$HERE/bin"
OUT="$OUT_DIR/whisperpro-apple-stt"
SRC="$HERE/AppleSTT.swift"

# ── 前置檢查 ──────────────────────────────────────────────────────────────
if ! command -v xcrun >/dev/null 2>&1; then
    echo "✗ 找不到 xcrun，請先安裝 Xcode 或 Command Line Tools" >&2
    exit 1
fi
if ! xcrun --find swiftc >/dev/null 2>&1; then
    echo "✗ 找不到 swiftc。若剛裝好 Xcode，可能還需要：sudo xcodebuild -license" >&2
    exit 1
fi

# macOS 26 才有 SpeechAnalyzer / SpeechTranscriber；更舊的系統編不過也跑不了
OS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
if [ "$OS_MAJOR" -lt 26 ]; then
    echo "✗ 需要 macOS 26 以上（目前 $(sw_vers -productVersion)）" >&2
    echo "  新的語音辨識引擎是 macOS 26 才加入的，舊系統沒有這些 API。" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

# ── 編譯 ──────────────────────────────────────────────────────────────────
# -parse-as-library：單一 .swift 檔預設會被當成 top-level code，@main 會編不過
# -target macos26.0：對齊 API 的 availability 標註，少掉一堆 if #available
echo "→ 編譯 $SRC"
xcrun swiftc -O -parse-as-library \
    -target arm64-apple-macos26.0 \
    -o "$OUT" "$SRC"

# ad-hoc 簽章：跟 build_app.sh 對 .app 的作法一致（都是 `--sign -`）。
# 沒有簽章的話，某些 Gatekeeper 設定下會被擋掉。
codesign --force --sign - "$OUT" 2>/dev/null || true

echo "✓ 完成：$OUT"

# ── 自我驗證 ──────────────────────────────────────────────────────────────
# 編出來不代表能跑。這裡直接探測一次，把引擎與模型狀態印出來——
# 「編譯成功」與「這台機器真的能辨識」是兩件事，不要只驗前者。
echo "→ 探測引擎狀態"
"$OUT" --probe --locale zh-TW
