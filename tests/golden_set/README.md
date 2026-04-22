# Golden Set — Phase 1.5 測試語料

放這裡的錄音檔會被 `eval_runner.py` 逐一跑過「Whisper → 潤飾」整條管線，
把結果與預期比對後輸出 CSV。

## 檔案命名

每組測試要三個檔（`meta.json` 可選）：

```
tests/golden_set/
├── 001_short_chinese.wav             # Whisper 要吃的音訊
├── 001_short_chinese.expected.txt    # 期望的潤飾結果（UTF-8）
├── 001_short_chinese.meta.json       # 選填：{"preset": "email", "app": "Mail"}
├── 002_mixed_lang.wav
├── 002_mixed_lang.expected.txt
└── ...
```

- `.wav` 格式：16 kHz 單聲道 float32 或 int16 皆可
- `.expected.txt`：你手寫的「理想潤飾後」文字
- `.meta.json`：若要模擬特定 preset／app 情境才加

## 建議涵蓋範圍（至少 10-15 條）

1. **極短**：「嗯 好 對」「可以」
2. **純中文帶 filler**：「嗯那個我今天有點累」
3. **中英混講**：「我今天 schedule 了一個 meeting」
4. **專有名詞**：「我想改用 Kubernetes 部署」
5. **長段敘述**（> 30 秒）：連續說話、多個子句
6. **email 風格**（meta 指定 preset=email）
7. **Slack 風格**（meta 指定 app=Slack）
8. **程式註解**（meta 指定 preset=code_comment）
9. **故意錯別字**：同音字情境（做／坐、在／再）
10. **只按快捷鍵沒說話**：空音訊，驗證 guard

## 執行

```bash
venv/bin/python3 eval_runner.py                  # 跑全部
venv/bin/python3 eval_runner.py --id 001         # 只跑 ID 為 001 的
venv/bin/python3 eval_runner.py --no-polish      # 只跑 Whisper，不呼叫 Ollama
```

輸出：`tests/reports/{timestamp}.csv` + console summary。
