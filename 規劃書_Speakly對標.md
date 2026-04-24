# Whisper Pro → Speakly 全功能對標規劃書

> 文件用途：在動工之前把「要做什麼、為什麼、怎麼拆、做到什麼程度算完成」一次講清楚，避免邊做邊偏。
> 原撰寫日期：2026-04-20（v2.0 時期）
> 最後狀態更新：2026-04-24（v2.1.0 發佈後）

---

## 📊 執行狀態追蹤（2026-04-24）

> 這個區塊是事後加上去的執行進度，讓閱讀者一眼看到「當初規劃 vs 實際做到哪」。下方第 0–8 節保留原規劃紀錄，不動。

### Phase 狀態總覽

| Phase | 原規劃目標 | 狀態 | 落地版本 | 備註 |
|---|---|---|---|---|
| **Phase 1** | Ollama 潤飾管線（MVP） | ✅ 完成 | v2.1.0（`0f7b6ce`）| 中英混講去 filler、修錯、補標點 |
| **Phase 1.5**（規劃外追加）| 原文／潤飾 toggle chip | ✅ 完成 | v2.1.0（`0824fe0`）| 緩解 §7.1 risk 3「LLM 過度改寫」 |
| **Phase 2** | 情境格式化（preset 系統）| ✅ 完成 | v2.1.0（`6623947`）| email/chat/note/code_comment/default |
| Phase 2 擴充 #2 | Prompt 熱重載 | ✅ 完成 | v2.1.0 | `prompt_reloader.py`，2s 輪詢 mtime |
| Phase 2 擴充 #3 | Polish log（規劃外追加）| ✅ 完成 | v2.1.0 | JSONL 落地 `~/.whisper_app/polish_log.jsonl` |
| Phase 2 擴充 #4 | 個人字典（原列 Phase 5 → 提前）| ✅ 完成 | v2.1.0 | `dictionary.py`，注入 Whisper + Ollama prompt |
| Regression CLI（規劃外追加）| `eval_runner.py` | ✅ 完成 | v2.1.0 | 跑 `tests/golden_set/` → CSV |
| **Phase 3** | Voice Shortcuts + 歷史紀錄 | ⏳ 待辦 | — | 見下方第 5.3 節 |
| **Phase 4** | 整合打磨（menu bar / mini 窗 / 自動語言）| ⏳ 待辦 | — | 見下方第 5.4 節 |
| **Phase 5** | 長線功能 | ⏸️ 暫不動 | — | 個人字典已提前完成 |

### Phase 1 驗收狀態（§1 末，第 265 行）

| 驗收項 | 狀態 |
|---|---|
| 中英混講 5 秒訊息從按下到貼上 < 4 秒 | 🔍 待實測 |
| 至少刪除 80% filler | 🔍 待實測（有 `eval_runner.py` 可跑）|
| 不誤刪 meaningful 內容 | 🔍 待實測 |
| Ollama 服務不在時 app 不崩、自動降級貼原文 | ✅ 已驗證（`ollama_client.py` 的 `except` 分支）|

### Phase 2 驗收狀態（§2 末，第 311 行）

| 驗收項 | 狀態 |
|---|---|
| 5 個不同 app 測同一句話輸出風格明顯不同 | 🔍 待實測 |
| 未命中任何 preset 時行為等同 Phase 1 default | ✅ 已驗證（`presets.py` 尾端降級邏輯）|

### 附錄 A 狀態同步（第 467 行那張表）

| Speakly 功能 | 原計畫 Phase | **最新狀態** |
|---|---|---|
| 全域熱鍵 | 已完成 | ✅ v1.0.0 |
| 多語 ASR | 已完成 | ✅ v1.0.0 |
| 去 filler / 修錯 / 標點 | Phase 1 | ✅ v2.1.0 |
| 自動格式化 | Phase 2 | ✅ v2.1.0 |
| 即時翻譯 | Phase 5 | ⏸️ 未做 |
| Voice shortcuts | Phase 3 | ⏳ 待辦 |
| 100+ 應用整合 | 繼承 auto-paste | ✅ |
| 每月無限使用 | 天生免費 | ✅ |
| 雲端 AI | 不做 | ❌（維持本地 Ollama）|
| 個人字典（原 Phase 5）| 長線 | ✅ v2.1.0 提前 |

### 和原規劃的偏差（誠實記錄）

1. **Phase 1 實際做完 = Phase 1 + 1.5**：原規劃沒有 toggle chip；實作時覺得「LLM 過度改寫」是真實痛點，臨時加碼。結果是好的。
2. **Phase 2 實際做完 = Phase 2 + 擴充 #2/#3/#4**：原規劃只有 preset 路由；CEO review（路徑 B）決定把「測試優先 + Phase 5 個人字典 + 熱重載 + polish log」都一起做。工程密度大但一次做完更連貫。
3. **Phase 1 貼上策略選 B**：原規劃建議「長錄音用 A、短錄音用 B」；實作時統一用 B（`ollama_paste_strategy="wait"`），因為 MLX + 3B 模型實測夠快，使用者看不到閃爍。
4. **推薦模型用 3B**：原規劃列 3B/7B/14B/20B；預設 `qwen2.5:3b-instruct`。
5. **新增基礎設施（規劃外）**：統一日誌系統（`logger.py`）、錄音完整性修正（VAD 放寬、音量正規化、尾音 padding）、macOS 26.4+ 穩定性修復。這些是做 Phase 1/2 時發現必須先處理的地基。

### 下一步建議

**先用一週**（§7.3 原則：每個 Phase 做完拉長使用一週再進下一個）。期間完成 v2.1.0 驗收實測（見 CLAUDE.md §11 末段清單），收集真實使用回饋。

之後二擇一：
- **Phase 3**（Voice Shortcuts + 歷史紀錄，約 1-2 天）— 擴功能
- **打磨**（間距統一、等寬數字、reduce-motion、App Icon、移除舊 token 別名）— 收尾

---

## 0. 文件結構導讀

這份文件按照「**先解構敵人、再盤點自己、再規劃作戰順序**」的順序組織：

1. **戰略定位**——釐清你要的不只是「複製 Speakly」，是「本地免費版 Speakly」
2. **Speakly 功能解構**——把它當產品拆成可實作的 building blocks
3. **現況盤點**——誠實列出 Whisper Pro 現在真的有什麼、真的缺什麼
4. **差距分析**——一張表看清要補什麼
5. **分期路線圖**——4 個 Phase，每個 Phase 都可獨立出貨
6. **技術架構決策**——Ollama 接入、prompt 設計、觸發模式
7. **風險 / 取捨 / 驗收標準**

建議讀法：先看 1、4、5 決定方向，其餘當字典用。

---

## 1. 戰略定位

### 1.1 為什麼不是「做出一樣的東西」

Speakly 的產品策略建立在「**把語音轉文字當成輸入法**」：使用者不關心背後怎麼運作，只要「按一下、說話、出文字、自動塞到游標」。你要做的本質相同，但**商業前提完全不同**：

| 維度 | Speakly | Whisper Pro（你） |
|---|---|---|
| 收費 | $24.99／年起 | 免費自用 |
| 運算位置 | 雲端（Google Cloud / Azure 等） | 本地（faster-whisper / MLX） |
| 隱私承諾 | 有加密但資料經過伺服器 | 完全不離機 |
| 延遲模型 | 受網路影響 | 受本機算力限制（Apple Silicon Metal GPU 很強） |
| 更新節奏 | 廠商決定 | 你自己控 |
| 客製彈性 | 低（付費功能綁訂閱） | 高（要加什麼就加） |

### 1.2 你的護城河

**不是「比它好」，是「它做不到的我做得到」**：

1. **真正本地**——錄音、STT、LLM 全部離線，敏感內容（病歷、律師筆記、面試答辯）Speakly 不能用的場景你能用
2. **零邊際成本**——講多少都不心疼，Speakly 週訂閱模式刻意限制重度使用
3. **可客製 prompt**——每個人的語癖、專有名詞、公司術語不同；Speakly 只能給通用 prompt，你可以自己調甚至按情境切換
4. **可被延伸**——開放的腳本介面可以串任何 Apple Shortcuts / Alfred / Raycast

### 1.3 但要承認的劣勢

1. **模型成本**——Ollama 跑中文潤飾至少要 4B+ 參數模型，第一次啟動要下載 2-5GB
2. **延遲天花板**——本地 LLM 推論比雲端 API 慢；要拿到 Speakly 同等流暢感需要精打細算
3. **沒有 iOS**——Speakly 核心戰場在手機輸入法；Mac 桌面是它的副本；你短期只能守 Mac
4. **格式化的智慧上限**——雲端 LLM 比本地 LLM 會更會判斷「這是 email 還是 Slack」等情境

明白這些之後，**目標不是功能 100% 對齊，是把 Mac 桌面上你最在意的 80% 做到完全替代**。

---

## 2. Speakly 功能解構

把 Speakly 當成一條管線拆成五層，每一層都是你可以獨立實作／替換的模組。

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1：觸發層（Trigger）                                  │
│  ─ 全域熱鍵按住說話                                            │
│  ─ 浮動按鈕（Mac menu bar / iOS keyboard）                    │
│  ─ Voice Shortcuts（講特定關鍵字即觸發特定動作）                │
├─────────────────────────────────────────────────────────────┤
│  Layer 2：語音辨識（ASR）                                     │
│  ─ 多語種（100+）                                             │
│  ─ 中英混講不用切輸入法                                        │
│  ─ 背景噪音容忍                                                │
├─────────────────────────────────────────────────────────────┤
│  Layer 3：AI 後處理（核心賣點）                                │
│  ─ 去 filler words（嗯、啊、um、uh、like、所以…）              │
│  ─ 修正 ASR 錯別字（同音字、專有名詞）                          │
│  ─ 標點補正 + 斷句                                              │
│  ─ 情境格式化（email/Slack/條列/純文字自適應）                  │
│  ─ 即時翻譯（語言 A → 語言 B）                                  │
├─────────────────────────────────────────────────────────────┤
│  Layer 4：輸出層（Output）                                     │
│  ─ 自動貼到游標處                                               │
│  ─ 複製到剪貼簿                                                │
│  ─ 顯示結果浮窗可編輯                                           │
├─────────────────────────────────────────────────────────────┤
│  Layer 5：整合層（Integration）                                │
│  ─ 偵測當前 app，切換格式化模式                                 │
│  ─ 歷史紀錄 / 搜尋                                              │
│  ─ Voice shortcuts / 快捷指令                                   │
└─────────────────────────────────────────────────────────────┘
```

### 2.1 Speakly 獨門功能深挖

這幾項特別值得拆解，因為它們是「看似簡單但工程上有 know-how」的地方：

#### A. Filler words 去除
**表面現象**：你講「嗯我今天那個去那個超市買東西」，輸出「我今天去超市買東西」。

**實作機制推測**：
- 不是正則表達式「全域取代『嗯』為空」——那會誤刪「嗯心」（藥名）這類合法內容
- 是 LLM 帶上下文判斷，prompt 會是類似：「刪除語氣詞與無意義重複，但保留語意完整性」
- 短訊息（< 30 字）刪得保守，長訊息（> 100 字）刪得積極

**對你的啟示**：Prompt 要**按長度分級**，並給 LLM 明確的「保留清單」（人名、地名、專業術語）。

#### B. 情境格式化
**表面現象**：在 Mail 裡輸出「Dear Jerry,\n\n...\n\nBest,\n」，在 Slack 裡輸出「hey jerry, ...」。

**實作機制推測**：
- 偵測前景 app（`get_frontmost_app()` 你已經有）
- 對應到「情境 preset」：formal email / casual chat / meeting notes / code comment / plain text
- 不同 preset 餵不同 system prompt 給 LLM

**對你的啟示**：這其實是**一個 app_name → preset 的 mapping 表加 prompt template**，工程量不大但要累積好的 preset。

#### C. 語言中途切換
**表面現象**：「我 schedule 了一個 meeting 明天 3 點」——這句話 Whisper 已經能正確辨識；Speakly 的重點是後處理時不會把「meeting」被錯誤「翻譯」成「會議」，而是原樣保留。

**實作機制推測**：
- Whisper 的 `initial_prompt` 指引「英文原文保留」（你已經這樣做）
- LLM 潤飾 prompt 也要強調「**英文專有名詞不要翻譯**」

**對你的啟示**：你的現有 prompt 方向正確，不需要大改。

#### D. Voice Shortcuts
**表面現象**：說「**summary mode** 今天的會議重點是…」→ 自動切換到條列格式產生會議摘要。

**實作機制推測**：
- 預先定義觸發關鍵字（"summary mode", "translate to English", "email to boss"）
- STT 後先掃描開頭是否命中觸發詞
- 命中 → 切換 preset 並從正文刪除關鍵字

**對你的啟示**：這其實就是**關鍵字路由到不同 prompt**，很好加。

#### E. 即時翻譯
**表面現象**：你說中文，直接輸出英文。

**實作機制推測**：
- STT 用原語言（Whisper 自動偵測）
- LLM prompt 加「翻譯成英文」
- 不是兩次獨立任務，而是「STT + 翻譯」合併一個 LLM call

**對你的啟示**：LLM 負擔會變重，中小型模型可能翻譯品質不如專用 MT 模型。不急。

---

## 3. 現況盤點（Whisper Pro v2.0）

這段不是為了自我肯定，是為了 Phase 1 動工前釐清哪些能直接沿用、哪些要重構。

### 3.1 已完成且堪用的部分

| 模組 | 狀態 | 備註 |
|---|---|---|
| 全域熱鍵（⌘⌥R） | ✅ 穩定 | 剛加完 self-heal，觸發率應該明顯提升 |
| 按住說話 UX | ✅ | press→放開自動轉錄 |
| 錄音（sounddevice） | ✅ | 16kHz mono float32，標準 |
| Whisper MLX 後端 | ✅ | large-v3-turbo 中英混講品質不錯 |
| CPU fallback | ✅ | MLX 失敗時自動退 faster-whisper |
| 幻覺過濾 | ✅ | `_HALLUCINATIONS` 清單 |
| 自動貼上 | ✅ | 偵測前景 app + ⌘V 模擬 |
| 錄音 UI（Ambient Chamber） | ✅ | 呼吸光圈美感不錯 |
| 設定持久化 | ✅ | `~/.whisper_app/config.json` |
| Log / faulthandler | ✅ | 剛補強 |

### 3.2 已寫好但未啟用或待啟用

| 模組 | 狀態 | 備註 |
|---|---|---|
| `ollama_client.py` | 🟡 程式架構預留 | 還沒實際跟 Ollama 服務對接、沒測試 |
| `prompts.OLLAMA_SYSTEM_PROMPT` | 🟡 基礎版 | 需要按情境擴充 |
| 「潤飾」按鈕 | 🟡 UI 有、後端未通 | `_on_ollama` 已寫但 `ollama.is_available()` 檢查會讓按鈕 disable |
| 轉錄結果 append 模式 | ✅ | 多段可累積在同一視窗 |

### 3.3 完全沒做的部分（即 Speakly 有但你沒有的）

| 功能 | 重要性 | 備註 |
|---|---|---|
| AI 潤飾（去 filler / 修錯 / 標點） | ★★★★★ | Speakly 最核心賣點，你的第一優先 |
| 情境格式化（按目標 app 切 preset） | ★★★★ | 工程量中等、體感差異巨大 |
| Voice shortcuts / 關鍵字路由 | ★★★ | 加法容易，邊際效益高 |
| 歷史紀錄 / 搜尋 | ★★★ | 本地 SQLite 即可 |
| 即時翻譯 | ★★ | 看個人使用情境，優先級視個人 |
| 多語音自動識別語言 | ★★ | Whisper 本身就會，但目前你強制設「中文」 |
| menu bar 常駐圖示 | ★★ | macOS native 體驗加分 |
| 浮動 mini 視窗 | ★★ | 不開主視窗也能錄 |
| iOS／其他平台 | 先不做 | 工程量過大 |

---

## 4. 差距分析（一張表）

依「做完後對使用體驗的提升幅度」排序：

| 功能 | 影響 | 工程量 | 依賴 |
|---|---|---|---|
| Ollama 潤飾基礎管線 | ★★★★★ | 中（1-2 天） | Ollama local runtime |
| 去 filler + 修錯 prompt | ★★★★★ | 小（半天，迭代） | 潤飾管線 |
| 情境 preset 系統 | ★★★★ | 中（1 天） | 潤飾管線 |
| 設定 UI 選「哪些 app 啟用潤飾」 | ★★★ | 小 | preset 系統 |
| Voice shortcuts | ★★★ | 小（半天） | 潤飾管線 |
| 歷史紀錄 + 搜尋 | ★★★ | 中（1 天） | SQLite |
| 潤飾前後雙版本顯示 | ★★ | 小 | UI |
| 自動語言偵測回來 | ★ | 極小 | - |
| menu bar icon | ★★ | 中（需要 rumps 或 py-menubar） | - |
| 即時翻譯 preset | ★★ | 小 | preset 系統 |

---

## 5. 階段性實作路線圖

### 🎯 Phase 1：Ollama 潤飾管線（MVP，2-3 天）

**目標**：按一次 ⌘⌥R 就能拿到「去 filler、修錯、補標點」的乾淨文字，自動貼上。**這一個 phase 做完你就已經 70% 取代 Speakly**。

#### 1.1 基礎 Ollama 串接
- 確認 Ollama 在本機可用（`ollama serve` / 檢查 `127.0.0.1:11434`）
- `ollama_client.py` 實作 HTTP `/api/generate` 呼叫，帶 streaming
- 超時處理（30 秒上限）、失敗降級（fallback 用未潤飾原文）
- 模型建議：`qwen2.5:7b-instruct`（中文好、3B 次選如 `qwen2.5:3b-instruct`）

#### 1.2 潤飾 prompt 迭代
初版 prompt（比現在的更嚴格）：
```
你是語音轉文字後處理助理。輸入是一段 Whisper 的中英混講轉錄，你要：
1. 刪除語氣詞（嗯、啊、那個、然後那個、所以那個、um、uh、like）
2. 修正明顯的同音錯字（例如「在」「再」「做」「坐」）
3. 補上標點與斷句
4. 保留所有英文原文（專有名詞、技術術語不要翻譯）
5. 保留語意與說話者原意，**不要增刪內容**

只輸出修正後的文字，不要加任何說明、標題、前綴。

原文：{text}
```

**迭代方法**：準備 10 條典型錄音（自己錄）作為 regression set，每次改 prompt 都重跑一遍看哪些變差。

#### 1.3 GUI 接入點
- 錄音停止後流程：
  ```
  Whisper 轉文字 → 顯示原文（淡色，標記「轉錄中」）
                 ↓
           Ollama 潤飾（背景）
                 ↓
     顯示潤飾版（淡入）+ 剪貼簿 + 自動貼上
  ```
- 設定開關：`cfg.ollama_enabled`（你已經有，但要新增 UI）
- 失敗時退回原文，不要讓使用者沒結果

#### 1.4 延遲策略
- 大原則：**先貼原文、再修正**（非阻塞）
- 對 3 秒內的錄音：等潤飾完再貼（本地 7B 模型推論 1-2 秒）
- 對長錄音：先貼原文到目標 app，2 秒後若潤飾完成才用 `backspace + 重貼` 取代
- 或：永遠先貼原文，顯示 toast「潤飾中…」，完成後 toast「已潤飾，⌘Z 可還原」
- 具體策略 Phase 1 末期實測兩種再決定

#### Phase 1 驗收標準
- [ ] 中英混講 5 秒訊息從按下到貼上 < 4 秒
- [ ] 至少刪除 80% filler（用手動測試集驗）
- [ ] 不誤刪 meaningful 內容（人名、單字）
- [ ] Ollama 服務不在時 app 不崩、自動降級貼原文

---

### 🎯 Phase 2：情境格式化（1-2 天）

**目標**：同一句話「安排明天下午三點跟 Jerry 開會討論 Q2 roadmap」在 Mail 變「Hi Jerry,\n\nCould we meet tomorrow at 3 pm to discuss Q2 roadmap?\n\nBest,」，在 Slack 變「hey jerry, 明天 3pm 聊 Q2 roadmap？」。

#### 2.1 Preset 系統設計
```python
# presets.py
PRESETS = {
    "email": {
        "triggers_app": ["Mail", "Outlook", "Thunderbird", "Superhuman"],
        "triggers_keyword": ["email to", "寄信給"],
        "system_prompt": "...formal email with greeting and sign-off...",
    },
    "chat": {
        "triggers_app": ["Line", "Slack", "Discord", "Messages", "Telegram"],
        "system_prompt": "...casual, lowercase, no sign-off...",
    },
    "note": {
        "triggers_app": ["Notion", "Obsidian", "Bear", "Notes"],
        "system_prompt": "...structured, bullet when listing...",
    },
    "code_comment": {
        "triggers_app": ["Xcode", "Visual Studio Code", "Cursor", "JetBrains"],
        "system_prompt": "...technical, concise, English if needed...",
    },
    "default": {
        "system_prompt": "...（Phase 1 的通用 prompt）...",
    },
}
```

#### 2.2 路由邏輯
```
轉錄完成 → 檢查 keyword 觸發 → 檢查前景 app →
         → 都沒命中 → 用 default preset
         → 命中 → 用對應 preset 的 system prompt
```

#### 2.3 設定 UI
- 每個 preset 可以用 toggle 啟用／停用
- 進階使用者可以自己編輯 prompt（先別做，Phase 2.5）

#### Phase 2 驗收標準
- [ ] 在 5 個不同 app 測同一句話，輸出風格明顯不同且合理
- [ ] 未命中任何 preset 時行為等同 Phase 1 default

---

### 🎯 Phase 3：Voice Shortcuts + 歷史紀錄（1-2 天）

#### 3.1 Voice Shortcuts
- 預設關鍵字：
  - `"翻譯英文"` / `"translate to English"` → 切翻譯 preset
  - `"條列"` / `"list mode"` → 切條列 preset
  - `"會議記錄"` / `"meeting notes"` → 切會議紀要 preset
- 命中後**從正文刪除關鍵字**再送 LLM

#### 3.2 歷史紀錄
- SQLite `~/.whisper_app/history.db`
- 欄位：`id, timestamp, duration, raw_text, polished_text, target_app, preset_used`
- GUI 加一頁「歷史紀錄」，支援搜尋、複製、重新潤飾
- 保留策略：預設無限，有明顯放大可以加「只留 30 天」選項

---

### 🎯 Phase 4：整合打磨（2-3 天）

這階段把「日常使用順手度」壓到 Speakly 同級：

1. **menu bar icon**：不開主視窗也能操作（`rumps` or `py-menubar`）
2. **浮動 mini 錄音窗**：錄音時出現螢幕角落小浮窗顯示電平，不擋視線
3. **自動偵測語言**：讓 `語言=自動偵測` 真的能用（目前 config 寫死中文）
4. **設定匯入／匯出**：preset / prompt / 歷史 一鍵備份
5. **首次啟動引導**：Ollama 沒裝時提示安裝命令、首次下載模型時顯示進度

---

### 🎯 Phase 5（可選，長線）：進階

不建議現在做，列出來讓你心裡有個譜：
- 即時翻譯（需要更強的 LLM 或專用 MT 模型）
- 個人字典（「我講到『瑟柏』其實是『cyberpunk』」這種本地 override）
- 多段錄音的 speaker diarization（誰講的）
- Obsidian / Notion API 直接寫入
- iOS 端（工程量等於重開專案）

---

## 6. 技術架構關鍵決策

### 6.1 Ollama vs 其他 LLM runtime
| 選項 | 優 | 劣 | 結論 |
|---|---|---|---|
| **Ollama** | 設定簡單、model pull 方便、local HTTP API | 啟動 overhead | ✅ **主選** |
| llama.cpp 直接 bind | 延遲最低 | 整合複雜 | 延後考慮 |
| MLX LM（Apple 原生） | Metal 最快 | 模型選擇少 | 觀察中 |
| 呼叫雲端 API | 品質高 | 違背「完全本地」 | ❌ 不做 |

### 6.2 推薦模型梯度

| 模型 | 參數 | VRAM | 速度 | 品質 | 建議情境 |
|---|---|---|---|---|---|
| `qwen2.5:3b-instruct` | 3B | ~2GB | 快（< 1s 潤飾 100 字） | 中文不錯 | **Mac 16GB 首選** |
| `qwen2.5:7b-instruct` | 7B | ~5GB | 中（1-2s） | 好 | **Mac 32GB 首選** |
| `qwen2.5:14b-instruct` | 14B | ~9GB | 慢（3-4s） | 很好 | Mac 48GB+ |
| `gpt-oss:20b` | 20B（MoE） | ~12GB | 中 | 最好 | 若日常潤飾品質需求高 |

**建議做法**：內建 `qwen2.5:3b` 為預設，設定 UI 讓使用者切換。

### 6.3 Prompt 工程原則

1. **一個 prompt 做一件事**：別把「去 filler + 翻譯 + 格式化」塞一個 prompt，拆開更可控
2. **明確「不要做什麼」比「做什麼」重要**：中文 LLM 容易過度改寫，要 explicit 說「不要增加內容」
3. **少樣本範例（few-shot）比規則描述有效**：在 system prompt 裡放 2-3 組 input/output 範例
4. **溫度設低**（`temperature=0.1-0.3`）：潤飾不需要創意
5. **建立 regression set**：10-20 條典型輸入當測試案例，每次改 prompt 都跑一遍

### 6.4 貼上策略（核心 UX 決策）

兩種路線：

**A. 先貼原文、潤飾完再替換**
- 優：立即回饋，延遲 0 秒
- 劣：使用者會看到兩次文字變化，可能出現閃爍
- 實作：`⌘V 貼原文` → `等潤飾` → `⌘A + ⌘V 取代`（但會蓋掉使用者其他輸入，危險）

**B. 等潤飾完再貼**
- 優：單次動作，乾淨
- 劣：延遲 1-3 秒（本地 7B 模型）
- 實作：`Whisper 完成` → `Ollama 完成` → `⌘V 一次貼上`

**建議**：**預設 B**，因為 3 秒內的錄音本地 3B 模型潤飾約 0.5-1 秒，使用者感受是「說完到貼出約 1.5-2 秒」，比 Speakly（雲端 + 網路延遲也要 1-2 秒）差距不大但更乾淨。長錄音（> 30 秒）再考慮 A。

### 6.5 錯誤處理分層

```
Ollama 服務未啟動 → toast「潤飾服務未啟動」+ 貼原文
Ollama 逾時（> 30s） → toast「潤飾逾時」+ 貼原文
Ollama 回傳異常格式 → toast「潤飾失敗」+ 貼原文 + log
Whisper 失敗 → 現有行為：錯誤訊息顯示
錄音失敗 → 現有行為：AccessibilityDialog / 權限引導
```

核心原則：**Ollama 任何問題都不能讓使用者沒結果**。原文永遠可用。

---

## 7. 風險、取捨、驗收

### 7.1 已知風險

| 風險 | 機率 | 影響 | 緩解 |
|---|---|---|---|
| 本地 LLM 延遲超預期 | 中 | UX 變差 | 預設用小模型、UI 顯示進度 |
| Ollama 服務管理困擾使用者 | 高 | 放棄使用 | 首次啟動引導、自動偵測 |
| LLM 過度改寫原意 | 中 | 信任崩壞 | prompt 強調保留、提供「原文／潤飾」切換 |
| 記憶體壓力（同時跑 Whisper + Ollama） | 中 | 系統變慢 | 監控、提供「省電模式」 |
| prompt 對某些使用者反效果 | 中 | 不滿意 | 開放自訂 prompt、按情境分 preset |
| macOS 權限（Accessibility / Input Monitoring） | 高 | 功能失效 | 目前已有引導、持續優化 |

### 7.2 刻意不做的取捨

明確寫下**不會做**的事，後續才不會偏離：

- ❌ 雲端 fallback（違背隱私承諾）
- ❌ iOS／Android（工程量不合理）
- ❌ 使用者帳號系統（本地工具不需要）
- ❌ 雲同步歷史紀錄（隱私）
- ❌ 團隊協作功能
- ❌ 付費訂閱模式

### 7.3 整體里程碑建議

| 時間點 | 目標 |
|---|---|
| Week 1 | Phase 1 完成（Ollama 接入 + 基礎潤飾） |
| Week 2 | Phase 2 完成（情境格式化） |
| Week 3 | Phase 3 完成（Voice shortcuts + 歷史） |
| Week 4 | Phase 4 打磨 + 自用一段時間 |
| Month 2+ | 依實際使用情況決定是否做 Phase 5 |

**關鍵原則**：每個 Phase 做完都要**拉長使用一週**再進下一個。否則容易在路上不斷推翻前面的決定。

---

## 8. 動工前最後確認清單

開始 Phase 1 之前，建議先把這些事情釐清：

- [ ] Ollama 裝好了嗎？`ollama --version` 能跑？
- [ ] 本機至少有一個模型 pulled？`ollama list` 看看
- [ ] 準備 10-15 條典型錄音當 regression set（自己錄 .wav 或 .txt 轉錄檔）
- [ ] 決定預設模型大小（看你 Mac 記憶體；建議 `qwen2.5:3b` 起跳）
- [ ] 決定貼上策略（建議 B：等潤飾完再貼）
- [ ] 確認願意接受「潤飾可能誤改」→ 需要保留原文切換 UI

---

## 附錄 A：Speakly 關鍵功能 vs 你的計畫對應表

| Speakly 功能 | 你的對應 Phase | 狀態 |
|---|---|---|
| 全域熱鍵 | 已完成 | ✅ |
| 多語 ASR | 已完成（Whisper） | ✅ |
| 去 filler / 修錯 / 標點 | Phase 1 | 🔜 |
| 自動格式化 | Phase 2 | 🔜 |
| 即時翻譯 | Phase 5 | ⏳ |
| Voice shortcuts | Phase 3 | 🔜 |
| 100+ 應用整合 | 繼承 Whisper Pro 現有 auto-paste | ✅ |
| 每月無限使用 | 天生免費 | ✅ |
| 雲端 AI | **不做**（本地 Ollama 取代） | ❌ |

---

## 附錄 B：Phase 1 技術債警示

做 Phase 1 時容易犯的錯（給未來的自己看）：

1. **別一次塞完所有 prompt 功能**——先做「去 filler + 標點」兩件事，穩了再加
2. **別把 Ollama 呼叫放主執行緒**——永遠用 thread
3. **別依賴 Ollama streaming 做 UI 即時顯示**——等整段完成再渲染，避免閃爍
4. **別把 prompt 寫在 `prompts.py` 不動**——要支援動態切換 preset
5. **別忘了降級路徑**——Ollama 失敗時**預設不要彈對話框**，toast 提示即可

---

**規劃書結束**——等你讀完後告訴我：

1. 整體方向是否同意？
2. Phase 1 要不要下週就開始？
3. 推薦模型（`qwen2.5:3b` / `qwen2.5:7b`）你傾向哪個？
4. 貼上策略選 A 還是 B？
5. 有沒有我沒想到但你在意的功能？
