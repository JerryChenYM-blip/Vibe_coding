"""
Phase 3.2 歷史紀錄：本地 SQLite 轉錄紀錄儲存。

落地位置：`~/.whisper_app/history.db`

設計要點：
  • 執行緒安全：所有 DB 操作走單一 `threading.Lock`；SQLite 用
    `check_same_thread=False` 允許從 UI 主執行緒與 polish 背景執行緒共用
  • Schema 自動建立：第一次使用即 `CREATE TABLE IF NOT EXISTS ...`；
    欄位擴充採 `ALTER TABLE ADD COLUMN` migration（目前尚無）
  • FTS5 全文索引：`transcriptions_fts` 虛擬表 + 3 個 trigger 保持同步；
    搜尋走 `MATCH` 比對，不 fallback 到 LIKE（v1 版）
  • 寫入失敗只 `log_error`，**絕不拋例外**，避免拖死 UI 主執行緒
  • 所有文字欄位允許 NULL（除 raw_text / model_whisper），讓沒走 Ollama
    的紀錄也能完整保存

對外 API：
  HistoryStore().insert(...)           # 錄音完成後馬上插 raw_text
  HistoryStore().update_polish(id, ..) # 潤飾完成後回頭更新
  HistoryStore().list_recent(limit)    # 歷史 tab 初始清單
  HistoryStore().search(query, limit)  # FTS5 搜尋
  HistoryStore().get(id)               # 重新潤飾取單筆
  HistoryStore().delete(id)            # 刪單筆
  HistoryStore().delete_before(days)   # 保留策略
  HistoryStore().count()               # 總筆數
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from logger import get_logger, log_error

log = get_logger("history")


# ── 常數 ──────────────────────────────────────────────────────────────────────

DB_PATH = Path.home() / ".whisper_app" / "history.db"


# ── 資料類別 ──────────────────────────────────────────────────────────────────

@dataclass
class HistoryEntry:
    """單筆歷史紀錄（對應 transcriptions 表一列）。"""

    id:            int
    timestamp:     int                     # epoch seconds
    duration_s:    float                   # 錄音長度
    raw_text:      str                     # Whisper 原文
    polished_text: Optional[str]           # Ollama 潤飾（None = 沒潤過或潤飾失敗）
    target_app:    Optional[str]           # 前景 app（None = 未偵測到或非 macOS）
    preset_used:   str                     # default / email / chat / translate_en 等
    model_whisper: str                     # e.g. large-v3-turbo
    model_ollama:  Optional[str]           # None = 沒走 Ollama
    language:      Optional[str]           # 偵測或指定的語言代碼

    def summary(self, n: int = 40) -> str:
        """回傳供清單顯示用的摘要（潤飾版優先，截斷 n 字）。"""
        text = self.polished_text or self.raw_text or ""
        text = text.replace("\n", " ").strip()
        return text[:n] + ("…" if len(text) > n else "")

    def has_polish(self) -> bool:
        return bool(self.polished_text)


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA_SCRIPT = """
CREATE TABLE IF NOT EXISTS transcriptions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      INTEGER NOT NULL,
    duration_s     REAL    NOT NULL,
    raw_text       TEXT    NOT NULL,
    polished_text  TEXT,
    target_app     TEXT,
    preset_used    TEXT    NOT NULL DEFAULT 'default',
    model_whisper  TEXT    NOT NULL,
    model_ollama   TEXT,
    language       TEXT
);
CREATE INDEX IF NOT EXISTS idx_timestamp ON transcriptions(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_preset    ON transcriptions(preset_used);

-- FTS5 全文索引虛擬表（external content，與 transcriptions 同步）
-- tokenize=trigram：SQLite 3.34+ 支援，適合 CJK（無空白的中文）
-- 3 字以上查詢走 MATCH；短查詢在 Python 層用 LIKE fallback
CREATE VIRTUAL TABLE IF NOT EXISTS transcriptions_fts
    USING fts5(raw_text, polished_text,
               content='transcriptions', content_rowid='id',
               tokenize='trigram');

-- Trigger：新增／更新／刪除 transcriptions 時同步 FTS 索引
CREATE TRIGGER IF NOT EXISTS transcriptions_ai AFTER INSERT ON transcriptions BEGIN
    INSERT INTO transcriptions_fts(rowid, raw_text, polished_text)
    VALUES (new.id, new.raw_text, COALESCE(new.polished_text, ''));
END;
CREATE TRIGGER IF NOT EXISTS transcriptions_au AFTER UPDATE ON transcriptions BEGIN
    INSERT INTO transcriptions_fts(transcriptions_fts, rowid, raw_text, polished_text)
    VALUES ('delete', old.id, old.raw_text, COALESCE(old.polished_text, ''));
    INSERT INTO transcriptions_fts(rowid, raw_text, polished_text)
    VALUES (new.id, new.raw_text, COALESCE(new.polished_text, ''));
END;
CREATE TRIGGER IF NOT EXISTS transcriptions_ad AFTER DELETE ON transcriptions BEGIN
    INSERT INTO transcriptions_fts(transcriptions_fts, rowid, raw_text, polished_text)
    VALUES ('delete', old.id, old.raw_text, COALESCE(old.polished_text, ''));
END;
"""


# ── HistoryStore ──────────────────────────────────────────────────────────────

class HistoryStore:
    """SQLite 歷史紀錄儲存（執行緒安全）。

    使用方式：
        store = HistoryStore()          # 第一次會自動建表
        rowid = store.insert(...)        # 錄音完成 → 寫入 raw_text
        store.update_polish(rowid, ...)  # 潤飾完成 → 回頭更新
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._lock    = threading.Lock()
        self._ensure_db()

    # ── 初始化 ────────────────────────────────────────────────────────────────

    def _ensure_db(self) -> None:
        """確保 DB 目錄存在並建立 schema（冪等）。"""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA_SCRIPT)
                conn.commit()
            log.debug(f"HISTORY: schema ready at {self._db_path}")
        except Exception:
            log_error("history_schema_init_failed", path=str(self._db_path))

    def _connect(self) -> sqlite3.Connection:
        """建立一條短命連線；每次操作用 with self._connect() 即可。

        `check_same_thread=False` 配合外部 `self._lock` 確保併發安全。
        """
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 寫入 ──────────────────────────────────────────────────────────────────

    def insert(
        self,
        *,
        duration_s:    float,
        raw_text:      str,
        model_whisper: str,
        target_app:    Optional[str] = None,
        preset_used:   str           = "default",
        model_ollama:  Optional[str] = None,
        polished_text: Optional[str] = None,
        language:      Optional[str] = None,
    ) -> Optional[int]:
        """寫入一筆新紀錄；回傳 rowid，失敗回 None。

        通常在 `_on_transcription_done` 時呼叫、只帶 raw_text；
        潤飾完成後呼叫 `update_polish(id, ...)` 補上 polished_text。
        """
        with self._lock:
            try:
                with self._connect() as conn:
                    cur = conn.execute(
                        "INSERT INTO transcriptions "
                        "(timestamp, duration_s, raw_text, polished_text, "
                        " target_app, preset_used, model_whisper, model_ollama, language) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            int(time.time()),
                            float(duration_s),
                            raw_text,
                            polished_text,
                            target_app,
                            preset_used,
                            model_whisper,
                            model_ollama,
                            language,
                        ),
                    )
                    conn.commit()
                    return cur.lastrowid
            except Exception:
                log_error("history_insert_failed", text_len=len(raw_text))
                return None

    def update_polish(
        self,
        id:            int,
        polished_text: str,
        preset_used:   Optional[str] = None,
        model_ollama:  Optional[str] = None,
    ) -> bool:
        """潤飾完成後回頭更新。preset_used / model_ollama 可選擇性覆寫。"""
        with self._lock:
            try:
                with self._connect() as conn:
                    sets = ["polished_text = ?"]
                    vals: list = [polished_text]
                    if preset_used is not None:
                        sets.append("preset_used = ?")
                        vals.append(preset_used)
                    if model_ollama is not None:
                        sets.append("model_ollama = ?")
                        vals.append(model_ollama)
                    vals.append(id)
                    conn.execute(
                        f"UPDATE transcriptions SET {', '.join(sets)} WHERE id = ?",
                        tuple(vals),
                    )
                    conn.commit()
                    return True
            except Exception:
                log_error("history_update_polish_failed", id=id)
                return False

    # ── 讀取 ──────────────────────────────────────────────────────────────────

    def list_recent(self, limit: int = 100) -> list[HistoryEntry]:
        """最新 N 筆（時間倒序）。"""
        with self._lock:
            try:
                with self._connect() as conn:
                    cur = conn.execute(
                        "SELECT * FROM transcriptions "
                        "ORDER BY timestamp DESC LIMIT ?",
                        (limit,),
                    )
                    return [_row_to_entry(r) for r in cur.fetchall()]
            except Exception:
                log_error("history_list_failed")
                return []

    def search(self, query: str, limit: int = 50) -> list[HistoryEntry]:
        """搜尋 raw_text / polished_text。空字串回 list_recent。

        策略：
          • query 長度 ≥ 3：走 FTS5 trigram MATCH（快、語言無關）
          • query 長度 < 3：走 LIKE fallback（trigram 索引需 ≥3 字）
          • 雙引號逃脫，整句以 phrase 比對避免 FTS5 操作符誤解
          • 任何例外降級回空結果，絕不拋
        """
        query = (query or "").strip()
        if not query:
            return self.list_recent(limit)
        # 短查詢（< 3 字，中文常見 2 字詞如「買菜」「明天」）走 LIKE
        if len(query) < 3:
            return self._search_like(query, limit)
        with self._lock:
            try:
                # 用 phrase query 包裹，讓使用者可以任意打中文／空白／標點
                # 不會被 FTS5 當成 AND / OR / NEAR 等操作符
                safe = '"' + query.replace('"', '""') + '"'
                with self._connect() as conn:
                    cur = conn.execute(
                        "SELECT t.* FROM transcriptions t "
                        "JOIN transcriptions_fts f ON t.id = f.rowid "
                        "WHERE transcriptions_fts MATCH ? "
                        "ORDER BY t.timestamp DESC LIMIT ?",
                        (safe, limit),
                    )
                    return [_row_to_entry(r) for r in cur.fetchall()]
            except Exception:
                # FTS 失敗（例如 trigram tokenizer 不可用）→ LIKE 降級
                log_error("history_fts_search_failed", query=query[:40])
                return self._search_like(query, limit)

    def _search_like(self, query: str, limit: int) -> list[HistoryEntry]:
        """LIKE 子字串搜尋（trigram 短查詢 fallback）。

        對幾千筆以下的個人歷史，LIKE 全掃不會有效能問題。
        """
        with self._lock:
            try:
                # LIKE 逃脫：_ 與 % 是 wildcard
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                like_pat = f"%{escaped}%"
                with self._connect() as conn:
                    cur = conn.execute(
                        "SELECT * FROM transcriptions "
                        "WHERE raw_text LIKE ? ESCAPE '\\' "
                        "   OR polished_text LIKE ? ESCAPE '\\' "
                        "ORDER BY timestamp DESC LIMIT ?",
                        (like_pat, like_pat, limit),
                    )
                    return [_row_to_entry(r) for r in cur.fetchall()]
            except Exception:
                log_error("history_like_search_failed", query=query[:40])
                return []

    def get(self, id: int) -> Optional[HistoryEntry]:
        """取單筆（重新潤飾用）。"""
        with self._lock:
            try:
                with self._connect() as conn:
                    cur = conn.execute(
                        "SELECT * FROM transcriptions WHERE id = ?", (id,),
                    )
                    row = cur.fetchone()
                    return _row_to_entry(row) if row else None
            except Exception:
                log_error("history_get_failed", id=id)
                return None

    # ── 刪除 / 保留策略 ───────────────────────────────────────────────────────

    def delete(self, id: int) -> bool:
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute("DELETE FROM transcriptions WHERE id = ?", (id,))
                    conn.commit()
                    return True
            except Exception:
                log_error("history_delete_failed", id=id)
                return False

    def delete_before(self, days: int) -> int:
        """刪除 N 天前的紀錄；days <= 0 視為「永久保留」直接跳過。

        Returns 刪除的筆數（失敗回 0）。
        """
        if days <= 0:
            return 0
        cutoff = int(time.time()) - days * 86_400
        with self._lock:
            try:
                with self._connect() as conn:
                    cur = conn.execute(
                        "DELETE FROM transcriptions WHERE timestamp < ?",
                        (cutoff,),
                    )
                    conn.commit()
                    count = cur.rowcount
                if count > 0:
                    log.info(f"HISTORY: retention delete_before({days}d) removed {count} rows")
                return count
            except Exception:
                log_error("history_delete_before_failed", days=days)
                return 0

    def count(self) -> int:
        with self._lock:
            try:
                with self._connect() as conn:
                    row = conn.execute("SELECT COUNT(*) FROM transcriptions").fetchone()
                    return int(row[0]) if row else 0
            except Exception:
                log_error("history_count_failed")
                return 0


# ── 內部 helper ───────────────────────────────────────────────────────────────

def _row_to_entry(row: sqlite3.Row) -> HistoryEntry:
    """將 sqlite3.Row 轉成 HistoryEntry。"""
    return HistoryEntry(
        id            = row["id"],
        timestamp     = row["timestamp"],
        duration_s    = row["duration_s"],
        raw_text      = row["raw_text"],
        polished_text = row["polished_text"],
        target_app    = row["target_app"],
        preset_used   = row["preset_used"],
        model_whisper = row["model_whisper"],
        model_ollama  = row["model_ollama"],
        language      = row["language"],
    )
