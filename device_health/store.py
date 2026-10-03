"""SQLite 持久化层：心跳明细的写入、读取与过期清理。"""
from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeats (
    device_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    level INTEGER NOT NULL,
    PRIMARY KEY (device_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
) WITHOUT ROWID;
"""


class Store:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def insert_batch(self, rows: list[tuple[str, int, int]]) -> None:
        """批量写入 (device_id, ts, level)，同设备同 ts 去重。"""
        if not rows:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO heartbeats (device_id, ts, level)"
                " VALUES (?, ?, ?)",
                rows,
            )

    def load_all(self) -> dict[str, list[tuple[int, int]]]:
        """加载全部明细（用于打开库时重建内存索引）。"""
        out: dict[str, list[tuple[int, int]]] = {}
        cur = self._conn.execute(
            "SELECT device_id, ts, level FROM heartbeats ORDER BY device_id, ts"
        )
        for device_id, ts, level in cur:
            out.setdefault(device_id, []).append((ts, level))
        return out

    def delete_expired(self, cutoff: int) -> int:
        """删除 ts <= cutoff 的明细，返回删除条数。"""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM heartbeats WHERE ts <= ?", (cutoff,)
            )
        return cur.rowcount

    def get_meta(self, key: str, default: int = 0) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def close(self) -> None:
        self._conn.close()
