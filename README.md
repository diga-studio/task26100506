# 设备健康时长统计库（healthstats）

供本地管理程序调用的 Python 库：统计设备当前健康状态及最近 30 天各等级累计秒数，
数据持久化在 SQLite，支持乱序/重复/补传心跳的精确修正与过期明细清理。

## 快速开始

```python
from healthstats import DeviceHealthService, Heartbeat, HealthLevel

svc = DeviceHealthService('health.db')   # 传 ':memory:' 可纯内存运行

# 1) 提交一批心跳（可乱序、可重复；同设备同采样时间内容须相同）
svc.submit_heartbeats([
    Heartbeat(device_id='server-01', ts=0,    cpu=10, mem=10, disk_free=50),
    Heartbeat(device_id='server-01', ts=600,  cpu=90, mem=10, disk_free=50),
    Heartbeat(device_id='server-01', ts=2700, cpu=10, mem=10, disk_free=50),
])

# 2) 推进业务时间（只能从 0 起单调前进）并清理过期明细
svc.advance_time(3600)

# 3) 查询单设备当前健康 + 最近 30 天各等级累计秒数
report = svc.get_device_health('server-01')
# report.current_level     -> HealthLevel.HEALTHY
# report.healthy_seconds   -> 1500
# report.warning_seconds   -> 1800
# report.critical_seconds  -> 300   （三者之和恒等于窗口长度）
svc.close()
```

仅以上三类操作：`submit_heartbeats` / `get_device_health` / `advance_time`。

## 业务规则要点

- 心跳自采样时刻生效，至下一条心跳生效或满 1800 秒（先到为准），恰好 1800 秒即失效。
- 等级判定：磁盘剩余 < 10% → Critical；否则 CPU 或内存 > 85% → Warning；其余 Healthy；
  无有效心跳时为 Critical。
- 报告区间为 `[max(0, T-2592000), T)`，含起点不含 T；三项秒数之和恒等于区间长度。
- 采样时间恰为 T 的心跳影响当前健康，但不贡献时长。
- 早于窗口起点的补传直接忽略；起点及之后的补传会修正历史；结果与到达顺序、重复次数无关。
- `advance_time` 会实际删除 `ts+1800 <= 窗口起点` 的明细与过期聚合，存储占用随周期趋于稳定。

## 实现说明

- `healthstats/logic.py`：纯函数核心（等级判定、区间时长扫描），易测易验。
- `healthstats/store.py`：SQLite 存储。心跳表 `(device_id, ts)` 主键去重；
  另设 3600 秒预聚合桶表（只存非全 Critical 的桶）。
- `healthstats/service.py`：提交时只重算受影响桶（一条心跳的影响范围至多 3 桶）；
  查询 = 整桶读聚合 + 头尾边缘桶读明细，单设备查询耗时与窗口内心跳总数无关。
- 清理删除 `ts <= 窗口起点-1800` 的明细与 `bucket_start <= 窗口起点-3600` 的桶；
  窗口起点前心跳在窗口内仍有效的尾段由保留的明细精确重建，不丢贡献。

## 测试

```bash
python3 -m pytest tests/ -q     # 20 项：单元 + 验收场景 + 随机差分（对拍暴力参考）
```

## 规模实测

```bash
python3 bench/perf_benchmark.py
```

本机（macOS, Python 3.14, SQLite 3.50）实测：

| 项目 | 结果 | 要求 |
| --- | --- | --- |
| 装载 100 万条窗口内心跳（1 万台设备，单设备 20 万条） | 24.5 s，DB 85.6 MB | — |
| 高密度设备 1000 轮「补传新采样时刻 + 紧接查询」总耗时 | **1.43 s** | ≤ 20 s |
| 单次查询 p95 / 最大 | **0.6 ms / 0.7 ms** | p95 ≤ 200 ms |
| 2000 条重复提交 | 46 ms，统计结果不变 | 精确 |
| 推进 90 万秒 + 清理后 | 与暴力参考逐秒一致，三项和 = 2592000 | 精确 |
| 连续 4 个 30 天周期（每周期等量 20 万条） | DB 恒定 85.6 MB，增长 +0.0 MB | 趋于稳定 |
