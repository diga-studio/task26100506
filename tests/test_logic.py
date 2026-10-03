"""纯计算逻辑单元测试。"""

from healthstats.logic import (
    HEARTBEAT_TTL,
    compute_durations,
    current_level,
    level_of,
)
from healthstats.types import HealthLevel


def test_level_rules():
    assert level_of(0, 0, 9) is HealthLevel.CRITICAL   # 磁盘 < 10%
    assert level_of(100, 100, 9) is HealthLevel.CRITICAL
    assert level_of(0, 0, 10) is HealthLevel.HEALTHY   # 恰好 10% 非 Critical
    assert level_of(86, 0, 50) is HealthLevel.WARNING  # CPU > 85
    assert level_of(0, 86, 50) is HealthLevel.WARNING  # 内存 > 85
    assert level_of(85, 85, 50) is HealthLevel.HEALTHY  # 恰好 85 非 Warning
    assert level_of(0, 0, 50) is HealthLevel.HEALTHY


def test_ttl_exact_expiry():
    samples = [(0, 10, 10, 50)]
    # 1800 秒恰好失效
    assert compute_durations(samples, 0, 1800) == [1800, 0, 0]
    assert compute_durations(samples, 0, 1801) == [1800, 0, 1]


def test_next_heartbeat_cuts_interval():
    samples = [(0, 10, 10, 50), (600, 90, 10, 50)]
    assert compute_durations(samples, 0, 1000) == [600, 400, 0]


def test_gap_is_critical():
    samples = [(0, 10, 10, 50), (5000, 10, 10, 50)]
    h, w, c = compute_durations(samples, 0, 5000)
    assert (h, w, c) == (1800, 0, 3200)


def test_window_clips_first_interval():
    # 起点前 900 秒的心跳，窗口内只剩 900 秒有效
    samples = [(100, 10, 10, 50)]
    assert compute_durations(samples, 1000, 5000) == [900, 0, 3100]


def test_sample_at_end_contributes_nothing():
    samples = [(3600, 90, 90, 5)]
    assert compute_durations(samples, 0, 3600) == [0, 0, 3600]


def test_current_level():
    healthy = [(100, 10, 10, 50)]
    assert current_level(healthy, 100) is HealthLevel.HEALTHY
    assert current_level(healthy, 100 + HEARTBEAT_TTL - 1) is HealthLevel.HEALTHY
    assert current_level(healthy, 100 + HEARTBEAT_TTL) is HealthLevel.CRITICAL
    assert current_level([], 0) is HealthLevel.CRITICAL
