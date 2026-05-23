#!/bin/bash
# build_app.sh — 建構 Whisper Pro .app bundle（含 Python shim）
#
# 目的：解決 macOS TCC（麥克風 / 輔助使用 / AppleScript）的兩個問題：
#   1. responsible process 歸帳 — 從不同終端機 / shell 啟動 main.py 會歸到不同的
#      responsible process，每個都要重新授權。包成 .app 後一律歸帳到 bundle ID。
#   2. silent deny — 系統 Python.app 的 Info.plist 沒有 NSMicrophoneUsageDescription
#      等字串，macOS TCC 無法跳對話框，直接 silent deny。
#      解法：在 .app 內塞一個複製來的 Python.app shim，改它的 Info.plist 加 4 條
#      usage description + 新 bundle id（com.jerrychen.whisperpro.python）。
#
# 用法：
#   bash build_app.sh
#
# 結果：
#   ~/Applications/WhisperPro.app/
#     Contents/
#       Info.plist            主 bundle（com.jerrychen.whisperpro）
#       MacOS/WhisperPro      launcher，exec 出去呼叫 shim Python
#       Frameworks/Python.app shim（com.jerrychen.whisperpro.python，含 4 條 usage description）
#       Resources/AppIcon.icns

set -euo pipefail

# ─── 設定 ────────────────────────────────────────────────────────────
PROJECT_DIR="/Users/jerrychen/project/Claude_code"

# 從 _version.py 讀單一真相版本字串。發版流程：改 _version.py → 跑本 script。
VERSION=$(grep -E '^__version__\s*=' "$PROJECT_DIR/_version.py" | sed -E 's/.*"([^"]+)".*/\1/')
if [ -z "$VERSION" ]; then
    echo "ERROR: cannot parse __version__ from $PROJECT_DIR/_version.py" >&2
    exit 1
fi
echo "BUILD: Whisper Pro v$VERSION"

APP_ROOT="$HOME/Applications/WhisperPro.app"
CONTENTS_DIR="$APP_ROOT/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
FRAMEWORKS_DIR="$CONTENTS_DIR/Frameworks"
LAUNCHER="$MACOS_DIR/WhisperPro"
PLIST="$CONTENTS_DIR/Info.plist"
ICON_DEST="$RESOURCES_DIR/AppIcon.icns"

SYSTEM_PYTHON_APP="/Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app"
SHIM_APP="$FRAMEWORKS_DIR/Python.app"
SHIM_PLIST="$SHIM_APP/Contents/Info.plist"

# ─── Step 1：建立 bundle 結構 ────────────────────────────────────────
mkdir -p "$HOME/Applications"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$FRAMEWORKS_DIR"

# ─── Step 2：寫主 Info.plist ─────────────────────────────────────────
# LSArchitecturePriority=arm64 讓 launchd 一開始就以 arm64 啟動 launcher，
# 不需要 arch -arm64 包裝（避免弄壞 responsibility chain）。
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>WhisperPro</string>
    <key>CFBundleDisplayName</key>
    <string>Whisper Pro</string>
    <key>CFBundleExecutable</key>
    <string>WhisperPro</string>
    <key>CFBundleIdentifier</key>
    <string>com.jerrychen.whisperpro</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>LSUIElement</key>
    <false/>
    <key>LSArchitecturePriority</key>
    <array>
        <string>arm64</string>
    </array>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Whisper Pro 需要存取麥克風以將你的語音轉為文字。</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>Whisper Pro 需要透過 AppleScript 偵測前景應用程式並自動貼上轉錄文字。</string>
    <key>NSInputMonitoringUsageDescription</key>
    <string>Whisper Pro 需要監聽全域熱鍵以啟動錄音。</string>
    <key>NSAccessibilityUsageDescription</key>
    <string>Whisper Pro 需要輔助使用權限以模擬 ⌘V 自動貼上至游標位置。</string>
</dict>
</plist>
PLIST_EOF

# ─── Step 3：寫 launcher 腳本 ────────────────────────────────────────
# 用 .app 內建 Python shim（含 NSMicrophoneUsageDescription），TCC 對話框才會跳。
cat > "$LAUNCHER" <<'LAUNCHER_EOF'
#!/bin/bash
# Whisper Pro launcher — 用 .app 內建的 Python shim，TCC 歸帳到 com.jerrychen.whisperpro.python
#
# 關鍵：
#   1. 主 Info.plist 已加 LSArchitecturePriority=arm64，launchd 會直接以 arm64 啟動本 launcher，
#      不需要 arch -arm64 包裝（避免弄壞 responsibility chain）。
#   2. 用 Contents/Frameworks/Python.app/Contents/MacOS/Python 而非 venv 的 symlink，
#      讓 TCC 歸帳到 shim 的 bundle id（含 NSMicrophoneUsageDescription 等）。
#   3. PYTHONPATH 接 venv site-packages 以提供 numpy / sounddevice / customtkinter 等套件。

PROJECT_DIR="/Users/jerrychen/project/Claude_code"
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SHIM_PYTHON="$APP_DIR/Contents/Frameworks/Python.app/Contents/MacOS/Python"
STDERR_LOG="$HOME/.whisper_app/launcher_stderr.log"

mkdir -p "$HOME/.whisper_app"
exec 2>"$STDERR_LOG"
echo "=== launcher start $(date '+%F %T') ===" >&2

if [[ ! -x "$SHIM_PYTHON" ]]; then
    osascript -e "display alert \"Whisper Pro 啟動失敗\" message \"Python shim 不存在：$SHIM_PYTHON。請重跑 build_app.sh 重建。\" as critical"
    exit 1
fi

cd "$PROJECT_DIR" || {
    osascript -e "display alert \"Whisper Pro 啟動失敗\" message \"專案目錄不存在：$PROJECT_DIR\" as critical"
    exit 1
}

# 接上 venv 的 site-packages（沒有 venv 我們 shim 也跑不起來，因為缺 numpy / sounddevice 等）
export PYTHONPATH="$PROJECT_DIR/venv/lib/python3.13/site-packages:${PYTHONPATH:-}"

exec "$SHIM_PYTHON" main.py
LAUNCHER_EOF

chmod +x "$LAUNCHER"

# ─── Step 4：複製 Python.app 並改 Info.plist（shim）──────────────────
if [[ ! -d "$SYSTEM_PYTHON_APP" ]]; then
    echo "  ✗ 錯誤：找不到系統 Python.app：$SYSTEM_PYTHON_APP"
    echo "    請先用 python.org 官方安裝包安裝 Python 3.13。"
    exit 1
fi

# 清乾淨再複製，確保 idempotent
rm -rf "$SHIM_APP"
cp -R "$SYSTEM_PYTHON_APP" "$SHIM_APP"
echo "  ✓ 複製 Python.app → Contents/Frameworks/Python.app"

# 改 shim Info.plist（先 convert 成 xml1，再用 plutil -remove/-insert 達成 idempotent）
plutil -convert xml1 "$SHIM_PLIST"

plutil -replace CFBundleIdentifier -string "com.jerrychen.whisperpro.python" "$SHIM_PLIST"
plutil -replace CFBundleName -string "Whisper Pro" "$SHIM_PLIST"
plutil -remove CFBundleDisplayName "$SHIM_PLIST" 2>/dev/null || true
plutil -insert CFBundleDisplayName -string "Whisper Pro" "$SHIM_PLIST"

for key in NSMicrophoneUsageDescription NSAccessibilityUsageDescription NSInputMonitoringUsageDescription NSAppleEventsUsageDescription; do
    plutil -remove "$key" "$SHIM_PLIST" 2>/dev/null || true
done
plutil -insert NSMicrophoneUsageDescription -string "Whisper Pro 需要存取麥克風以將你的語音轉為文字。" "$SHIM_PLIST"
plutil -insert NSAccessibilityUsageDescription -string "Whisper Pro 需要輔助使用權限以模擬 ⌘V 自動貼上至游標位置。" "$SHIM_PLIST"
plutil -insert NSInputMonitoringUsageDescription -string "Whisper Pro 需要監聽全域熱鍵以啟動錄音。" "$SHIM_PLIST"
plutil -insert NSAppleEventsUsageDescription -string "Whisper Pro 需要透過 AppleScript 偵測前景應用程式並自動貼上轉錄文字。" "$SHIM_PLIST"

plutil -lint "$SHIM_PLIST" >/dev/null
echo "  ✓ shim Info.plist 已注入 bundle id 與 4 條 usage description"

# 清掉舊簽章後 adhoc 簽 shim
xattr -cr "$SHIM_APP"
codesign --force --deep --sign - "$SHIM_APP" 2>/dev/null
echo "  ✓ shim adhoc 簽章完成（com.jerrychen.whisperpro.python）"

# ─── Step 5：複製圖示 ───────────────────────────────────────────────
if [[ -f "$PROJECT_DIR/assets/WhisperPro.icns" ]]; then
    cp "$PROJECT_DIR/assets/WhisperPro.icns" "$ICON_DEST"
    echo "  ✓ 圖示：assets/WhisperPro.icns → Resources/AppIcon.icns"
elif [[ -f "$PROJECT_DIR/assets/icon.icns" ]]; then
    cp "$PROJECT_DIR/assets/icon.icns" "$ICON_DEST"
    echo "  ✓ 圖示：assets/icon.icns → Resources/AppIcon.icns"
else
    echo "  ⚠ 警告：找不到 assets/WhisperPro.icns 或 assets/icon.icns；App 將以預設圖示顯示"
fi

# ─── Step 6：清 xattr 並重簽整個 .app ───────────────────────────────
xattr -cr "$APP_ROOT" 2>/dev/null || true
codesign --force --deep --sign - "$APP_ROOT" 2>/dev/null
echo "  ✓ WhisperPro.app adhoc 簽章完成（com.jerrychen.whisperpro）"

# ─── Step 7：清掉 TCC 舊紀錄（讓對話框首次重跳）──────────────────────
for service in Microphone Accessibility ListenEvent AppleEvents; do
    tccutil reset "$service" com.jerrychen.whisperpro.python 2>/dev/null || true
done

# ─── Step 8：報告結果 ───────────────────────────────────────────────
cat <<'REPORT_EOF'

✓ WhisperPro.app 建構完成
位置：~/Applications/WhisperPro.app

首次啟動：
  1. 用 Spotlight 搜 "Whisper Pro"（或從 Finder 雙擊 ~/Applications/WhisperPro.app）
  2. 第一次跳 Gatekeeper 警告：右鍵 → 打開（或系統設定 → 隱私權與安全性 → 允許）
  3. 按熱鍵開始錄音時會跳 3 個權限對話框，全部「允許」（會以 shim bundle
     com.jerrychen.whisperpro.python 名義出現）

之後不管從哪裡開（Spotlight / Dock / Finder）權限都會延續，不用重複授權。
REPORT_EOF
