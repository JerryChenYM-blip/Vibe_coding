"""稽核日誌（audit log）— 每次轉錄寫一筆 JSON line。

路徑：~/.whisper_app/transcribe_log.jsonl
可疑音檔備份：~/.whisper_app/suspicious/{ts}_{pid}_{reason}.wav + .json

設計鐵律：
1. 失敗一律 silent — log_error() 通知但絕不 raise（log code 炸主流程是噩夢）
2. 執行緒安全 — 背景 thread 會呼叫 write_transcribe()，用 module-level lock
3. numpy 物件序列化前要轉成 Python 原生（float/int）
4. 寫完轉錄行後通知 session_summary 累加統計
"""
from __future__ import annotations

import json
import os
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

import numpy as np

from logger import get_logger, log_error

log = get_logger("audit")

_LOG_PATH       = Path.home() / ".whisper_app" / "transcribe_log.jsonl"
_SUSPICIOUS_DIR = Path.home() / ".whisper_app" / "suspicious"
_LOCK = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# v2.20.3 N8：config hash — 標記每筆 audit entry 所在的設定 snapshot
# 未來 A/B 分析 jq 一行可 group by：
#   jq -c 'select(.type=="transcribe") | {hash: .config_hash, rtf: .rtf}' transcribe_log.jsonl
# ─────────────────────────────────────────────────────────────────────────────
_config_hash_lock = threading.Lock()
_current_config_hash: str = "init"


def set_config_hash(config_obj) -> str:
    """v2.20.3 N8：算 config 的 8-char SHA-1 hash、設為 current。

    只 hash 「會影響 transcribe behavior」的欄位、其他（theme、history_retention_days）不算
    避免外觀改動造成假 A/B 分組。

    回傳 hash（也設定到 module-level state）。
    """
    import hashlib
    global _current_config_hash
    # 只取會影響轉錄/潤飾/streaming 行為的欄位
    relevant = {
        "model":                 getattr(config_obj, "model", None),
        "language":              getattr(config_obj, "language", None),
        "polish_backend":        getattr(config_obj, "polish_backend", None),
        "ollama_enabled":        getattr(config_obj, "ollama_enabled", None),
        "ollama_model":          getattr(config_obj, "ollama_model", None),
        "silero_vad_enabled":    getattr(config_obj, "silero_vad_enabled", None),
        "silero_vad_threshold":  getattr(config_obj, "silero_vad_threshold", None),
        "pinyin_guard_enabled":  getattr(config_obj, "pinyin_guard_enabled", None),
        "streaming_algo":        getattr(config_obj, "streaming_algo", None),
        "chinese_variant":       getattr(config_obj, "chinese_variant", None),
        "preset_routing_enabled": getattr(config_obj, "preset_routing_enabled", None),
        "dictionary_enabled":    getattr(config_obj, "dictionary_enabled", None),
        "vertex_model":          getattr(config_obj, "vertex_model", None),
    }
    serialized = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha1(serialized.encode("utf-8")).hexdigest()[:8]
    with _config_hash_lock:
        _current_config_hash = h
    return h


def get_config_hash() -> str:
    """v2.20.3 N8：讀 current config hash（給內部用）。"""
    with _config_hash_lock:
        return _current_config_hash


# ─────────────────────────────────────────────────────────────────────────────
# JSON 序列化 helpers — 處理 numpy / Path 等非原生型別
# ─────────────────────────────────────────────────────────────────────────────

def _to_native(obj: Any) -> Any:
    """遞迴把 numpy / Path 等型別轉成 JSON 認得的 Python 原生型別。

    - numpy scalar → Python float/int/bool
    - numpy array  → list
    - Path         → str
    - dict / list / tuple → 遞迴
    - 其他 → 原樣（json.dumps 會自己處理或炸）
    """
    # numpy scalar（np.float32, np.int64, np.bool_ ...）
    if isinstance(obj, np.generic):
        try:
            return obj.item()
        except Exception:
            return float(obj) if isinstance(obj, np.floating) else int(obj)

    # numpy array
    if isinstance(obj, np.ndarray):
        return [_to_native(x) for x in obj.tolist()]

    if isinstance(obj, Path):
        return str(obj)

    if isinstance(obj, dict):
        return {str(k): _to_native(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [_to_native(x) for x in obj]

    return obj


def _ensure_log_dir() -> bool:
    """確保 ~/.whisper_app/ 存在。失敗回 False、silent。"""
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        return True
    except Exception:
        try:
            log_error("audit_dir_create_failed", path=str(_LOG_PATH.parent))
        except Exception:
            pass
        return False


def _now_iso() -> str:
    """ISO 8601 含毫秒、不含時區（與 logger 風格一致）。"""
    return datetime.now().isoformat(timespec="milliseconds")


def _append_jsonl(record: dict) -> bool:
    """Atomic-ish append 一行 JSON 進 _LOG_PATH。失敗 silent 回 False。"""
    if not _ensure_log_dir():
        return False

    try:
        native = _to_native(record)
        line = json.dumps(native, ensure_ascii=False)
    except Exception:
        try:
            log_error("audit_json_serialize_failed", record_type=record.get("type"))
        except Exception:
            pass
        return False

    try:
        with _LOCK:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception:
        try:
            log_error("audit_write_failed", path=str(_LOG_PATH))
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public API — transcribe / event / suspicious
# ─────────────────────────────────────────────────────────────────────────────

def write_transcribe(entry: dict) -> None:
    """Append 一行 transcribe 紀錄 + 通知 session_summary。

    Entry schema 範例見 docstring（不嚴格驗證、缺欄位 silent 處理）。
    失敗 silent fail — 呼叫 log_error("audit_write_failed")、絕不 raise。
    """
    if not isinstance(entry, dict):
        try:
            log_error("audit_write_invalid_entry", got=type(entry).__name__)
        except Exception:
            pass
        return

    # 補上必要欄位（若呼叫端沒帶就帶上）
    rec = dict(entry)
    rec.setdefault("type", "transcribe")
    rec.setdefault("ts", _now_iso())
    # v2.20.3 N8：自動帶 config_hash（呼叫端沒帶才補；呼叫端有蓋值就尊重）
    rec.setdefault("config_hash", get_config_hash())

    _append_jsonl(rec)

    # 通知 session_summary —— 失敗 silent
    try:
        import session_summary  # noqa: PLC0415 — local import 避免 import-time cycle
        session_summary.record_transcribe(rec)
    except Exception:
        try:
            log_error("audit_session_summary_failed")
        except Exception:
            pass


def write_event(event_type: str, pipeline_id: Optional[str], **fields: Any) -> None:
    """寫一行非 transcribe 的事件（hotkey_press / polish_dispatch / paste_done 等）。

    Schema: {"type": event_type, "ts": "...", "pipeline_id": pid, **fields}
    """
    rec: dict[str, Any] = {
        "type": str(event_type),
        "ts":   _now_iso(),
        "pipeline_id": pipeline_id,
        "config_hash": get_config_hash(),  # v2.20.3 N8：自動帶 config snapshot 標記
    }
    # 把 fields 平鋪進 record（不嵌套，方便 jq 撈）
    # 注意：若 fields 含 config_hash、會蓋掉上面預設值（呼叫端有意覆寫就尊重）
    for k, v in fields.items():
        rec[k] = v

    _append_jsonl(rec)


def save_suspicious_audio(
    audio: "np.ndarray",
    sample_rate: int,
    pipeline_id: str,
    reason: str,
    metadata: dict,
    enabled: bool,
    max_size_mb: int = 200,
) -> Optional[str]:
    """若 enabled=True，存音檔 + metadata json。回 saved path 或 None。

    自動 rotate：若 _SUSPICIOUS_DIR 總大小 > max_size_mb，從最舊開始砍直到 < 80% max。
    用 scipy.io.wavfile.write 寫（已是專案依賴）；失敗 silent。

    reason 範例: "dict_dump" / "blacklist_hit" / "dedupe_triggered" / "rtf_slow"
    """
    if not enabled:
        return None

    if audio is None:
        return None

    # ── 確保目錄存在 ────────────────────────────────────────────────────
    try:
        _SUSPICIOUS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        try:
            log_error("suspicious_dir_create_failed", path=str(_SUSPICIOUS_DIR))
        except Exception:
            pass
        return None

    # ── Rotate：先檢查目錄大小 ─────────────────────────────────────────
    try:
        _rotate_suspicious_if_needed(max_size_mb)
    except Exception:
        # rotate 失敗繼續寫（不阻擋）
        try:
            log_error("suspicious_rotate_failed")
        except Exception:
            pass

    # ── 寫檔 ────────────────────────────────────────────────────────────
    ts_str = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe_reason = _safe_filename_part(reason)
    safe_pid    = _safe_filename_part(pipeline_id or "nopid")
    base = f"{ts_str}_{safe_pid}_{safe_reason}"
    wav_path  = _SUSPICIOUS_DIR / f"{base}.wav"
    json_path = _SUSPICIOUS_DIR / f"{base}.json"

    # 寫 wav — scipy 是專案依賴
    try:
        from scipy.io import wavfile  # type: ignore  # noqa: PLC0415

        # scipy 要 int16 / int32 / float32；float64 會 warn。
        # 確保 contiguous 並轉成 float32（不縮放：呼叫端應該已是 -1..1 範圍）。
        data = np.ascontiguousarray(audio)
        if data.dtype == np.float64:
            data = data.astype(np.float32)
        wavfile.write(str(wav_path), int(sample_rate), data)
    except Exception:
        try:
            log_error("suspicious_wav_write_failed", path=str(wav_path))
        except Exception:
            pass
        # wav 寫失敗就不要寫 json 了
        return None

    # 寫 json metadata
    try:
        meta = dict(metadata) if metadata else {}
        meta.setdefault("ts",          _now_iso())
        meta.setdefault("pipeline_id", pipeline_id)
        meta.setdefault("reason",      reason)
        meta.setdefault("sample_rate", int(sample_rate))
        meta.setdefault("samples",     int(len(audio)) if hasattr(audio, "__len__") else None)
        meta.setdefault("wav_path",    str(wav_path))

        native = _to_native(meta)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(native, f, ensure_ascii=False, indent=2)
    except Exception:
        try:
            log_error("suspicious_json_write_failed", path=str(json_path))
        except Exception:
            pass
        # JSON 失敗不影響 wav，已寫的 wav 留著

    return str(wav_path)


# ─────────────────────────────────────────────────────────────────────────────
# Internal: rotate / sanitize
# ─────────────────────────────────────────────────────────────────────────────

def _safe_filename_part(s: str) -> str:
    """把字串清成安全的檔名片段（只留字母、數字、底線、減號）。"""
    if not s:
        return "x"
    out = []
    for ch in str(s):
        if ch.isalnum() or ch in ("_", "-"):
            out.append(ch)
        else:
            out.append("_")
    cleaned = "".join(out).strip("_")
    return cleaned or "x"


def _rotate_suspicious_if_needed(max_size_mb: int) -> None:
    """若 _SUSPICIOUS_DIR 總大小 > max_size_mb，從最舊開始砍直到 < 80% max。

    每個「樣本」是一對 .wav + .json，依 mtime 由舊到新刪。
    """
    if max_size_mb <= 0:
        return
    if not _SUSPICIOUS_DIR.exists():
        return

    max_bytes      = max_size_mb * 1024 * 1024
    threshold_bytes = int(max_bytes * 0.8)  # 砍到 < 80% max 為止

    # 收集所有檔案 + 總 size
    try:
        files = [p for p in _SUSPICIOUS_DIR.iterdir() if p.is_file()]
    except Exception:
        return

    total = 0
    sizes: dict[Path, int] = {}
    for p in files:
        try:
            sz = p.stat().st_size
        except Exception:
            sz = 0
        sizes[p] = sz
        total += sz

    if total <= max_bytes:
        return  # 還沒爆

    # 把檔案按 mtime 排序（舊在前）
    try:
        files_sorted = sorted(files, key=lambda p: p.stat().st_mtime)
    except Exception:
        files_sorted = files

    # 從最舊砍直到 total < threshold
    for p in files_sorted:
        if total <= threshold_bytes:
            break
        sz = sizes.get(p, 0)
        try:
            p.unlink()
            total -= sz
        except Exception:
            # 砍不掉就跳過
            continue
