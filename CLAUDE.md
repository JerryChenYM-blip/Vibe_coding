# CLAUDE.md — 專案交接手冊

> 這份檔案讓任何新的 Claude session 能無縫接手這個專案。
> 位置：`/Users/jerrychen/project/Claude_code/CLAUDE.md`
> 最後更新：2026-04-28（v2.3.0 發佈）

---

## 1. 專案快照

**Whisper Pro v2.3.0** — macOS 桌面語音轉文字 GUI 應用程式，含本地 AI 潤飾、情境路由、Voice Shortcuts、歷史紀錄、App Icon、Splash、Mini HUD、Ollama 環境診斷、設定匯入匯出。

- **使用者語言**：繁體中文（回覆一律用繁體中文；程式碼註解可中英混用）
- **目標使用者**：中英夾雜的開發者 / 知識工作者
- **核心流程**：按 `⌘⌥R` → 錄音 → faster-whisper large-v3-turbo 轉錄 →（可選）Ollama 潤飾 → 自動貼上到游標處
- **隱私**：100% 本地運算，不送雲端（Whisper + Ollama 都在本機跑；歷史紀錄存本地 SQLite）
- **版本定位**：
  - v2.1.0（2026-04-23）：Speakly Phase 1 + 2（潤飾管線 + preset 路由 + 字典 + 熱重載）
  - v2.2.0（2026-04-24）：Speakly Phase 3（Voice Shortcuts + SQLite 歷史紀錄）
  - **v2.3.0（2026-04-28）：Speakly Phase 4.1/4.3/4.4/4.5（App Icon + Splash + Mini HUD + 匯入匯出 + Ollama 環境診斷）**

---

## 2. 技術棧

| 用途 | 套件 / 方式 | 備註 |
|---|---|---|
| GUI 介面 | `customtkinter 5.2.2` | tkinter 包裝，深色模式 |
| 音訊錄音 | `sounddevice 0.5.1` | 16 kHz 單聲道 float32 |
| 語音辨識（MLX 後端）| `mlx-whisper` | Apple Silicon Metal GPU 加速（預設） |
| 語音辨識（CPU 後端）| `faster-whisper 1.1.1` | int8 量化、非 Apple Silicon 的 fallback |
| VAD 靜音偵測 | `faster-whisper` 內建 Silero VAD | v2.1.0 放寬：threshold 0.3、min_silence 800ms |
| 剪貼簿 | `pyperclip 1.9.0` | macOS 原生 pbcopy |
| 全域快捷鍵 | `pynput 1.7.7`（主 listener）+ Tk 原生 binding（綁定對話框）| 需「輔助使用」權限；綁定對話框改 Tk 原生避 macOS 26.4+ TSM crash |
| 自動貼上 | `pynput` 按鍵模擬 | ⌘V |
| 圖示 | **純 PIL 手繪**（非 SVG） | Lucide 風格 2px 線寬、4 倍超採樣 |
| AI 潤飾 | Ollama HTTP（預設關閉）| 建議 `qwen2.5:3b-instruct`；timeout 30s；失敗降級回原文 |
| 日誌系統 | Python `logging`（自訂 rotation）| 寫 `~/.whisper_app/logs/whisper_app.log`，5MB × 5 份 |
| 崩潰捕捉 | `faulthandler` | 寫 `fault.log` 到專案根 |

Python 版本：**3.13 arm64**（Apple Silicon）。

---

## 3. 檔案結構與職責

**Python 模組（20 檔、約 6,700 行）**

```
/Users/jerrychen/project/Claude_code/
├── main.py               # 入口：faulthandler、logger、依賴檢查、啟動 GUI
├── gui.py                # AppWindow：主視窗、狀態機、所有 UI 邏輯（2,452 行，最大）
├── tokens.py             # ★ 設計系統單一真相來源（色彩／字型／間距／圓角／動畫）
├── icons.py              # ★ 純 PIL 手繪 Lucide 風格圖示（11 個，零依賴）
├── animation.py          # 純函式：easing、breathing、blend、Ripple（Ambient Chamber 用）
├── transcriber.py        # Whisper 封裝（MLX + CTranslate2 雙後端）、VAD 放寬、音量正規化、幻覺過濾
├── recorder.py           # AudioRecorder：sounddevice 回呼 + RMS 電平
├── hotkey_manager.py     # pynput 全域快捷鍵監聽 + Tk 原生 binding（綁定對話框）
├── auto_paste.py         # ⌘V 模擬自動貼上 + 前景 App 偵測
├── config.py             # ~/.whisper_app/config.json 讀寫（原子性儲存）
├── logger.py             # ★ 統一日誌系統：rotation 5MB×5、log_action/log_state/log_settings/log_error
├── ollama_client.py      # Ollama HTTP：健康檢查（同步/非同步雙路）、潤飾、Polish log 落地 JSONL
├── prompts.py            # Whisper initial prompt + Ollama polish prompt（預設 + 4 個 preset）
├── presets.py            # ★ Phase 2：情境 preset 路由（email/chat/note/code_comment/default）
├── dictionary.py         # ★ Phase 2：個人字典（~/.whisper_app/dictionary.json）
├── prompt_reloader.py    # ★ Phase 2：prompts.py / presets.py mtime 熱重載（2s 輪詢）
├── eval_runner.py        # ★ Phase 2：regression CLI，輸出 CSV 到 tests/reports/
├── history.py            # ★ Phase 3.2：SQLite 歷史紀錄（FTS5 trigram + CRUD）
├── app_icon.py           # ★ Phase 4.1：純 PIL 手繪 App Icon 生成器（CLI）
├── splash.py             # ★ Phase 4.1：啟動畫面 SplashScreen
├── onboarding.py         # ★ Phase 4.5：Ollama 環境診斷（純函式，給設定 UI 用）
├── assets/               # ★ Phase 4.1：icon.png / icon.iconset/ / WhisperPro.icns
├── vad.py                # VAD 靜音偵測（輔助模組）
├── test_hotkey.py        # 快捷鍵互動測試（legacy）
└── test_full_app.py      # 整合測試（legacy）
```

**文件**
```
├── CLAUDE.md                        # 本檔：專案交接手冊
├── 使用手冊.md                      # 最終使用者手冊（v2.1.0 全面改版）
├── 應用程式運作流程.md              # 技術架構與流程圖
├── 規劃書_Speakly對標.md            # v2.1.0 的 Phase 規劃原稿（含執行狀態標註）
├── UX_OPTIMIZATION_STRATEGY.md      # 2026 UI 現代化策略（已合併）
├── docs/工作紀錄/                   # 逐日實作紀錄
├── docs/superpowers/plans/          # Ambient Chamber 等實作計畫
├── docs/superpowers/specs/          # 規格文件（Ambient Chamber、App Icon）
└── docs/changelog/                  # 主要版本紀錄
```

**測試**
```
├── tests/golden_set/                # regression 語料（.wav + .expected.txt + .meta.json）
└── tests/reports/                   # eval_runner.py 輸出的 CSV
```

**修改指引**
- UI 相關 → 改 `gui.py`
- 色彩／字型／間距 → 改 `tokens.py`
- 新增圖示 → 改 `icons.py`
- Ollama prompt → 改 `prompts.py`（熱重載開啟時不用重啟 App）
- preset 路由規則 → 改 `presets.py`
- log 點位 → `log_action` / `log_state` / `log_settings` / `log_error`（from `logger`）

---

## 4. 設計系統（tokens.py）

### 色彩主張
- **Zinc 暖灰**（Linear 風格靈感）搭配 **Cyan #06B6D4** 強調色（AI 工具感，刻意避開 Apple Blue）
- 4 級表面深度（dp 0-4），不使用陰影（tkinter 無法真的做陰影）

### 關鍵 token
```python
# 表面層次（dp 0-4）
BG = "#000000"         # 視窗背景
SURF_1 = "#0E0E10"     # 卡片主體
SURF_2 = "#18181B"     # 狀態列、游標停駐底色
SURF_3 = "#27272A"     # 按下狀態
SURF_4 = "#3F3F46"     # 邊框、分隔線

# 文字階層
TEXT_1 = "#FAFAFA"     # 標題
TEXT_2 = "#E4E4E7"     # 內文（約 75%）
TEXT_3 = "#A1A1AA"     # 輔助文字（約 55%）
TEXT_4 = "#71717A"     # 停用狀態（約 35%）

# 語意色彩
ACCENT  = "#06B6D4"    # 青色，主要 CTA
SUCCESS = "#22C55E"    # 閒置狀態綠
DANGER  = "#EF4444"    # 錄音中紅
WARN    = "#F59E0B"    # 處理中琥珀
INDIGO  = "#818CF8"    # 自動貼上靛紫

# 字型
FONT_FAMILY_UI   = "SF Pro Display"
FONT_FAMILY_TEXT = "SF Pro Text"
FONT_FAMILY_MONO = "SF Mono"   # ← 計時器／數值務必用 mono 字體避免排版跳動

# 間距（4pt 基線）：SPACE_XS=4, SM=8, MD=12, LG=16, XL=24, 2XL=32, 3XL=48
# 圓角：RADIUS_SM=6, MD=10, LG=14, XL=20, PILL=999
# 動畫時長：DUR_FAST=120, NORMAL=240, SLOW=400（毫秒）
```

### 舊別名（過渡期保留，新程式碼勿用）
`SURF1..4`、`TEXT1..4`、`BLUE`、`GREEN`、`RED`、`ORANGE` → 都是舊名稱，最終會移除。

### 鐵律
1. **任何 UI 檔案都不可以再寫死 hex 字串** — 一律 `from tokens import ...`
2. **數字顯示（計時器、RMS 分貝、檔案大小）一律用 `FONT_FAMILY_MONO`** — 等寬數字，避免排版跳動
3. **新增顏色先加進 `tokens.py` 再使用**

---

## 5. UI 狀態機

```
閒置 ──[按錄音 或 ⌘⌥R 按住]──► 錄音中
                                    │
                                    ▼
                              [放開 或 再按]
                                    │
                                    ▼
                              處理中 ──[轉錄完成]──► 閒置
```

| 狀態 | 按鈕色 | 特效 | 背景活動 |
|---|---|---|---|
| 閒置（IDLE）| 綠（SUCCESS）| 6 秒呼吸光圈 | 無 |
| 錄音中（RECORDING）| 紅（DANGER）| 2.5 秒脈衝 7 層 | sounddevice 回呼、RMS 波形 |
| 處理中（PROCESSING）| 琥珀（WARN）| 12 格旋轉弧 | 背景執行緒跑 Whisper |

**執行緒安全鐵律**：只有主執行緒能操作 tkinter 元件；背景執行緒完成後一律用 `master.after(0, callback)` 回到主執行緒。

---

## 6. 快捷鍵

- **預設**：`cmd+alt+r`（⌘⌥R）— 不用 `cmd+shift+space`（macOS 會衝突）
- **按住說話**：按住錄音，放開停止並自動轉錄
- `config.py` 讀取時會自動把舊的 `cmd+shift+space` 重置為 `cmd+alt+r`

---

## 7. 設定檔

路徑：`~/.whisper_app/config.json`

```python
@dataclass
class Config:
    # ── 基本操作 ───────────────────────────────────────────────────
    hotkey:         str           = "cmd+alt+r"        # 全域錄音快捷鍵
    model:          str           = "large-v3-turbo"   # Whisper 模型
    language:       str           = "自動偵測"         # 轉錄語言
    input_device:   Optional[str] = None               # None = 系統預設麥克風
    append_results: bool          = True               # 結果追加 vs. 覆蓋
    auto_copy:      bool          = False              # 完成後自動複製剪貼簿
    auto_paste:     bool          = True               # 完成後自動 ⌘V 貼入游標

    # ── Ollama AI 潤飾（Phase 1）──────────────────────────────────
    ollama_enabled:       bool = False                   # 預設關閉
    ollama_model:         str  = "qwen2.5:3b-instruct"   # 3B 為 16GB Mac 最佳
    ollama_base_url:      str  = "http://localhost:11434"
    ollama_timeout:       int  = 30                       # 秒；超時降級回原文
    ollama_paste_strategy: str = "wait"                   # "wait"/"raw"

    # ── Phase 2 情境 preset 路由 ─────────────────────────────────
    preset_routing_enabled: bool = True
    preset_overrides:       dict = {}       # {"code_comment": False} 可停用個別 preset

    # ── Phase 2 #4 個人字典 ─────────────────────────────────────
    dictionary_enabled: bool = True
    dictionary_path:    str  = ""           # 空字串 = ~/.whisper_app/dictionary.json

    # ── Phase 2 #2 Prompt 熱重載 ────────────────────────────────
    prompt_hot_reload:  bool = True         # prompts.py / presets.py mtime 變化自動 reload

    # ── Phase 2 #3 Polish log ───────────────────────────────────
    polish_log_enabled: bool = True         # 每次潤飾落地一行 JSONL
```

- **原子性儲存**：先寫 `.tmp` 再用 `replace()`，防斷電毀檔
- **損毀自救**：解析失敗會改名成 `.json.bak`，用預設值重建
- **向前相容**：`valid_data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}` — 舊設定檔不會因新欄位而失效，未知欄位靜默忽略

### 相關檔案
- `~/.whisper_app/dictionary.json` — 個人字典（術語清單）
- `~/.whisper_app/polish_log.jsonl` — 潤飾紀錄（時間戳、model、preset、耗時、in/out 長度、error）
- `~/.whisper_app/logs/whisper_app.log` — App 主日誌（rotation 5MB × 5）

---

## 8. 目前 Git／Release 狀態（2026-04-24）

### 目前版本
- `main` 位於 `8101349`（Phase 4.3 + 4.4）+ 後續 docs commit
- 最新 tag：**`v2.3.0`**（2026-04-28 發）
- 目前沒有 open PR

### 版本時間軸
| Tag | 日期 | 主要內容 |
|---|---|---|
| `v1.0.0` | 初版 | 基本錄音 / 轉錄 / 貼上 |
| `v2.0.0` | 2026-04-19 | Ambient Chamber 光場錄音鈕 + 穩定性修復 + Speakly 規劃書 |
| `v2.1.0` | 2026-04-23 | Phase 1 Ollama 潤飾 + Phase 2 preset / 字典 / 熱重載 + 統一日誌 + 錄音完整性 |
| `v2.2.0` | 2026-04-24 | Phase 3.1 Voice Shortcuts（3 action preset）+ Phase 3.2 SQLite 歷史紀錄 |
| **`v2.3.0`** | 2026-04-28 | Phase 4.1 App Icon + Splash / 4.3 浮動 mini HUD / 4.4 設定匯入匯出 / 4.5 Ollama 環境診斷（4.2 menu bar 跳過待 PoC）|

### v2.1.0 PR 歷史（全部已處理）
| # | 內容 | 狀態 |
|---|---|---|
| 8 | transcription accuracy + settings crash | ✅ 整合進 v2.1.0（關閉） |
| 9 | 完整日誌系統 | ✅ 整合進 v2.1.0（關閉） |
| 10 | VAD 放寬 / 尾音 padding / 音量正規化 | ✅ 整合進 v2.1.0（關閉） |
| 11 | Ambient Chamber 錄音鈕重構 | ✅ 合併進 v2.0.0 |
| 12 | v2.1.0 總發佈 | ✅ merge commit `301fef2` |

### 歷史分支（已合併，未刪除）
`feat/ambient-chamber`、`feat/comprehensive-logging`、`feat/design-tokens-zinc-palette`、`feat/lucide-icons`、`feat/phase1-ollama-polish`、`feat/record-button-breathing-glow`、`feat/transcription-completeness`、`fix/transcription-accuracy-and-settings-crash` — 保留供查，後續可清。

---

## 9. 開發流程重點

### 執行 App
```bash
cd /Users/jerrychen/project/Claude_code
venv/bin/python3 main.py
```

### 跑 regression 測試
```bash
# 把錄音放進 tests/golden_set/*.wav + *.expected.txt
venv/bin/python3 eval_runner.py              # 全跑一次
venv/bin/python3 eval_runner.py --id sample1 # 只跑特定 id
venv/bin/python3 eval_runner.py --no-polish  # 只測 Whisper，不跑 Ollama
```
輸出 CSV 落在 `tests/reports/{timestamp}.csv`。

### 讀 log
```bash
tail -f ~/.whisper_app/logs/whisper_app.log        # 即時追
grep "USER_ACTION" ~/.whisper_app/logs/*.log       # 只看使用者動作
grep "ERROR" ~/.whisper_app/logs/*.log             # 只看錯誤 + stack trace
cat ~/.whisper_app/polish_log.jsonl | jq .         # 潤飾紀錄
```

### 常見坑
1. **CTkOptionMenu 不接受 `fg_color="transparent"`** — 一律用實色
2. **tkinter 沒有真正的 alpha／blur／陰影** — 透明度用 `_blend(fg, bg, alpha)` 做 RGB 線性插值模擬；模糊／陰影不要硬上
3. **圖示不要用 cairosvg／svglib** — Python 3.13 + libcairo 裝不起來，已改用純 PIL 手繪（4 倍超採樣 + LANCZOS 縮放）
4. **pynput 需要 macOS「輔助使用」權限** — 首次執行會跳權限引導視窗
5. **git push 被拒絕** → 用 `git pull --rebase origin <branch>`，而非 `--merge`
6. **macOS 26.4+ TSM 斷言** — HotkeyBindDialog（重綁快捷鍵）**禁止**用 pynput Listener 在背景執行緒 capture，必須用 Tk 原生 `<KeyPress>` binding 在主執行緒收（已在 `hotkey_manager.py` 實作）
7. **Cocoa CFRunLoop race** — `SettingsWindow` destroy 與 pynput Listener restart 不能在同一個 tick；`_on_settings_saved` 用 `self.after(100, ...)` 延遲重啟
8. **Ollama 沒在跑時不要 block UI** — `health_check_async()` 才是正確路徑，`health_check_sync()` 只給測試用
9. **VAD 參數修過** — `threshold=0.3 / min_silence=800ms / min_speech=50ms`。再改小心「小聲說話被吃」vs「環境噪音被吃進去」的 tradeoff
10. **logger 的 console handler 用 `sys.__stderr__`** — 不是 `sys.stderr`；避免跟任何 stdout 重導機制循環

---

## 10. 技能與 MCP

### 已安裝的 skills
- `superpowers`（含 brainstorming、debugging、using-subagents 等）
- `claude-hud`
- `skill-creator`
- `ui-ux-pro-max`（設計系統查詢）

### 常用 MCP
- `computer-use`（截圖／點擊，分級權限：「read」＝瀏覽器、「click」＝IDE、「full」＝一般 app）
- `Claude_in_Chrome`（瀏覽器 DOM 操作）

---

## 11. 進度路線圖

> 主要規劃來源：[規劃書_Speakly對標.md](規劃書_Speakly對標.md)（頂部有執行狀態區塊）

### ✅ v2.0.0 完成項（2026-04-19）
- [x] 基本錄音／轉錄／貼上流程
- [x] 全域快捷鍵 ⌘⌥R
- [x] 預設模型 large-v3-turbo
- [x] Apple MacBook Pro 風格 UI 重構（PR #3）
- [x] Ambient Chamber 光場錄音鈕（PR #4 → PR #11）
- [x] 設計 token 系統抽離（PR #5）
- [x] Lucide 手繪圖示（PR #6）

### ✅ v2.1.0 完成項（2026-04-23）— Speakly Phase 1 + 2
- [x] **Phase 1 Ollama 潤飾管線**（`ollama_client.py` + `prompts.py`）
- [x] **Phase 1.5 原文／潤飾 toggle chip**（結果卡 header，一鍵切換）
- [x] **Phase 2 情境 preset 路由**（email / chat / note / code_comment / default，5 個 preset）
- [x] **Phase 2 #2 Prompt 熱重載**（`prompt_reloader.py`，2s 輪詢 mtime）
- [x] **Phase 2 #3 Polish log**（JSONL 落地 `~/.whisper_app/polish_log.jsonl`）
- [x] **Phase 2 #4 個人字典**（`dictionary.py`，注入 Whisper + Ollama prompt）
- [x] **Regression CLI**（`eval_runner.py`，golden set 驅動）
- [x] **統一日誌系統**（`logger.py`，rotation 5MB × 5）
- [x] **錄音完整性**（VAD 放寬、音量正規化、segment-level 幻覺過濾、尾音 padding 300ms）
- [x] **macOS 26.4+ 穩定性修復**（TSM 斷言、CFRunLoop race）

### ✅ v2.2.0 完成項（2026-04-24）— Speakly Phase 3
- [x] **Phase 3.1 Voice Shortcuts**（3 個 action preset：翻英文 / 條列 / 會議紀錄）
- [x] **Phase 3.2 SQLite 歷史紀錄**（`history.py` + FTS5 trigram 搜尋 + 重新潤飾）
- [x] **設定面板**新增歷史紀錄區段（啟用開關 + 保留天數）
- [x] **使用手冊**補「歷史紀錄」章節 + Voice Shortcuts 使用範例

### ✅ v2.3.0 完成項（2026-04-28）— Speakly Phase 4（部分）
- [x] **Phase 4.1 App Icon + 啟動畫面**（`app_icon.py` 純 PIL 手繪生成器、`splash.py` 1.5s + 200ms 淡出、`assets/icon.png` + `WhisperPro.icns`）
- [x] **Phase 4.3 浮動 mini HUD**（`MiniRecordingWindow` Toplevel，140×38，always-on-top，狀態圓點+計時，可開關）
- [x] **Phase 4.4 設定匯入/匯出**（zip 含 config.json + dictionary.json + manifest.json，schema_version=1，排除隱私 history.db）
- [x] **Phase 4.5 Ollama 環境診斷**（`onboarding.py` 純函式，4 種狀態 + 一鍵複製建議命令；自動語言偵測在 v2.1.0 已就位）
- [ ] **Phase 4.2 menu bar icon**：因 `rumps` + tkinter event loop 共存風險，刻意跳過。要做需先 PoC 30 min

### 🟡 隨時可撿起（小打磨）
- [ ] **間距統一** — 全面套用 `SPACE_*` 常數（目前還有裸數字）
- [ ] **等寬數字** — 計時器、RMS 數值改用 `FONT_FAMILY_MONO`（部分已用）
- [ ] **減少動態偏好** — 偵測系統 `prefers-reduced-motion`，關閉呼吸光圈
- [ ] **舊 token 別名移除** — `SURF1..4`、`TEXT1..4`、`BLUE/GREEN/RED/ORANGE` 過渡期結束
- [ ] **App Icon + 啟動畫面**（設計文件已寫在 `docs/superpowers/specs/2026-04-22-app-icon-splash-design.md`）

### 📌 剩餘 Phase 4 候選（短打磨輪）
- [ ] **Phase 4.2 menu bar icon**（`rumps`，要先做 30 min PoC 確認與 tk event loop 不衝突）
- [ ] App Icon 視覺微調（目前膠囊轉角已平、想再優化的項目見 `assets/icon.png` 視覺檢查）

### ⏸️ Phase 5（長線，暫不規劃）
- 即時翻譯、speaker diarization、Obsidian/Notion API 整合、iOS 端

### 🔍 v2.1.0 實測待辦
發版前建議真實錄一次音驗這幾項（從 PR #12 body 的 test plan 來）：
- [ ] 尾音測試：「我今天很好」放開熱鍵看「好」有沒有漏
- [ ] 小聲測試：輕聲說話不應是「（未偵測到語音內容）」
- [ ] 句中停頓：500ms 停頓不該被切段
- [ ] 幻覺誤殺：「請訂閱我們的 YouTube 頻道獲得最新消息」應完整轉出
- [ ] Ollama 潤飾：啟用後能 toggle 切回原文
- [ ] 設定儲存：切模型 → 儲存，App 不崩
- [ ] 熱鍵重綁（macOS 26.4+）：不崩
- [ ] log 檢查：`~/.whisper_app/logs/whisper_app.log` 有完整 USER_ACTION / STATE / SETTINGS / ERROR

---

## 12. 溝通協定（給下一個 session 看的）

1. **語言**：使用者講中文就用繁體中文；講英文用英文。若回錯語言，使用者會明確說「講中文」。
2. **所有文件檔案一律用繁體中文撰寫**（2026-04-19 新規）— 包含：
   - 所有 `.md` 文件（CLAUDE.md、使用手冊、策略文件、README 等）
   - Commit message 本文（但 type 前綴保留英文慣例，如 `docs:` `feat:` `fix:` `chore:`）
   - PR 標題與描述
   - 工作紀錄／交接文件
   - 若使用者明確指示用英文才例外
3. **慎用 emoji**：使用者不反對 UI 上有少數情緒 emoji（🎙🎤⚡），但**功能性圖示（複製／下載／設定）一律用 `icons.py` 的 Lucide 手繪**，不要 emoji。
4. **不要主動建立 `.md` 檔** — 除非使用者明確要求（這份 CLAUDE.md 是被明確要求的例外）。
5. **不要主動 commit** — 除非使用者說「幫我 commit」或「上傳」。
6. **遇到 context 滿了** → 建議 `/compact`，不要自己亂清記憶。

---

## 13. 關鍵檔案快速索引

| 要改什麼 | 改哪個檔 | 提示 |
|---|---|---|
| 按鈕顏色／文字顏色 | `tokens.py` | 改 token 即可，全局生效 |
| 新增圖示 | `icons.py` | 用 `_Pen` class 在 24 viewport 裡畫 |
| 錄音按鈕行為 | `gui.py` `_draw_chamber()` / `_on_record_btn()` | 三態對應三種光場繪製 |
| 狀態機 | `gui.py` `_transition_to_*()` | idle / recording / processing 三態 |
| 尾音 padding | `gui.py` `TAIL_PADDING_MS` + `_try_stop()` | 300ms 延遲停錄音 |
| Whisper 參數 | `transcriber.py` | `beam_size=5, vad_filter=True`，VAD `threshold=0.3` |
| VAD / 音量 / 幻覺過濾 | `transcriber.py` | `_normalize_volume`、`_is_hallucination`、segment-level 過濾 |
| 快捷鍵邏輯（主）| `hotkey_manager.py` | pynput 監聽器、`_pressed` 集合 |
| 快捷鍵重綁對話框 | `gui.py` `HotkeyBindDialog` | Tk 原生 binding（避 macOS 26.4+ TSM crash）|
| 自動貼上 | `auto_paste.py` | ⌘V 模擬，限 macOS |
| 前景 App 偵測 | `auto_paste.py` `get_frontmost_app()` | osascript，錄音開始就抓（供 preset 路由 + ⌘V 目標） |
| Ollama 潤飾 | `ollama_client.py` | `process()` + `health_check_async()` |
| Preset 路由 | `presets.py` | keyword > frontmost_app > default |
| Ollama prompt | `prompts.py` | 預設 + 7 個 preset（email/chat/note/code_comment/translate_en/list/meeting_notes），支援熱重載 |
| Voice Shortcuts | `presets.py` | action preset `triggers_app=set()`，純 keyword 觸發 |
| 個人字典 | `dictionary.py` | `~/.whisper_app/dictionary.json` |
| Prompt 熱重載 | `prompt_reloader.py` | 2s 輪詢 mtime + `importlib.reload` |
| Regression 測試 | `eval_runner.py` | CLI，讀 `tests/golden_set/` → CSV 報告 |
| 歷史紀錄 CRUD | `history.py` `HistoryStore` | SQLite + FTS5 trigram 搜尋 |
| 歷史視窗 | `gui.py` `HistoryWindow` | Toplevel，左清單 + 右詳細 + 動作按鈕 |
| App Icon 生成 | `app_icon.py` | 純 PIL 手繪 → 1024 主圖 + Apple iconset + `.icns` |
| 啟動畫面 | `splash.py` `SplashScreen` | tk.Toplevel 無邊框，1.5s + 200ms 淡出 |
| Mini 錄音 HUD | `gui.py` `MiniRecordingWindow` | 140×38 always-on-top，狀態圓點+計時 |
| Ollama 環境診斷 | `onboarding.py` | 純函式：missing_binary / not_running / no_models / ready |
| 設定匯入匯出 | `gui.py` SettingsWindow `_export_settings` / `_import_settings` | zip + manifest.json schema_version=1 |
| 日誌系統 | `logger.py` | `log_action` / `log_state` / `log_settings` / `log_error` |
| 首次權限引導 | `main.py` + `gui.py` | pynput 輔助使用權限檢查 |
| 設定欄位 | `config.py` | dataclass + 原子性儲存 |

---

**CLAUDE.md 結束** — 任何新 session 讀完這份就能直接接手。如果專案有大改動，**請同步更新這份檔案**。
