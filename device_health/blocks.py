"""单设备心跳的 sqrt 分解索引：支持快速插入与区间等级时长求和。

每条心跳 i 的贡献区间为 [ts_i, min(ts_i + TTL, ts_{i+1}))，
查询时再按窗口 [start, end) 裁剪。每个块缓存块内各等级贡献秒数，
块的最后一个心跳依赖下一块首心跳的时间，因此结构变化时需重算
被修改块及其前驱块的缓存。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort

from .heartbeat import TTL_SECONDS

_BLOCK = 512  # 块目标大小；超过 2*_BLOCK 时分裂


class DeviceBlocks:
    """按采样时间有序的心跳分块结构（内存索引，与 SQLite 中的明细一致）。"""

    def __init__(self) -> None:
        self._ts: list[list[int]] = []
        self._lv: list[list[int]] = []
        self._sums: list[list[int]] = []
        self._firsts: list[int] = []
        self._seen: set[int] = set()
        self._n = 0

    # ---- 内部工具 ----

    def _recompute(self, i: int) -> None:
        ts, lv = self._ts[i], self._lv[i]
        nxt = self._firsts[i + 1] if i + 1 < len(self._ts) else None
        sums = [0, 0, 0]
        last = len(ts) - 1
        for j, t in enumerate(ts):
            n = ts[j + 1] if j < last else nxt
            cap = t + TTL_SECONDS
            if n is not None and n < cap:
                cap = n
            sums[lv[j]] += cap - t
        self._sums[i] = sums

    def _recompute_around(self, i: int) -> None:
        for k in range(max(0, i - 1), min(len(self._ts), i + 2)):
            self._recompute(k)

    # ---- 写入 ----

    def add(self, ts: int, level: int) -> bool:
        """插入一条心跳；同 ts 重复时忽略并返回 False。"""
        if ts in self._seen:
            return False
        if not self._ts:
            self._ts.append([ts])
            self._lv.append([level])
            self._firsts.append(ts)
            self._sums.append([0, 0, 0])
            self._recompute(0)
            self._seen.add(ts)
            self._n = 1
            return True
        i = bisect_right(self._firsts, ts) - 1
        if i < 0:
            i = 0
        blk_ts, blk_lv = self._ts[i], self._lv[i]
        pos = bisect_left(blk_ts, ts)
        blk_ts.insert(pos, ts)
        blk_lv.insert(pos, level)
        if pos == 0:
            self._firsts[i] = ts
        self._seen.add(ts)
        self._n += 1
        if len(blk_ts) > 2 * _BLOCK:
            mid = len(blk_ts) // 2
            self._ts.insert(i + 1, blk_ts[mid:])
            self._lv.insert(i + 1, blk_lv[mid:])
            del blk_ts[mid:], blk_lv[mid:]
            self._firsts.insert(i + 1, self._ts[i + 1][0])
            self._sums.insert(i + 1, [0, 0, 0])
        self._recompute_around(i)
        return True

    def add_many(self, rows: list[tuple[int, int]]) -> int:
        """批量插入 (ts, level)，自动去重，返回新增条数。

        小批量走逐条增量插入；大批量直接合并重建，避免逐条重算块缓存。
        """
        seen_now: set[int] = set()
        new: list[tuple[int, int]] = []
        for t, l in rows:
            if t not in self._seen and t not in seen_now:
                seen_now.add(t)
                new.append((t, l))
        if not new:
            return 0
        if len(new) * 4 * _BLOCK <= self._n + len(new):
            for t, l in new:
                self.add(t, l)
            return len(new)
        new.sort()
        merged = sorted(
            [(t, l) for blk_t, blk_l in zip(self._ts, self._lv) for t, l in zip(blk_t, blk_l)]
            + new
        )
        self.load_sorted(merged)
        return len(new)

    def load_sorted(self, rows: list[tuple[int, int]]) -> None:
        """从已按 ts 排序的 (ts, level) 序列批量重建。"""
        self.__init__()
        for off in range(0, len(rows), _BLOCK):
            chunk = rows[off : off + _BLOCK]
            self._ts.append([t for t, _ in chunk])
            self._lv.append([l for _, l in chunk])
            self._firsts.append(chunk[0][0])
            self._sums.append([0, 0, 0])
        self._seen = {t for t, _ in rows}
        self._n = len(rows)
        for i in range(len(self._ts)):
            self._recompute(i)

    # ---- 查询 ----

    def durations(self, start: int, end: int) -> list[int]:
        """返回 [start, end) 内三个等级的累计秒数（不含无心跳的 Critical）。"""
        res = [0, 0, 0]
        if start >= end or self._n == 0:
            return res
        hi_blk = bisect_left(self._firsts, end)  # 首心跳 < end 的块才可能相关
        if hi_blk == 0:
            return res
        # 窗口起点前 TTL 秒内的心跳仍可能有效，需回退到覆盖
        # (start - TTL) 的块；更早的块内心跳必然已全部失效。
        lo_blk = bisect_right(self._firsts, start - TTL_SECONDS + 1) - 1
        if lo_blk < 0:
            lo_blk = 0
        full_to = -1  # 最后一个按整块累加的块索引
        for i in range(lo_blk, hi_blk):
            blk_ts, blk_lv = self._ts[i], self._lv[i]
            if blk_ts[0] >= start and blk_ts[-1] < end:
                s = self._sums[i]
                res[0] += s[0]
                res[1] += s[1]
                res[2] += s[2]
                full_to = i
            else:
                nxt_blk_first = (
                    self._firsts[i + 1] if i + 1 < len(self._ts) else None
                )
                last = len(blk_ts) - 1
                for j, t in enumerate(blk_ts):
                    if t >= end or t + TTL_SECONDS <= start:
                        continue
                    n = blk_ts[j + 1] if j < last else nxt_blk_first
                    cap = t + TTL_SECONDS
                    if n is not None and n < cap:
                        cap = n
                    if cap > end:
                        cap = end
                    lo = t if t > start else start
                    if cap > lo:
                        res[blk_lv[j]] += cap - lo
        # 窗口内最后一条心跳：若按整块累加，其缓存时长用的是下一条心跳
        # （>= end）或 TTL 上限，需按 end 重新封顶。
        if full_to >= 0 and full_to == hi_blk - 1:
            blk_ts, blk_lv = self._ts[full_to], self._lv[full_to]
            j = bisect_left(blk_ts, end) - 1
            if j >= 0:
                t = blk_ts[j]
                n = blk_ts[j + 1] if j + 1 < len(blk_ts) else (
                    self._firsts[full_to + 1]
                    if full_to + 1 < len(self._ts)
                    else None
                )
                stored = t + TTL_SECONDS
                if n is not None and n < stored:
                    stored = n
                stored -= t
                actual = min(t + TTL_SECONDS, end) - t
                res[blk_lv[j]] += actual - stored
        return res

    def level_at(self, t: int) -> int | None:
        """t 时刻生效心跳的等级；无有效心跳返回 None。"""
        i = bisect_right(self._firsts, t) - 1
        if i < 0:
            return None
        blk_ts = self._ts[i]
        j = bisect_right(blk_ts, t) - 1
        if j < 0:
            return None
        if t - blk_ts[j] < TTL_SECONDS:
            return self._lv[i][j]
        return None

    # ---- 清理 ----

    def expire_before(self, cutoff: int) -> int:
        """删除 ts <= cutoff 的明细，返回删除条数。"""
        if self._n == 0:
            return 0
        removed = 0
        keep_from = 0
        for i, blk in enumerate(self._ts):
            if blk[-1] <= cutoff:
                removed += len(blk)
                keep_from = i + 1
            else:
                break
        if keep_from:
            del self._ts[:keep_from]
            del self._lv[:keep_from]
            del self._sums[:keep_from]
            del self._firsts[:keep_from]
        if self._ts and self._ts[0][0] <= cutoff:
            blk_ts, blk_lv = self._ts[0], self._lv[0]
            pos = bisect_right(blk_ts, cutoff)
            removed += pos
            del blk_ts[:pos], blk_lv[:pos]
            self._firsts[0] = blk_ts[0]
        if removed == 0:
            return 0
        self._n -= removed
        # 重建 seen 代价可接受（清理非高频操作）
        self._seen = {t for blk in self._ts for t in blk}
        if self._ts:
            self._recompute(0)
        return removed

    def first_ts(self) -> int | None:
        return self._firsts[0] if self._firsts else None

    def __len__(self) -> int:
        return self._n
