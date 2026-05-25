# Whisper Pro

> **按一下 ⌘、講話、文字直接出現在你正在打字的地方**
>
> 完全本地、不上雲、不收費的 macOS 語音轉文字工具。

[![Version](https://img.shields.io/badge/version-v2.17.0-blue.svg)](https://github.com/JerryChenYM-blip/Vibe_coding/releases)
[![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)](#系統需求)
[![Privacy](https://img.shields.io/badge/privacy-100%25%20local-success.svg)](#為什麼選這個)

---

## 這是什麼？

一個躲在背景裡的語音轉文字小工具。**按熱鍵錄音 → 講話 → 放開 → 文字就出現在你的游標位置**。

- 在 Slack 寫訊息？按熱鍵講、文字就跑進去
- 在 Mail 寫信？同樣
- 在 Notion / Cursor / 任何 app 都可以
- **不需要打開特定軟體、不需要切換視窗**

就像 [Speakly](https://speakly.app) 那種雲端語音工具的桌面體驗，但你的聲音**從不離開你的 Mac**。

---

## 為什麼選這個

| | Whisper Pro | 雲端方案（Speakly / Otter / ...） |
|---|---|---|
| **隱私** | ✅ 100% 本地，從不上傳 | ❌ 音檔上雲、公司看得到 |
| **費用** | ✅ 完全免費 | ❌ 訂閱制（每月 $10-30）|
| **離線** | ✅ 沒網路也能用 | ❌ 沒網路就掛 |
| **配額** | ✅ 無上限 | ❌ 字數 / 分鐘配額 |
| **準確度** | ✅ SOTA 中文（Qwen3-ASR 1.7B、競爭商用 API）| ✅ 跟商用 API 同級 |

特別適合：
- 講敏感內容（病歷、律師筆記、財務、面試）
- 中英混講的開發者 / PM / 設計師
- 寫長段中文的工作者（部落格、會議紀錄、信件）
- 重視隱私的人

---

## 系統需求

| 項目 | 需求 |
|---|---|
| 電腦 | **Apple Silicon Mac**（M1 / M2 / M3 / M4）|
| 系統 | macOS 12 Monterey 以上 |
| 記憶體 | 16 GB 以上（跑 1.7B 模型用）|
| 硬碟空間 | ~10 GB（模型 + cache）|
| 網路 | 第一次安裝下載模型用、之後完全離線 |

⚠️ **Intel Mac 跟 Windows 跑不動**，App 深度綁定 Apple Silicon 的 MLX 框架。

---

## 安裝

### 1. 抓 code

```bash
git clone https://github.com/JerryChenYM-blip/Vibe_coding.git
cd Vibe_coding
```

### 2. 裝系統依賴（Homebrew）

```bash
brew install portaudio ffmpeg
```

### 3. 建 Python 環境

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. 裝 Ollama（選用、AI 潤飾用）

如果想要 AI 自動修錯字、補標點、刪贅詞，裝 Ollama：

```bash
brew install ollama
ollama serve &   # 背景跑
ollama pull qwen3.5:4b   # 下載潤飾模型（~3 GB）
```

不裝也能用、只是純語音轉文字、沒 AI 後處理。

### 5. 打包成 .app（推薦）

```bash
bash build_app.sh
```

完成後 `~/Applications/WhisperPro.app` 可從 Spotlight 啟動、所有系統授權永久綁定到這個 app。

---

## 第一次啟動

### 1. 開 App

雙擊 `~/Applications/WhisperPro.app` 或 Spotlight 搜 `Whisper Pro`。

### 2. macOS 跳權限對話框 → **全部要按「允許」**

| 權限 | 用途 |
|---|---|
| **🎙 麥克風** | 不開沒法錄音 |
| **♿️ 輔助使用** | 不開沒法接全域熱鍵 + 自動貼上 |
| **🤖 自動化（AppleScript）** | 不開沒法偵測前景 app |

第一次拒絕了？去：**系統設定 → 隱私權與安全性** 手動勾起 `Whisper Pro`。

### 3. 等下載模型

首次啟動會自動下載 **Qwen3-ASR-1.7B**（~3.4 GB）。Splash 會卡 2-8 分鐘看網速。**這只發生一次**、之後永久 cache。

下載完成、狀態列顯示「就緒 (qwen3-asr-large · ⚡ Metal)」就 OK。

---

## 怎麼用（30 秒上手）

### 基本流程

1. **打開任何 app**（Mail / Slack / Notion / 你想打字的地方）
2. 把游標放在輸入框
3. **按一下 Right ⌘**（右邊的 Command 鍵）→ 開始錄音
4. **講話**
5. **再按一下 Right ⌘** → 停止錄音
6. ~2-5 秒後、文字**自動跑進游標位置**

完成。

### 重點注意

- **熱鍵預設 = 單按右 Cmd**（要按右邊那顆、不是左邊）。可在設定改
- 錄音時螢幕下方會出現**小紅點 HUD** 提示「錄音中 0:15」
- 講完後不用切回 Whisper Pro 視窗、它在背景處理、結果直接貼到你原本的位置
- **不要等 Whisper Pro 視窗跳出來**——它不會跳出來、結果是直接貼到游標處

---

## 重要提醒

### ⚠️ 第一次跑長段（≥ 12 秒）會慢

長段語音第一次跑時、模型要 cold-load + Metal shader 編譯、可能等 5-15 秒。第二段開始正常。

### ⚠️ 雙擊熱鍵 vs 按住熱鍵

預設是 **tap toggle**（按一下開始、再按一下停止）。**不是按住錄音 / 放開停止**。要改成按住模式請改設定。

### ⚠️ 全螢幕 app（Claude / Cursor / Notion 全螢幕）

App 從 v2.16.2 起會自動防止 Space 切換、按熱鍵不會強制把你拉回 Whisper Pro 視窗。如果遇到、檢查是否更新到 v2.16.2 以上。

### ⚠️ 中英混講

中文混英文（例：「我用 Claude Code 寫程式」）支援得很好。但很罕見的英文專有名詞可能被誤辨識成中文音譯（例：Dock → 大可）、加進個人字典即可改善。

---

## 進階功能（用熟後再看）

### 個人字典

把你常用的專有名詞加進 `~/.whisper_app/dictionary.json` terms 段（例：你公司名、產品名、同事名），ASR 會偏向預測對的字。差異化核心：**雲端方案做不到、你的詞表它們不會看到**。

```json
{
  "terms": [
    {"term": "Claude Code", "note": "Anthropic CLI"},
    {"term": "潤飾", "note": "常被誤辨成潤視"}
  ],
  "corrections": [
    {"from": "Cloud Code", "to": "Claude Code"}
  ]
}
```

### AI 潤飾（Ollama）

在設定 → AI 潤飾 → **啟用**。會把錄到的原文做最小修改（補標點、刪贅詞「嗯啊」、修同音錯字），保持你的口語風格。

**Streaming Polish（v2.17.0）**：邊講邊潤、放開後幾乎立即 paste 出潤飾結果、長段 22 秒壓到 5 秒。

### 情境 preset

App 會根據你目前的前景 app 自動切換潤飾風格：
- 在 **Mail** → email 風格（補招呼語、正式分段）
- 在 **Slack** → chat 風格（短句、口語）
- 在 **Notion** → note 風格（結構分段）
- 在 **Cursor / VSCode** → code comment 風格（精簡、技術術語英文）

### Voice Shortcuts

講特定關鍵字觸發特殊處理：
- 「**翻譯英文**，...」→ 內容翻譯成自然英文
- 「**條列**，...」→ 整理成 bullet list
- 「**會議紀錄**，...」→ 整理成重點 / 行動項格式

### 歷史紀錄

每筆轉錄存進本地 SQLite（`~/.whisper_app/history.db`），可搜尋、重新潤飾、複製。隱私：100% 本地、不同步任何雲端。

---

## 模型選擇

設定 → 語音辨識 → 模型大小：

| 選項 | 速度 | 準度 | RAM | 推薦給 |
|---|---|---|---|---|
| **Qwen3-ASR 0.6B** | 快（~1s）| 好 | 1.2 GB | 想要速度優先 |
| **Qwen3-ASR 1.7B** ★（預設） | 中（~2s）| **最強** | 3.4 GB | 16GB+ Mac 中文使用者 |
| Whisper Large V3 Turbo | 快（~1s）| 好 | 800 MB | Whisper fallback |

切換後自動下載 + 暖機、下次按熱鍵就用新模型。

---

## 隱私聲明

| 項目 | 處理方式 |
|---|---|
| 你的音檔 | **不存、不傳**。錄完轉成文字立即釋放記憶體 |
| 你的文字 | **本地 SQLite 歷史紀錄**（`~/.whisper_app/history.db`）、永遠不離開你的 Mac |
| 字典術語 | **本地 JSON 檔**、不同步雲端 |
| AI 模型 | 第一次從 HuggingFace 下載、之後全離線運算 |
| 網路通訊 | **零**（除了首次模型下載） |

跟 Speakly / Otter / Whisper API 等雲端方案的根本差異：**你的內容從不離開你的 Mac**。

---

## 常見問題

### 按熱鍵沒反應？

1. 確認 macOS 設定 → 隱私 → 輔助使用 → ✅ Whisper Pro
2. 確認啟動的是 `~/Applications/WhisperPro.app`（不是 terminal 跑 `python main.py`）
3. 看 `~/.whisper_app/logs/whisper_app.log` 找 `HOTKEY` 紀錄

### 麥克風沒聲音？

1. macOS 設定 → 隱私 → 麥克風 → ✅ Whisper Pro
2. 設定 → 麥克風來源 → 選對你想用的裝置
3. 講話時看 chamber 中央電平球有沒有跳

### 轉錄結果有怪字？

加進個人字典 terms 段、ASR 下次會偏向預測對的字。或加進 corrections 段做規則式替換。

### 「轉錄超時」？

通常是首次切換模型時的 cold start。再試一次應該正常。

### 哪裡可以調設定？

主視窗右上角齒輪 ⚙️、或按 ⌘,。

---

## 文件導航

- **[使用手冊](使用手冊.md)** — 詳細使用說明（17 章節、含設定面板逐欄解釋、preset 路由規則、字典格式等）
- **[CLAUDE.md](CLAUDE.md)** — 開發者交接手冊（程式架構、設計系統、技術細節）
- **[應用程式運作流程](應用程式運作流程.md)** — 技術架構流程圖
- **[規劃書_Speakly對標](規劃書_Speakly對標.md)** — 戰略文件 / 對標路線

---

## 技術棧（給好奇的工程師）

- **ASR**：Qwen3-ASR-0.6B / 1.7B（MLX）、Whisper Large V3 Turbo（mlx-whisper）
- **GUI**：customtkinter（Tk 包裝、深色主題）
- **錄音**：sounddevice（PortAudio 包裝）
- **全域熱鍵**：PyObjC NSEvent monitor（macOS 26.4+ 相容）
- **AI 潤飾**：Ollama（qwen3.5:4b 預設）
- **字體轉換**：OpenCC（簡↔繁、台灣慣用語）
- **打包**：純 shell + Python.app shim（責任歸帳給 bundle ID）

完整檔案 / 模組結構見 [CLAUDE.md §3](CLAUDE.md#3-檔案結構與職責)。

---

## 授權

[未指定 — repo 為私人使用]

## 致謝

- [Qwen team (Alibaba)](https://huggingface.co/Qwen) — Qwen3-ASR 模型
- [Apple ML team](https://github.com/ml-explore) — MLX 框架
- [OpenAI](https://github.com/openai/whisper) — Whisper 原始模型
- [ml-explore community](https://huggingface.co/mlx-community) — MLX 量化版本
- [moona3k](https://github.com/moona3k/mlx-qwen3-asr) — mlx-qwen3-asr Python wrapper

🤖 主要由 [Claude Code](https://claude.com/claude-code) 協作開發
