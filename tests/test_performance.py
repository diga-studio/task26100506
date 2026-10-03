"""规模与性能实测：10000 台设备、100 万条窗口内心跳、单设备 20 万条。

运行：python3 -m pytest tests/test_performance.py -q -s
"""
import os
import time

import pytest

from device_health import HEALTHY, DeviceHealthStats, Heartbeat
from device_health.heartbeat import WINDOW_SECONDS

N_DEVICES = 10000
HOT_DEVICE = "hot-0"
HOT_BEATS = 200_000
HOT_STEP = 12  # 200000 * 12 = 2_400_000 < 2592000
COLD_BEATS_PER_DEVICE = 80  # 9999 * 80 + 200000 ~= 1_000_000
BACKFILL_ROUNDS = 1000


def _gen_cold():
    for d in range(1, N_DEVICES):
        device_id = f"cold-{d}"
        for i in range(COLD_BEATS_PER_DEVICE):
            yield Heartbeat(device_id, i * 30000 + (d % 7), 50, 50, 50)


@pytest.fixture(scope="module")
def stats(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("perf") / "perf.db")
    s = DeviceHealthStats(path)
    t0 = time.perf_counter()
    hot = [
        Heartbeat(HOT_DEVICE, i * HOT_STEP, 50 + (i % 40), 50, 50)
        for i in range(HOT_BEATS)
    ]
    s.submit(hot)
    batch = []
    for hb in _gen_cold():
        batch.append(hb)
        if len(batch) >= 50000:
            s.submit(batch)
            batch = []
    if batch:
        s.submit(batch)
    s.advance(WINDOW_SECONDS)
    load_s = time.perf_counter() - t0
    print(f"\n[perf] 加载 1,000,000 条心跳（含建库）: {load_s:.2f}s, "
          f"hot device beats={len(s._devices[HOT_DEVICE])}")
    yield s, path
    s.close()


def test_backfill_and_query_latency(stats):
    """1000 次补传+查询：累计 <= 20s，单次查询 p95 <= 200ms。"""
    s, _ = stats
    base = HOT_BEATS * HOT_STEP  # 2400000，窗口内尚未使用的采样时刻
    query_times = []
    t0 = time.perf_counter()
    for i in range(BACKFILL_ROUNDS):
        ts = base - (2 * i + 1)  # 落在已有心跳间隔内的新时刻
        s.submit([Heartbeat(HOT_DEVICE, ts, 92, 50, 50)])
        q0 = time.perf_counter()
        report = s.query(HOT_DEVICE)
        query_times.append(time.perf_counter() - q0)
    total = time.perf_counter() - t0
    query_times.sort()
    p95 = query_times[int(len(query_times) * 0.95)]
    d = report["durations"]
    assert sum(d.values()) == WINDOW_SECONDS  # 精确秒数：三项之和等于窗口长度
    print(f"[perf] 1000 次补传+查询累计: {total:.2f}s (要求 <= 20s)")
    print(f"[perf] 单次查询 p95: {p95*1000:.2f}ms (要求 <= 200ms), "
          f"max: {query_times[-1]*1000:.2f}ms")
    assert total <= 20
    assert p95 <= 0.2


def test_duplicates_and_exact_seconds_at_scale(stats):
    """规模下重复提交不改变结果，时长精确到秒。"""
    s, _ = stats
    before = s.query(HOT_DEVICE)["durations"]
    dup = [Heartbeat(HOT_DEVICE, i * HOT_STEP, 50 + (i % 40), 50, 50)
           for i in range(0, HOT_BEATS, 1000)]
    assert s.submit(dup) == 0  # 全部重复，新增为 0
    after = s.query(HOT_DEVICE)["durations"]
    assert before == after
    assert sum(after.values()) == WINDOW_SECONDS


def test_advance_cleanup_and_storage_stable(stats):
    """时间推进+清理后结果精确，且每周期数据量相同时存储趋于稳定。"""
    s, path = stats
    # 记录热设备当前报告，推进一个完整窗口后应全部过期为 Critical
    s.advance(WINDOW_SECONDS * 2)
    report = s.query(HOT_DEVICE)
    assert report["durations"] == {
        "Healthy": 0, "Warning": 0, "Critical": WINDOW_SECONDS
    }
    assert report["current"] == "Critical"

    # 持续运行 5 个 30 天周期，每周期写入相同数据量并清理
    sizes = []
    for cycle in range(5):
        base = WINDOW_SECONDS * (2 + cycle)
        beats = [
            Heartbeat(HOT_DEVICE, base + i * HOT_STEP, 50, 50, 50)
            for i in range(HOT_BEATS // 4)  # 每周期 5 万条，控制测试时长
        ]
        s.submit(beats)
        s.advance(WINDOW_SECONDS * (3 + cycle))
        s._store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        sizes.append(os.path.getsize(path))
        d = s.query(HOT_DEVICE)["durations"]
        assert sum(d.values()) == WINDOW_SECONDS
    print(f"[perf] 各周期清理后 DB 文件大小: {[f'{x/1e6:.1f}MB' for x in sizes]}")
    # 存储趋于稳定：末周期不超过首周期的 1.2 倍
    assert sizes[-1] <= sizes[0] * 1.2
