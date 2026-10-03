"""随机差分测试：服务结果与暴力参考实现逐项一致。"""

import random

from healthstats import DeviceHealthService, Heartbeat
from healthstats.logic import HEARTBEAT_TTL, WINDOW_SECONDS, compute_durations, level_of
from healthstats.types import HealthLevel


def brute_force(samples, t):
    """samples: {(device, ts): (cpu, mem, disk_free)}；直接对全量明细计算。"""
    ws = max(0, t - WINDOW_SECONDS)
    reports = {}
    devices = {d for d, _ in samples}
    for dev in devices:
        pts = sorted(
            (ts, v[0], v[1], v[2]) for (d, ts), v in samples.items() if d == dev
        )
        relevant = [p for p in pts if p[0] + HEARTBEAT_TTL > ws and p[0] <= t]
        h, w, c = compute_durations(relevant, ws, t) if t > ws else (0, 0, 0)
        cur = HealthLevel.CRITICAL
        for p in reversed(pts):
            if p[0] <= t:
                if p[0] + HEARTBEAT_TTL > t:
                    cur = level_of(p[1], p[2], p[3])
                break
        reports[dev] = (cur, h, w, c)
    return reports


def test_randomized_differential():
    rng = random.Random(20261003)
    svc = DeviceHealthService(':memory:')
    truth = {}
    t = 0
    devices = [f'dev{i}' for i in range(8)]
    for round_ in range(60):
        # 随机提交一批（含乱序、重复、补传）
        batch = []
        ws_now = max(0, t - WINDOW_SECONDS)
        for _ in range(rng.randint(1, 40)):
            dev = rng.choice(devices)
            ts = rng.randint(max(0, t - 2700000), t) if t else 0
            hb = (rng.randint(0, 100), rng.randint(0, 100), rng.randint(0, 100))
            if ts >= ws_now:
                truth[(dev, ts)] = hb  # 重复提交内容相同；早于起点的补传被忽略
            batch.append(Heartbeat(dev, ts, *hb))
            if rng.random() < 0.3:
                batch.append(Heartbeat(dev, ts, *hb))  # 重复
        rng.shuffle(batch)
        svc.submit_heartbeats(batch)
        # 随机推进时间（含跨窗口的大步进）
        t += rng.choice([0, 17, 1800, 50000, 900000])
        svc.advance_time(t)
        # 比对全部设备
        expected = brute_force(truth, t)
        for dev in devices:
            r = svc.get_device_health(dev)
            got = (r.current_level, r.healthy_seconds, r.warning_seconds, r.critical_seconds)
            want = expected.get(dev, (HealthLevel.CRITICAL, 0, 0, t - max(0, t - WINDOW_SECONDS)))
            assert got == want, f'round={round_} dev={dev} got={got} want={want}'
    svc.close()
