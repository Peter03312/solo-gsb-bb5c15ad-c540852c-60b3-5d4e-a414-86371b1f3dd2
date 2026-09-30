"""路跑分段计时裁决算法（纯函数，不依赖数据库，便于单元测试）。

路径定义：
    起跑（枪响时刻 start_time，无扫描）-> 第1检查点 -> ... -> 终点。

合法路径必须同时满足：
1. 各点选用的扫描按发生时间严格递增（start_time < cp1 < cp2 < ... < finish）；
2. 第 k 段的耗时（扫描时间 - 上一节点时间）落在对应闭区间 [lower_k, upper_k]，
   单位均为秒，非负；
3. 终点扫描时间不晚于 cutoff_time。

选择规则：
    先取终点扫描发生时间最早的路径（最早完赛）；
    完赛时间相同则取所用扫描编号序列字典序最小的路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

# 某一段的闭区间：(下限秒, 上限秒)
Bound = tuple[float, float]

# 候选扫描：(扫描编号, 点位代码, 发生时间[秒，任意统一时刻基准])
Candidate = tuple[str, str, float]

# 时间戳相减的浮点容差（微秒级），吸收 800.0+2800.0 类误差。
EPS = 1e-6


@dataclass(frozen=True)
class Path:
    """一条走到某检查点的候选路径。"""

    time: float               # 当前点位扫描的发生时间
    scan_ids: tuple[str, ...]  # 本路径已用扫描编号序列

    def extend(self, scan_id: str, at: float) -> "Path":
        return Path(time=at, scan_ids=self.scan_ids + (scan_id,))


def _preferred(a: Path, b: Path) -> Path:
    """同点同扫描之外的比较：完赛时间最早，其次编号序列字典序最小。

    用于终点选择。前缀路径在 DP 中按“扫描编号序列字典序最小”保留
    （同一扫描的时间固定，可行性与成绩只取决于时间）。
    """
    if a.time != b.time:
        return a if a.time < b.time else b
    return a if a.scan_ids <= b.scan_ids else b


def adjudicate(
    point_codes: Sequence[str],
    bounds: Sequence[Bound],
    scans: Iterable[Candidate],
    start_time: float,
    cutoff_time: float,
) -> tuple[Path | None, list[str]]:
    """在某选手的全部扫描中寻找最早完赛路径。

    参数
    ----
    point_codes: 有序检查点代码列表，最后一个为终点。
    bounds: 与点一一对应的各段闭区间；bounds[k] 为从上一节点
            （k==0 时为起跑）到 point_codes[k] 的允许耗时区间。
    scans: 候选扫描 (scan_id, point_code, occurred_at 秒)。
    start_time / cutoff_time: 起跑与截止时刻（同一秒级时间基准）。

    返回
    ----
    (Path, used_scan_ids)：成功时 Path 非空，used_scan_ids 为其扫描序列；
    无完整路径时返回 (None, [])。
    """
    if len(point_codes) != len(bounds):
        raise ValueError("points 与 bounds 数量不一致")
    if not point_codes:
        return None, []

    # 按点位分组；只保留与赛事点位匹配的扫描。
    by_point: dict[str, list[tuple[str, float]]] = {code: [] for code in point_codes}
    for scan_id, point_code, at in scans:
        if point_code in by_point:
            by_point[point_code].append((scan_id, at))

    # 同一编号序列长度相同（等于所在阶段），只需比较首个不同编号即可定字典序。
    def better_prefix(a: Path, b: Path) -> bool:
        return a.scan_ids < b.scan_ids

    # 阶段 0：起跑 -> 首个检查点。
    first = point_codes[0]
    lo, hi = bounds[0]
    # states 以“当前扫描编号”为键：到达同一扫描的路径，时间相同，
    # 后续可行性完全一致，只保留编号序列字典序最小的前缀。
    states: dict[str, Path] = {}
    for scan_id, at in by_point[first]:
        elapsed = at - start_time
        if (at > start_time
                and lo - EPS <= elapsed <= hi + EPS
                and at <= cutoff_time + EPS):
            states[scan_id] = Path(time=at, scan_ids=(scan_id,))

    # 阶段 1..n-1：逐检查点推进。
    for stage in range(1, len(point_codes)):
        code = point_codes[stage]
        lo, hi = bounds[stage]
        next_states: dict[str, Path] = {}
        for scan_id, at in by_point[code]:
            best: Path | None = None
            for prev in states.values():
                elapsed = at - prev.time
                # at <= cutoff 同时过滤掉中间点晚于截止的扫描：之后时间
                # 必须严格递增，已不可能在截止前到达终点。
                if (elapsed > 0
                        and lo - EPS <= elapsed <= hi + EPS
                        and at <= cutoff_time + EPS):
                    cand = prev.extend(scan_id, at)
                    if best is None or better_prefix(cand, best):
                        best = cand
            if best is not None:
                next_states[scan_id] = best
        states = next_states
        if not states:
            return None, []

    # 终点阶段：在所有到达终点的路径中选最早完赛、平局取字典序最小。
    winner: Path | None = None
    for path in states.values():
        if winner is None or _preferred(path, winner) is path:
            winner = path

    if winner is None:
        return None, []
    return winner, list(winner.scan_ids)
