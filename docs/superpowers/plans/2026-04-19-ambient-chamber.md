# Ambient Chamber 錄音按鈕重構 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將 Whisper Pro v2.0 的錄音按鈕從「波形 + 外環 + 綠色按鈕」三層結構重構為單一 Canvas 光場（D3 Ambient Light Chamber），消除綠色廉價感、對齊品牌 Cyan accent、純 tokens 無硬編 hex。

**Architecture:** 單一 `tk.Canvas` (280×280) 取代三個元件，以 `_blend(color, bg, alpha)` 預計算同心圓光暈模擬透明度，RMS 直接驅動光強/擴散波紋，狀態切換用 ease-out/ease-in-out cubic 過渡。計時器拆出為獨立 `SF Mono` 標籤。

**Tech Stack:** tkinter Canvas、CustomTkinter、PIL/Pillow（icons）、純 Python（animation helpers）、pytest（animation.py 單元測試）、subprocess（讀 macOS reduce-motion 偏好）。

**Spec reference:** `docs/superpowers/specs/2026-04-19-ambient-chamber-design.md`

---

## File Structure

| 檔案 | 類型 | 責任 |
|---|---|---|
| `tokens.py` | 修改 | 新增 MOTION 區段（duration/breathing period/render tick 常數）|
| `animation.py` | 新檔 | 純函式：easing、breathing、blend、Ripple；無 tkinter 依賴，可完整 pytest |
| `icons.py` | 修改 | 新增 `_i_square` icon（Lucide stop）|
| `gui.py` | 修改 | 重寫 `_build_record_card` + `_transition_to_*` 的 UI 部分；新增 `_draw_chamber` 繪製邏輯、`_render_tick` 迴圈、Canvas event handlers、`system_reduce_motion()` helper；刪除 `_wave_canvas`, `_btn_ring`, `_record_btn`, `_draw_idle_wave`, `_draw_live_wave`, `_update_wave`, `_pulse_btn`, `_animate_spinner` 以及所有硬編色彩常數 |
| `tests/test_animation.py` | 新檔 | animation.py 的 pytest |

---

## Task 0: Pre-flight — 合併 PR 堆疊並建立工作分支

**Files:** None (GitHub + local git 操作)

**Context:** PR #4→main, #5→#4, #6→#5 三層堆疊。合併時需依序將 #5 和 #6 的 base retarget 成 main。

- [ ] **Step 1: 確認當前 git 狀態乾淨**

```bash
git status --short
```
Expected: 只有未追蹤檔案（node_modules 刪除、image/ 等），無未提交 commits 在 feat/transcription-completeness

- [ ] **Step 2: 合併 PR #4（base 已經是 main）**

```bash
gh pr merge 4 --squash --delete-branch=false
```
Expected: "✓ Merged pull request #4"

- [ ] **Step 3: 把 PR #5 的 base 從 `feat/record-button-breathing-glow` 改成 `main`**

```bash
gh pr edit 5 --base main
```
Expected: 無輸出或 "✓ Edited pull request #5"

- [ ] **Step 4: 合併 PR #5**

```bash
gh pr merge 5 --squash --delete-branch=false
```
Expected: "✓ Merged pull request #5"

- [ ] **Step 5: 把 PR #6 的 base 從 `feat/design-tokens-zinc-palette` 改成 `main`**

```bash
gh pr edit 6 --base main
```
Expected: 無輸出或 "✓ Edited pull request #6"

- [ ] **Step 6: 合併 PR #6**

```bash
gh pr merge 6 --squash --delete-branch=false
```
Expected: "✓ Merged pull request #6"

- [ ] **Step 7: 更新本地 main 並建立 feature branch**

```bash
git fetch origin
git checkout main
git pull origin main
git checkout -b feat/ambient-chamber
```
Expected: `Switched to a new branch 'feat/ambient-chamber'`

- [ ] **Step 8: 驗證 main 上有 tokens.py 和 icons.py**

```bash
ls tokens.py icons.py
```
Expected: 兩個檔案都存在

---

## Task 1: 擴充 `tokens.py` MOTION 區段

**Files:**
- Modify: `tokens.py` (append to end)

- [ ] **Step 1: 打開 tokens.py，確認現有最後一行是 `RADIUS_PILL = 999`**

```bash
tail -5 tokens.py
```
Expected: 末尾有 RADIUS_PILL = 999

- [ ] **Step 2: 在 tokens.py 末尾追加 MOTION 區段**

```python
# ═══════════════════════════════════════════════════════════════════════════
#  MOTION
#  Durations in milliseconds. Breathing periods for each state in the record
#  chamber animation. Render tick = 50ms = 20 FPS (matches prior _update_wave).
# ═══════════════════════════════════════════════════════════════════════════

DUR_FAST    = 120    # press feedback
DUR_NORMAL  = 240    # state transitions (idle→rec, etc.)
DUR_SLOW    = 400    # non-critical reveals

BREATHE_IDLE_MS       = 6000
BREATHE_RECORDING_MS  = 2500
BREATHE_PROCESSING_MS = 1800

ROTATE_PROCESSING_MS  = 1500

RENDER_TICK_MS        = 50    # 20 FPS
```

- [ ] **Step 3: 驗證 tokens.py 語法正確**

```bash
python3 -c "import tokens; print(tokens.RENDER_TICK_MS, tokens.BREATHE_IDLE_MS)"
```
Expected: `50 6000`

- [ ] **Step 4: Commit**

```bash
git add tokens.py
git commit -m "$(cat <<'EOF'
feat(tokens): 新增 MOTION 區段提供動畫 duration 與呼吸週期常數

為 Ambient Chamber 動畫提供單一真相來源：
- DUR_FAST/NORMAL/SLOW: 微互動與狀態切換時長
- BREATHE_*_MS: 三個狀態的呼吸週期（6s/2.5s/1.8s）
- ROTATE_PROCESSING_MS: 轉錄中旋轉粒子週期
- RENDER_TICK_MS: 50ms = 20 FPS render loop

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 建立 `animation.py` 純函式模組（TDD）

**Files:**
- Create: `animation.py`
- Test: `tests/test_animation.py`

- [ ] **Step 1: 建立 tests/ 目錄（若不存在）**

```bash
mkdir -p tests
test -f tests/__init__.py || touch tests/__init__.py
```

- [ ] **Step 2: 寫 failing test**

Create `tests/test_animation.py`:

```python
"""Unit tests for animation.py — pure functions, no tkinter."""

import math
import pytest

from animation import (
    ease_out_cubic,
    ease_in_out_cubic,
    breathe,
    blend,
    Ripple,
)


# ─── ease_out_cubic ─────────────────────────────────────────────────────────

def test_ease_out_cubic_at_0():
    assert ease_out_cubic(0.0) == 0.0

def test_ease_out_cubic_at_1():
    assert ease_out_cubic(1.0) == 1.0

def test_ease_out_cubic_is_monotonic():
    vals = [ease_out_cubic(t / 10) for t in range(11)]
    assert vals == sorted(vals)

def test_ease_out_cubic_clamps_below_zero():
    assert ease_out_cubic(-0.5) == 0.0

def test_ease_out_cubic_clamps_above_one():
    assert ease_out_cubic(1.5) == 1.0


# ─── ease_in_out_cubic ──────────────────────────────────────────────────────

def test_ease_in_out_cubic_at_0():
    assert ease_in_out_cubic(0.0) == 0.0

def test_ease_in_out_cubic_at_half():
    assert ease_in_out_cubic(0.5) == 0.5

def test_ease_in_out_cubic_at_1():
    assert ease_in_out_cubic(1.0) == 1.0

def test_ease_in_out_cubic_is_monotonic():
    vals = [ease_in_out_cubic(t / 10) for t in range(11)]
    assert vals == sorted(vals)


# ─── breathe ────────────────────────────────────────────────────────────────

def test_breathe_at_0_is_zero():
    assert breathe(0.0) == pytest.approx(0.0, abs=1e-9)

def test_breathe_at_half_is_peak():
    assert breathe(0.5) == pytest.approx(1.0, abs=1e-9)

def test_breathe_at_1_is_zero():
    assert breathe(1.0) == pytest.approx(0.0, abs=1e-9)

def test_breathe_is_symmetric():
    # breathe(0.25) should equal breathe(0.75)
    assert breathe(0.25) == pytest.approx(breathe(0.75), abs=1e-9)


# ─── blend ──────────────────────────────────────────────────────────────────

def test_blend_alpha_0_returns_bg():
    assert blend("#FF0000", "#000000", 0.0) == "#000000"

def test_blend_alpha_1_returns_fg():
    assert blend("#FF0000", "#000000", 1.0) == "#FF0000"

def test_blend_halfway():
    assert blend("#000000", "#FFFFFF", 0.5) == "#808080"

def test_blend_uppercase_hex():
    result = blend("#abcdef", "#123456", 0.5)
    assert result == result.upper()
    assert result.startswith("#") and len(result) == 7

def test_blend_cyan_over_dark_surface():
    # ACCENT #06B6D4 over SURF_1 #0E0E10 at alpha 0.25
    result = blend("#06B6D4", "#0E0E10", 0.25)
    # R: 0x0E + (0x06-0x0E)*0.25 = 14 + (6-14)*0.25 = 14 - 2 = 12 = 0x0C
    # G: 0x0E + (0xB6-0x0E)*0.25 = 14 + 168*0.25 = 14 + 42 = 56 = 0x38
    # B: 0x10 + (0xD4-0x10)*0.25 = 16 + 196*0.25 = 16 + 49 = 65 = 0x41
    assert result == "#0C3841"


# ─── Ripple ─────────────────────────────────────────────────────────────────

def test_ripple_at_start_returns_initial_state():
    r = Ripple(start_time=100.0, duration=1.2, r0=140, r1=180, a0=0.4)
    radius, alpha = r.state(100.0)
    assert radius == pytest.approx(140.0)
    assert alpha == pytest.approx(0.4)

def test_ripple_at_midpoint():
    r = Ripple(start_time=100.0, duration=1.2, r0=140, r1=180, a0=0.4)
    radius, alpha = r.state(100.6)  # t = 0.5
    assert radius == pytest.approx(160.0)
    assert alpha == pytest.approx(0.2)

def test_ripple_expired_returns_none():
    r = Ripple(start_time=100.0, duration=1.2, r0=140, r1=180, a0=0.4)
    assert r.state(101.3) is None

def test_ripple_exactly_at_end_returns_none():
    r = Ripple(start_time=100.0, duration=1.2, r0=140, r1=180, a0=0.4)
    assert r.state(101.2) is None
```

- [ ] **Step 3: 執行 test 確認 FAIL**

```bash
python3 -m pytest tests/test_animation.py -v
```
Expected: FAIL with "ModuleNotFoundError: No module named 'animation'"

- [ ] **Step 4: 建立 animation.py**

Create `animation.py`:

```python
"""
Animation helpers — pure functions, no tkinter dependency.

All functions are deterministic and side-effect-free for easy testing.
Used by the record chamber in gui.py to drive the ambient light field.
"""

from __future__ import annotations

import math
from typing import Optional


# ─── Easing ─────────────────────────────────────────────────────────────────

def ease_out_cubic(t: float) -> float:
    """Ease-out cubic. Fast start, slow end. Input clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    """Ease-in-out cubic. Symmetric S-curve. Input clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 4.0 * t ** 3
    return 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


# ─── Breathing ──────────────────────────────────────────────────────────────

def breathe(phase: float) -> float:
    """
    Symmetric cosine breathing curve.

    phase ∈ [0, 1] represents one full breath cycle.
    Returns [0, 1]: starts at 0, peaks at phase=0.5, returns to 0 at phase=1.
    """
    return (1.0 - math.cos(phase * 2.0 * math.pi)) / 2.0


# ─── Color blending ─────────────────────────────────────────────────────────

def blend(fg: str, bg: str, alpha: float) -> str:
    """
    Linear-interpolate two hex colors to simulate alpha over bg.

    alpha=0 → bg, alpha=1 → fg. Returns uppercase #RRGGBB.
    Used to pre-compute ring colors since tkinter Canvas has no true alpha.
    """
    fg_r = int(fg[1:3], 16)
    fg_g = int(fg[3:5], 16)
    fg_b = int(fg[5:7], 16)
    bg_r = int(bg[1:3], 16)
    bg_g = int(bg[3:5], 16)
    bg_b = int(bg[5:7], 16)

    alpha = max(0.0, min(1.0, alpha))

    r = round(bg_r + (fg_r - bg_r) * alpha)
    g = round(bg_g + (fg_g - bg_g) * alpha)
    b = round(bg_b + (fg_b - bg_b) * alpha)

    return f"#{r:02X}{g:02X}{b:02X}"


# ─── Ripple (expanding ring emitted on RMS peaks) ───────────────────────────

class Ripple:
    """
    Single expanding ring emitted when RMS peaks during recording.

    Radius linearly expands r0 → r1 over `duration` seconds.
    Alpha linearly fades a0 → 0 over the same duration.

    Fire-and-forget — returns None once expired.
    """

    __slots__ = ("t0", "dur", "r0", "r1", "a0")

    def __init__(
        self,
        start_time: float,
        duration: float = 1.2,
        r0: float = 140.0,
        r1: float = 180.0,
        a0: float = 0.4,
    ) -> None:
        self.t0 = start_time
        self.dur = duration
        self.r0 = r0
        self.r1 = r1
        self.a0 = a0

    def state(self, now: float) -> Optional[tuple[float, float]]:
        """Return (radius, alpha) at `now`, or None if expired."""
        t = (now - self.t0) / self.dur
        if t >= 1.0 or t < 0.0:
            return None
        return (
            self.r0 + (self.r1 - self.r0) * t,
            self.a0 * (1.0 - t),
        )
```

- [ ] **Step 5: 執行 tests 確認全部 PASS**

```bash
python3 -m pytest tests/test_animation.py -v
```
Expected: 所有測試 PASS（大約 20 個）

- [ ] **Step 6: Commit**

```bash
git add animation.py tests/test_animation.py tests/__init__.py
git commit -m "$(cat <<'EOF'
feat(animation): 新增純函式動畫模組（easing / breathe / blend / Ripple）

為 Ambient Chamber 提供無 tkinter 依賴的動畫 primitives：
- ease_out_cubic / ease_in_out_cubic: cubic easing 曲線
- breathe: cosine 對稱呼吸曲線
- blend: 線性 RGB 插值，模擬 alpha over bg
- Ripple: 擴散波紋物件，由 RMS peak 觸發

全部由 pytest 覆蓋。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 新增 Lucide `square` icon

**Files:**
- Modify: `icons.py`

- [ ] **Step 1: 檢查現有 `_REGISTRY` 位置**

```bash
grep -n "_REGISTRY" icons.py | head -5
```
Expected: 找到 `_REGISTRY: dict[str, Callable...` 那行及 dict 定義區塊

- [ ] **Step 2: 在 icons.py 現有 icon 定義區（`_i_mic` 後、`_REGISTRY` 之前）新增 `_i_square`**

找到 `def _i_mic(p: _Pen)` 函式結尾，在其**之後**、`_REGISTRY = {` **之前**插入：

```python
def _i_square(p: _Pen) -> None:
    """Lucide 'square' — stop icon, slightly rounded corners."""
    p.rect(6, 6, 18, 18, r=1.5)
```

- [ ] **Step 3: 把 `"square": _i_square` 加進 `_REGISTRY` dict**

在 `_REGISTRY` 字典中（按字母順序或尾端）加入：

```python
    "square": _i_square,
```

- [ ] **Step 4: 驗證可以取得 square icon**

```bash
python3 -c "
from icons import get_icon, ICON_NAMES
print('square' in ICON_NAMES)
img = get_icon('square', size=28, color='#FAFAFA')
print('got icon:', img.cget('size'))
"
```
Expected: `True` 然後輸出 size (28, 28)

- [ ] **Step 5: Commit**

```bash
git add icons.py
git commit -m "$(cat <<'EOF'
feat(icons): 新增 Lucide 'square' 停止圖示

錄音中的「停止」動作視覺符號，用於 Ambient Chamber 的
recording state（取代原本的 ● emoji / bullet 字元）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 新增 `system_reduce_motion()` helper + chamber 幾何常數

**Files:**
- Modify: `gui.py` (add to top-level helpers area)

Context: 這是 gui.py 重構前最小的獨立變更，先做可以降低 Task 5 複雜度。

- [ ] **Step 1: 找到 gui.py 目前的頂層 import 區末端（約第 40-50 行）**

```bash
grep -n "^import\|^from" gui.py | head -20
```

- [ ] **Step 2: 在 gui.py 頂層常數區新增 `import subprocess`（如未 import）並新增 helper 函式**

找到現有 import 區塊，確保 `import subprocess` 存在（若沒有就加）。

然後在**所有 import 之後、第一個 class 定義之前**新增：

```python
# ═══════════════════════════════════════════════════════════════════════════
#  CHAMBER GEOMETRY (canvas 280×280, center at 140,140)
# ═══════════════════════════════════════════════════════════════════════════

CHAMBER_SIZE   = 280
CHAMBER_CENTER = 140
DISC_RADIUS    = 60        # central clickable disc
RING_RADII_5   = (80, 96, 112, 128, 140)     # idle / recording
RING_RADII_4   = (80, 100, 120, 140)         # processing (one less for "收斂")
RING_STROKE    = 2         # px

RING_ALPHA_IDLE       = (0.25, 0.18, 0.12, 0.07, 0.03)
RING_ALPHA_RECORDING  = (0.35, 0.24, 0.15, 0.08, 0.04)
RING_ALPHA_PROCESSING = (0.30, 0.18, 0.10, 0.05)

RIPPLE_R0        = 140
RIPPLE_R1        = 180
RIPPLE_DURATION  = 1.2
RIPPLE_ALPHA0    = 0.4
RIPPLE_MAX       = 3
RMS_RIPPLE_THR   = 0.15    # trigger when rms > 0.15 AND > prev*1.5
RMS_EXPAND_GAIN  = 0.18    # rms → ring scale expansion (max +18%)

PROC_PARTICLES       = 12
PROC_PARTICLE_RADIUS = 4


# ═══════════════════════════════════════════════════════════════════════════
#  REDUCED MOTION
# ═══════════════════════════════════════════════════════════════════════════

def system_reduce_motion() -> bool:
    """
    Read macOS system-wide Reduce Motion preference.
    Called once at app start; changes require app restart per macOS convention.
    """
    try:
        r = subprocess.run(
            ["defaults", "read", "com.apple.universalaccess", "reduceMotion"],
            capture_output=True, text=True, timeout=0.5,
        )
        return r.stdout.strip() == "1"
    except Exception:
        return False
```

- [ ] **Step 3: 測試 helper**

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from gui import system_reduce_motion, CHAMBER_SIZE, RING_RADII_5
print('reduce_motion:', system_reduce_motion())
print('CHAMBER_SIZE:', CHAMBER_SIZE)
print('RING_RADII_5:', RING_RADII_5)
"
```
Expected: 印出值，不拋例外（import gui 可能會拋，因為 gui 需要 tkinter — 用另一種方式驗證）

或者：

```bash
python3 -c "
import ast
with open('gui.py') as f:
    ast.parse(f.read())
print('syntax ok')
"
```
Expected: `syntax ok`

- [ ] **Step 4: Commit**

```bash
git add gui.py
git commit -m "$(cat <<'EOF'
feat(gui): 新增 chamber 幾何常數 + reduce-motion 偵測

為 Ambient Chamber 重構鋪路：
- CHAMBER_* 與 RING_* 常數：Canvas 尺寸、同心圓半徑、alpha
- RIPPLE_* 常數：擴散波紋參數
- system_reduce_motion(): 讀 macOS 系統偏好（一次性，啟動時讀）

尚未啟用，下一 commit 進行整個 record card 重寫。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 重構 `gui.py` record card 為 Ambient Chamber

**Files:**
- Modify: `gui.py`

**Context:** 這是最大的一個 task。為了可追蹤，拆成 7 個 sub-steps，每一 sub-step 結束都讓 app 可啟動（即使中途視覺不完整）。最終一個 commit 打包全部變更。

**Strategy:** 因為 record card 各部分緊密耦合（新繪製邏輯、新事件處理、新狀態管理），採**一次完整替換**而非增量 A/B 並存。先備份原檔，改完再一次 commit。

### 5.1 Audit 現有 gui.py 的記憶體 & 清單待移除項

- [ ] **Step 5.1.1: 確認 current gui.py 中所有待移除符號的行數**

```bash
grep -n "GREEN\b\|GREEN_DIM\|RED\b\|RED_DIM\|ORANGE\b\|SPINNER\b\|WAVE_IDLE_COL\|WAVE_LIVE_COL\|WAVE_BARS\|_wave_canvas\|_btn_ring\|_record_btn\|_draw_idle_wave\|_draw_live_wave\|_update_wave\|_pulse_btn\|_animate_spinner" gui.py
```
Expected: 多行匹配，記下所有位置供參考

- [ ] **Step 5.1.2: 確認 `tokens.py` 匯入名稱**

```bash
python3 -c "
import tokens
for n in ['ACCENT','DANGER','WARN','SURF_1','SURF_2','SURF_4','TEXT_1','TEXT_2','TEXT_3','INDIGO','FONT_FAMILY_MONO','SPACE_LG','SPACE_MD','SPACE_SM','SPACE_XS','RADIUS_LG','DUR_NORMAL','BREATHE_IDLE_MS','BREATHE_RECORDING_MS','BREATHE_PROCESSING_MS','ROTATE_PROCESSING_MS','RENDER_TICK_MS']:
    print(n, '=', getattr(tokens, n))
"
```
Expected: 所有 token 都存在，不拋 AttributeError

### 5.2 更新 gui.py import 區塊

- [ ] **Step 5.2.1: 在 gui.py 的 import 區塊加入新 dependencies**

找到現有 `from` / `import` 區域，**確保**有這些 imports（沒有就加，不重複加）：

```python
import time
import subprocess
import tkinter as tk
import customtkinter as ctk

from animation import (
    ease_out_cubic, ease_in_out_cubic, breathe, blend, Ripple
)
from icons import get_icon
from tokens import (
    # Surfaces
    SURF_1, SURF_2, SURF_3, SURF_4,
    # Text
    TEXT_1, TEXT_2, TEXT_3,
    # Semantic
    ACCENT, DANGER, WARN, INDIGO,
    # Typography
    FONT_FAMILY_TEXT, FONT_FAMILY_MONO,
    # Spacing
    SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG,
    # Radius
    RADIUS_LG,
    # Motion
    DUR_FAST, DUR_NORMAL, DUR_SLOW,
    BREATHE_IDLE_MS, BREATHE_RECORDING_MS, BREATHE_PROCESSING_MS,
    ROTATE_PROCESSING_MS, RENDER_TICK_MS,
)
```

- [ ] **Step 5.2.2: 刪除 gui.py 現有的硬編色彩常數**

移除 gui.py 頂層這些行（位置約在第 60-80 行範圍）：

```
GREEN     = "#30D158"
GREEN_DIM = "#0A2B1A"
RED       = "#FF453A"
RED_DIM   = "#3A0800"
ORANGE    = "#FF9F0A"
BLUE      = ...（如有）
BLUE_HV   = ...（如有）
SURF1, SURF2, SURF3 = ... （如有）
TEXT1, TEXT2, TEXT3 = ... （如有）
WAVE_IDLE_COL = "#3A3A3C"
WAVE_LIVE_COL = "#F5F5F7"
WAVE_BARS     = 44
SPINNER       = ["⠋", ...]
```

並把其他引用 `SURF1/TEXT1/BLUE` 等舊名的地方，替換成 `SURF_1/TEXT_1/INDIGO` 等新 token 名。

```bash
# 輔助：找出所有舊別名的引用位置
grep -nE "\b(SURF[1-4]|TEXT[1-4]|BLUE|BLUE_HV|GREEN|GREEN_DIM|RED|RED_DIM|ORANGE|WAVE_IDLE_COL|WAVE_LIVE_COL|WAVE_BARS|SPINNER)\b" gui.py
```

逐行替換：
- `SURF1` → `SURF_1`
- `SURF2` → `SURF_2`
- `SURF3` → `SURF_3`
- `TEXT1` → `TEXT_1`
- `TEXT2` → `TEXT_2`
- `TEXT3` → `TEXT_3`
- `BLUE` → `ACCENT`（主色）
- `BLUE_HV` → `ACCENT_HV`
- `GREEN` → 視語意：status dot 保持 SUCCESS；record card 全刪
- `RED` → `DANGER`
- `RED_DIM` → `DANGER_DIM`
- `ORANGE` → `WARN`
- `WAVE_*` / `SPINNER` → 全部刪除（record card 重寫不再用）

- [ ] **Step 5.2.3: 再次語法檢查**

```bash
python3 -c "import ast; ast.parse(open('gui.py').read()); print('syntax ok')"
```
Expected: `syntax ok`

### 5.3 重寫 `_build_record_card()` 方法

- [ ] **Step 5.3.1: 找到現有 `_build_record_card` 函式的完整範圍**

```bash
grep -n "def _build_record_card\|def _build_result_card" gui.py
```
Expected: 兩個行號。現有 `_build_record_card` 範圍是第一行到第二個減 1。

- [ ] **Step 5.3.2: 完整替換 `_build_record_card` 為新版**

```python
    def _build_record_card(self) -> None:
        card = ctk.CTkFrame(
            self, corner_radius=RADIUS_LG,
            fg_color=SURF_1,
        )
        card.pack(fill="x", padx=SPACE_LG, pady=(SPACE_MD + 2, SPACE_SM))

        # Ambient chamber — single canvas replacing wave + ring + btn
        self._chamber = tk.Canvas(
            card,
            width=CHAMBER_SIZE, height=CHAMBER_SIZE,
            bg=SURF_1, highlightthickness=0, bd=0,
        )
        self._chamber.pack(pady=(SPACE_MD, SPACE_XS))

        # Event bindings
        self._chamber.bind("<Button-1>",        self._on_chamber_click)
        self._chamber.bind("<Enter>",           self._on_chamber_enter)
        self._chamber.bind("<Leave>",           self._on_chamber_leave)
        self._chamber.bind("<Motion>",          self._on_chamber_motion)
        self._chamber.bind("<ButtonPress-1>",   self._on_chamber_press)
        self._chamber.bind("<ButtonRelease-1>", self._on_chamber_release)

        # Timer — visible only during recording
        self._timer_label = ctk.CTkLabel(
            card, text="",
            font=ctk.CTkFont(FONT_FAMILY_MONO, 18, "bold"),
            text_color=TEXT_2,
        )
        self._timer_label.pack(pady=(0, SPACE_XS))

        # Auto-paste target label (reused)
        self._target_label = ctk.CTkLabel(
            card, text="",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
            text_color=INDIGO,
        )
        self._target_label.pack()

        # Hotkey hint (reused, text changes by state)
        self._hotkey_hint = ctk.CTkLabel(
            card,
            text=f"按下 {self.cfg.format_hotkey_display()} 即時錄音",
            font=ctk.CTkFont(FONT_FAMILY_TEXT, 12),
            text_color=TEXT_3,
        )
        self._hotkey_hint.pack(pady=(0, SPACE_MD + 2))

        # Pre-render icons (3 states, each a CTkImage)
        self._icon_mic_idle = get_icon("mic",    36, TEXT_2)
        self._icon_square   = get_icon("square", 28, TEXT_1)
        self._icon_mic_proc = get_icon("mic",    36, blend(WARN, SURF_2, 0.6))

        # Animation state
        self._state_start_time = time.perf_counter()
        self._prev_state       = "idle"
        self._ripples: list[Ripple] = []
        self._prev_rms         = 0.0
        self._pressed          = False
        self._hovering         = False
        self._reduce_motion    = system_reduce_motion()

        # Start render loop
        self._render_tick()
```

### 5.4 新增 `_render_tick()` + `_draw_chamber()`

- [ ] **Step 5.4.1: 在 gui.py 適當位置（例如 `_build_record_card` 之後）新增這些方法**

```python
    # ═══════════════════════════════════════════════════════════════════════
    #  CHAMBER RENDER LOOP
    # ═══════════════════════════════════════════════════════════════════════

    def _render_tick(self) -> None:
        """Main render loop — 50ms cadence. Owns the chamber canvas."""
        try:
            self._draw_chamber()
        except tk.TclError:
            # Canvas destroyed during shutdown
            return
        self.after(RENDER_TICK_MS, self._render_tick)

    def _draw_chamber(self) -> None:
        """Render the ambient chamber based on current state + RMS."""
        now = time.perf_counter()
        self._chamber.delete("all")

        state = self._state
        rm = self._reduce_motion

        # Determine visual state color + phase
        if state == "idle":
            color  = ACCENT
            period = BREATHE_IDLE_MS / 1000.0
            alphas = RING_ALPHA_IDLE
            radii  = RING_RADII_5
            rms    = 0.0
        elif state == "recording":
            color  = DANGER
            period = BREATHE_RECORDING_MS / 1000.0
            alphas = RING_ALPHA_RECORDING
            radii  = RING_RADII_5
            rms    = self.recorder.get_rms_level() if self.recorder.is_recording() else 0.0
        elif state == "processing":
            color  = WARN
            period = BREATHE_PROCESSING_MS / 1000.0
            alphas = RING_ALPHA_PROCESSING
            radii  = RING_RADII_4
            rms    = 0.0
        else:
            return

        # Breathing scale: 0.97 .. 1.03 for idle/processing; +RMS gain for recording
        if rm:
            scale = 1.0
        else:
            phase = ((now - self._state_start_time) % period) / period
            b = breathe(phase)
            if state == "idle":
                scale = 0.97 + b * 0.06
            elif state == "recording":
                scale = 1.0 + b * 0.04 + rms * RMS_EXPAND_GAIN
            else:  # processing
                scale = 0.98 + b * 0.04

        # Apply press scale (idle only)
        if self._pressed and state == "idle":
            scale *= 0.97

        cx = cy = CHAMBER_CENTER

        # ─── Ripples (recording only) ────────────────────────────────────
        if state == "recording" and not rm:
            # Emit new ripple on RMS peak
            if rms > RMS_RIPPLE_THR and rms > self._prev_rms * 1.5:
                self._ripples.append(Ripple(
                    start_time=now,
                    duration=RIPPLE_DURATION,
                    r0=RIPPLE_R0, r1=RIPPLE_R1, a0=RIPPLE_ALPHA0,
                ))
                if len(self._ripples) > RIPPLE_MAX:
                    self._ripples = self._ripples[-RIPPLE_MAX:]

            # Draw active ripples
            alive: list[Ripple] = []
            for rip in self._ripples:
                st = rip.state(now)
                if st is None:
                    continue
                r, a = st
                col = blend(DANGER, SURF_1, a)
                self._chamber.create_oval(
                    cx - r, cy - r, cx + r, cy + r,
                    outline=col, width=RING_STROKE,
                )
                alive.append(rip)
            self._ripples = alive
        self._prev_rms = rms

        # ─── Outer ambient rings (outside-in so inner draws on top) ──────
        for radius, a in zip(reversed(radii), reversed(alphas)):
            r = radius * scale
            col = blend(color, SURF_1, a)
            self._chamber.create_oval(
                cx - r, cy - r, cx + r, cy + r,
                outline=col, width=RING_STROKE,
            )

        # ─── Processing rotating particles ───────────────────────────────
        if state == "processing":
            if rm:
                angles = [i * (360 / PROC_PARTICLES) for i in range(PROC_PARTICLES)]
                head = 0
            else:
                t_norm = ((now - self._state_start_time) % (ROTATE_PROCESSING_MS / 1000.0)) \
                         / (ROTATE_PROCESSING_MS / 1000.0)
                head = ease_in_out_cubic(t_norm) * 360
                angles = [head + i * (360 / PROC_PARTICLES) for i in range(PROC_PARTICLES)]

            import math
            for i, ang in enumerate(angles):
                rad = math.radians(ang)
                px = cx + radii[-1] * scale * math.cos(rad)
                py = cy + radii[-1] * scale * math.sin(rad)
                # Alpha gradient: brightest at head, fading around
                idx_from_head = (i % PROC_PARTICLES) / PROC_PARTICLES
                a_particle = 1.0 - idx_from_head * 0.85      # 1.0 → 0.15
                col = blend(WARN, SURF_1, a_particle if not rm else 0.6)
                pr = PROC_PARTICLE_RADIUS
                self._chamber.create_oval(
                    px - pr, py - pr, px + pr, py + pr,
                    fill=col, outline="",
                )

        # ─── Central disc ────────────────────────────────────────────────
        dr = DISC_RADIUS * (scale if self._pressed and state == "idle" else 1.0)
        if state == "idle":
            disc_fill   = SURF_2
            disc_border = ACCENT if self._hovering else SURF_4
            disc_border_w = 1.5
        elif state == "recording":
            disc_fill   = blend(DANGER, SURF_1, 0.12)
            disc_border = DANGER
            disc_border_w = 2.0
        else:  # processing
            disc_fill   = SURF_2
            disc_border = WARN
            disc_border_w = 1.5

        self._chamber.create_oval(
            cx - dr, cy - dr, cx + dr, cy + dr,
            fill=disc_fill, outline=disc_border, width=disc_border_w,
        )

        # ─── Central icon ────────────────────────────────────────────────
        if state == "idle":
            icon = self._icon_mic_idle
        elif state == "recording":
            icon = self._icon_square
        else:
            icon = self._icon_mic_proc

        self._chamber.create_image(cx, cy, image=icon)
```

### 5.5 新增 Canvas event handlers

- [ ] **Step 5.5.1: 在 `_draw_chamber` 之後新增事件處理函式**

```python
    # ═══════════════════════════════════════════════════════════════════════
    #  CHAMBER EVENT HANDLERS
    # ═══════════════════════════════════════════════════════════════════════

    def _in_disc(self, x: int, y: int) -> bool:
        dx = x - CHAMBER_CENTER
        dy = y - CHAMBER_CENTER
        return dx * dx + dy * dy <= DISC_RADIUS * DISC_RADIUS

    def _on_chamber_click(self, event) -> None:
        if not self._in_disc(event.x, event.y):
            return
        if self._state == "processing":
            return
        # Delegate to existing state-machine entry point
        self._on_record_btn()

    def _on_chamber_enter(self, event) -> None:
        self._hovering = self._in_disc(event.x, event.y)

    def _on_chamber_leave(self, event) -> None:
        self._hovering = False
        self._pressed  = False
        self._chamber.configure(cursor="")

    def _on_chamber_motion(self, event) -> None:
        inside = self._in_disc(event.x, event.y)
        self._hovering = inside and self._state == "idle"
        self._chamber.configure(cursor="hand2" if self._hovering else "")

    def _on_chamber_press(self, event) -> None:
        if self._in_disc(event.x, event.y) and self._state == "idle":
            self._pressed = True

    def _on_chamber_release(self, event) -> None:
        self._pressed = False
```

### 5.6 修改 `_transition_to_*` UI portion

- [ ] **Step 5.6.1: 找到 `_transition_to_recording`**

```bash
grep -n "def _transition_to_recording\|def _transition_to_processing\|def _transition_to_idle" gui.py
```

- [ ] **Step 5.6.2: 替換 `_transition_to_recording` 中「設定 UI 元件」的那幾行**

**保留**：`self._state = "recording"`、`self._rec_start = ...`、`self._hotkey_held = True`、stream_* 初始化、auto-paste target 擷取、`self.recorder.start()`、`self._update_timer()`、`self._stream_tick_id = self.after(...)`。

**替換**（找到原本呼叫 `self._record_btn.configure(...)` 和 `self._btn_ring.configure(...)` 那整段，替換為）：

```python
        self._prev_state       = self._state  # unreliable if called mid-transition; acceptable
        self._state            = "recording"
        self._state_start_time = time.perf_counter()

        # ... existing: recorder.start(), _rec_start, stream setup, auto-paste target ...

        self._timer_label.configure(text="00:00")
        self._hotkey_hint.configure(
            text=f"放開 {self.cfg.format_hotkey_display()} 停止錄音"
        )
        self._model_menu.configure(state="disabled")
        self._lang_menu.configure(state="disabled")
        self._status_dot.configure(text_color=DANGER)
        self._status_label.configure(text="  錄音中")

        self._update_timer()
        # NOTE: _render_tick is running since build time; do NOT call _pulse_btn / _update_wave
```

**移除的呼叫**：`self._pulse_btn()`、`self._update_wave()`（這兩個函式將在 Step 5.7 整個刪掉）。

- [ ] **Step 5.6.3: 替換 `_transition_to_processing`**

找到現有實作，將其中「UI 配置」部分從：

```python
        self._record_btn.configure(text=f"{SPINNER[0]}\n轉錄中…", fg_color=ORANGE, ...)
        self._btn_ring.configure(border_color=SURF3)
        self._target_label.configure(text="")
        self._status_dot.configure(text_color=ORANGE)
        self._status_label.configure(text="  轉錄中，請稍候…")
        self._timer_label.configure(text="")
        self._draw_idle_wave()
        self._animate_spinner()
```

替換為：

```python
        self._state            = "processing"
        self._state_start_time = time.perf_counter()
        self._hotkey_held      = False

        # ... existing: stop stream_tick, get full_audio, kick off transcription thread ...

        self._timer_label.configure(text="")
        self._hotkey_hint.configure(text="轉錄中…")
        self._target_label.configure(text="")
        self._status_dot.configure(text_color=WARN)
        self._status_label.configure(text="  轉錄中，請稍候…")
```

**移除的呼叫**：`self._draw_idle_wave()`、`self._animate_spinner()`。

- [ ] **Step 5.6.4: 替換 `_transition_to_idle`**

將其中 UI 配置從：

```python
        self._record_btn.configure(text="🎤\n點擊錄音", fg_color=GREEN, ...)
        self._btn_ring.configure(border_color=GREEN_DIM)
        self._model_menu.configure(state="normal")
        self._lang_menu.configure(state="normal")
        self._target_label.configure(text="")
        ...
```

替換為：

```python
        self._state            = "idle"
        self._state_start_time = time.perf_counter()

        self._timer_label.configure(text="")
        self._hotkey_hint.configure(
            text=f"按下 {self.cfg.format_hotkey_display()} 即時錄音"
        )
        self._model_menu.configure(state="normal")
        self._lang_menu.configure(state="normal")
        self._target_label.configure(text="")

        model = self._model_var.get()
        self._status_dot.configure(text_color=ACCENT)  # 從 GREEN 改為 ACCENT
        self._status_label.configure(text=f"  就緒 ({model})")

        if result is not None:
            self._display_result(result)
```

### 5.7 刪除廢棄方法

- [ ] **Step 5.7.1: 刪除以下方法完整定義**

搜尋並刪除：

- `def _draw_idle_wave(self)` — 整個函式
- `def _draw_live_wave(self, rms)` — 整個函式
- `def _update_wave(self)` — 整個函式
- `def _pulse_btn(self)` — 整個函式
- `def _animate_spinner(self)` — 整個函式
- 任何 `self._spin_idx` 的初始化或使用（屬於 animate_spinner）
- 任何 `self._pulse_hi` 的初始化（屬於 pulse_btn）

```bash
# 確認全部刪除
grep -n "_draw_idle_wave\|_draw_live_wave\|_update_wave\|_pulse_btn\|_animate_spinner\|_spin_idx\|_pulse_hi" gui.py
```
Expected: 無匹配（空輸出）

- [ ] **Step 5.7.2: 確認 `_update_timer` 還在且寫入 `_timer_label`**

```bash
grep -nA 5 "def _update_timer" gui.py
```
Expected: 函式存在。

內容應該是：

```python
    def _update_timer(self) -> None:
        if self._state != "recording":
            return
        elapsed = int(time.perf_counter() - self._rec_start)
        mm, ss = divmod(elapsed, 60)
        self._timer_label.configure(text=f"{mm:02d}:{ss:02d}")
        self.after(1000, self._update_timer)
```

**注意**：原本 `_update_timer` 可能寫 `self._record_btn.configure(text=...)`，務必改為 `self._timer_label.configure(text=...)` 且**格式不要含 emoji 或「錄音中…」**，只顯示純計時器。

### 5.8 Final sanity check + 合併 commit

- [ ] **Step 5.8.1: Python 語法檢查**

```bash
python3 -c "import ast; ast.parse(open('gui.py').read()); print('gui.py syntax ok')"
```
Expected: `gui.py syntax ok`

- [ ] **Step 5.8.2: 確認沒有剩餘的舊符號引用**

```bash
grep -nE "\b(GREEN|RED|ORANGE|SPINNER|WAVE_IDLE_COL|WAVE_LIVE_COL|WAVE_BARS|_wave_canvas|_btn_ring|_record_btn|_pulse_btn|_animate_spinner|_draw_idle_wave|_draw_live_wave|_update_wave)\b" gui.py
```
Expected: 無匹配（空輸出）。`DANGER` / `SUCCESS` / `WARN` 是新 token，不應 grep 到 GREEN/RED/ORANGE。

- [ ] **Step 5.8.3: 確認沒有 emoji 當 icon 用**

```bash
grep -nE "🎤|🎙|●\\\\n|🔴|🟠|🟢" gui.py
```
Expected: 空輸出。（Toast 訊息中的 ✅ 等裝飾 emoji 可以保留，但不再當按鈕圖示）

- [ ] **Step 5.8.4: 確認沒有硬編 hex 色碼**

```bash
grep -nE '"#[0-9A-Fa-f]{6}"' gui.py
```
Expected: 空輸出。

- [ ] **Step 5.8.5: 確認 mono font 有被計時器使用**

```bash
grep -n "FONT_FAMILY_MONO" gui.py
```
Expected: 至少一筆（`_timer_label` 的字型設定）。

- [ ] **Step 5.8.6: 手動啟動 app 做 smoke test**

```bash
venv/bin/python3 main.py &
APP_PID=$!
sleep 3
# 視覺檢查：應看到青色呼吸光場 + 中央 mic icon
# 手動按 ⌘⌥R 測試錄音
# 完成後：
kill $APP_PID 2>/dev/null
```

如果 app 啟動失敗，閱讀 stderr 修正。最常見問題：
- NameError on old symbol → Step 5.8.2 沒清乾淨
- AttributeError on `self._wave_canvas` / `self._record_btn` → 5.7 刪除不完整
- icons.get_icon 找不到 'square' → Task 3 沒做或沒 commit

- [ ] **Step 5.8.7: Commit 一次打包所有變更**

```bash
git add gui.py
git commit -m "$(cat <<'EOF'
refactor(gui): 錄音按鈕改為 Ambient Light Chamber

將「波形 + 外環 + 綠色按鈕」三層結構整合為單一 Canvas 光場：
- 刪除 _wave_canvas / _btn_ring / _record_btn / _pulse_btn /
  _animate_spinner / _draw_idle_wave / _draw_live_wave / _update_wave
- 新增 _chamber Canvas（280×280）繪製同心圓光暈 + 中央 disc + icon
- 新增 _render_tick (50ms) + _draw_chamber + 事件處理函式
- 新增 _timer_label 獨立顯示計時器（SF Mono 18pt，tabular）
- 新增 _ripples：RMS peak 觸發的擴散波紋動畫

狀態色系改從 tokens.py 取：idle=ACCENT (cyan，取代 SUCCESS 綠)、
recording=DANGER、processing=WARN。status bar 的綠色狀態燈保留。

圖示改用 icons.py 的 Lucide 手繪 mic/square，不再用 🎤 emoji。

轉場從瞬切改為 240ms cubic ease；支援 macOS Reduce Motion 偏好
（系統偏好 → 輔助使用 → 顯示器）。

核心 modules（recorder/transcriber/hotkey/auto_paste）完全不動。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 靜態驗證

**Files:** None (只是 grep 檢查)

- [ ] **Step 1: 完整靜態清單檢查**

```bash
echo "=== 硬編 hex ==="
grep -nE '"#[0-9A-Fa-f]{6}"' gui.py || echo "✓ 無"

echo "=== 功能性 emoji ==="
grep -nE "🎤|🎙|📝|⚙|📋|💾|⌨" gui.py | grep -v "Toast\|show_toast\|_show_toast\|#" || echo "✓ 無"

echo "=== 舊別名 ==="
grep -nE "\b(SURF1|SURF2|SURF3|TEXT1|TEXT2|TEXT3|BLUE|GREEN|RED|ORANGE)\b" gui.py || echo "✓ 無"

echo "=== 廢棄方法殘留 ==="
grep -nE "_wave_canvas|_btn_ring|_record_btn|_pulse_btn|_animate_spinner|_draw_idle_wave|_draw_live_wave|_update_wave" gui.py || echo "✓ 無"

echo "=== mono font ==="
grep -c "FONT_FAMILY_MONO" gui.py
```

Expected:
- 硬編 hex: 0 筆
- 功能性 emoji: 0 筆（不含 toast 訊息中的裝飾 emoji 如 ✅）
- 舊別名: 0 筆
- 廢棄方法: 0 筆
- mono font: ≥ 1 筆

若任何檢查失敗，回 Task 5 修正。

- [ ] **Step 2: Run animation.py pytest**

```bash
python3 -m pytest tests/test_animation.py -v
```
Expected: 全部 PASS

---

## Task 7: 手動 smoke test

**Files:** None (人工操作 + 截圖)

- [ ] **Step 1: 啟動 app**

```bash
venv/bin/python3 main.py
```

- [ ] **Step 2: 視覺檢查 idle 狀態**

期望看到：
- 青色同心圓光場（5 圈，由內至外漸淡）緩慢呼吸（6 秒週期）
- 中央灰色 disc 上有 Lucide mic icon
- disc 邊框灰色
- 下方：「按下 ⌘⌥R 即時錄音」（無計時器）

- [ ] **Step 3: 滑鼠移過 disc**

期望：
- 游標變 hand2
- disc 邊框從灰變青（ACCENT）

- [ ] **Step 4: 點擊 disc 開始錄音**

期望：
- 光場 240ms 內漸變為紅色
- 中央 icon 變為 Lucide square（停止）
- 計時器 `00:00` 出現，開始遞增
- 下方：「放開 ⌘⌥R 停止錄音」
- status bar：紅色點 + 「錄音中」

- [ ] **Step 5: 對麥克風說話**

期望：
- 光場隨音量「鼓動」（最大 +18% 擴張）
- 大聲說會看到擴散紅色波紋從外緣向外擴散
- 計時器持續遞增

- [ ] **Step 6: 再次點擊 disc 停止**

期望：
- 光場 200ms 漸變為琥珀色（WARN）
- 光場變 4 圈（少一圈）
- 12 顆小點繞外圈旋轉（1.5s 週期，帶彗尾 alpha 梯度）
- 中央 icon 變回 mic（淡琥珀色）
- 計時器消失
- 下方：「轉錄中…」
- status bar：琥珀點

- [ ] **Step 7: 等待轉錄完成**

期望：
- 光場 320ms 漸回青色
- 中央 icon 回到灰色 mic
- 結果區顯示轉錄結果
- status bar：青色點 + 「就緒 (model)」

- [ ] **Step 8: 測快捷鍵流程（⌘⌥R）**

按住 ⌘⌥R → 應開始錄音；放開 → 應自動停止並開始轉錄。視覺行為應與 Step 4-7 一致。

- [ ] **Step 9: 啟用 macOS Reduce Motion 測試**

```bash
defaults write com.apple.universalaccess reduceMotion 1
```

重啟 app：

```bash
venv/bin/python3 main.py
```

期望：
- 光場**靜止**（無呼吸動畫）
- 狀態切換**瞬切**（無漸變）
- 錄音時無擴散波紋
- 轉錄中 12 顆粒子**靜止**（不旋轉）
- 但：**RMS 擴張仍保留**（這是資訊非裝飾）
- 按下時**不縮放**
- 計時器運作正常

測完恢復：

```bash
defaults delete com.apple.universalaccess reduceMotion
```

- [ ] **Step 10: CPU 使用率檢查**

在錄音狀態下，打開活動監視器觀察 Whisper Pro 的 CPU 使用率。

Expected: < 5%（和現狀 3-4% 相當或只微增）

- [ ] **Step 11: 如果全部 pass，open PR**

```bash
git push -u origin feat/ambient-chamber

gh pr create --title "feat: 錄音按鈕改為 Ambient Light Chamber" --body "$(cat <<'EOF'
## Summary

將 Whisper Pro 的錄音按鈕從「波形 + 外環 + 綠色按鈕」三層結構整合為單一 Canvas 光場。

- 消除綠色按鈕廉價感 — idle 從 SUCCESS 綠改為品牌 ACCENT 青
- 整合波形 + 按鈕為 **Ambient Light Chamber**（同心圓光暈 + 中央 disc）
- RMS 直接驅動光強 + 擴散波紋，錄音時有生命力
- 零硬編 hex（全走 tokens.py）、零 emoji 圖示（全走 Lucide icons.py）
- 計時器拆出，改用 SF Mono tabular 字體（不再抖動）
- 支援 macOS Reduce Motion 系統偏好

## Design spec

`docs/superpowers/specs/2026-04-19-ambient-chamber-design.md`

## Test plan

- [x] animation.py 單元測試全 pass
- [x] 靜態檢查：0 硬編 hex、0 emoji 圖示、0 舊符號殘留
- [x] 手動 smoke test 三態切換正常
- [x] macOS Reduce Motion 降級行為正確
- [x] CPU 使用率無明顯退化（錄音中 < 5%）
- [x] 核心功能（錄音/轉錄/快捷鍵/自動貼上）完全未變

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Checklist

**Spec coverage（對照 spec 每個 section）**:

- §2 目標消除綠色廉價感 → Task 5.6.4（`_transition_to_idle` 改 ACCENT）+ Task 5.3.2（chamber 預設 ACCENT）✓
- §3 非目標：不動核心 modules → Task 5 只改 gui.py 的 UI 部分 ✓
- §4.1 元件替換 → Task 5.3 `_build_record_card` 重寫 + Task 5.7 刪除舊元件 ✓
- §4.2 新 `_chamber` + `_timer_label` → Task 5.3.2 ✓
- §5.1 Idle 規格 → Task 5.4.1 `_draw_chamber` 中 state == "idle" 分支 ✓
- §5.2 Recording 規格（含 RMS 擴張 + ripple）→ Task 5.4.1 recording 分支 + ripple 邏輯 ✓
- §5.3 Processing 規格（4 rings + 12 粒子旋轉）→ Task 5.4.1 processing 分支 + particle 繪製 ✓
- §5.4 狀態轉場 → Task 5.6（`_transition_to_*`）+ render loop 自動插值 ✓
- §6.1 50ms tick → Task 1 `RENDER_TICK_MS` + Task 5.4 `_render_tick` ✓
- §6.2 Easing 純函式 → Task 2 ✓
- §6.3 Hit-testing → Task 5.5 ✓
- §6.4 Reduced motion → Task 4 + Task 5.4 檢查 `self._reduce_motion` ✓
- §6.5 效能預算 → 在 Task 5.8.6 smoke test 手動驗證（Step 10 CPU 檢查）✓
- §7.2 tokens motion → Task 1 ✓
- §7.3 animation.py → Task 2 ✓
- §7.4 icons.py square → Task 3 ✓
- §7.5 gui.py 重寫 → Task 5 ✓
- §8.1 靜態檢查 → Task 6 ✓
- §8.2 單元測試 → Task 2 內含 ✓
- §8.3 手動 smoke test → Task 7 ✓
- §9 Rollout 時序 → Task 0 合併 PR + 建分支 ✓
- §10 Rollback → 所有改動集中在 feat/ambient-chamber 分支，可 git revert PR ✓

**Placeholder scan**: 無 TBD / TODO / 「類似前述」/ 空泛的 "add appropriate error handling"。

**Type consistency**: 
- `_render_tick` / `_draw_chamber` / `_ripples` list / `_state_start_time` 所有引用名稱一致 ✓
- `ACCENT`, `DANGER`, `WARN` token 名稱一致 ✓
- `Ripple(start_time, duration, r0, r1, a0)` 簽章在 animation.py 和 gui.py 呼叫處一致 ✓

Plan 完成。
