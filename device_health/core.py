"""对外门面：提交心跳、查询设备健康报告、推进业务时间并清理。"""
from __future__ import annotations

from .blocks import DeviceBlocks
from .heartbeat import (
    CRITICAL,
    LEVEL_NAMES,
    TTL_SECONDS,
    WINDOW_SECONDS,
    Heartbeat,
    classify,
)
from .store import Store

_META_NOW = "now"


class DeviceHealthStats:
    """设备健康时长统计库主入口（调用串行，不支持并发）。

    - 业务时间 T 从 0 开始，只能前进；调用方保证心跳采样时间不晚于 T。
    - 统计窗口为 [max(0, T - 2592000), T)，含起点、不含 T。
    """

    def __init__(self, db_path: str) -> None:
        self._store = Store(db_path)
        self._now = self._store.get_meta(_META_NOW, 0)
        self._devices: dict[str, DeviceBlocks] = {}
        for device_id, rows in self._store.load_all().items():
            blocks = DeviceBlocks()
            blocks.load_sorted(rows)
            self._devices[device_id] = blocks

    @property
    def now(self) -> int:
        """当前业务时间 T。"""
        return self._now

    def _window_start(self) -> int:
        return max(0, self._now - WINDOW_SECONDS)

    def submit(self, batch: list[Heartbeat]) -> int:
        """提交一批心跳，返回实际新增条数（重复与过期的不计）。

        同设备同采样时间视为同一条心跳；早于当前窗口起点的补传直接忽略。
        """
        start = self._window_start()
        pending: dict[str, list[tuple[int, int]]] = {}
        for hb in batch:
            if hb.ts < start:
                continue
            pending.setdefault(hb.device_id, []).append(
                (hb.ts, classify(hb.cpu, hb.mem, hb.disk_free))
            )
        rows: list[tuple[str, int, int]] = []
        added = 0
        for device_id, pairs in pending.items():
            blocks = self._devices.get(device_id)
            if blocks is None:
                blocks = DeviceBlocks()
                self._devices[device_id] = blocks
            n = blocks.add_many(pairs)
            if n:
                # add_many 去重后实际新增的条数；逐条确认以写库
                for ts, level in pairs:
                    rows.append((device_id, ts, level))
                added += n
        # SQLite 以 INSERT OR IGNORE 兜底去重，与内存索引保持一致
        self._store.insert_batch(rows)
        return added

    def query(self, device_id: str) -> dict:
        """查询单设备当前健康及最近 30 天各等级累计秒数。"""
        start = self._window_start()
        end = self._now
        blocks = self._devices.get(device_id)
        durations = blocks.durations(start, end) if blocks else [0, 0, 0]
        # 无有效心跳的时段计入 Critical，保证三项之和等于区间长度
        durations[2] += (end - start) - sum(durations)
        current = CRITICAL
        if blocks:
            level = blocks.level_at(end)
            if level is not None:
                current = LEVEL_NAMES[level]
        return {
            "device_id": device_id,
            "now": end,
            "window_start": start,
            "current": current,
            "durations": {
                LEVEL_NAMES[0]: durations[0],
                LEVEL_NAMES[1]: durations[1],
                LEVEL_NAMES[2]: durations[2],
            },
        }

    def advance(self, new_now: int) -> None:
        """推进业务时间并清理过期明细。时间只能前进。"""
        if new_now < self._now:
            raise ValueError("业务时间只能前进")
        if new_now == self._now:
            return
        self._now = new_now
        self._store.set_meta(_META_NOW, new_now)
        # 保留窗口起点前 TTL 秒内的心跳：其在窗口内仍可能有贡献
        cutoff = self._window_start() - TTL_SECONDS
        if cutoff < 0:
            return
        self._store.delete_expired(cutoff)
        empty = []
        for device_id, blocks in self._devices.items():
            blocks.expire_before(cutoff)
            if len(blocks) == 0:
                empty.append(device_id)
        for device_id in empty:
            del self._devices[device_id]

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "DeviceHealthStats":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
