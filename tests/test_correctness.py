"""验收测试：业务规则与结果不变性。"""
import random

import pytest

from device_health import CRITICAL, HEALTHY, WARNING, DeviceHealthStats, Heartbeat
from device_health.heartbeat import TTL_SECONDS, WINDOW_SECONDS


@pytest.fixture()
def stats(tmp_path):
    s = DeviceHealthStats(str(tmp_path / "test.db"))
    yield s
    s.close()


def hb(device, ts, cpu=50, mem=50, disk=50):
    return Heartbeat(device_id=device, ts=ts, cpu=cpu, mem=mem, disk_free=disk)


def durations(report):
    d = report["durations"]
    return d[HEALTHY], d[WARNING], d[CRITICAL]


def test_acceptance_ordered(stats):
    """0/600/2700 分别 Healthy/Warning/Healthy，3600 时应得 1500/1800/300。"""
    stats.submit([
        hb("d", 0),
        hb("d", 600, cpu=90),
        hb("d", 2700),
    ])
    stats.advance(3600)
    report = stats.query("d")
    assert report["current"] == HEALTHY
    assert durations(report) == (1500, 1800, 300)


def test_acceptance_late_backfill(stats):
    """补入 1800 秒 Healthy 后应得 2400/1200/0。"""
    stats.submit([hb("d", 0), hb("d", 600, cpu=90), hb("d", 2700)])
    stats.advance(3600)
    stats.submit([hb("d", 1800)])
    report = stats.query("d")
    assert durations(report) == (2400, 1200, 0)
    assert sum(durations(report)) == 3600


def test_order_and_duplicates_invariant(tmp_path):
    """同一组心跳按序/乱序/重复提交，最终结果完全一致。"""
    beats = [
        hb("d", 0), hb("d", 600, cpu=90), hb("d", 2700),
        hb("d", 1800), hb("d", 4000, disk=5), hb("d", 3200, mem=95),
    ]
    s1 = DeviceHealthStats(str(tmp_path / "a.db"))
    s1.submit(beats)
    s1.advance(5000)
    ordered = s1.query("d")
    s1.close()

    s2 = DeviceHealthStats(str(tmp_path / "b.db"))
    shuffled = beats[:]
    random.Random(1).shuffle(shuffled)
    s2.submit(shuffled[:3])
    s2.submit([shuffled[0], shuffled[0]])          # 重复
    s2.submit(shuffled[3:])
    s2.submit(list(reversed(shuffled)))            # 整批重复乱序
    s2.advance(5000)
    unordered = s2.query("d")
    s2.close()
    assert ordered["current"] == unordered["current"]
    assert durations(ordered) == durations(unordered)


def test_heartbeat_at_exact_ttl_expires(stats):
    """恰好经过 1800 秒即失效，之后无心跳为 Critical。"""
    stats.submit([hb("d", 1000)])
    stats.advance(1000 + TTL_SECONDS)
    report = stats.query("d")
    assert report["current"] == CRITICAL
    assert durations(report) == (TTL_SECONDS, 0, 1000)


def test_heartbeat_at_now_affects_current_only(stats):
    """采样时间恰为 T 的心跳影响当前健康，但不贡献时长。"""
    stats.submit([hb("d", 0)])
    stats.advance(3600)
    stats.submit([hb("d", 3600, disk=5)])
    report = stats.query("d")
    assert report["current"] == CRITICAL
    assert durations(report) == (1800, 0, 1800)


def test_window_start_before_heartbeat(stats):
    """起点前 900 秒 Healthy 心跳：窗口内恰 900 秒 Healthy，清理不变。"""
    # 在 T=2592000 时窗口起点为 0；先提交 0 秒心跳，再推进使起点越过它
    stats.submit([hb("d", 0), hb("x", 0), hb("x", 500)])
    stats.advance(2592000)          # 窗口 [0, 2592000)
    stats.advance(2592000 + 900)    # 窗口 [900, 2592900)，心跳在起点前 900 秒
    report = stats.query("d")
    assert report["window_start"] == 900
    assert durations(report) == (900, 0, 2592000 - 900)
    # 推进触发清理（删除设备 x 已完全失效的明细）不改变 d 的结果
    stats.advance(2592000 + 1800)   # 起点 1800，cutoff=0，ts<=0 的明细被删除
    assert durations(stats.query("d")) == (0, 0, 2592000)
    assert stats.query("d")["current"] == CRITICAL
    # 设备 x 在 500 秒的心跳贡献 [1800, 2300) 共 500 秒，不受清理影响
    assert durations(stats.query("x")) == (500, 0, 2592000 - 500)


def test_late_arrival_before_window_ignored(stats):
    """早于窗口起点的补传直接忽略。"""
    stats.advance(WINDOW_SECONDS + 10000)
    stats.submit([hb("d", 1)])  # 远早于起点
    report = stats.query("d")
    assert durations(report) == (0, 0, WINDOW_SECONDS)


def test_time_cannot_go_back(stats):
    stats.advance(100)
    with pytest.raises(ValueError):
        stats.advance(99)


def test_unknown_device_all_critical(stats):
    stats.advance(5000)
    report = stats.query("ghost")
    assert report["current"] == CRITICAL
    assert durations(report) == (0, 0, 5000)


def test_sum_equals_window_length(stats):
    """任意数据下三项之和必须等于区间长度。"""
    random.seed(9)
    beats = [hb("d", random.randint(0, 100000),
                cpu=random.uniform(0, 100),
                mem=random.uniform(0, 100),
                disk=random.uniform(0, 100))
             for _ in range(500)]
    stats.submit(beats)
    stats.advance(100000)
    report = stats.query("d")
    assert sum(durations(report)) == 100000


def test_persistence_across_reopen(tmp_path):
    """关闭重开后结果一致（数据保存在 SQLite）。"""
    path = str(tmp_path / "p.db")
    s = DeviceHealthStats(path)
    s.submit([hb("d", 0), hb("d", 600, cpu=90), hb("d", 2700)])
    s.advance(3600)
    before = s.query("d")
    s.close()
    s2 = DeviceHealthStats(path)
    assert s2.now == 3600
    assert s2.query("d") == before
    s2.close()


def test_cleanup_removes_expired_rows(tmp_path):
    """清理须实际移除过期明细。"""
    path = str(tmp_path / "c.db")
    s = DeviceHealthStats(path)
    s.submit([hb("d", 0), hb("d", 100), hb("d", 200)])
    s.advance(WINDOW_SECONDS + 10000)
    s.advance(WINDOW_SECONDS + 20000)  # 触发清理
    rows = s._store._conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0]
    assert rows == 0
    s.close()


def test_medium_scale_exact_durations(stats):
    """3 万条心跳与朴素逐条算法精确对拍（含乱序补传与重复）。"""
    random.seed(11)
    n = 30000
    beats = []
    for i in range(n):
        ts = i * 80 + random.randint(0, 40)  # 平均 80 秒一条，覆盖 30 天窗口
        beats.append((ts, random.choice([
            (50, 50, 50), (90, 50, 50), (50, 50, 5), (50, 95, 50)])))
    shuffled = beats[:]
    random.Random(2).shuffle(shuffled)
    stats.submit([hb("d", ts, cpu=c, mem=m, disk=k) for ts, (c, m, k) in shuffled[:n // 2]])
    stats.advance(n * 80 + 40)
    stats.submit([hb("d", ts, cpu=c, mem=m, disk=k) for ts, (c, m, k) in shuffled[n // 2:]])
    stats.submit([hb("d", ts, cpu=c, mem=m, disk=k) for ts, (c, m, k) in shuffled[:1000]])  # 重复
    report = stats.query("d")

    from device_health.heartbeat import classify as _classify
    ordered = sorted({ts: lv for ts, lv in
                      ((ts, _classify(c, m, k)) for ts, (c, m, k) in beats)}.items())
    start = report["window_start"]
    end = report["now"]
    want = [0, 0, 0]
    for i, (ts, lv) in enumerate(ordered):
        nxt = ordered[i + 1][0] if i + 1 < len(ordered) else None
        cap = min(ts + TTL_SECONDS, nxt if nxt is not None else ts + TTL_SECONDS, end)
        if cap > max(ts, start):
            want[lv] += cap - max(ts, start)
    want[2] += (end - start) - sum(want)
    got = report["durations"]
    assert (got[HEALTHY], got[WARNING], got[CRITICAL]) == tuple(want)
