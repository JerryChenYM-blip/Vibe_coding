"""waveform.py（Aperture「Spectral Bands」引擎純 Python 實作）regression test。

涵蓋：
  (a) 靜音輸入時 bars 全部落在地板附近且會隨時間律動
  (b) attack 比 release 快（同幅度變化，attack 單幀變動 > release 單幀變動）
  (c) peak 永遠 >= cur 且會緩慢下降（不會瞬間跌回 cur）
  (d) rms=0.99 時 clipping=True 且 bars 被壓在 0.965 以下
  (e) bars 長度恆等於 n_bars、值恆在 [0,1]（含 n_bars=1 邊界）
  (f) 決定性：同 seed 同輸入序列 → 同輸出

數學規格照抄自 /tmp/aperture_engine.js 的 bucket() 與 drawBands()，
詳見 waveform.py 內的規格註解。
"""

from waveform import WaveformEngine

FRAME_MS = 16.667


# ═════════════════════════════════════════════════════════════════════════════
#  (a) 靜音地板 + 隨時間律動
# ═════════════════════════════════════════════════════════════════════════════

def test_silent_input_bars_near_floor_and_oscillate():
    eng = WaveformEngine(n_bars=10, seed=3.0)

    # release 很慢（1 - 0.5^(dt*0.085)），跑夠多幀讓 cur 收斂貼近地板
    for _ in range(300):
        eng.update(0.0, FRAME_MS)

    samples = []
    for _ in range(400):
        eng.update(0.0, FRAME_MS)
        bars = eng.bars
        # 地板公式範圍是 0.016 ~ 0.030，留寬裕邊界確認「落在地板附近」
        assert all(0.0 <= b <= 0.05 for b in bars)
        samples.append(bars[0])

    # 400 幀（約 6.7 秒）涵蓋地板行波一個以上週期，應觀察到明顯律動
    assert max(samples) - min(samples) > 0.005


# ═════════════════════════════════════════════════════════════════════════════
#  (b) 非對稱包絡：attack 快、release 慢
# ═════════════════════════════════════════════════════════════════════════════

def test_attack_faster_than_release():
    eng = WaveformEngine(n_bars=6, seed=0.0)

    before = eng.bars[0]
    eng.update(1.0, FRAME_MS)   # 大聲一幀 → attack
    after_attack = eng.bars[0]
    attack_delta = after_attack - before

    eng.update(0.0, FRAME_MS)   # 靜音一幀 → release
    after_release = eng.bars[0]
    release_delta = after_attack - after_release

    assert attack_delta > 0
    assert release_delta > 0
    assert attack_delta > release_delta


# ═════════════════════════════════════════════════════════════════════════════
#  (c) 峰值帽重力：peak >= cur，且緩慢下降
# ═════════════════════════════════════════════════════════════════════════════

def test_peak_tracks_and_decays_slowly():
    eng = WaveformEngine(n_bars=5, seed=2.0)

    for _ in range(6):
        eng.update(1.0, FRAME_MS)   # 連續大聲，讓 cur 衝高、peak 跟上
    peak_at_loud = eng.peaks[0]

    assert all(p >= c - 1e-9 for p, c in zip(eng.peaks, eng.bars))

    eng.update(0.0, FRAME_MS)       # 突然靜音一幀
    cur_after = eng.bars[0]
    peak_after = eng.peaks[0]

    # peak 不會跟著 cur 一起瞬間掉下去
    assert peak_after >= cur_after - 1e-9
    assert peak_after > cur_after
    # 但確實有緩慢下降（重力），不是原地不動或上升
    assert peak_after < peak_at_loud


# ═════════════════════════════════════════════════════════════════════════════
#  (d) 削波：lvl > 0.86 → clipping=True，bars 壓在 0.965 以下
# ═════════════════════════════════════════════════════════════════════════════

def test_clipping_caps_bars_at_ceiling():
    eng = WaveformEngine(n_bars=12, seed=5.0)

    for _ in range(50):
        eng.update(0.99, FRAME_MS)

    assert eng.clipping is True
    assert all(b <= 0.965 + 1e-9 for b in eng.bars)


# ═════════════════════════════════════════════════════════════════════════════
#  (e) bars 長度／值域不變量（含 n_bars=1 邊界，避免除以零）
# ═════════════════════════════════════════════════════════════════════════════

def test_bars_length_and_range_invariant():
    dt_values = [8.0, FRAME_MS, 33.3, 50.0, 0.0]
    rms_values = [0.0, 0.02, 0.5, 0.86, 0.99, 1.5, -0.3]   # 含超界輸入

    for n in (1, 3, 46, 100):
        eng = WaveformEngine(n_bars=n, seed=7.0)
        for k in range(30):
            eng.update(rms_values[k % len(rms_values)], dt_values[k % len(dt_values)])
            bars, peaks = eng.bars, eng.peaks
            assert len(bars) == n
            assert len(peaks) == n
            assert all(0.0 <= b <= 1.0 for b in bars)
            assert all(0.0 <= p <= 1.0 for p in peaks)


# ═════════════════════════════════════════════════════════════════════════════
#  (f) 決定性：同 seed 同輸入序列 → 同輸出
# ═════════════════════════════════════════════════════════════════════════════

def test_deterministic_with_same_seed():
    seq = [
        (0.0, FRAME_MS), (0.3, FRAME_MS), (0.9, 33.3),
        (0.99, FRAME_MS), (0.0, 50.0),
    ] * 5

    eng1 = WaveformEngine(n_bars=20, seed=42.0)
    eng2 = WaveformEngine(n_bars=20, seed=42.0)
    for rms, dt in seq:
        eng1.update(rms, dt)
        eng2.update(rms, dt)

    assert eng1.bars == eng2.bars
    assert eng1.peaks == eng2.peaks
    assert eng1.clipping == eng2.clipping


# ═════════════════════════════════════════════════════════════════════════════
#  reset()：歸零狀態，phase／n_bars 不變
# ═════════════════════════════════════════════════════════════════════════════

def test_reset_returns_to_initial_state():
    eng = WaveformEngine(n_bars=8, seed=9.0)

    for _ in range(20):
        eng.update(0.9, FRAME_MS)
    assert any(b > 0 for b in eng.bars)

    eng.reset()
    assert eng.bars == [0.0] * 8
    assert eng.peaks == [0.0] * 8
    assert eng.clipping is False


# ─────────────────────────────────────────────────────────────────────────
# 能量色溫斜坡（tokens.energy_color / Aperture D2）
#   註：這批測試原由色溫 agent 撰寫，但與波形引擎 agent 平行寫同一個檔案時
#   被覆蓋掉，由總管依原始清單補回。
#   斜坡語意：深色主題 灰→青→冰藍→白熾（越亮＝越大聲）；
#            淺色主題 灰→青→深墨（白底上「越深」才讀得出越大聲）。
# ─────────────────────────────────────────────────────────────────────────

import tokens


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _luminance(rgb):
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_energy_ramp_dark_endpoints():
    """深色斜坡端點：石板灰 (63,74,85) → 白熾 (255,255,255)。"""
    assert tokens._energy_ramp_at(tokens.ENERGY_RAMP_DARK, 0.0) == (63, 74, 85)
    assert tokens._energy_ramp_at(tokens.ENERGY_RAMP_DARK, 1.0) == (255, 255, 255)


def test_energy_ramp_light_endpoints():
    """淺色斜坡端點：淺灰 (148,163,184) → 深墨 (6,42,54)。"""
    assert tokens._energy_ramp_at(tokens.ENERGY_RAMP_LIGHT, 0.0) == (148, 163, 184)
    assert tokens._energy_ramp_at(tokens.ENERGY_RAMP_LIGHT, 1.0) == (6, 42, 54)


def test_energy_color_returns_hex_format():
    """energy_color 一律回 #RRGGBB。"""
    import re
    for v in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", tokens.energy_color(v)), v


def test_energy_color_endpoints_match_active_theme():
    """energy_color 的端點要跟「import 時鎖定的 theme」那條斜坡一致。"""
    stops = (tokens.ENERGY_RAMP_LIGHT if tokens._THEME == "light"
             else tokens.ENERGY_RAMP_DARK)
    assert _hex_to_rgb(tokens.energy_color(0.0)) == stops[0][1]
    assert _hex_to_rgb(tokens.energy_color(1.0)) == stops[-1][1]


def test_energy_ramp_dark_luminance_monotonic_increasing():
    """深色主題：音量越大 → 亮度單調遞增（灰→白熾）。"""
    lums = [_luminance(tokens._energy_ramp_at(tokens.ENERGY_RAMP_DARK, i / 40))
            for i in range(41)]
    for a, b in zip(lums, lums[1:]):
        assert b >= a - 1e-6, f"深色斜坡亮度不該下降：{a} → {b}"
    assert lums[-1] > lums[0] * 2


def test_energy_ramp_light_luminance_monotonic_decreasing():
    """淺色主題：音量越大 → 亮度單調遞減（墨水濃度、白底上越深＝越大聲）。"""
    lums = [_luminance(tokens._energy_ramp_at(tokens.ENERGY_RAMP_LIGHT, i / 40))
            for i in range(41)]
    for a, b in zip(lums, lums[1:]):
        assert b <= a + 1e-6, f"淺色斜坡亮度不該上升：{a} → {b}"
    assert lums[-1] < lums[0] / 2


def test_energy_ramp_clamp_below_zero():
    """v < 0 要 clamp 到起點、不可爆掉。"""
    for stops in (tokens.ENERGY_RAMP_DARK, tokens.ENERGY_RAMP_LIGHT):
        assert tokens._energy_ramp_at(stops, -5.0) == stops[0][1]
    assert _hex_to_rgb(tokens.energy_color(-5.0)) == _hex_to_rgb(tokens.energy_color(0.0))


def test_energy_ramp_clamp_above_one():
    """v > 1 要 clamp 到終點、不可爆掉。"""
    for stops in (tokens.ENERGY_RAMP_DARK, tokens.ENERGY_RAMP_LIGHT):
        assert tokens._energy_ramp_at(stops, 9.9) == stops[-1][1]
    assert _hex_to_rgb(tokens.energy_color(9.9)) == _hex_to_rgb(tokens.energy_color(1.0))


def test_energy_lut_size_is_64():
    """LUT 必須是 64 階（每幀 46 bar × 20fps，不能每次做浮點內插搜尋）。"""
    assert tokens._ENERGY_LUT_SIZE == 64
    assert len(tokens._ENERGY_LUT) == 64


def test_energy_lut_matches_direct_interpolation_within_tolerance():
    """LUT 查表 + 相鄰內插的結果，與直接內插的誤差要 < 2/255。"""
    stops = (tokens.ENERGY_RAMP_LIGHT if tokens._THEME == "light"
             else tokens.ENERGY_RAMP_DARK)
    worst = 0
    for i in range(1001):
        v = i / 1000
        direct = tokens._energy_ramp_at(stops, v)
        via_lut = _hex_to_rgb(tokens.energy_color(v))
        worst = max(worst, max(abs(a - b) for a, b in zip(direct, via_lut)))
    assert worst < 2, f"LUT 與直接內插誤差過大：{worst}/255"
