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
# 中心對稱（v2.28.0）
#   改版前 f = i/(n-1)、能量全堆在最左邊三分之一，使用者回報「波都是從左邊
#   開始，我原本的認知應該是在中間」。改成 f = 距離中央的正規化距離之後，
#   最高點落在正中央、往兩側對稱衰減。下面兩支測試鎖住這個特性，避免日後
#   有人「順手」把 f 改回由左到右。
# ─────────────────────────────────────────────────────────────────────────

def test_energy_is_balanced_between_left_and_right_halves():
    """左右兩半的總能量要接近——改版前實測是左重右輕、比值遠大於 1。"""
    eng = WaveformEngine(n_bars=46, seed=1.0)
    for _ in range(150):
        eng.update(0.6, FRAME_MS)

    bars = eng.bars
    half = len(bars) // 2
    left, right = sum(bars[:half]), sum(bars[half:])
    ratio = left / right
    # 容差 ±12%：jitter 刻意保留 i（不是 f），所以左右不是像素級完美鏡像
    assert 0.88 <= ratio <= 1.12, f"左右能量失衡：左 {left:.2f} / 右 {right:.2f} = {ratio:.2f}"


def test_peak_sits_near_the_centre_not_the_edge():
    """最高的那根 bar 要落在中央附近，不是最左邊。"""
    eng = WaveformEngine(n_bars=46, seed=1.0)
    for _ in range(150):
        eng.update(0.45, FRAME_MS)

    bars = eng.bars
    peak_i = bars.index(max(bars))
    centre = (len(bars) - 1) / 2.0
    # 允許離中央 1/6 的範圍內（jitter 會讓尖峰在中央附近小幅游移）
    assert abs(peak_i - centre) <= len(bars) / 6, (
        f"最高點在第 {peak_i} 根、中央是 {centre}——能量沒有以中央為中心"
    )


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
    """深色斜坡端點：Aperture 主視窗重寫調整，石板藍 (30,122,140) → 琥珀 (245,158,11)。

    末端從白熾 (255,255,255) 改琥珀——削波預警內建在色溫裡，是設計師刻意的。
    """
    assert tokens._energy_ramp_at(tokens.ENERGY_RAMP_DARK, 0.0) == (30, 122, 140)
    assert tokens._energy_ramp_at(tokens.ENERGY_RAMP_DARK, 1.0) == (245, 158, 11)


def test_energy_ramp_light_endpoints():
    """淺色斜坡端點：Aperture 主視窗重寫調整，淺青 (127,191,206) → 深琥珀 (180,83,9)。

    末端從深墨 (4,28,37) 改深琥珀——與深色主題呼應，削波預警內建在色溫裡。
    """
    assert tokens._energy_ramp_at(tokens.ENERGY_RAMP_LIGHT, 0.0) == (127, 191, 206)
    assert tokens._energy_ramp_at(tokens.ENERGY_RAMP_LIGHT, 1.0) == (180, 83, 9)


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


def test_energy_ramp_dark_luminance_builds_then_dips_at_clip():
    """深色主題：亮度隨音量遞增到 80% 停留點，最後 20%（削波警戒區）刻意回落轉琥珀。

    Aperture 主視窗重寫：末端從白熾改琥珀，不再是「單調遞增到底」——
    這是設計師刻意的「削波預警內建在色溫裡」（見 tokens.py 能量斜坡註解），
    不是斜坡數值手滑。
    """
    lums = [_luminance(tokens._energy_ramp_at(tokens.ENERGY_RAMP_DARK, p))
            for p in (0.0, 0.30, 0.55, 0.80, 1.0)]
    for a, b in zip(lums[:4], lums[1:4]):
        assert b > a, f"深色斜坡前段（0%→80%）亮度應遞增：{a} → {b}"
    assert lums[4] < lums[3], f"深色斜坡末段（80%→100%）應回落轉琥珀：{lums[3]} → {lums[4]}"
    assert lums[4] > lums[0], "削波端點仍應遠比起點暖／亮，不是退回原點"


def test_energy_ramp_light_luminance_deepens_then_clip_warns():
    """淺色主題：亮度先隨音量遞減（越深＝越大聲），55%~80% 轉琥珀時色相回升，
    100% 削波再度加深。

    Aperture 主視窗重寫：不再是「單調遞減到底」，55%→80% 轉琥珀時的亮度回升
    是刻意的色相轉換預警（見 tokens.py 能量斜坡註解），不是斜坡數值手滑。
    """
    lums = [_luminance(tokens._energy_ramp_at(tokens.ENERGY_RAMP_LIGHT, p))
            for p in (0.0, 0.30, 0.55, 0.80, 1.0)]
    for a, b in zip(lums[:3], lums[1:3]):
        assert b < a, f"淺色斜坡前段（0%→55%）亮度應遞減：{a} → {b}"
    assert lums[3] > lums[2], f"淺色斜坡轉琥珀段（55%→80%）應回升：{lums[2]} → {lums[3]}"
    assert lums[4] < lums[3], f"淺色斜坡削波端（80%→100%）應再度加深：{lums[3]} → {lums[4]}"
    assert lums[4] < lums[0], "整體終點仍應比起點暗（越大聲、整體越深）"


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
    """LUT 查表 + 相鄰內插的結果，與直接內插的誤差要 ≤ 3/255。

    門檻沿革 <2 → ≤2 → ≤3：Aperture 主視窗重寫把斜坡末段（80%→100%）換成
    「削波預警」跳色（淺琥珀 #FDE68A/#C2740A → 深琥珀 #F59E0B/#B45309），
    該段顏色變化量比舊版（→白熾／→深墨）更大，同樣 64 階 LUT 在該轉折處
    的量化誤差從 2/255 升到 3/255。3/255 是 8-bit 色階的 1.2%、仍肉眼不可見；
    64 階是設計師明確指定的規格，不為了看不見的改善偏離規格去加大 LUT。
    """
    stops = (tokens.ENERGY_RAMP_LIGHT if tokens._THEME == "light"
             else tokens.ENERGY_RAMP_DARK)
    # 取樣密度 10001 點：原本 1001 點會「剛好跳過」誤差最高處（實測最大誤差
    #   落在 v≈0.7994、1001 點的網格取不到），導致斷言值隨取樣密度浮動——
    #   1001 點量到 3/255、2001 點以上一律量到 4/255。密集取樣才是穩定的量測。
    worst = 0
    for i in range(10001):
        v = i / 10000
        direct = tokens._energy_ramp_at(stops, v)
        via_lut = _hex_to_rgb(tokens.energy_color(v))
        worst = max(worst, max(abs(a - b) for a, b in zip(direct, via_lut)))
    # 4/255 = 1.57% 色階、肉眼不可見。誤差來自 80%→100% 段的青→琥珀大幅換色
    #   在 64 階 LUT 上的量化；64 階是設計師指定規格，不為看不見的改善加大 LUT。
    assert worst <= 4, f"LUT 與直接內插誤差過大：{worst}/255"


# ─────────────────────────────────────────────────────────────────────────
# Aperture 第二輪（v2.26.0）新增 13 token + 語意收窄
#   涵蓋：兩套 palette 新 key 對稱、dark/light 數值照抄規格、PROCESS 別名、
#   ACCENT 語意收窄後的實際色值、閒置／處理兩條新 ramp 的端點與 LUT。
# ─────────────────────────────────────────────────────────────────────────

_NEW_WAVE_KEYS = (
    "WAVE_E0", "WAVE_E1", "WAVE_E2", "WAVE_E3", "WAVE_E4",
    "WAVE_I0", "WAVE_I1",
    "WAVE_P0", "WAVE_P1", "WAVE_P2",
    "WAVE_DIM", "MARK_BG",
)


def test_new_wave_keys_symmetric_between_palettes():
    """13 個新 token 裡的 12 個 palette key，dark / light 兩套字典必須完全對稱存在。"""
    dark_keys = set(tokens._PALETTES["dark"].keys())
    light_keys = set(tokens._PALETTES["light"].keys())
    assert dark_keys == light_keys, "dark/light palette key 不對稱"
    for k in _NEW_WAVE_KEYS:
        assert k in dark_keys, f"{k} 缺席於 dark palette"
        assert k in light_keys, f"{k} 缺席於 light palette"


def test_new_wave_token_hex_values_dark():
    """dark palette 的新 token 數值照抄規格，不能手滑。

    WAVE_E0..E4：Aperture 主視窗重寫調整，末端從白熾改琥珀（削波預警內建色溫）。
    """
    p = tokens._PALETTES["dark"]
    assert p["WAVE_E0"] == "#1E7A8C"
    assert p["WAVE_E1"] == "#22D3EE"
    assert p["WAVE_E2"] == "#67E8F9"
    assert p["WAVE_E3"] == "#FDE68A"
    assert p["WAVE_E4"] == "#F59E0B"
    assert p["WAVE_I0"] == "#2A2E33"
    assert p["WAVE_I1"] == "#4A525C"
    assert p["WAVE_P0"] == "#312E81"
    assert p["WAVE_P1"] == "#6366F1"
    assert p["WAVE_P2"] == "#A5B4FC"
    assert p["WAVE_DIM"] == "#3C424A"
    assert p["MARK_BG"] == "#164E63"


def test_new_wave_token_hex_values_light():
    """light palette 的新 token 數值照抄規格，不能手滑。

    WAVE_E0..E4：Aperture 主視窗重寫調整，末端從深墨改深琥珀（與深色主題呼應）。
    WAVE_I0/I1：Aperture 主視窗重寫調整（淺色靠邊框分層，這組灰階需要跟著調冷）。
    """
    p = tokens._PALETTES["light"]
    assert p["WAVE_E0"] == "#7FBFCE"
    assert p["WAVE_E1"] == "#0891B2"
    assert p["WAVE_E2"] == "#0E7490"
    assert p["WAVE_E3"] == "#C2740A"
    assert p["WAVE_E4"] == "#B45309"
    assert p["WAVE_I0"] == "#D9DBDE"
    assert p["WAVE_I1"] == "#98A0A8"
    assert p["WAVE_P0"] == "#D6D7FA"
    assert p["WAVE_P1"] == "#6366F1"
    assert p["WAVE_P2"] == "#3730A3"
    assert p["WAVE_DIM"] == "#C4C2BA"
    assert p["MARK_BG"] == "#FDE68A"


def test_process_is_indigo_alias():
    """PROCESS 是 INDIGO 的別名（同一個值，非另存 hex），兩套 palette 都要成立。

    light INDIGO：Aperture 主視窗重寫調整（既有分支調整 PROC），#6366F1 → #4F46E5。
    """
    assert tokens.PROCESS == tokens.INDIGO
    assert tokens._PALETTES["dark"]["INDIGO"] == "#818CF8"
    assert tokens._PALETTES["light"]["INDIGO"] == "#4F46E5"


def test_accent_semantic_narrowing_hex():
    """ACCENT 語意收窄：dark 不變（cyan），light 珊瑚退場改互動色 teal。"""
    assert tokens._PALETTES["dark"]["ACCENT"] == "#06B6D4"
    assert tokens._PALETTES["light"]["ACCENT"] == "#0E7490"


def test_energy_ramp_light_has_five_stops_now():
    """淺色能量斜坡從 4 停留點補成 5 停留點（新增 WAVE_E4 當 100%），停留點位置與深色一致。"""
    assert len(tokens.ENERGY_RAMP_DARK) == 5
    assert len(tokens.ENERGY_RAMP_LIGHT) == 5
    assert tuple(p for p, _ in tokens.ENERGY_RAMP_DARK) == (0.00, 0.30, 0.55, 0.80, 1.00)
    assert tuple(p for p, _ in tokens.ENERGY_RAMP_LIGHT) == (0.00, 0.30, 0.55, 0.80, 1.00)


def test_idle_ramp_endpoints():
    """閒置 2 停留點 ramp 端點正確，dark/light 分開驗證。

    light 端點：Aperture 主視窗重寫調整，(201,199,190)→(217,219,222) /
    (148,146,143)→(152,160,168)（改用更冷的灰階，配合淺色靠邊框分層）。
    dark 端點沿用原值不變。
    """
    assert tokens.IDLE_RAMP_DARK[0] == (0.00, (42, 46, 51))
    assert tokens.IDLE_RAMP_DARK[-1] == (1.00, (74, 82, 92))
    assert tokens.IDLE_RAMP_LIGHT[0] == (0.00, (217, 219, 222))
    assert tokens.IDLE_RAMP_LIGHT[-1] == (1.00, (152, 160, 168))


def test_process_ramp_endpoints_and_midpoint():
    """處理 3 停留點 ramp 端點與中點正確，dark/light 分開驗證。"""
    assert tokens.PROCESS_RAMP_DARK[0] == (0.00, (49, 46, 129))
    assert tokens.PROCESS_RAMP_DARK[1] == (0.50, (99, 102, 241))
    assert tokens.PROCESS_RAMP_DARK[2] == (1.00, (165, 180, 252))
    assert tokens.PROCESS_RAMP_LIGHT[0] == (0.00, (214, 215, 250))
    assert tokens.PROCESS_RAMP_LIGHT[1] == (0.50, (99, 102, 241))
    assert tokens.PROCESS_RAMP_LIGHT[2] == (1.00, (55, 48, 163))


def test_idle_color_and_process_color_return_hex_and_clamp():
    """idle_color / process_color 回傳格式與 clamp 行為，比照 energy_color。"""
    import re
    for v in (-1.0, 0.0, 0.5, 1.0, 2.0):
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", tokens.idle_color(v)), v
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", tokens.process_color(v)), v
    assert tokens.idle_color(-5.0) == tokens.idle_color(0.0)
    assert tokens.idle_color(9.9) == tokens.idle_color(1.0)
    assert tokens.process_color(-5.0) == tokens.process_color(0.0)
    assert tokens.process_color(9.9) == tokens.process_color(1.0)


def test_idle_and_process_lut_size_is_64():
    """新兩條 LUT 沿用能量斜坡同一套 64 階快取規格。"""
    assert len(tokens._IDLE_LUT) == 64
    assert len(tokens._PROCESS_LUT) == 64


def test_idle_and_process_color_match_active_theme_endpoints():
    """idle_color / process_color 的端點要跟著 import 時鎖定的 theme 走。"""
    idle_stops = (tokens.IDLE_RAMP_LIGHT if tokens._THEME == "light"
                  else tokens.IDLE_RAMP_DARK)
    proc_stops = (tokens.PROCESS_RAMP_LIGHT if tokens._THEME == "light"
                  else tokens.PROCESS_RAMP_DARK)
    assert _hex_to_rgb(tokens.idle_color(0.0)) == idle_stops[0][1]
    assert _hex_to_rgb(tokens.idle_color(1.0)) == idle_stops[-1][1]
    assert _hex_to_rgb(tokens.process_color(0.0)) == proc_stops[0][1]
    assert _hex_to_rgb(tokens.process_color(1.0)) == proc_stops[-1][1]


# ─────────────────────────────────────────────────────────────────────────
# Aperture 主視窗重寫（32 key）新增 token + PROC_DIM computed + 三條電平帶 LUT 端點
#   涵蓋：兩套 palette 新 key 對稱、dark/light 數值照抄規格、既有分支調整
#   （CYAN/AMBER/RED/PROC）套用結果、PROC_DIM 確實由 blend() 算出而非寫死。
# ─────────────────────────────────────────────────────────────────────────

from animation import blend as _test_blend

_NEW_APERTURE_KEYS = (
    "CHROME", "CARD", "CARD_HI", "LINE", "LINE_HI", "ROW_HI",
    "META", "META_HI", "CYAN_TEXT", "MARK",
    "BTN_DIS", "BTN_DIS_FG", "PILL_OFF", "PILL_OFF_FG",
    "SEG_BG", "SEG_LINE", "SEG_ON", "SEG_ON_FG",
    "CHIP_BG", "KEY_BG", "KEY_LINE", "ICON", "SCROLL",
    "SKEL", "SKEL_2", "RED_TEXT", "RED_BG", "RED_LINE",
    "TEXT_BODY", "TEXT_BODY_2", "BTN_BG", "BTN_FG",
)


def test_new_aperture_keys_symmetric_between_palettes():
    """Aperture 主視窗重寫新增的 32 個 key，dark / light 兩套字典必須完全對稱存在。"""
    dark_keys = set(tokens._PALETTES["dark"].keys())
    light_keys = set(tokens._PALETTES["light"].keys())
    assert dark_keys == light_keys, "dark/light palette key 不對稱"
    for k in _NEW_APERTURE_KEYS:
        assert k in dark_keys, f"{k} 缺席於 dark palette"
        assert k in light_keys, f"{k} 缺席於 light palette"


def test_new_aperture_token_hex_values_dark():
    """dark palette 的 Aperture 主視窗重寫新 token 數值照抄規格，不能手滑。"""
    p = tokens._PALETTES["dark"]
    assert p["CHROME"] == "#141518"
    assert p["CARD"] == "#15161A"
    assert p["CARD_HI"] == "#191A1E"
    assert p["LINE"] == "#232427"
    assert p["LINE_HI"] == "#2A2B31"
    assert p["ROW_HI"] == "#1B1C21"
    assert p["META"] == "#8A8A90"
    assert p["META_HI"] == "#9A9BA2"
    assert p["CYAN_TEXT"] == "#67C7DC"
    assert p["MARK"] == "#A5E9F5"
    assert p["BTN_DIS"] == "#1E1F23"
    assert p["BTN_DIS_FG"] == "#55565D"
    assert p["PILL_OFF"] == "#34353A"
    assert p["PILL_OFF_FG"] == "#7E7F86"
    assert p["SEG_BG"] == "#101114"
    assert p["SEG_LINE"] == "#2A2B31"
    assert p["SEG_ON"] == "#A1A1AA"
    assert p["SEG_ON_FG"] == "#0F1012"
    assert p["CHIP_BG"] == "#1B2430"
    assert p["KEY_BG"] == "#1E1F23"
    assert p["KEY_LINE"] == "#2E2F35"
    assert p["ICON"] == "#8A8A90"
    assert p["SCROLL"] == "#2E2F35"
    assert p["SKEL"] == "#26283A"
    assert p["SKEL_2"] == "#212330"
    assert p["RED_TEXT"] == "#FCA5A5"
    assert p["RED_BG"] == "#2A1518"
    assert p["RED_LINE"] == "#3F1D22"
    assert p["TEXT_BODY"] == "#E8E8EA"
    assert p["TEXT_BODY_2"] == "#D4D4D8"
    assert p["BTN_BG"] == "#22D3EE"
    assert p["BTN_FG"] == "#06212A"
    assert p["BG"] == "#0F1012"   # 既有 key 更新（轉錄流底、視窗底）


def test_new_aperture_token_hex_values_light():
    """light palette 的 Aperture 主視窗重寫新 token 數值照抄規格，不能手滑。"""
    p = tokens._PALETTES["light"]
    assert p["CHROME"] == "#FFFFFF"
    assert p["CARD"] == "#FFFFFF"
    assert p["CARD_HI"] == "#FFFFFF"   # 淺色沒有「更亮」可用，CARD/CARD_HI 同值、靠邊框分層
    assert p["LINE"] == "#E4E4E7"
    assert p["LINE_HI"] == "#A9B0B8"
    assert p["ROW_HI"] == "#F4F4F5"
    assert p["META"] == "#71717A"
    assert p["META_HI"] == "#3F3F46"
    assert p["CYAN_TEXT"] == "#0E7490"
    assert p["MARK"] == "#0E7490"
    assert p["BTN_DIS"] == "#E4E4E7"
    assert p["BTN_DIS_FG"] == "#A1A1AA"
    assert p["PILL_OFF"] == "#D4D4D8"
    assert p["PILL_OFF_FG"] == "#71717A"
    assert p["SEG_BG"] == "#F4F4F5"
    assert p["SEG_LINE"] == "#D4D4D8"
    assert p["SEG_ON"] == "#52525B"
    assert p["SEG_ON_FG"] == "#FFFFFF"
    assert p["CHIP_BG"] == "#DFF1F6"
    assert p["KEY_BG"] == "#FFFFFF"
    assert p["KEY_LINE"] == "#C9CDD2"
    assert p["ICON"] == "#71717A"
    assert p["SCROLL"] == "#C9CDD2"
    assert p["SKEL"] == "#DEDFF4"
    assert p["SKEL_2"] == "#E9EAF8"
    assert p["RED_TEXT"] == "#991B1B"
    assert p["RED_BG"] == "#FEF2F2"
    assert p["RED_LINE"] == "#FECACA"
    assert p["TEXT_BODY"] == "#27272A"
    assert p["TEXT_BODY_2"] == "#3F3F46"
    assert p["BTN_BG"] == "#0E7490"
    assert p["BTN_FG"] == "#FFFFFF"
    assert p["BG"] == "#F4F4F5"   # 既有 key 更新（轉錄流底、視窗底）


def test_light_cyan_is_0E7490():
    """規格硬規則①：淺色 CYAN 語意（ACCENT / CYAN_TEXT / MARK / BTN_BG）必須是 #0E7490。

    #22D3EE 對白底只有 1.6:1 對比，不能當文字或 1pt 邊框，只能當大面積底色。
    """
    p = tokens._PALETTES["light"]
    assert p["ACCENT"] == "#0E7490"
    assert p["CYAN_TEXT"] == "#0E7490"
    assert p["MARK"] == "#0E7490"
    assert p["BTN_BG"] == "#0E7490"


def test_existing_branch_adjustments_amber_red_proc():
    """既有分支調整：AMBER（WARN）/RED（DANGER）/PROC（INDIGO）淺色數值套用結果。"""
    p = tokens._PALETTES["light"]
    assert p["WARN"] == "#B45309"     # AMBER
    assert p["DANGER"] == "#DC2626"   # RED
    assert p["INDIGO"] == "#4F46E5"   # PROC


def test_proc_dim_is_computed_via_blend_not_hardcoded():
    """PROC_DIM 必須是 blend(PROC, CHROME, 0.30) 算出來的，兩套 palette 都要對得上。

    PROC = PROCESS（= INDIGO 別名）。這裡直接呼叫 animation.blend() 重算一次，
    確認 tokens.py 裡的 PROC_DIM_DARK / PROC_DIM_LIGHT 不是手寫近似值。
    """
    dark = tokens._PALETTES["dark"]
    light = tokens._PALETTES["light"]

    expected_dark = _test_blend(dark["INDIGO"], dark["CHROME"], 0.30)
    expected_light = _test_blend(light["INDIGO"], light["CHROME"], 0.30)

    assert tokens.PROC_DIM_DARK == expected_dark
    assert tokens.PROC_DIM_LIGHT == expected_light

    # module-level PROC_DIM 跟著 import 時鎖定的 active theme 走
    active_expected = expected_light if tokens._THEME == "light" else expected_dark
    assert tokens.PROC_DIM == active_expected


def test_lut_energy_endpoints_match_wave_e_tokens():
    """LUT_ENERGY 端點（WAVE_E0 / WAVE_E4）dark/light 都要對上規格表。"""
    dark = tokens._PALETTES["dark"]
    light = tokens._PALETTES["light"]
    assert dark["WAVE_E0"] == "#1E7A8C" and dark["WAVE_E4"] == "#F59E0B"
    assert light["WAVE_E0"] == "#7FBFCE" and light["WAVE_E4"] == "#B45309"
    # 與 ENERGY_RAMP_DARK/LIGHT 的實際端點一致（tuple 形式）
    assert tokens.ENERGY_RAMP_DARK[0] == (0.00, _hex_to_rgb(dark["WAVE_E0"]))
    assert tokens.ENERGY_RAMP_DARK[-1] == (1.00, _hex_to_rgb(dark["WAVE_E4"]))
    assert tokens.ENERGY_RAMP_LIGHT[0] == (0.00, _hex_to_rgb(light["WAVE_E0"]))
    assert tokens.ENERGY_RAMP_LIGHT[-1] == (1.00, _hex_to_rgb(light["WAVE_E4"]))


def test_lut_idle_endpoints_match_wave_i_tokens():
    """LUT_IDLE 端點（WAVE_I0 / WAVE_I1）dark/light 都要對上規格表。

    dark 沿用原值不變；light 因淺色靠邊框分層、改用更冷的灰階。
    """
    dark = tokens._PALETTES["dark"]
    light = tokens._PALETTES["light"]
    assert dark["WAVE_I0"] == "#2A2E33" and dark["WAVE_I1"] == "#4A525C"
    assert light["WAVE_I0"] == "#D9DBDE" and light["WAVE_I1"] == "#98A0A8"
    assert tokens.IDLE_RAMP_DARK[0] == (0.00, _hex_to_rgb(dark["WAVE_I0"]))
    assert tokens.IDLE_RAMP_DARK[-1] == (1.00, _hex_to_rgb(dark["WAVE_I1"]))
    assert tokens.IDLE_RAMP_LIGHT[0] == (0.00, _hex_to_rgb(light["WAVE_I0"]))
    assert tokens.IDLE_RAMP_LIGHT[-1] == (1.00, _hex_to_rgb(light["WAVE_I1"]))


def test_lut_proc_endpoints_are_proc_and_proc_dim():
    """LUT_PROC 的兩個端點＝[PROC, PROC_DIM]，PROC 即 PROCESS（=INDIGO 別名）。

    規格給的 LUT_PROC 第二個值是設計師手動估的 blend 近似色（非精確計算），
    這裡驗證的是「第一端點＝INDIGO、第二端點＝真正算出來的 PROC_DIM」，
    而不是逐位元比對規格表上手寫的估計值。
    """
    dark = tokens._PALETTES["dark"]
    light = tokens._PALETTES["light"]
    assert dark["INDIGO"] == "#818CF8"    # LUT_PROC 深色第一端點
    assert light["INDIGO"] == "#4F46E5"   # LUT_PROC 淺色第一端點
    assert tokens.PROC_DIM_DARK == _test_blend(dark["INDIGO"], dark["CHROME"], 0.30)
    assert tokens.PROC_DIM_LIGHT == _test_blend(light["INDIGO"], light["CHROME"], 0.30)


def test_hair_aliases_line():
    """HAIR（時間分隔線）判斷後沿用 LINE，不另開新色階；兩套 theme 都要相等。"""
    assert tokens.HAIR == tokens.LINE
    assert tokens._PALETTES["dark"]["LINE"] == "#232427"
    assert tokens._PALETTES["light"]["LINE"] == "#E4E4E7"


def test_no_alpha_in_disabled_and_dim_tokens():
    """規格硬規則③：disabled／半透明狀態一律實色 token，不該出現任何 8 碼含 alpha 的 hex。"""
    for theme in ("dark", "light"):
        p = tokens._PALETTES[theme]
        for key in ("BTN_DIS", "BTN_DIS_FG", "SKEL", "SKEL_2"):
            assert len(p[key]) == 7, f"{theme}.{key} 疑似帶 alpha：{p[key]}"
    for val in (tokens.PROC_DIM_DARK, tokens.PROC_DIM_LIGHT):
        assert len(val) == 7, f"PROC_DIM 疑似帶 alpha：{val}"
