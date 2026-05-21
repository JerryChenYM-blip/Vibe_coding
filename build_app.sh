#!/bin/bash
# build_app.sh — 建構 Whisper Pro 薄殼 .app bundle
#
# 目的：解決 macOS TCC（麥克風 / 輔助使用 / AppleScript）按 responsible process
# 歸帳的問題。從不同終端機 / shell 啟動 main.py 會被歸到不同的 responsible
# process，每個都要重新授權。包成 .app bundle 後，所有路徑都歸帳到同一個
# bundle ID（com.jerrychen.whisperpro），授權一次永遠有效。
#
# 用法：
#   bash build_app.sh
#
# 結果：
#   ~/Applications/WhisperPro.app/
#
# 這是「薄殼」做法 —— .app 內不含 Python interpreter，只放一個 bash 啟動
# 腳本，exec 出去呼叫專案 venv 的 python3 main.py。

set -euo pipefail

# ─── 設定 ────────────────────────────────────────────────────────────
PROJECT_DIR="/Users/jerrychen/project/Claude_code"
APP_ROOT="$HOME/Applications/WhisperPro.app"
CONTENTS_DIR="$APP_ROOT/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
LAUNCHER="$MACOS_DIR/WhisperPro"
PLIST="$CONTENTS_DIR/Info.plist"
ICON_DEST="$RESOURCES_DIR/AppIcon.icns"

# ─── Step 1：建立 bundle 結構 ────────────────────────────────────────
mkdir -p "$HOME/Applications"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"

# ─── Step 2：寫 Info.plist ───────────────────────────────────────────
cat > "$PLIST" <<'PLIST_EOF'
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
    <string>2.3.0</string>
    <key>CFBundleShortVersionString</key>
    <string>2.3.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>LSUIElement</key>
    <false/>
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
# 關鍵：用 exec 替換 process，確保 responsible process 鏈
# = WhisperPro.app → python3（bash 不殘留為中間層）
cat > "$LAUNCHER" <<'LAUNCHER_EOF'
#!/bin/bash
# Whisper Pro launcher — keeps macOS Privacy/TCC attribution on this .app bundle.
# 用 exec 替換 process，確保 responsible process 鏈 = WhisperPro.app → python3

PROJECT_DIR="/Users/jerrychen/project/Claude_code"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
    osascript -e "display alert \"Whisper Pro 啟動失敗\" message \"找不到 Python venv：$VENV_PYTHON\" as critical"
    exit 1
fi

cd "$PROJECT_DIR" || {
    osascript -e "display alert \"Whisper Pro 啟動失敗\" message \"無法切換到專案目錄：$PROJECT_DIR\" as critical"
    exit 1
}

exec "$VENV_PYTHON" main.py
LAUNCHER_EOF

chmod +x "$LAUNCHER"

# ─── Step 4：複製圖示 ───────────────────────────────────────────────
if [[ -f "$PROJECT_DIR/assets/WhisperPro.icns" ]]; then
    cp "$PROJECT_DIR/assets/WhisperPro.icns" "$ICON_DEST"
    echo "  ✓ 圖示：assets/WhisperPro.icns → Resources/AppIcon.icns"
elif [[ -f "$PROJECT_DIR/assets/icon.icns" ]]; then
    cp "$PROJECT_DIR/assets/icon.icns" "$ICON_DEST"
    echo "  ✓ 圖示：assets/icon.icns → Resources/AppIcon.icns"
else
    echo "  ⚠ 警告：找不到 assets/WhisperPro.icns 或 assets/icon.icns；App 將以預設圖示顯示"
fi

# ─── Step 5：清掉 quarantine flag ───────────────────────────────────
xattr -cr "$APP_ROOT" 2>/dev/null || true

# ─── Step 6：報告結果 ───────────────────────────────────────────────
cat <<'REPORT_EOF'

✓ WhisperPro.app 建構完成
位置：~/Applications/WhisperPro.app

首次啟動：
  1. 用 Spotlight 搜 "Whisper Pro"（或從 Finder 雙擊 ~/Applications/WhisperPro.app）
  2. 第一次跳 Gatekeeper 警告：右鍵 → 打開（或系統設定 → 隱私權與安全性 → 允許）
  3. 按熱鍵開始錄音時會跳 3 個權限對話框，全部「允許」

之後不管從哪裡開（Spotlight / Dock / Finder）權限都會延續，不用重複授權。
REPORT_EOF
