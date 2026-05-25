#!/bin/bash
# Whisper Pro 強制重啟工具（v2.18.1）
#
# 用途：當老 process 沒死、新 App 啟動 hang 時，用這支腳本一鍵把所有殘留
# Python / Helper process 殺掉，再從 ~/Applications/WhisperPro.app 重啟。
#
# 觸發場景：
#   - 雙擊 .app 沒反應、Dock 上 Whisper Pro icon 在跳但視窗沒開
#   - hotkey ⌘⌥R 按了沒反應、log 也沒新訊息
#   - 開發中改完 code 想完全重啟（venv python 模式跟 .app 模式都會處理）
#
# 用法：
#   bash scripts/restart.sh
#   或 chmod +x 後直接 ./scripts/restart.sh

set -euo pipefail

echo "==> Whisper Pro restart"
echo "[1/4] 殺所有 main.py Python process..."
pkill -9 -f "Python.*main\.py" 2>/dev/null || true

echo "[2/4] 殺所有 WhisperPro / Helper process..."
pkill -9 -f "WhisperPro" 2>/dev/null || true
pkill -9 -f "Whisper Pro Helper" 2>/dev/null || true

echo "[3/4] 等 2 秒讓 process 真的死透..."
sleep 2

# 確認 ps 沒殘留
remaining=$(pgrep -f "Python.*main\.py" 2>/dev/null || true)
if [ -n "$remaining" ]; then
    echo "WARN: 還有殘留 PID: $remaining，再強殺一次"
    echo "$remaining" | xargs kill -9 2>/dev/null || true
    sleep 1
fi

# 清掉可能的 stale pidfile（lockfile 機制會自己處理，但這裡先清一道避免下次混淆）
PID_FILE="$HOME/.whisper_app/whisper_pro.pid"
if [ -f "$PID_FILE" ]; then
    echo "    清掉 stale pidfile: $PID_FILE"
    rm -f "$PID_FILE"
fi

echo "[4/4] 啟動 ~/Applications/WhisperPro.app..."
if [ -d "$HOME/Applications/WhisperPro.app" ]; then
    open "$HOME/Applications/WhisperPro.app"
else
    echo "ERROR: $HOME/Applications/WhisperPro.app 不存在"
    echo "       請先執行 bash build_app.sh 重建 .app bundle"
    exit 1
fi

echo ""
echo "==> 已重啟、請觀察 startup log（Ctrl+C 結束 tail）："
echo "    tail -f ~/.whisper_app/logs/whisper_app.log"
echo ""
echo "    或直接執行下面這行："
echo "    tail -f ~/.whisper_app/logs/whisper_app.log"
