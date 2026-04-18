# CLAUDE.md — 專案交接手冊

> 這份檔案讓任何新 Claude session 能無縫接手這個專案。  
> 位置：`/Users/jerrychen/project/Claude_code/CLAUDE.md`  
> 最後更新：2026-04-18

---

## 1. 專案快照

**Whisper Pro v2.0** — macOS 桌面語音轉文字 GUI 應用程式。

- **使用者語言**：繁體中文（回覆一律用繁中；程式碼註解可中英混用）
- **目標使用者**：中英夾雜的開發者 / 知識工作者
- **核心流程**：按 `⌘⌥R` → 錄音 → faster-whisper large-v3-turbo 轉錄 → 可選自動貼上到游標處
- **隱私**：100% 本地運算，不送雲端

---

## 2. 技術棧

| 用途 | 套件 | 備註 |
|---|---|---|
| GUI | `customtkinter 5.2.2` | tkinter 包裝，深色模式 |
| 音訊 | `sounddevice 0.5.1` | 16 kHz mono float32 |
| STT | `faster-whisper 1.1.1` | int8 量化、Metal GPU 加速 |
| 剪貼簿 | `pyperclip 1.9.0` | macOS 原生 pbcopy |
| 全域快捷鍵 | `pynput 1.7.7` | 需「輔助使用」權限 |
| 自動貼上 | `pynput` key 模擬 | ⌘V |
| 圖示 | **純 PIL 手繪**（非 SVG） | Lucide 風格 2px stroke, 4× 超采樣 |
| LLM 潤飾 | Ollama（預設關閉） | `OLLAMA_ENABLED = False` |

Python 版本：**3.13 arm64**（Apple Silicon）。

---

## 3. 檔案結構 & 職責

```
/Users/jerrychen/project/Claude_code/
├── main.py               # 入口：依賴檢查、模型預熱、啟動 GUI
├── gui.py                # AppWindow：主視窗、狀態機、所有 UI 邏輯（1375 行，最大）
├── tokens.py             # ★ 設計系統單一真相來源（色彩/字型/間距/圓角/動畫）
├── icons.py              # ★ 純 PIL 手繪 Lucide 風格圖示（11 個，零依賴）
├── transcriber.py        # Whisper 封裝，thread-safe，lazy-load 模型
├── recorder.py           # AudioRecorder：sounddevice callback + RMS 電平
├── hotkey_manager.py     # pynput 全域快捷鍵監聽（daemon thread）
├── auto_paste.py         # ⌘V 模擬自動貼上
├── config.py             # ~/.whisper_app/config.json 讀寫（atomic save）
├── ollama_client.py      # Ollama HTTP API stub（未啟用）
├── prompts.py            # Whisper initial_prompt 與 Ollama 潤飾 prompt
├── vad.py                # VAD silence detection
├── 使用手冊.md           # 面向最終使用者的繁中手冊
├── UX_OPTIMIZATION_STRATEGY.md  # 2026 現代化策略（已合併的 PR #3）
└── 應用程式運作流程.md  # 技術架構說明
```

**修改 UI 時，優先改 `gui.py`；色彩/字型要改，改 `tokens.py`；新增圖示，改 `icons.py`。**

---

## 4. 設計系統（tokens.py）

### 色彩主張
- **Zinc 暖灰** (Linear-inspired) + **Cyan #06B6D4** 強調色（AI 工具感，刻意避開 Apple Blue）
- 4 級表面深度（dp 0-4），不用陰影（tkinter 無法真的做陰影）

### 關鍵 token
```python
# Surface (dp 0-4)
BG = "#000000"         # 視窗背景
SURF_1 = "#0E0E10"     # 卡片主體
SURF_2 = "#18181B"     # status bar, hover base
SURF_3 = "#27272A"     # pressed
SURF_4 = "#3F3F46"     # borders, dividers

# Text
TEXT_1 = "#FAFAFA"     # headline
TEXT_2 = "#E4E4E7"     # body (75%)
TEXT_3 = "#A1A1AA"     # caption (55%)
TEXT_4 = "#71717A"     # disabled (35%)

# Semantic
ACCENT  = "#06B6D4"    # Cyan, CTA
SUCCESS = "#22C55E"    # idle 綠
DANGER  = "#EF4444"    # recording 紅
WARN    = "#F59E0B"    # processing 琥珀
INDIGO  = "#818CF8"    # auto-paste 紫

# Typography
FONT_FAMILY_UI   = "SF Pro Display"
FONT_FAMILY_TEXT = "SF Pro Text"
FONT_FAMILY_MONO = "SF Mono"   # ← 計時器/數值務必用 mono 避免跳動

# Spacing (4pt baseline): SPACE_XS=4, SM=8, MD=12, LG=16, XL=24, 2XL=32, 3XL=48
# Radius: RADIUS_SM=6, MD=10, LG=14, XL=20, PILL=999
# Motion: DUR_FAST=120, NORMAL=240, SLOW=400 (ms)
```

### Legacy aliases（過渡期保留，新程式碼勿用）
`SURF1..4`, `TEXT1..4`, `BLUE`, `GREEN`, `RED`, `ORANGE` → 都是舊名稱，最終會移除。

### 鐵律
1. **任何 UI 檔案都不可以再寫死 hex 字串** — 一律 `from tokens import ...`
2. **數字顯示（計時器、RMS dB、檔案大小）一律用 `FONT_FAMILY_MONO`** — tabular figures，避免排版跳動
3. **新增顏色先加進 `tokens.py` 再使用**

---

## 5. UI 狀態機

```
IDLE ──[按錄音 or ⌘⌥R 按住]──► RECORDING
                                    │
                                    ▼
                              [放開 or 再按]
                                    │
                                    ▼
                              PROCESSING ──[轉錄完成]──► IDLE
```

| 狀態 | 按鈕色 | 特效 | 背景活動 |
|---|---|---|---|
| IDLE | 綠（SUCCESS）| 6s 呼吸光圈 | 無 |
| RECORDING | 紅（DANGER）| 2.5s 脈衝 7 層 | sounddevice callback、RMS 波形 |
| PROCESSING | 琥珀（WARN）| 12 格旋轉 conic arc | 背景 thread 跑 Whisper |

**執行緒安全鐵律**：只有主執行緒能碰 tkinter widget；背景 thread 完成後一律 `master.after(0, callback)` 回到主執行緒。

---

## 6. 快捷鍵

- **預設**：`cmd+alt+r`（⌘⌥R）— 不用 `cmd+shift+space`（macOS 會衝突）
- **Push-to-talk**：按住錄音，放開停止 + 自動轉錄
- `config.py` 會在讀取時自動把舊的 `cmd+shift+space` 重置成 `cmd+alt+r`

---

## 7. 設定檔

路徑：`~/.whisper_app/config.json`

```python
@dataclass
class Config:
    hotkey: str = "cmd+alt+r"
    model: str = "large-v3-turbo"      # 809M, Metal GPU, 中英混合最佳
    language: str = "自動偵測"
    input_device: Optional[str] = None
    append_results: bool = True
    auto_copy: bool = False
    auto_paste: bool = True            # ← 錄完自動 ⌘V 到游標
    ollama_enabled: bool = False
```

- **Atomic save**：先寫 `.tmp` 再 `replace()`，防斷電毀檔
- **損毀自救**：parse 失敗會 rename 成 `.json.bak` + 用預設值重建

---

## 8. 目前 Git / PR 狀態（2026-04-18）

### 分支拓撲（堆疊 PR）
```
main
 └─ PR #4  feat/record-button-breathing-glow     ← 呼吸光圈
      └─ PR #5  feat/design-tokens-zinc-palette  ← 把 hex 抽到 tokens.py
           └─ PR #6  feat/lucide-icons           ← emoji → 手繪 Lucide 圖示
```

### PR 狀態
| # | 分支 | 內容 | 狀態 |
|---|---|---|---|
| 3 | `feat/apple-ui-redesign-and-ux-strategy` | 初版 Apple 風 UI + UX 策略 | ✅ 已合併 |
| 4 | `feat/record-button-breathing-glow` | Canvas 呼吸光圈（IDLE/REC/PROC 三態）| 🟡 OPEN |
| 5 | `feat/design-tokens-zinc-palette` | `tokens.py` 誕生 + Zinc/Cyan 調色盤 | 🟡 OPEN（基於 #4）|
| 6 | `feat/lucide-icons` | `icons.py` 手繪圖示 + `icons.py` 11 個 icon | 🟡 OPEN（基於 #5）|

**堆疊策略原因**：三個 PR 都會改 `gui.py`，用堆疊避免合併衝突。

### 合併計畫
依序 `gh pr merge 4 --squash` → `5 --squash` → `6 --squash`（使用者確認後）。

---

## 9. 開發流程重點

### 執行 App
```bash
cd /Users/jerrychen/project/Claude_code
venv/bin/python3 main.py
```

### 切換分支驗證
```bash
git checkout feat/record-button-breathing-glow     # 先驗 #4
git checkout feat/design-tokens-zinc-palette       # 再驗 #5
git checkout feat/lucide-icons                     # 最後 #6
```

### 常見坑
1. **CTkOptionMenu 不接受 `fg_color="transparent"`** — 一律用實色
2. **tkinter 無真 alpha/blur/shadow** — alpha 用 `_blend(fg, bg, alpha)` RGB lerp 模擬；模糊/陰影不要硬上
3. **圖示不要用 cairosvg/svglib** — Python 3.13 + libcairo 裝不起來，已改用純 PIL 手繪（4× 超采樣 + LANCZOS）
4. **pynput 需要 macOS「輔助使用」權限** — 首次執行跳權限引導視窗
5. **git push 被拒絕** → `git pull --rebase origin <branch>` 而非 `--merge`

---

## 10. 技能 & 指令

### 已安裝的 skills
- `superpowers`（含 brainstorming, debugging, using-subagents 等）
- `claude-hud`
- `skill-creator`
- `ui-ux-pro-max`（設計系統查詢）

### 常用 MCP
- `computer-use`（截圖/點擊，寫死 tier："read"=瀏覽器, "click"=IDE, "full"=一般 app）
- `Claude_in_Chrome`（瀏覽器 DOM）

---

## 11. 進度路線圖

### ✅ 已完成
- [x] 基本錄音 / 轉錄 / 貼上 pipeline
- [x] 全域快捷鍵 ⌘⌥R
- [x] large-v3-turbo 預設
- [x] Apple MacBook Pro 風格 UI 重構（PR #3）
- [x] 呼吸光圈錄音鈕（PR #4）
- [x] 設計 token 系統抽離（PR #5）
- [x] Lucide 手繪圖示（PR #6）

### 🟡 Tier 1 剩餘（合併 #4#5#6 後要做）
- [ ] **間距統一** — 全面套用 `SPACE_*` 常數（目前還有裸數字）
- [ ] **Tabular figures** — 計時器、RMS 數值改 `FONT_FAMILY_MONO`
- [ ] **Reduced-motion 支援** — 偵測系統偏好、關閉呼吸光圈

### 📌 Tier 2 候選（使用者二擇一）
- **A. 波形頻譜升級**（約 1 天）— 從 RMS bar 改為 FFT 頻譜
- **B. Result Timeline 卡片**（約 2 天，差異化最高）— 每筆轉錄做成時間軸卡片，可點擊展開/收合/標記

---

## 12. 溝通協定（給下一個 session 看的）

1. **語言**：使用者講中文就用繁中；講英文用英文。如果回錯語言，使用者會明確說「講中文」。
2. **慎用 emoji**：使用者不反對在 UI 上有少數情緒 emoji（🎙🎤⚡），但**功能性圖示（複製/下載/設定）一律用 `icons.py` 的 Lucide 手繪**，不要 emoji。
3. **不要主動建立 `.md` 檔** — 除非使用者明確要求（這份 CLAUDE.md 是被明確要求的例外）。
4. **不要主動 commit** — 除非使用者說「幫我 commit」或「上傳」。
5. **PR 描述 & commit message 用英文**（GitHub 生態慣例），但對話用中文。
6. **遇到 context 滿了** → 建議 `/compact`，不要自己亂清記憶。

---

## 13. 關鍵檔案快速索引

| 要改什麼 | 改哪個檔 | 提示 |
|---|---|---|
| 按鈕顏色 / 文字顏色 | `tokens.py` | 改 token 即可，全局生效 |
| 新增圖示 | `icons.py` | 用 `_Pen` class 在 24 viewport 畫 |
| 錄音按鈕行為 | `gui.py` `_draw_glow()` / `_on_record_click()` | 三態對應三種繪製 |
| Whisper 參數 | `transcriber.py` | `beam_size=5, vad_filter=True` |
| 快捷鍵邏輯 | `hotkey_manager.py` | pynput listener, `_pressed` set |
| 自動貼上 | `auto_paste.py` | ⌘V 模擬，macOS-only |
| 首次權限引導 | `main.py` + `gui.py` | pynput accessibility check |

---

**END OF CLAUDE.md** — 任何新 session 讀完這份就能直接接手。如果專案大改，**請同步更新這份檔案**。
