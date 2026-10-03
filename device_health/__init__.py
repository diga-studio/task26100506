"""设备健康时长统计库。"""
from .core import DeviceHealthStats
from .heartbeat import CRITICAL, HEALTHY, WARNING, Heartbeat

__all__ = [
    "DeviceHealthStats",
    "Heartbeat",
    "HEALTHY",
    "WARNING",
    "CRITICAL",
]
