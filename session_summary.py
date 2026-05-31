"""Session 統計聚合器。

每筆 audit_log.write_transcribe() 會呼叫 record_transcribe()；
App 關閉時呼叫 emit_summary() 印出總結。

執行緒安全：聚合器有 internal lock。

注意：本模組**不要** import audit_log（避免 cycle）。
audit_log → session_summary 是單向相依。
"""
from __future__ import annotations

import time
import threading
from datetime import datetime
from typing import Any

from logger import get_logger, log_error

log = get_logger("session_summary")

_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# 內部狀態 — module-level dict（用 reset() 重置）
# ─────────────────────────────────────────────────────────────────────────────

def _fresh_state() -> dict[str, Any]:
    return {
        "session_start_ts":  time.monotonic(),
        "session_start_wall": datetime.now(),  # 用於 emit summary 顯示
        "transcribe_count":  0,
        "backend_counts":    {},   # {"qwen3-asr": 82, "large-v3-turbo": 5}
        "rtf_list":          [],   # for p50/p95/p99（user-perceived RTF = elapsed_s / duration）
        "inference_rtf_list": [],  # v2.20.1：真實模型 RTF（breakdown.inference_ms / duration）
        "elapsed_list":      [],   # 轉錄秒數（不算 polish）
        "gates_fired":       {},   # {"duration_short": 3, "rms_silent": 12, ...}
        "hallucinations":    {     # 三種反幻覺機制各記一個 counter
            "dict_dump":   0,
            "blacklist":   0,
            "dedupe":      0,
            "segment_hallucination": 0,  # transcriber 內 segment-level 過濾
        },
        "corrections_total": 0,
        "polish_enabled_count":  0,
        "polish_disabled_count": 0,
        "polish_latencies":  [],
        "paste_latencies":   [],
        "errors":            0,
    }


_state: dict[str, Any] = _fresh_state()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def record_transcribe(entry: dict) -> None:
    """從 audit_log.write_transcribe(entry) 內部呼叫。讀 entry 各欄位累加統計。

    Silent fail — 統計失敗不能影響主流程。
    """
    try:
        with _lock:
            _state["transcribe_count"] += 1

            # backend 計數
            backend = entry.get("backend") or "unknown"
            _state["backend_counts"][backend] = _state["backend_counts"].get(backend, 0) + 1

            # RTF / elapsed
            rtf = entry.get("rtf")
            if isinstance(rtf, (int, float)):
                _state["rtf_list"].append(float(rtf))
            elapsed = entry.get("elapsed_s")
            if isinstance(elapsed, (int, float)):
                _state["elapsed_list"].append(float(elapsed))
            # v2.20.1：真實推論 RTF（剔除 prep + postprocess）
            inf_rtf = entry.get("inference_rtf")
            if isinstance(inf_rtf, (int, float)) and inf_rtf > 0:
                _state["inference_rtf_list"].append(float(inf_rtf))

            # Gates fired（True 才算 fire；is_warmup 也算記一筆）
            gates = entry.get("gates") or {}
            for k, v in gates.items():
                if bool(v):
                    _state["gates_fired"][k] = _state["gates_fired"].get(k, 0) + 1

            # Hallucinations / post-filter
            post = entry.get("post_filters") or {}
            if post.get("dict_dump_detected"):
                _state["hallucinations"]["dict_dump"] += 1
            if post.get("blacklist_hit"):
                _state["hallucinations"]["blacklist"] += 1
            if post.get("dedupe_hit"):
                _state["hallucinations"]["dedupe"] += 1
            seg_removed = post.get("hallucination_segments_removed")
            if isinstance(seg_removed, (int, float)) and seg_removed > 0:
                _state["hallucinations"]["segment_hallucination"] += int(seg_removed)

            # Corrections
            corr = post.get("corrections_count")
            if isinstance(corr, (int, float)):
                _state["corrections_total"] += int(corr)

            # Errors
            if entry.get("error"):
                _state["errors"] += 1
    except Exception:
        try:
            log_error("session_summary_record_failed")
        except Exception:
            pass


def record_polish(elapsed_s: float, enabled: bool) -> None:
    """記一筆 polish 事件（enabled=True 代表使用者真的用了 Ollama/Vertex polish）。"""
    try:
        with _lock:
            if enabled:
                _state["polish_enabled_count"] += 1
                if isinstance(elapsed_s, (int, float)) and elapsed_s >= 0:
                    _state["polish_latencies"].append(float(elapsed_s))
            else:
                _state["polish_disabled_count"] += 1
    except Exception:
        try:
            log_error("session_summary_polish_failed")
        except Exception:
            pass


def record_paste_latency(latency_s: float) -> None:
    """hotkey release → paste 完成的總延遲（單位 s）。"""
    try:
        with _lock:
            if isinstance(latency_s, (int, float)) and latency_s >= 0:
                _state["paste_latencies"].append(float(latency_s))
    except Exception:
        try:
            log_error("session_summary_paste_failed")
        except Exception:
            pass


def record_pipeline_timing(durations: dict) -> None:
    """v2.20.3 N3：每條 pipeline 各段時間（ms）。

    durations dict 形如：
        {"hotkey_release_ms": 0.0, "transcribe_start_ms": 45.0,
         "transcribe_done_ms": 945.0, "paste_complete_ms": 1010.0}

    內部用 dict-of-list 累積、emit_summary 時取中位數。Silent fail。
    """
    try:
        with _lock:
            if not isinstance(durations, dict) or not durations:
                return
            bucket = _state.setdefault("pipeline_timing", {})
            for k, v in durations.items():
                if isinstance(v, (int, float)):
                    bucket.setdefault(k, []).append(float(v))
    except Exception:
        try:
            log_error("session_summary_pipeline_timing_failed")
        except Exception:
            pass


def emit_summary() -> str:
    """產生多行 summary 字串。也寫一筆 type=session 進 audit JSONL。

    格式範例：
        SESSION ended 2026-05-26T15:46:07 (duration=4h32m, transcriptions=87)
          ├─ backend: qwen3-asr (82) / large-v3-turbo (5)
          ├─ avg RTF: 0.34 (p95=0.91, p99=2.10)
          ├─ gates fired: duration_short=3, rms_silent=12, total_short_circuited=15 (17%)
          ├─ hallucinations blocked: dict_dump=2, blacklist=1, dedupe=4
          ├─ corrections applied: 23 (avg 0.26/transcription)
          ├─ polish: enabled=12, disabled=75, avg latency=4.1s
          └─ avg user-perceived latency (hotkey release→paste): 1.6s
    """
    try:
        with _lock:
            snap = _snapshot_locked()
    except Exception:
        # 取 snapshot 失敗也要回個非空字串
        try:
            log_error("session_summary_snapshot_failed")
        except Exception:
            pass
        return "SESSION summary unavailable (snapshot failed)"

    lines = _format_summary(snap)
    text = "\n".join(lines)

    # ── 同步寫一筆 audit JSONL（type=session）─────────────────────────────
    # 延後 import 避免 cycle
    try:
        import audit_log  # noqa: PLC0415
        audit_log.write_event(
            "session",
            None,
            duration_s=snap["duration_s"],
            transcribe_count=snap["transcribe_count"],
            backend_counts=snap["backend_counts"],
            rtf_p50=snap["rtf_p50"],
            rtf_p95=snap["rtf_p95"],
            rtf_p99=snap["rtf_p99"],
            # v2.20.1：真實推論 RTF（剔除 startup overhead）
            inference_rtf_median=snap["inference_rtf_median"],
            inference_rtf_count=snap["inference_rtf_count"],
            gates_fired=snap["gates_fired"],
            hallucinations=snap["hallucinations"],
            corrections_total=snap["corrections_total"],
            polish_enabled_count=snap["polish_enabled_count"],
            polish_disabled_count=snap["polish_disabled_count"],
            polish_avg_latency_s=snap["polish_avg_latency"],
            paste_avg_latency_s=snap["paste_avg_latency"],
            errors=snap["errors"],
        )
    except Exception:
        try:
            log_error("session_summary_emit_audit_failed")
        except Exception:
            pass

    # 也順手寫進 log（方便 grep）
    try:
        for line in lines:
            log.info(line)
    except Exception:
        pass

    return text


def reset() -> None:
    """清空所有計數（測試用）。"""
    global _state
    with _lock:
        _state = _fresh_state()


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _percentile(xs: list[float], p: float) -> float:
    """簡單 percentile（不依賴 numpy）。p 為 0-100。"""
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    # 線性內插
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _snapshot_locked() -> dict[str, Any]:
    """在 lock 之下取一份穩定的 snapshot（避免 race）。"""
    duration_s = max(0.0, time.monotonic() - _state["session_start_ts"])
    rtfs = list(_state["rtf_list"])
    inf_rtfs = list(_state["inference_rtf_list"])
    polish_lat = list(_state["polish_latencies"])
    paste_lat = list(_state["paste_latencies"])
    gates_fired = dict(_state["gates_fired"])
    backend_counts = dict(_state["backend_counts"])
    hallucinations = dict(_state["hallucinations"])
    # v2.20.3 N3：深拷貝 pipeline timing buckets（list 重新建構避免外部 mutate）
    pt_src = _state.get("pipeline_timing") or {}
    pipeline_timing = {k: list(v) for k, v in pt_src.items()}

    total_short_circuited = sum(
        c for k, c in gates_fired.items() if k in ("duration_short", "rms_silent", "is_warmup")
    )
    tx_count = _state["transcribe_count"]
    short_circuit_ratio = (total_short_circuited / tx_count) if tx_count else 0.0

    return {
        "session_start_wall": _state["session_start_wall"],
        "duration_s":          duration_s,
        "transcribe_count":    tx_count,
        "backend_counts":      backend_counts,
        "rtf_avg":             _avg(rtfs),
        "rtf_p50":             _percentile(rtfs, 50),
        "rtf_p95":             _percentile(rtfs, 95),
        "rtf_p99":             _percentile(rtfs, 99),
        # v2.20.1：真實推論 RTF（剔除 startup overhead）；空 list 時為 0
        "inference_rtf_median": _percentile(inf_rtfs, 50),
        "inference_rtf_count":  len(inf_rtfs),
        "gates_fired":         gates_fired,
        "total_short_circuited": total_short_circuited,
        "short_circuit_ratio": short_circuit_ratio,
        "hallucinations":      hallucinations,
        "corrections_total":   _state["corrections_total"],
        "polish_enabled_count":  _state["polish_enabled_count"],
        "polish_disabled_count": _state["polish_disabled_count"],
        "polish_avg_latency":  _avg(polish_lat),
        "paste_avg_latency":   _avg(paste_lat),
        "errors":              _state["errors"],
        # v2.20.3 N3：pipeline 各 milestone 累積 list
        "pipeline_timing":     pipeline_timing,
    }


def _fmt_duration(s: float) -> str:
    """秒數轉「4h32m」/「12m3s」/「45s」格式。"""
    s = int(s)
    if s >= 3600:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h{m:02d}m"
    if s >= 60:
        m = s // 60
        sec = s % 60
        return f"{m}m{sec:02d}s"
    return f"{s}s"


def _format_summary(snap: dict[str, Any]) -> list[str]:
    """格式化成多行 list[str]。"""
    end_wall = datetime.now().isoformat(timespec="seconds")
    duration = _fmt_duration(snap["duration_s"])
    tx_count = snap["transcribe_count"]

    lines: list[str] = [
        f"SESSION ended {end_wall} (duration={duration}, transcriptions={tx_count})"
    ]

    # Backend 拆解
    backends = snap["backend_counts"]
    if backends:
        parts = [f"{name} ({c})" for name, c in sorted(backends.items(), key=lambda x: -x[1])]
        lines.append(f"  ├─ backend: {' / '.join(parts)}")
    else:
        lines.append("  ├─ backend: (none)")

    # RTF
    if snap["rtf_p50"] or snap["rtf_p95"]:
        lines.append(
            f"  ├─ avg RTF: {snap['rtf_avg']:.2f} "
            f"(p95={snap['rtf_p95']:.2f}, p99={snap['rtf_p99']:.2f})"
        )
    else:
        lines.append("  ├─ avg RTF: (no data)")

    # v2.20.1：真實推論 RTF（剔除 prep + postprocess startup overhead）
    # vs 上一行的 total RTF（user 體感）：這行才是「模型本體有多快」
    if snap["inference_rtf_count"] > 0:
        lines.append(
            f"  ├─ avg inference_rtf: {snap['inference_rtf_median']:.2f} "
            f"(median, vs total RTF {snap['rtf_p50']:.2f})"
        )
    else:
        lines.append("  ├─ avg inference_rtf: (no data)")

    # Gates fired
    gates = snap["gates_fired"]
    if gates:
        gate_parts = [f"{k}={v}" for k, v in sorted(gates.items())]
        ratio_pct = int(round(snap["short_circuit_ratio"] * 100))
        lines.append(
            f"  ├─ gates fired: {', '.join(gate_parts)}, "
            f"total_short_circuited={snap['total_short_circuited']} ({ratio_pct}%)"
        )
    else:
        lines.append("  ├─ gates fired: (none)")

    # Hallucinations
    h = snap["hallucinations"]
    h_parts = [f"{k}={v}" for k, v in h.items() if v > 0]
    if h_parts:
        lines.append(f"  ├─ hallucinations blocked: {', '.join(h_parts)}")
    else:
        lines.append("  ├─ hallucinations blocked: (none)")

    # Corrections
    corr = snap["corrections_total"]
    avg_corr = (corr / tx_count) if tx_count else 0.0
    lines.append(
        f"  ├─ corrections applied: {corr} (avg {avg_corr:.2f}/transcription)"
    )

    # Polish
    lines.append(
        f"  ├─ polish: enabled={snap['polish_enabled_count']}, "
        f"disabled={snap['polish_disabled_count']}, "
        f"avg latency={snap['polish_avg_latency']:.1f}s"
    )

    # Paste latency
    lines.append(
        f"  └─ avg user-perceived latency (hotkey release→paste): "
        f"{snap['paste_avg_latency']:.1f}s"
    )

    # v2.20.3 N3：pipeline timing breakdown（中位數、ms）
    # 用「median per milestone」拆解 user-perceived latency；
    # 預期 milestone：hotkey_release_ms（恆為 0）、transcribe_start_ms、
    # transcribe_done_ms、polish_start_ms、polish_done_ms、
    # paste_start_ms、paste_complete_ms（依路徑而定）
    pt = snap.get("pipeline_timing") or {}
    if pt:
        medians = {k: round(_percentile(v, 50), 1) for k, v in pt.items() if v}
        if medians:
            lines.append(f"  ├─ pipeline timing (median ms): {medians}")

    if snap["errors"]:
        lines.append(f"  (errors during session: {snap['errors']})")

    return lines
