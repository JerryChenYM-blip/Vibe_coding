# Whisper 語音轉文字工具 - 專案優化計劃 (Optimization Plan)

本計畫旨在提升「Whisper 語音轉文字工具」的效能穩定性、即時響應能力以及使用者體驗。以下建議基於對現有程式碼架構（Python + customtkinter + faster-whisper/mlx-whisper）的深度分析。

---

## 1. 核心效能優化 (Core Performance)

### 1.1 音訊緩衝區管理優化
*   **現狀分析**：`recorder.py` 中的 `_frames` 採用 Python List 持續追加音訊數據。長時間錄音會導致記憶體線性增長。
*   **優化方案**：
    *   引入 `collections.deque` 並設定 `maxlen` 作為環形緩衝區（適用於短時間即時回饋）。
    *   對於完整錄音，實作**分段暫存機制**：每隔固定時間（如 5 分鐘）將記憶體中的音訊數據寫入系統臨時目錄 (`/tmp`) 的 `.wav` 檔，錄音結束後再合併。
*   **預期效果**：顯著降低長時會議錄音時的 RAM 佔用。

### 1.2 模型載入與記憶體回收 (LRU Cache)
*   **現狀分析**：切換模型時，舊模型可能仍殘留在 VRAM/RAM 中，且缺乏明確的釋放機制。
*   **優化方案**：
    *   在 `transcriber.py` 中實作一個簡單的 **LRU (Least Recently Used) 緩存**。
    *   當使用者切換模型時，主動呼叫 `del model` 並執行 `gc.collect()`；若為 MLX 後端，確保釋放 Metal 加速相關資源。
*   **預期效果**：避免 VRAM 溢出，讓應用程式在長時間開啟後依然保持流暢。

---

## 2. 功能即時性提升 (Responsiveness)

### 2.1 引入語音活動偵測 (VAD)
*   **現狀分析**：目前的串流轉錄 (`_stream_tick`) 是固定時間間隔觸發，容易在話語中途切斷。
*   **優化方案**：
    *   整合 `silero-vad` 或 `webrtcvad`。
    *   **邏輯變更**：只有在偵測到「講話結束 (Silence Detected)」或「緩衝區已滿」時，才觸發 `transcribe_fast`。
*   **預期效果**：轉錄文字出現的時機更自然，且能有效減少對空白音訊的無謂運算。

---

## 3. 使用者體驗與穩定性 (UX & Stability)

### 3.1 異常處理與主動引導
*   **現狀分析**：若 Ollama 未啟動或麥克風權限遺失，使用者可能無法及時得知原因。
*   **優化方案**：
    *   **啟動檢查**：在 `main.py` 啟動時異步檢查 Ollama API 是否可用，並在 UI 顯示狀態燈。
    *   **權限對話框**：若 `pynput` 或 `sounddevice` 報權限錯誤，主動跳出帶有「開啟系統設定」按鈕的 `CTkMessagebox`。
*   **預期效果**：降低技術門檻，讓非開發者也能輕鬆排除故障。

### 3.2 樣式與佈局微調
*   **現狀分析**：目前的 `customtkinter` 介面已具備基本框架，但在 macOS 上的間距與字體渲染可進一步優化。
*   **優化方案**：
    *   統一使用 `system-ui` 字體家族。
    *   為長文字輸出區域增加自動滾動 (Auto-scroll) 的開關。

---

## 4. 發布與部署優化 (Deployment)

### 4.1 獨立應用程式打包 (.app)
*   **優化方案**：
    *   撰寫 `spec` 檔案 (PyInstaller)，將 `libportaudio` 與 Python 執行環境封裝。
    *   加入 `Info.plist` 宣告 `NSMicrophoneUsageDescription` 與 `NSAccessibilityUsageDescription`，解決 macOS 安全性權限提示問題。
*   **預期效果**：使用者無需安裝 Python 或 Homebrew 即可直接運行。

---

## 實施建議順序 (Priority)

1.  **High**: VAD 語音偵測 (直接影響使用感)。
2.  **High**: 異常處理與權限引導 (解決基本可用性)。
3.  **Medium**: 音訊分段暫存與模型釋放 (穩定性優化)。
4.  **Low**: 打包流程與 UI 樣式微調。
