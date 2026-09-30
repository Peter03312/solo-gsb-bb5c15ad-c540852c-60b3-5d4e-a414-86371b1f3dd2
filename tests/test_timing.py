"""timing.adjudicate 的单元测试（无外部依赖，直接 pytest / 或 python 运行）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.timing import adjudicate  # noqa: E402

POINTS = ["CP1", "CP2", "FIN"]
BOUNDS = [(300, 900), (300, 900), (300, 900)]
START = 0.0
CUTOFF = 3600.0


def test_basic_path():
    scans = [
        ("s1", "CP1", 400), ("s2", "CP2", 800), ("s3", "FIN", 1200),
    ]
    path, ids = adjudicate(POINTS, BOUNDS, scans, START, CUTOFF)
    assert ids == ["s1", "s2", "s3"]
    assert path.time == 1200


def test_unordered_scan_submission():
    # 乱序传入不影响结果
    scans = [
        ("s3", "FIN", 1200), ("s1", "CP1", 400), ("s2", "CP2", 800),
    ]
    _, ids = adjudicate(POINTS, BOUNDS, scans, START, CUTOFF)
    assert ids == ["s1", "s2", "s3"]


def test_earliest_finish_wins():
    scans = [
        ("a1", "CP1", 500), ("a2", "CP2", 1000), ("a3", "FIN", 1500),
        ("b1", "CP1", 350), ("b2", "CP2", 700), ("b3", "FIN", 1100),
    ]
    _, ids = adjudicate(POINTS, BOUNDS, scans, START, CUTOFF)
    assert ids == ["b1", "b2", "b3"]


def test_tie_finish_lexicographically_smallest_sequence():
    # 两条路径都在 900 完赛：a 序列 vs b 序列，取首编号字典序更小的 a
    scans = [
        ("a1", "CP1", 300), ("a2", "CP2", 600), ("a3", "FIN", 900),
        ("b1", "CP1", 301), ("b2", "CP2", 601), ("b3", "FIN", 900),
    ]
    _, ids = adjudicate(POINTS, BOUNDS, scans, START, CUTOFF)
    assert ids == ["a1", "a2", "a3"]


def test_tie_lex_compare_can_depend_on_later_element():
    # 首点相同扫描（同 id 共享前缀），终点编号字典序决胜
    scans = [
        ("c1", "CP1", 400),
        ("z2", "CP2", 750), ("a2", "CP2", 760),
        ("z3", "FIN", 1100), ("a3", "FIN", 1100),
    ]
    # 从 c1 出发：
    #   c1(400)->z2(750,350)->? 1100-750=350 合法 => c1,z2,{a3,z3}
    #   c1(400)->a2(760,360)->? 1100-760=340 合法 => c1,a2,{a3,z3}
    # 终点平局 1100：候选序列 (c1,a2,a3) vs (c1,a2,z3) vs (c1,z2,..)
    # 字典序最小为 c1,a2,a3
    _, ids = adjudicate(POINTS, BOUNDS, scans, START, CUTOFF)
    assert ids == ["c1", "a2", "a3"]


def test_strictly_increasing_required():
    # CP2 与 CP1 同时刻 -> 不合法（必须严格递增）
    scans = [("s1", "CP1", 400), ("s2", "CP2", 400), ("s3", "FIN", 1000)]
    path, ids = adjudicate(POINTS, BOUNDS, scans, START, CUTOFF)
    assert path is None and ids == []


def test_segment_bounds_enforced():
    # CP1->CP2 仅 100 秒，低于下限 300
    scans = [("s1", "CP1", 400), ("s2", "CP2", 500), ("s3", "FIN", 900)]
    assert adjudicate(POINTS, BOUNDS, scans, START, CUTOFF)[0] is None
    # CP1->CP2 1000 秒，高于上限 900
    scans = [("s1", "CP1", 400), ("s2", "CP2", 1400), ("s3", "FIN", 1800)]
    assert adjudicate(POINTS, BOUNDS, scans, START, CUTOFF)[0] is None


def test_finish_after_cutoff_dnf():
    scans = [("s1", "CP1", 400), ("s2", "CP2", 800), ("s3", "FIN", 3601)]
    assert adjudicate(POINTS, BOUNDS, scans, START, CUTOFF)[0] is None
    # 恰好等于截止时间合法（终点不晚于截止时间）
    scans = [("s1", "CP1", 400), ("s2", "CP2", 800), ("s3", "FIN", 1700)]
    assert adjudicate(POINTS, BOUNDS, scans, START, 1700.0)[1] == [
        "s1", "s2", "s3"]


def test_non_greedy_earlier_prefix_can_be_dead_end():
    # 贪心“首点越早越好”会选 e1(300)，但 e1->任一CP2 都会超上限：
    # CP2 最早合法到达 e1+300=600，但给的 CP2 是 700（间隔400合法）...
    # 构造：e1=300；CP2 只有一个 1250（距 e1 为 950 > 900，死路），
    # 而晚到的首点 l1=400 距 1250 为 850 合法，再到 FIN 1600（350）。
    scans = [
        ("e1", "CP1", 300),
        ("l1", "CP1", 400),
        ("p2", "CP2", 1250),
        ("f3", "FIN", 1600),
    ]
    _, ids = adjudicate(POINTS, BOUNDS, scans, START, CUTOFF)
    assert ids == ["l1", "p2", "f3"]


def test_missing_point_dnf():
    scans = [("s1", "CP1", 400), ("s3", "FIN", 1200)]
    assert adjudicate(POINTS, BOUNDS, scans, START, CUTOFF)[0] is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
