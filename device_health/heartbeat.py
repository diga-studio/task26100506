"""心跳数据模型与等级判定规则。"""
from __future__ import annotations

from dataclasses import dataclass

# 健康等级（对外使用字符串常量）
HEALTHY = "Healthy"
WARNING = "Warning"
CRITICAL = "Critical"

# 内部使用的整数编码，便于列表索引累计
LV_HEALTHY = 0
LV_WARNING = 1
LV_CRITICAL = 2
LEVEL_NAMES = (HEALTHY, WARNING, CRITICAL)

# 一条心跳的有效时长（秒），恰好经过该时长即失效
TTL_SECONDS = 1800

# 统计窗口长度（30 天，秒）
WINDOW_SECONDS = 2592000

# 判定阈值：磁盘剩余低于 10% 为 Critical；CPU/内存超过 85% 为 Warning
DISK_FREE_CRITICAL_PCT = 10.0
UTIL_WARNING_PCT = 85.0


@dataclass(frozen=True)
class Heartbeat:
    """一条心跳。

    cpu / mem 为利用率百分比（0-100），disk_free 为磁盘剩余百分比（0-100）。
    同设备同 ts 视为同一条心跳，重复提交内容保证相同。
    """

    device_id: str
    ts: int
    cpu: float
    mem: float
    disk_free: float


def classify(cpu: float, mem: float, disk_free: float) -> int:
    """按业务规则判定单条心跳生效期间的健康等级。"""
    if disk_free < DISK_FREE_CRITICAL_PCT:
        return LV_CRITICAL
    if cpu > UTIL_WARNING_PCT or mem > UTIL_WARNING_PCT:
        return LV_WARNING
    return LV_HEALTHY
