"""纯计算逻辑：等级判定与区间时长统计（不依赖存储）。"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from .types import HealthLevel

#: 心跳有效时长（秒）
HEARTBEAT_TTL = 1800
#: 报告窗口长度（秒），30 天
WINDOW_SECONDS = 2592000
#: 聚合桶大小（秒）
BUCKET_SECONDS = 3600

# 一条采样记录：(ts, cpu, mem, disk_free)
Sample = Tuple[int, int, int, int]


def level_of(cpu: int, mem: int, disk_free: int) -> HealthLevel:
    """根据利用率判定单条心跳的健康等级。"""
    if disk_free < 10:
        return HealthLevel.CRITICAL
    if cpu > 85 or mem > 85:
        return HealthLevel.WARNING
    return HealthLevel.HEALTHY


def compute_durations(samples: Sequence[Sample], start: int, end: int) -> List[int]:
    """统计 [start, end) 内 [healthy, warning, critical] 秒数。

    samples 需覆盖 [start - HEARTBEAT_TTL, end) 范围且按 ts 升序。
    """
    healthy = 0
    warning = 0
    cursor = start
    n = len(samples)
    for i in range(n):
        ts, cpu, mem, disk_free = samples[i]
        eff_start = ts if ts > cursor else cursor
        nxt = samples[i + 1][0] if i + 1 < n else None
        eff_end = ts + HEARTBEAT_TTL
        if nxt is not None and nxt < eff_end:
            eff_end = nxt
        if eff_end > end:
            eff_end = end
        if eff_end > eff_start:
            lvl = level_of(cpu, mem, disk_free)
            if lvl is HealthLevel.HEALTHY:
                healthy += eff_end - eff_start
            elif lvl is HealthLevel.WARNING:
                warning += eff_end - eff_start
            if eff_end > cursor:
                cursor = eff_end
    critical = (end - start) - healthy - warning
    return [healthy, warning, critical]


def current_level(samples: Sequence[Sample], now: int) -> HealthLevel:
    """now 时刻的健康等级；samples 为 ts <= now 的若干末尾采样。"""
    if samples:
        ts, cpu, mem, disk_free = samples[-1]
        if ts + HEARTBEAT_TTL > now:
            return level_of(cpu, mem, disk_free)
    return HealthLevel.CRITICAL
