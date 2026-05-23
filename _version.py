"""Whisper Pro 版本字串 ── 單一真相來源（Single Source of Truth）。

任何顯示 / 打包版本的地方都從這裡讀：
- `main.py`         — splash 右下角文字
- `build_app.sh`    — `.app` bundle 的 `CFBundleShortVersionString` 與 `CFBundleVersion`

發版流程：
  1. 改 `__version__`（語意化版本：MAJOR.MINOR.PATCH）
  2. `bash build_app.sh` 重建 `.app` bundle
  3. `git tag -a v$(...)__version__ -m "..."` 對齊 release notes 與 Git tag

歷史：v2.4.1 以前 splash 字串 hard-code 在 `main.py:167`，從 v2.2.0 之後
4 次發版（v2.3.0 → v2.4.0）都漏改。集中到本檔避免再發生。
"""

__version__ = "2.6.0"
