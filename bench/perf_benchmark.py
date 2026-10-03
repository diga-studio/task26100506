"""规模实测：10000 设备 / 100 万窗口内心跳 / 单设备 20 万条。

验证：
1. 高密度设备连续 1000 次「补传新采样时刻 + 查询」总耗时 <= 20s，查询 p95 <= 200ms；
2. 补传/重复提交/时间推进/清理后统计仍精确（与暴力参考一致）；
3. 持续运行（每个 30 天周期等量数据）存储占用趋于稳定。

运行：python3 bench/perf_benchmark.py
"""

import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from healthstats import DeviceHealthService, Heartbeat
from healthstats.logic import HEARTBEAT_TTL, WINDOW_SECONDS, compute_durations, level_of
from healthstats.types import HealthLevel

NUM_DEVICES = 10000
DENSE_SAMPLES = 200_000
SPARSE_TOTAL = 800_000
BACKFILL_ROUNDS = 1000


def fmt(sec):
    return f"{sec * 1000:.1f}ms" if sec < 1 else f"{sec:.2f}s"


def main():
    tmpdir = tempfile.mkdtemp(prefix='healthbench_')
    db = os.path.join(tmpdir, 'bench.db')
    svc = DeviceHealthService(db)
    t0 = WINDOW_SECONDS + 3_600_000  # 初始业务时间，窗口起点 > 0
    svc.advance_time(t0)
    ws = t0 - WINDOW_SECONDS

    # ---------- 装载 100 万条窗口内心跳 ----------
    t_start = time.perf_counter()
    dense_id = 'dense0'
    step = WINDOW_SECONDS / DENSE_SAMPLES  # 均匀铺满窗口
    batch = []
    for i in range(DENSE_SAMPLES):
        ts = ws + int(i * step)
        cpu = (i * 7) % 100
        mem = (i * 13) % 100
        disk = (i * 3) % 100
        batch.append(Heartbeat(dense_id, ts, cpu, mem, disk))
        if len(batch) == 5000:
            svc.submit_heartbeats(batch)
            batch = []
    if batch:
        svc.submit_heartbeats(batch)
    per_dev = SPARSE_TOTAL // (NUM_DEVICES - 1)
    batch = []
    for d in range(1, NUM_DEVICES):
        base = d * 7919
        for j in range(per_dev):
            ts = ws + (base + j * 32533) % WINDOW_SECONDS
            batch.append(Heartbeat(f'dev{d}', ts, (base + j) % 100, j % 100, (base + j * 3) % 100))
            if len(batch) == 5000:
                svc.submit_heartbeats(batch)
                batch = []
    if batch:
        svc.submit_heartbeats(batch)
    load_time = time.perf_counter() - t_start
    size_after_load = os.path.getsize(db)
    print(f'[装载] 100 万条心跳（含 1 台 20 万条高密度设备）: {fmt(load_time)}, '
          f'DB 文件 {size_after_load / 1e6:.1f} MB')

    # ---------- 1000 次 补传+查询 ----------
    # 预生成 1000 个未使用的新采样时刻（严格避开已占用的整数秒）
    used = {r[0] for r in svc._store.samples_range(dense_id, 0, t0)}
    before = len(used)
    new_ts = []
    cand = ws + 1
    while len(new_ts) < BACKFILL_ROUNDS:
        if cand not in used and cand < t0:
            new_ts.append(cand)
            used.add(cand)
        cand += 997  # 步进取点，避免与均匀装载的点重合
    query_times = []
    t_start = time.perf_counter()
    for i, ts in enumerate(new_ts):
        svc.submit_heartbeats([Heartbeat(dense_id, ts, 50, 50, 50)])
        q0 = time.perf_counter()
        svc.get_device_health(dense_id)
        query_times.append(time.perf_counter() - q0)
    total = time.perf_counter() - t_start
    after = len(svc._store.samples_range(dense_id, 0, t0))
    assert after == before + BACKFILL_ROUNDS, '补传的采样时刻并非全新'
    p95 = statistics.quantiles(query_times, n=20)[18]
    print(f'[补传+查询] {BACKFILL_ROUNDS} 轮总耗时 {fmt(total)} '
          f'(要求 <= 20s)；单次查询 p95 {fmt(p95)} (要求 <= 200ms)，'
          f'最大 {fmt(max(query_times))}')
    assert total <= 20, '补传+查询总耗时超标'
    assert p95 <= 0.2, '查询 p95 超标'

    # ---------- 精确性：与暴力参考一致 ----------
    rows = svc._store.samples_range(dense_id, 0, t0)
    h, w, c = compute_durations(rows, ws, t0)
    r = svc.get_device_health(dense_id)
    assert (r.healthy_seconds, r.warning_seconds, r.critical_seconds) == (h, w, c), \
        f'服务 {(r.healthy_seconds, r.warning_seconds, r.critical_seconds)} != 暴力 {(h, w, c)}'
    assert h + w + c == WINDOW_SECONDS
    last = rows[-1]
    want_cur = level_of(last[1], last[2], last[3]) if last[0] + HEARTBEAT_TTL > t0 \
        else HealthLevel.CRITICAL
    assert r.current_level is want_cur
    print(f'[精确性] 高密度设备 服务==暴力参考: H={h} W={w} C={c}，合计={h + w + c}')

    # ---------- 重复提交不改变结果 ----------
    dup = [Heartbeat(dense_id, ts, 50, 50, 50) for ts in new_ts[:500]]
    t0_ = time.perf_counter()
    svc.submit_heartbeats(dup * 4)  # 2000 条重复
    dup_time = time.perf_counter() - t0_
    r2 = svc.get_device_health(dense_id)
    assert (r2.healthy_seconds, r2.warning_seconds, r2.critical_seconds) == (h, w, c)
    print(f'[重复提交] 2000 条重复心跳 {fmt(dup_time)}，统计不变')

    # ---------- 时间推进 + 清理后仍精确 ----------
    t1 = t0 + 900_000
    svc.advance_time(t1)
    ws1 = t1 - WINDOW_SECONDS
    rows = svc._store.samples_range(dense_id, 0, t1)
    h1, w1, c1 = compute_durations(rows, ws1, t1)
    r3 = svc.get_device_health(dense_id)
    assert (r3.healthy_seconds, r3.warning_seconds, r3.critical_seconds) == (h1, w1, c1)
    assert h1 + w1 + c1 == WINDOW_SECONDS
    print(f'[推进+清理] T 前进 900000s 后统计仍精确: H={h1} W={w1} C={c1}')

    # ---------- 存储稳定性：连续 4 个 30 天周期，每周期等量数据 ----------
    print('[存储稳定性] 每周期 20 万条/高密度设备 + 推进 30 天并清理:')
    sizes = []
    t_cur = t1
    for cycle in range(4):
        t_cur += WINDOW_SECONDS
        svc.advance_time(t_cur)  # 清理上一周期过期明细
        ws_c = t_cur - WINDOW_SECONDS
        batch = []
        for i in range(DENSE_SAMPLES):
            ts = ws_c + int(i * step)
            batch.append(Heartbeat(dense_id, ts, (i * 11) % 100, (i * 5) % 100, (i * 7) % 100))
            if len(batch) == 5000:
                svc.submit_heartbeats(batch)
                batch = []
        if batch:
            svc.submit_heartbeats(batch)
        svc.advance_time(t_cur)  # 触发清理与 WAL 截断
        svc._store.checkpoint()
        size = os.path.getsize(db)
        sizes.append(size)
        print(f'  周期 {cycle + 1}: DB 文件 {size / 1e6:.1f} MB')
    growth = (sizes[-1] - sizes[0]) / 1e6
    print(f'  首末周期增长 {growth:+.1f} MB（应趋近于 0）')
    assert sizes[-1] <= sizes[0] * 1.2, '存储占用持续增长，未趋于稳定'

    svc.close()
    print('全部规模实测通过。')


if __name__ == '__main__':
    main()
