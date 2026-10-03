"""对外服务：提交心跳、查询健康报告、推进业务时间并清理。"""

from __future__ import annotations

from typing import Iterable

from .logic import (
    BUCKET_SECONDS,
    HEARTBEAT_TTL,
    WINDOW_SECONDS,
    compute_durations,
    current_level,
)
from .store import Store
from .types import HealthReport, Heartbeat


class DeviceHealthService:
    def __init__(self, db_path: str) -> None:
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "DeviceHealthService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def now(self) -> int:
        """当前业务时间 T。"""
        return self._store.get_time()

    def _window_start(self, t: int) -> int:
        return max(0, t - WINDOW_SECONDS)

    def submit_heartbeats(self, heartbeats: Iterable[Heartbeat]) -> None:
        """提交一批心跳（可乱序、可重复）。"""
        ws = self._window_start(self._store.get_time())
        rows = []
        dirty = set()
        for hb in heartbeats:
            if hb.ts < ws:
                continue  # 早于窗口起点的补传直接忽略
            rows.append((hb.device_id, hb.ts, hb.cpu, hb.mem, hb.disk_free))
            base = (hb.ts // BUCKET_SECONDS) * BUCKET_SECONDS
            for b in (base - BUCKET_SECONDS, base, base + BUCKET_SECONDS):
                if b >= ws:
                    dirty.add((hb.device_id, b))
        self._store.insert_heartbeats(rows)
        # 重算受影响桶（幂等；重复提交只是多算一次，结果不变）
        upserts = []
        deletes = []
        for device_id, bucket_start in dirty:
            healthy, warning = self._recompute_bucket(device_id, bucket_start)
            if healthy == 0 and warning == 0:
                deletes.append((device_id, bucket_start))
            else:
                upserts.append((device_id, bucket_start, healthy, warning))
        self._store.apply_bucket_changes(upserts, deletes)

    def _recompute_bucket(self, device_id: str, bucket_start: int) -> tuple:
        bucket_end = bucket_start + BUCKET_SECONDS
        samples = self._store.samples_range(
            device_id, bucket_start - HEARTBEAT_TTL, bucket_end - 1
        )
        healthy, warning, _ = compute_durations(samples, bucket_start, bucket_end)
        return healthy, warning

    def get_device_health(self, device_id: str) -> HealthReport:
        """查询单设备当前健康及最近 30 天各等级累计秒数。"""
        t = self._store.get_time()
        ws = self._window_start(t)
        latest = self._store.latest_sample_at_or_before(device_id, t)
        level = current_level([latest] if latest else [], t)
        healthy = warning = critical = 0
        if t > ws:
            b0 = ((ws + BUCKET_SECONDS - 1) // BUCKET_SECONDS) * BUCKET_SECONDS
            b1 = (t // BUCKET_SECONDS) * BUCKET_SECONDS
            if b0 >= b1:
                # 窗口不足一个整桶，整体按明细计算
                samples = self._store.samples_range(device_id, ws - HEARTBEAT_TTL, t)
                healthy, warning, critical = compute_durations(samples, ws, t)
            else:
                # 头部边缘桶 [ws, b0)
                head = self._store.samples_range(device_id, ws - HEARTBEAT_TTL, b0 - 1)
                h, w, c = compute_durations(head, ws, b0)
                healthy += h
                warning += w
                critical += c
                # 完整桶 [b0, b1)：读预聚合，缺省为 Critical
                rows = self._store.get_buckets(device_id, b0, b1)
                agg = {r[0]: (r[1], r[2]) for r in rows}
                b = b0
                while b < b1:
                    h, w = agg.get(b, (0, 0))
                    healthy += h
                    warning += w
                    critical += BUCKET_SECONDS - h - w
                    b += BUCKET_SECONDS
                # 尾部边缘桶 [b1, t)
                tail = self._store.samples_range(device_id, b1 - HEARTBEAT_TTL, t)
                h, w, c = compute_durations(tail, b1, t)
                healthy += h
                warning += w
                critical += c
        return HealthReport(
            device_id=device_id,
            current_level=level,
            healthy_seconds=healthy,
            warning_seconds=warning,
            critical_seconds=critical,
            window_start=ws,
            window_end=t,
        )

    def advance_time(self, new_t: int) -> None:
        """推进业务时间并清理过期明细。"""
        now = self._store.get_time()
        if new_t < now:
            raise ValueError(f"业务时间只能前进: 当前 {now}, 收到 {new_t}")
        if new_t == now:
            return
        self._store.set_time(new_t)
        ws = self._window_start(new_t)
        self._store.delete_heartbeats_before(ws)
        self._store.delete_buckets_before(ws)
        self._store.checkpoint()
