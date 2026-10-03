"""服务层行为测试：乱序/重复不变性、窗口、清理、时间推进。"""

import random

import pytest

from healthstats import DeviceHealthService, HealthLevel, Heartbeat
from healthstats.logic import WINDOW_SECONDS


@pytest.fixture()
def svc():
    s = DeviceHealthService(':memory:')
    yield s
    s.close()


def durations(report):
    return (report.healthy_seconds, report.warning_seconds, report.critical_seconds)


def test_acceptance_basic_flow(svc):
    svc.submit_heartbeats([
        Heartbeat('d', 0, 10, 10, 50),      # Healthy
        Heartbeat('d', 600, 90, 10, 50),    # Warning
        Heartbeat('d', 2700, 10, 10, 50),   # Healthy
    ])
    svc.advance_time(3600)
    assert durations(svc.get_device_health('d')) == (1500, 1800, 300)
    # 补入 1800 秒的 Healthy 心跳，修正历史
    svc.submit_heartbeats([Heartbeat('d', 1800, 10, 10, 50)])
    assert durations(svc.get_device_health('d')) == (2400, 1200, 0)


def test_order_and_duplicates_invariant(svc):
    base = [
        Heartbeat('d', 0, 10, 10, 50),
        Heartbeat('d', 600, 90, 10, 50),
        Heartbeat('d', 1800, 10, 10, 50),
        Heartbeat('d', 2700, 10, 10, 50),
    ]
    svc.submit_heartbeats(base)
    svc.advance_time(3600)
    ordered = svc.get_device_health('d')

    svc2 = DeviceHealthService(':memory:')
    shuffled = base * 3  # 重复提交
    random.Random(42).shuffle(shuffled)
    svc2.submit_heartbeats(shuffled)
    svc2.advance_time(3600)
    unordered = svc2.get_device_health('d')
    svc2.close()

    assert durations(ordered) == durations(unordered)
    assert ordered.current_level is unordered.current_level


def test_sum_equals_window_length(svc):
    svc.submit_heartbeats([Heartbeat('d', 100, 90, 90, 50)])
    svc.advance_time(123456)
    r = svc.get_device_health('d')
    total = r.healthy_seconds + r.warning_seconds + r.critical_seconds
    assert total == r.window_end - r.window_start


def test_window_start_900s_healthy_tail(svc):
    # 先在远古时期建立一条 Healthy 心跳，再把时间推进到窗口起点之后 900 秒
    svc.submit_heartbeats([Heartbeat('d', 100000, 10, 10, 50)])
    t = 100000 + 900 + WINDOW_SECONDS  # 窗口起点 = 100000 + 900
    svc.advance_time(t)
    r = svc.get_device_health('d')
    assert r.window_start == 100000 + 900
    # 心跳有效至 100000+1800，窗口内恰有 900 秒 Healthy
    assert durations(r) == (900, 0, WINDOW_SECONDS - 900)
    # 清理不改变结果（advance_time 内部已清理，再推进 1 秒验证持续正确）
    svc.advance_time(t + 1)
    r2 = svc.get_device_health('d')
    assert durations(r2) == (899, 0, WINDOW_SECONDS - 899)
    # 再推进 899 秒（累计 900 秒），Healthy 贡献归零
    svc.advance_time(t + 900)
    r3 = svc.get_device_health('d')
    assert durations(r3) == (0, 0, WINDOW_SECONDS)
    assert r3.current_level is HealthLevel.CRITICAL


def test_cleanup_actually_removes_rows(svc):
    svc.submit_heartbeats([Heartbeat('d', 1000, 10, 10, 50)])
    svc.advance_time(WINDOW_SECONDS + 100000)
    assert svc._store.samples_range('d', 0, 10**12) == []


def test_backfill_before_window_ignored(svc):
    svc.advance_time(WINDOW_SECONDS + 10000)
    svc.submit_heartbeats([Heartbeat('d', 100, 10, 10, 50)])  # 远早于窗口起点
    r = svc.get_device_health('d')
    assert durations(r) == (0, 0, WINDOW_SECONDS)
    assert svc._store.samples_range('d', 0, 10**12) == []


def test_sample_at_T_affects_current_not_duration(svc):
    svc.advance_time(5000)
    svc.submit_heartbeats([Heartbeat('d', 5000, 90, 90, 5)])
    r = svc.get_device_health('d')
    assert r.current_level is HealthLevel.CRITICAL  # 磁盘 5% < 10%
    assert durations(r) == (0, 0, 5000)  # 尚未贡献时长


def test_time_cannot_go_backward(svc):
    svc.advance_time(100)
    with pytest.raises(ValueError):
        svc.advance_time(99)
    svc.advance_time(100)  # 不变允许


def test_no_heartbeat_is_critical(svc):
    svc.advance_time(7200)
    r = svc.get_device_health('ghost')
    assert r.current_level is HealthLevel.CRITICAL
    assert durations(r) == (0, 0, 7200)


def test_advance_without_new_data_changes_window(svc):
    svc.submit_heartbeats([Heartbeat('d', 0, 10, 10, 50)])
    svc.advance_time(1000)
    assert svc.get_device_health('d').current_level is HealthLevel.HEALTHY
    svc.advance_time(1801)  # 无新心跳，健康状态随时间改变
    r = svc.get_device_health('d')
    assert r.current_level is HealthLevel.CRITICAL
    assert durations(r) == (1800, 0, 1)


def test_multi_device_isolation(svc):
    svc.submit_heartbeats([
        Heartbeat('a', 0, 10, 10, 50),
        Heartbeat('b', 0, 90, 90, 50),
    ])
    svc.advance_time(600)
    assert svc.get_device_health('a').current_level is HealthLevel.HEALTHY
    assert svc.get_device_health('b').current_level is HealthLevel.WARNING
    assert durations(svc.get_device_health('a')) == (600, 0, 0)
    assert durations(svc.get_device_health('b')) == (0, 600, 0)


def test_persistent_db_file(tmp_path):
    db = str(tmp_path / 'health.db')
    svc = DeviceHealthService(db)
    svc.submit_heartbeats([Heartbeat('d', 0, 10, 10, 50)])
    svc.advance_time(600)
    svc.close()
    svc2 = DeviceHealthService(db)
    assert durations(svc2.get_device_health('d')) == (600, 0, 0)
    svc2.close()
