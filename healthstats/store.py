"""SQLite 存储层：心跳明细、预聚合桶、业务时间。"""

from __future__ import annotations

import sqlite3
from typing import List, Optional

from .logic import Sample


class Store:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS heartbeats (
                device_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                cpu INTEGER NOT NULL,
                mem INTEGER NOT NULL,
                disk_free INTEGER NOT NULL,
                PRIMARY KEY (device_id, ts)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS buckets (
                device_id TEXT NOT NULL,
                bucket_start INTEGER NOT NULL,
                healthy INTEGER NOT NULL,
                warning INTEGER NOT NULL,
                PRIMARY KEY (device_id, bucket_start)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS idx_hb_ts ON heartbeats(ts);
            CREATE INDEX IF NOT EXISTS idx_bucket_start ON buckets(bucket_start);
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- 业务时间 ----
    def get_time(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'now'"
        ).fetchone()
        return int(row[0]) if row else 0

    def set_time(self, t: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('now', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (t,),
            )

    # ---- 心跳 ----
    def insert_heartbeats(self, rows: List[tuple]) -> None:
        """批量插入 (device_id, ts, cpu, mem, disk_free)；重复行自动忽略。"""
        if not rows:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO heartbeats(device_id, ts, cpu, mem, disk_free) "
                "VALUES (?,?,?,?,?)",
                rows,
            )

    def samples_range(self, device_id: str, lo: int, hi: int) -> List[Sample]:
        """按 ts 升序返回 ts 在 [lo, hi] 内的采样。"""
        cur = self._conn.execute(
            "SELECT ts, cpu, mem, disk_free FROM heartbeats "
            "WHERE device_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (device_id, lo, hi),
        )
        return [tuple(r) for r in cur.fetchall()]

    def latest_sample_at_or_before(self, device_id: str, ts: int) -> Optional[Sample]:
        row = self._conn.execute(
            "SELECT ts, cpu, mem, disk_free FROM heartbeats "
            "WHERE device_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (device_id, ts),
        ).fetchone()
        return tuple(row) if row else None

    def delete_heartbeats_before(self, cutoff_ts: int) -> int:
        """删除 ts + TTL <= cutoff_ts 的明细，返回删除行数。"""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM heartbeats WHERE ts <= ?", (cutoff_ts - 1800,)
            )
        return cur.rowcount

    # ---- 聚合桶 ----
    def get_buckets(self, device_id: str, lo: int, hi: int) -> List[tuple]:
        """返回 [lo, hi) 内已聚合的桶 (bucket_start, healthy, warning)。"""
        cur = self._conn.execute(
            "SELECT bucket_start, healthy, warning FROM buckets "
            "WHERE device_id = ? AND bucket_start >= ? AND bucket_start < ? ORDER BY bucket_start",
            (device_id, lo, hi),
        )
        return [tuple(r) for r in cur.fetchall()]

    def upsert_bucket(self, device_id: str, bucket_start: int, healthy: int, warning: int) -> None:
        self.apply_bucket_changes([(device_id, bucket_start, healthy, warning)], [])

    def apply_bucket_changes(self, upserts: List[tuple], deletes: List[tuple]) -> None:
        """单事务应用桶更新 (device, start, healthy, warning) 与删除 (device, start)。"""
        with self._conn:
            if deletes:
                self._conn.executemany(
                    "DELETE FROM buckets WHERE device_id = ? AND bucket_start = ?",
                    deletes,
                )
            if upserts:
                self._conn.executemany(
                    "INSERT INTO buckets(device_id, bucket_start, healthy, warning) "
                    "VALUES (?,?,?,?) "
                    "ON CONFLICT(device_id, bucket_start) DO UPDATE SET "
                    "healthy = excluded.healthy, warning = excluded.warning",
                    upserts,
                )

    def delete_buckets_before(self, cutoff_start: int) -> int:
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM buckets WHERE bucket_start <= ?", (cutoff_start - 3600,)
            )
        return cur.rowcount

    def checkpoint(self) -> None:
        """截断 WAL，防止长期运行后 WAL 文件持续增长。"""
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
