"""Whisper Pro 版本字串 ── 單一真相來源（Single Source of Truth）。

任何顯示 / 打包版本的地方都從這裡讀：
- `main.py`         — splash 右下角文字
- `build_app.sh`    — `.app` bundle 的 `CFBundleShortVersionString` 與 `CFBundleVersion`

⚠️ 發版鐵律（每個 PR 都檢查）：
  1. **先改 `__version__`**（語意化版本：MAJOR.MINOR.PATCH）
  2. 跑測試 + commit
  3. merge PR → main
  4. `bash build_app.sh` 重建 `.app` bundle
  5. `git tag -a v<__version__> -m "..."` 對齊 release notes 與 Git tag

歷史：
- v2.4.1 以前 splash 字串 hard-code 在 `main.py:167`，從 v2.2.0 後 4 次發版
  （v2.3.0 → v2.4.0）都漏改。集中到本檔避免再發生（v2.4.1 重構）。
- **v2.6.0 → v2.11.0 連續 6 個 release 都沒同步本檔**（splash 一直顯示 v2.6.0）
  → 在 v2.12.0 修正、並在 `build_app.sh` 加 sanity check 比對 `_version.py`
  與 git latest tag，build 時不一致就警告。
"""

__version__ = "2.13.1"
