"""数据类型定义：心跳、健康等级、健康报告。"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class HealthLevel(enum.Enum):
    HEALTHY = "Healthy"
    WARNING = "Warning"
    CRITICAL = "Critical"


@dataclass(frozen=True)
class Heartbeat:
    device_id: str
    ts: int  # 采样时间（整数秒）
    cpu: int  # CPU 利用率百分比 0-100
    mem: int  # 内存利用率百分比 0-100
    disk_free: int  # 磁盘剩余比例百分比 0-100


@dataclass(frozen=True)
class HealthReport:
    device_id: str
    current_level: HealthLevel
    healthy_seconds: int
    warning_seconds: int
    critical_seconds: int
    window_start: int
    window_end: int
