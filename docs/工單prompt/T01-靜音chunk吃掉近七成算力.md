# T01　串流靜音 chunk 吃掉 69% 的推論算力

> **專案：Whisper Pro（`/Users/jerrychen/project/Claude_code`）**——不是 MeetingNotes，別做錯專案
> **開視窗次數上限：0**（全部用終端機與離線資料驗）　預計 commit：2–3 個
> 來源：`docs/優化策略-更快更準-2026-09-05.md` §2 / §4
> 🔴 **順序不准跳。〔A〕沒過就不准做〔B〕。**

---

## 一、已量到的事實（規劃 session 驗證，2026-09-05）

資料來源：`~/.whisper_app/transcribe_log.jsonl`（112,436 筆，2026-05-31 ~ 09-05）

| 指標 | 數值 |
|---|---|
| 被判定為字典 dump 的 ASR 呼叫 | **2,486 筆（佔 transcribe 紀錄 37.8%）** |
| 其中是串流 chunk（9.5–12.5 秒） | **2,424 筆（97.5%）** |
| 這些呼叫的推論中位數 | **9,175 ms** |
| 累計推論時間 | **390.5 分鐘（6.5 小時）** |
| **佔全部推論時間** | **69.2%**（全部 564.3 分鐘） |
| 產出 | 2,485 筆「（未偵測到語音內容）」，全部丟棄 |

**這些 dump 是真的 dump，不是誤殺**（已查證）：被判定者的 `raw_text` 中位長度
**286 字**，但音訊中位只有 **12.0 秒**——12 秒講不出 286 個字。
未被判定者的 `raw_text` 中位是 37 字。

### 連帶影響（三件先前無法解釋的事）

1. 串流 chunk 的推論比整段轉錄慢 **3.8 倍**（中位 7,379ms vs 1,927ms）——
   垃圾 chunk 塞滿 `transcriber.py:664` 的 `_transcription_lock`（Qwen3-ASR 不支援並發、必須序列化）
2. `2026-09-05 13:34` 一筆 **1.4 秒**錄音總共等了 **23.96 秒**，
   其中 `transcribe_start_ms = 15222`（正常中位 200ms）——在排隊等前面的 chunk
3. 6.5 小時的無效推論造成的發熱／降頻

---

## 二、根因（已查證）

`transcriber.py:941`：

```python
if (self._silero_vad_enabled and duration <= 4.0
        and _silero_no_voice(audio, self._silero_vad_threshold)):
```

**Silero VAD 閘有 `duration <= 4.0` 限制**（v2.21.4 加的「長音逃生門」，
原因寫在 `transcriber.py:936-940`：實測 21 筆被擋的音檔裡有 5.4 秒的，
極可能是真語音被誤殺）。

**而串流 chunk 是 10–12 秒 → 永遠繞過這道閘。**

`transcriber.py:1596` 另有一道無時長限制的 Silero 閘，但
`gates.silero_no_voice` 在 2,486 筆 dump 裡開火 **0 次**。
⚠️ **這個 0 不可信**：`transcriber.py:1461` 有把 `"silero_no_voice": False`
**寫死**當 schema 佔位（註解在 :1460）。**所以無法從 log 判斷 Silero 到底有沒有跑。**
這是〔A〕要回答的問題之一。

---

## 三、🚨 已經被推翻的方向——不要再試

規劃 session 已經用離線資料試過「用音量特徵擋」，**失敗**。
拿 2,424 筆 dump chunk vs 2,444 筆產出真文字的 chunk：

| 特徵 | 零誤擋下的攔截率 |
|---|---|
| `peak` | **0.0%** |
| `rms` | **0.0%** |

分布幾乎完全重疊：**dump 的最大 peak = 1.1712**（有些很大聲），
**真語音的最小 peak = 0.0443**（有些很小聲）。放寬到允許 5 筆誤擋，攔截率仍只有 0.5%。

**結論：不要調 `_MIN_RMS`（`transcriber.py:318`），不要用 peak/rms 做門檻。**
音量分不開「安靜的說話」與「大聲的雜音」。

---

## 四、要做的

### 〔A〕先量：加量測、**不改任何行為**　🔴 這是閘

**為什麼不能直接改**：判別特徵需要原始音訊，而 audit log 只留了
`rms / peak / rms_first_500ms / rms_last_500ms / clipping_ratio / samples`——
**沒有留音訊，所以無法離線驗證任何以音訊為基礎的判定。**

要做的事：

1. 在**送 ASR 之前**，對每一次呼叫算出 Silero VAD 的語音統計，寫進 audit log：
   - `silero_speech_ratio` — speech segment 總長 ÷ 音訊總長
   - `silero_segment_count` — speech segment 數量
   - `silero_max_gap_s` — 最長的無語音間隔
   - `silero_ran` — **布林值，Silero 到底有沒有跑**（解決 §二那個「0 不可信」的問題）
2. **完全不改變任何 gate 行為**——照樣送 ASR，只是多記幾個數字。
3. 修掉 `transcriber.py:1461` 把 `silero_no_voice` 寫死成 `False` 的佔位，
   改成記錄真實結果（或明確記 `null` 表示沒跑）。

**驗收條件（驗證順位 ①：終端機指令）**

```bash
# 跑一週日常使用後
python3 - <<'PY'
import json,os
p=os.path.expanduser('~/.whisper_app/transcribe_log.jsonl')
n=ran=0
for line in open(p,encoding='utf-8',errors='replace'):
    if '"transcribe"' not in line: continue
    o=json.loads(line)
    if o.get('type')!='transcribe': continue
    a=o.get('audio') or {}
    if a.get('silero_speech_ratio') is not None: n+=1
    if (o.get('gates') or {}).get('silero_ran'): ran+=1
print('有新欄位的紀錄:',n,'  Silero 實際跑過:',ran)
PY
```

| # | 條件 | 順位 |
|---|---|---|
| A1 | 新欄位在**通過 RMS 靜音閘**的 transcribe 紀錄裡 ≥ 95% | ① 終端機 |

> 🔴 **2026-09-05 修正（規劃 session 的錯，不是執行者的錯）**：
> A1 原本寫「≥95% 的 transcribe 紀錄」，**這個門檻寫錯了、會誤判成失敗**。
> 量測點在 `transcriber.py:1002`，而 RMS 靜音閘在 `transcriber.py:972` 就 `return` 了——
> 被 RMS 擋下的音檔（歷史比例 **6.28%**）永遠不會走到量測點，欄位會是 `None`。
> 所以全體覆蓋率的上限約 **93.7%**，本來就達不到 95%。
> **分母要排除 `gates.rms_silent = True` 的紀錄。** 看到 93.7% 不是 bug，不要去「修」它。
| A2 | **行為零變化**：同一份 golden set 跑 `eval_runner.py`，輸出與改動前 **byte-identical** | ① 終端機 |
| A3 | 推論總時間沒有增加超過 3%（Silero 本身的開銷） | ① 終端機 |

🔒 **不准輸出任何逐字稿內容**，只記數值。

### 〔B〕依〔A〕的資料決定門檻　—　**〔A〕跑滿一週前不准開始**

拿〔A〕收集的資料做離線分離測試（**跟 §三 同一套方法**）：

```
把 chunk 分成兩組：dict_dump_detected=True vs 產出真文字
找一個 silero_speech_ratio 門檻，要求「誤擋真語音 = 0」
```

| 量到的結果 | 該做的 |
|---|---|
| 零誤擋下攔截率 **≥ 70%** | 做〔C〕，門檻取零誤擋下的最大值再**乘 0.8 當安全邊際** |
| 攔截率 **30–70%** | 做〔C〕，但只套用在**串流 chunk**，不套用在整段錄音（整段誤擋的代價高得多） |
| 攔截率 **< 30%** | 🔴 **停止，這條路走不通。** 寫交接紀錄說明，把 T01 關掉 |

### 〔C〕實作 gate — 只有〔B〕過關才做

1. 把門檻套在**送 ASR 之前**
2. **先紅後綠（強制）**：
   - 準備一段「安靜但真的有人小聲說話」的真人錄音（代號引用，**不進版控**）
   - 先確認**沒有這道 gate 時**它會被正常轉錄
   - 加上 gate 後，斷言它**仍然**被正常轉錄（沒被擋）
   - **如果擋了 → 門檻不合格，退回〔B〕重訂**
3. 故意餵一段純底噪，斷言它**被擋下**（確認 gate 真的會開火，不是永遠回 PASS）

---

## 五、驗收（整張工單）

| # | 條件 | 怎麼量 | 順位 |
|---|---|---|---|
| 1 | dump 呼叫佔 transcribe 紀錄比例從 **37.8%** 下降 | 跑 §一 的統計腳本 | ① 終端機 |
| 2 | 串流 chunk 推論中位數從 **7,379ms** 下降 | 同上 | ① 終端機 |
| 3 | **真語音零損失** | 〔C〕的先紅後綠 + `eval_runner.py` 全綠 | ① 終端機 |
| 4 | `pipeline_done` 的 `transcribe_start_ms` 中位數不上升 | 解析 `logs/whisper_app.log*` | ① 終端機 |

**⚠️ 不要用「使用者感受到的延遲改善」當驗收條件**——規劃 session 沒有量到那個數字，
不要編一個目標值。改完之後量出來多少就是多少，據實寫進交接紀錄。

---

## 六、風險與反悔條件

- **最大風險：誤擋真語音。** 延遲是體感問題，掉字是資料損失，兩者不對稱。
  **任何情況下，寧可多跑一次 ASR，也不要擋掉可能有語音的 chunk。**
- **反悔條件**：〔B〕量到攔截率 < 30% → 整張工單作廢，不要硬做。
- **反悔條件 2**：如果〔C〕之後量到 `transcribe_start_ms` 反而上升
  （Silero 前置計算的開銷大於省下的推論），退回〔A〕的狀態（只量測、不擋）。

---

## 七、完成後

在 `docs/工作紀錄/` 新增一份交接紀錄（`YYYY-MM-DD_T01-*.md`），**跟程式碼一起提交**。
五個欄位裡「**意外發現**」與「**留給下一個人的坑**」最重要。

務必寫進去的：〔B〕實際量到的攔截率與誤擋數（不管結果好壞）、最終門檻值與它的由來。
